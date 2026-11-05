"""DataPipeline (COMP-12, FR-013): 수집 → 정제 → 중복 제거 → 적재(미커밋) → 임베딩 → 커밋, ingest_log 기록.
실행: python -m pipeline.run [--seed] [source ...]   (source는 config.yaml sources의 키, 생략 시 전체)"""
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


# --- 1. 수집 ---
def collect(source: str, seed: bool = False) -> list[dict]:
    """API 원본 행. seed=True면 data/seed/ CSV(같은 필드 이름)에서 읽는다."""
    return busan.load_seed(source) if seed else _fetch(source)


# --- 2. 정제: 좌표·이름·구가 없는(축제는 기간을 읽을 수 없는) 행은 제외 ---
def clean(rows: list[dict], source: str) -> list[dict]:
    to_row = busan.to_festival if config()["sources"][source]["kind"] == "festival" else busan.to_place
    return [x for x in (to_row(r, source) for r in rows) if x]


# --- 3. 중복 제거: 한 응답 안에 같은 원본 id가 여러 번 오면 마지막 것만 ---
def dedup(items: list[dict]) -> list[dict]:
    return list({x["source_id"]: x for x in items}.values())


# --- 4. 적재: 내용이 바뀐 행만 upsert (커밋은 임베딩까지 성공한 뒤 run에서) ---
def load_places(cur, places: list[dict]) -> tuple[list[tuple[int, dict]], int, int]:
    """(바뀐 (place_id, 행) 목록, 신규 수, 갱신 수)."""
    changed, new, updated = [], 0, 0
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
    return changed, new, updated


def deactivate(cur, source: str, places: list[dict]) -> list[str]:
    """이번 응답에서 사라진 장소는 폐업·삭제로 보고 비활성화 (검색은 활성 행만 → 환각 방지)."""
    cur.execute("UPDATE place SET is_active = FALSE WHERE source = %s AND is_active "
                "AND NOT (source_id = ANY(%s)) RETURNING place_id", (source, [p["source_id"] for p in places]))
    return [str(r[0]) for r in cur.fetchall()]


def load_festivals(cur, fests: list[dict]) -> tuple[int, int]:
    new = updated = 0
    for f in fests:
        cur.execute(_UPSERT_FESTIVAL, [f[c] for c in FESTIVAL_COLS])
        hit = cur.fetchone()
        if hit:
            new, updated = new + hit[1], updated + (not hit[1])
    return new, updated


# --- 5. 임베딩: 바뀐 장소만 (재실행 시 호출 0회), 사라진 장소의 벡터는 지운다 ---
def embed(changed: list[tuple[int, dict]], gone: list[str], coll) -> None:
    if changed:
        max_chars = config()["embedding"]["max_doc_chars"]
        docs = [f"{p['name']}\n{p['description'] or ''}"[:max_chars] for _, p in changed]
        coll.upsert(ids=[str(i) for i, _ in changed], embeddings=embed_texts(docs), documents=docs,
                    metadatas=[{k: p[k] for k in ("gu", "category", "indoor") if p[k] is not None} for _, p in changed])
    if gone:
        coll.delete(ids=gone)


def ingest_source(conn, source: str, coll, seed: bool = False) -> tuple[int, int, int, int]:
    """한 소스: 수집 → 정제 → 중복 제거 → 적재(미커밋) → 임베딩 → 커밋. (신규, 갱신, 제외, 비활성화)."""
    rows = collect(source, seed)
    items = dedup(clean(rows, source))
    skipped = len(rows) - len(items)
    with conn.cursor() as cur:
        if config()["sources"][source]["kind"] == "festival":
            new, updated = load_festivals(cur, items)
            gone = []
        else:
            changed, new, updated = load_places(cur, items)
            gone = [] if seed else deactivate(cur, source, items)  # 시드는 일부뿐이라 비활성화하지 않는다
            embed(changed, gone, coll)
    conn.commit()
    return new, updated, skipped, len(gone)


def run(sources: list[str] | None = None, seed: bool = False) -> list[dict]:
    """소스별로 하나의 트랜잭션. 실패한 소스는 롤백하고 건너뛴 뒤 ingest_log에 기록한다(ERR-008).
    seed=True면 API 대신 data/seed/ CSV로 적재한다(API 장애·오프라인 시연용)."""
    sources = sources or list(config()["sources"])
    coll = db.collection("place_text")
    results = []
    with db.connect() as conn:
        for source in sources:
            new = updated = skipped = gone = 0
            try:
                new, updated, skipped, gone = ingest_source(conn, source, coll, seed)
                notes = (["시드 CSV"] if seed else []) + ([f"비활성화 {gone}건"] if gone else [])
                status, message = "ok", ", ".join(notes) or None
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
    args = [a for a in sys.argv[1:] if a != "--seed"]
    sys.exit(0 if all(r["status"] == "ok" for r in run(args or None, seed="--seed" in sys.argv)) else 1)
