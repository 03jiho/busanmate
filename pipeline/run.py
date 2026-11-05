"""DataPipeline (COMP-12, FR-013): 수집 → 정제 → 중복 없는 upsert → 임베딩 → 적재, ingest_log 기록.
실행: python -m pipeline.run [source ...]   (source는 config.yaml sources의 키, 생략 시 전체)"""
import logging
import os
import sys
import time

import requests
from psycopg.types.json import Jsonb

import db
from pipeline import busan
from services.embedding import embed_texts
from settings import config

log = logging.getLogger("pipeline")

PLACE_COLS = ["source", "source_id", "name", "category", "sub_category", "gu", "address", "lat", "lng",
              "description", "open_hours", "indoor", "tel", "url", "license"]
FESTIVAL_COLS = ["source_id", "name", "description", "start_date", "end_date", "lat", "lng", "address",
                 "url", "license"]
_SECRET_ENVS = ["DATA_GO_KR_SERVICE_KEY", "GEMINI_API_KEY", "KAKAO_REST_API_KEY", "ADMIN_TOKEN", "POSTGRES_PASSWORD"]


def _upsert_sql(table, cols, key):
    """내용이 바뀐 행만 갱신하고 (id, 신규 여부)를 돌려준다. 그대로인 행은 아무것도 돌려주지 않는다."""
    data = [c for c in cols if c not in key]
    active = ", is_active = TRUE" if table == "place" else ""
    old = ", ".join(f"{table}.{c}" for c in data) + (f", {table}.is_active" if active else "")
    new = ", ".join(f"EXCLUDED.{c}" for c in data) + (", TRUE" if active else "")
    return (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET "
            + ", ".join(f"{c} = EXCLUDED.{c}" for c in data) + f", fetched_at = now(){active} "
            f"WHERE ({old}) IS DISTINCT FROM ({new}) RETURNING {table}_id, (xmax = 0)")


_UPSERT_PLACE = _upsert_sql("place", PLACE_COLS, ["source", "source_id"])
_UPSERT_FESTIVAL = _upsert_sql("festival", FESTIVAL_COLS, ["source_id"])


def _safe(e: Exception) -> str:
    """로그용 오류 설명. requests 예외는 URL에 serviceKey가 들어 있으므로 종류·상태코드만 남긴다."""
    if isinstance(e, requests.HTTPError):
        return f"HTTP {e.response.status_code}"
    if isinstance(e, requests.RequestException):
        return type(e).__name__
    msg = f"{type(e).__name__}: {e}"
    for name in _SECRET_ENVS:
        if os.environ.get(name):
            msg = msg.replace(os.environ[name], "***")
    return msg[:500]


def _fetch(source: str) -> list[dict]:
    retries = config()["tools"]["max_retries"]
    for attempt in range(retries):
        try:
            rows = busan.fetch(source)
            if rows:
                return rows
            raise RuntimeError("빈 응답")  # 빈 응답으로 전체 비활성화되는 것을 막는다
        except Exception as e:
            if attempt == retries - 1:
                raise
            log.warning("%s 수집 재시도 %d: %s", source, attempt + 1, _safe(e))
            time.sleep(2 ** attempt)


