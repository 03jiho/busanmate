"""SearchService (COMP-05, FR-002): 속성 필터 + 벡터 유사도 하이브리드 검색, 후보가 부족하면 조건 완화."""
import logging
import re
from dataclasses import dataclass, field, replace

from psycopg.rows import dict_row

import db
from services.embedding import embed_texts
from settings import config

log = logging.getLogger("search")
PLACE_FIELDS = ["place_id", "source", "name", "category", "gu", "address", "lat", "lng", "indoor",
                "open_hours", "description", "url", "license"]


@dataclass
class Conditions:
    """QueryParser(COMP-04)가 만드는 구조화 조건."""
    query: str                                        # 의미 검색용 자연어 (테마·분위기)
    gu: list[str] = field(default_factory=list)       # "해운대구" 등, 비면 부산 전체
    categories: list[str] = field(default_factory=list)
    indoor: bool | None = None                        # 사용자가 실내/실외를 명시했을 때만
    days: int = 1
    hours: float | None = None                        # 하루 가용 시간, None이면 config 기본값
    mode: str = "auto"                                # auto / walk / taxi
    theme: str | None = None                          # config preferences.theme_categories 키
    companion: str | None = None


def fetch_places(ids: list[int]) -> dict[int, dict]:
    """이름·좌표·출처는 응답 직전에 항상 DB에서 다시 읽는다 (활성 행만, FR-003·NFR-004)."""
    if not ids:
        return {}
    with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        # 썸네일: place_image는 이용 조건이 확인된 이미지만 적재한다(NFR-007), 장소당 첫 장
        image = ("(SELECT json_build_object('url', i.url, 'license', i.license) FROM place_image i "
                 "WHERE i.place_id = place.place_id ORDER BY i.image_id LIMIT 1) AS image")
        cur.execute(f"SELECT {', '.join(PLACE_FIELDS)}, {image} FROM place WHERE is_active AND place_id = ANY(%s)",
                    (list(ids),))
        return {r["place_id"]: r for r in cur.fetchall()}


def theme_points(theme: str | None) -> list[tuple[float, float]]:
    """config theme_near의 기준 분류 장소 좌표(예: 바다 → 활성 해변 장소). 코스 점수의 테마 가산점용."""
    near = (config()["preferences"].get("theme_near") or {}).get(theme)
    if not near:
        return []
    with db.connect() as conn:
        rows = conn.execute("SELECT lat, lng FROM place WHERE is_active AND category = %s", (near["category"],))
        return [(float(a), float(b)) for a, b in rows.fetchall()]


def source_of(p: dict) -> dict:
    return {"place_id": p["place_id"], "name": p["name"], "label": config()["sources"][p["source"]]["label"],
            "url": p["url"], "license": p["license"], "image": p.get("image")}


def _where(c: Conditions) -> dict | None:
    clauses = []
    if c.gu:
        clauses.append({"gu": {"$in": c.gu}})
    if c.categories:
        clauses.append({"category": {"$in": c.categories}})
    if c.indoor is not None:
        clauses.append({"indoor": c.indoor})
    return {"$and": clauses} if len(clauses) > 1 else (clauses[0] if clauses else None)


def _query(vec: list[float], c: Conditions, n: int) -> list[dict]:
    r = db.collection("place_text").query(query_embeddings=[vec], n_results=n, where=_where(c))
    sims = {int(i): 1 - d for i, d in zip(r["ids"][0], r["distances"][0])}  # cosine 거리 → 유사도
    if not sims:
        return []
    with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT {', '.join(PLACE_FIELDS)} FROM place WHERE is_active AND place_id = ANY(%s)",
                    (list(sims),))
        rows = cur.fetchall()
    return sorted(({**p, "sim": sims[p["place_id"]]} for p in rows), key=lambda p: -p["sim"])


def _keyword_query(c: Conditions, n: int) -> list[dict]:
    """임베딩을 쓸 수 없을 때(Gemini 장애) 대체 검색: 속성 필터 + 이름·설명에 질의 낱말이 들어간 수 (SDD §7.4)."""
    words = re.findall(r"[가-힣A-Za-z0-9]{2,}", c.query)[:8]
    hit = " + ".join(["((name || ' ' || coalesce(description, '')) ILIKE %s)::int"] * len(words)) or "0"
    where, params = ["is_active"], [f"%{w}%" for w in words]
    for col, val in (("gu", c.gu), ("category", c.categories)):
        if val:
            where.append(f"{col} = ANY(%s)")
            params.append(val)
    if c.indoor is not None:
        where.append("indoor = %s")
        params.append(c.indoor)
    with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT {', '.join(PLACE_FIELDS)}, ({hit}) AS hits FROM place WHERE {' AND '.join(where)} "
                    f"ORDER BY hits DESC, place_id LIMIT %s", (*params, n))
        rows = cur.fetchall()
    return [{**{k: v for k, v in p.items() if k != "hits"}, "sim": min(1.0, 0.3 + 0.1 * p["hits"])} for p in rows]


def search(cond: Conditions, n: int | None = None, embed=embed_texts) -> tuple[list[dict], list[str]]:
    """후보(유사도 내림차순)와 사용자 안내문. 후보가 min_candidates 미만이면 relax_order 순서로 조건을 푼다.
    질의 임베딩을 만들 수 없으면(Gemini 장애) 키워드 검색으로 대체한다 (AI-REQ-02, AC-006)."""
    cfg = config()["search"]
    n = n or cfg["candidates_n"] * min(cond.days, config()["course"]["max_days"])  # 다일 일정은 날짜 수만큼
    notices = []
    try:
        vec = embed([cond.query], task="RETRIEVAL_QUERY")[0]
        _query_fn = _query
    except Exception as e:
        log.warning("질의 임베딩 실패, 키워드 검색으로 대체: %s", type(e).__name__)
        vec, notices = None, ["AI 검색을 쓸 수 없어 키워드와 조건으로 찾았습니다."]
        _query_fn = lambda v, c, k: _keyword_query(c, k)  # noqa: E731
    hits = _query_fn(vec, cond, n)
    for step in cfg["relax_order"]:
        if len(hits) >= cfg["min_candidates"]:
            break
        if step == "indoor" and cond.indoor is not None:
            cond = replace(cond, indoor=None)
            notices.append("조건에 맞는 곳이 적어 실내/실외 조건을 풀어 다시 찾았습니다.")
        elif step == "gu" and cond.gu:
            cond = replace(cond, gu=[])
            notices.append("조건에 맞는 곳이 적어 부산 전체에서 다시 찾았습니다.")
        else:
            continue  # open_hours: 운영시간을 아직 파싱하지 않아 필터가 없다
        hits = _query_fn(vec, cond, n)
    if len(hits) < cfg["min_candidates"]:
        notices.append("조건에 맞는 장소를 충분히 찾지 못했습니다. 조건을 조금 넓혀 볼까요?")
    return hits, notices