def _ingest_places(cur, source, rows, coll):
    places = [p for p in (busan.to_place(r, source) for r in rows) if p]
    new = updated = 0
    changed = []
    for p in places:
        cur.execute(_UPSERT_PLACE, [Jsonb(p[c]) if c == "open_hours" and p[c] else p[c] for c in PLACE_COLS])
        hit = cur.fetchone()
        if not hit:
            continue
        place_id, inserted = hit
        new, updated = new + inserted, updated + (not inserted)
        changed.append((place_id, p))
        cur.execute("DELETE FROM place_image WHERE place_id = %s AND url IS DISTINCT FROM %s", (place_id, p["image_url"]))
        if p["image_url"]:
            cur.execute("INSERT INTO place_image (place_id, url, license) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (place_id, p["image_url"], p["license"]))
    # 이번 응답에서 사라진 장소는 폐업·삭제로 보고 비활성화 (환각 방지: 검색은 활성 행만)
    cur.execute("UPDATE place SET is_active = FALSE WHERE source = %s AND is_active "
                "AND NOT (source_id = ANY(%s)) RETURNING place_id", (source, [p["source_id"] for p in places]))
    gone = [str(r[0]) for r in cur.fetchall()]

    if changed:  # 바뀐 장소만 임베딩 (재실행 시 호출 0회)
        max_chars = config()["embedding"]["max_doc_chars"]
        docs = [f"{p['name']}\n{p['description'] or ''}"[:max_chars] for _, p in changed]
        coll.upsert(ids=[str(i) for i, _ in changed], embeddings=embed_texts(docs), documents=docs,
                    metadatas=[{k: p[k] for k in ("gu", "category", "indoor") if p[k] is not None} for _, p in changed])
    if gone:
        coll.delete(ids=gone)
    return new, updated, len(rows) - len(places), len(gone)


def _ingest_festivals(cur, source, rows):
    fests = [f for f in (busan.to_festival(r, source) for r in rows) if f]
    new = updated = 0
    for f in fests:
        cur.execute(_UPSERT_FESTIVAL, [f[c] for c in FESTIVAL_COLS])
        hit = cur.fetchone()
        if hit:
            new, updated = new + hit[1], updated + (not hit[1])
    return new, updated, len(rows) - len(fests), 0


def run(sources: list[str] | None = None) -> list[dict]:
    """소스별로 하나의 트랜잭션. 실패한 소스는 롤백하고 건너뛴 뒤 ingest_log에 기록한다(ERR-008)."""
    sources = sources or list(config()["sources"])
    coll = db.collection("place_text")
    results = []
    with db.connect() as conn:
        for source in sources:
            new = updated = skipped = gone = 0
            try:
                rows = _fetch(source)
                with conn.cursor() as cur:
                    if config()["sources"][source]["kind"] == "festival":
                        new, updated, skipped, gone = _ingest_festivals(cur, source, rows)
                    else:
                        new, updated, skipped, gone = _ingest_places(cur, source, rows, coll)
                conn.commit()
                status, message = "ok", (f"비활성화 {gone}건" if gone else None)
            except Exception as e:
                conn.rollback()
                new = updated = skipped = 0
                status, message = "failed", _safe(e)
            conn.execute("INSERT INTO ingest_log (source, new_cnt, updated_cnt, skipped_cnt, status, message) "
                         "VALUES (%s, %s, %s, %s, %s, %s)", (source, new, updated, skipped, status, message))
            conn.commit()
            result = {"source": source, "status": status, "new": new, "updated": updated,
                      "skipped": skipped, "message": message}
            log.info("%s", result)
            results.append(result)
        if any(config()["sources"][r["source"]]["kind"] == "place" and r["status"] == "ok" for r in results):
            result = _embed_images()
            conn.execute("INSERT INTO ingest_log (source, new_cnt, updated_cnt, skipped_cnt, status, message) "
                         "VALUES (%s, %s, 0, %s, %s, %s)",
                         ("place_image", result["new"], result["skipped"], result["status"], result["message"]))
            conn.commit()
            log.info("%s", result)
            results.append(result)
    return results


def _embed_images() -> dict:
    """새 장소 이미지를 CLIP으로 임베딩한다(DATA-06). torch가 무거워 필요할 때만 불러온다."""
    try:
        from pipeline.images import embed_pending
        out = embed_pending()
        return {"source": "place_image", "status": "ok", "updated": 0, "message": None, **out}
    except Exception as e:
        return {"source": "place_image", "status": "failed", "new": 0, "updated": 0, "skipped": 0, "message": _safe(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(0 if all(r["status"] == "ok" for r in run(sys.argv[1:] or None)) else 1)
