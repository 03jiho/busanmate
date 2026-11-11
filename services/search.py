"""SearchService (COMP-05, FR-002): 속성 필터 + 벡터 유사도 하이브리드 검색, 후보가 부족하면 조건 완화."""
import logging
import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher

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
    must: list[int] = field(default_factory=list)     # 사용자가 이름으로 콕 집은 장소 중 DB에서 찾은 place_id (코스에 꼭 넣는다)


def all_place_names() -> list[dict]:
    with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT place_id, name, gu, category FROM place WHERE is_active")
        return cur.fetchall()


def _norm(s: str) -> str:
    return re.sub(r"\(.*?\)|\s", "", s).lower()


def match_names(requested: list[str], rows: list[dict], gu: list[str] | None = None,
                strip: list[str] | None = None) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """사용자가 말한 장소명 → DB 장소. (찾은 것 {말한 이름: 행}, 못 찾은 것 {말한 이름: 비슷한 장소명 상위 N}).
    일치: 정규화(공백·괄호 제거)한 이름의 SequenceMatcher 비율이 임계 이상, 또는 말한 이름이 DB 이름의 앞부분
    ("용두산" → 용두산공원), 또는 DB 이름 뒤에 일반 접미어만 붙은 경우("해운대해수욕장" → 해운대, "태종대 유원지" → 태종대).
    DB 이름이 말한 이름 안에 들어 있는 것만으로는 일치로 보지 않는다("해운대 블루오션 스카이타워" ≠ 해운대).
    앞의 지역 단어(strip)를 뗀 형태는 비슷한 장소 안내에만 쓴다(일치에 쓰면 "해운대해수욕장" → "해수욕장" → 임랑해수욕장)."""
    c = config()["named_place"]
    suffixes = [_norm(s) for s in c["generic_suffixes"]]
    found, missing = {}, {}
    for said in requested:
        full = _norm(said)
        variants = {full}
        for w in strip or []:
            if full.startswith(_norm(w)) and len(full) > len(_norm(w)) + 1:
                variants.add(full[len(_norm(w)):])

        def score(row):
            name = _norm(row["name"])
            best = SequenceMatcher(None, full, name).ratio()
            prefix = len(full) >= 2 and name.startswith(full) and len(full) / len(name) >= 0.5
            if prefix or (full.startswith(name) and full[len(name):] in suffixes):
                best = max(best, c["match_threshold"])
            return best

        ranked = sorted(rows, key=lambda r: (score(r), r["gu"] in (gu or [])), reverse=True)
        if ranked and score(ranked[0]) >= c["match_threshold"]:
            found[said] = ranked[0]
            continue
        # 비슷한 장소: 지역 단어를 뗀 이름의 유사도에, 요청 이름이 뜻하는 분류(전망대·타워 → 관광지·공원)와 맞으면
        # 점수를 더한다("블루오션 스카이타워"에 횟집이 아니라 전망대가 나오게). 점수가 similar_min 미만이면 안내하지 않는다.
        core = [v for v in variants if v != _norm(said)] or list(variants)
        ctx = {cat for word, cats in c.get("context_categories", {}).items() if word in said for cat in cats}

        def sim(row):
            ratio = max(SequenceMatcher(None, v, _norm(row["name"])).ratio() for v in core)
            return 0.6 * ratio + 0.4 * (row.get("category") in ctx) if ctx else ratio
        close = sorted(rows, key=lambda r: (sim(r), r["gu"] in (gu or [])), reverse=True)
        missing[said] = [r["name"] for r in close[:c["similar_n"]] if sim(r) >= c["similar_min"]]
    return found, missing


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


def places_where(category: str | None = None, indoor: bool | None = None) -> list[dict]:
    """분류·실내 여부로 활성 장소를 읽는다 (코스 제자리 수정: 카페 추가, 실내로 바꾸기)."""
    q, params = f"SELECT {', '.join(PLACE_FIELDS)} FROM place WHERE is_active", []
    if category:
        q, params = q + " AND category = %s", params + [category]
    if indoor is not None:
        q, params = q + " AND indoor = %s", params + [indoor]
    with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(q, params)
        return cur.fetchall()


def theme_points(theme: str | None) -> list[tuple[float, float]]:
    """config theme_near의 기준 분류 장소 좌표(예: 바다 → 활성 해변 장소). 코스 점수의 테마 가산점용."""
    near = (config()["preferences"].get("theme_near") or {}).get(theme)
    if not near:
        return []
    with db.connect() as conn:
        rows = conn.execute("SELECT lat, lng FROM place WHERE is_active AND category = %s", (near["category"],))
        return [(float(a), float(b)) for a, b in rows.fetchall()]


def source_of(p: dict) -> dict:
    src = config()["sources"][p["source"]]
    return {"place_id": p["place_id"], "name": p["name"], "label": src["label"], "url": p["url"],
            "portal_url": src.get("portal_url"), "license": p["license"], "image": p.get("image"),
            "original": p.get("description")}  # 카드의 '원문 보기': 공공데이터 소개문 그대로


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
    if cond.indoor and not cond.categories:
        # "실내"·"비 오는 날": 실내 필터만 걸면 실내로 분류된 식당·카페가 후보를 채운다 → 실내 명소를 따로 찾아 앞에 둔다
        attr = _query_fn(vec, replace(cond, categories=cfg["indoor_categories"]), n)
        if len(attr) < cfg["min_indoor_attractions"] and cond.gu:  # 그 구에 실내 명소가 거의 없으면 다른 구까지
            seen = {h["place_id"] for h in attr}
            attr += [h for h in _query_fn(vec, replace(cond, gu=[], categories=cfg["indoor_categories"]), n)
                     if h["place_id"] not in seen]
            notices.append(f"{', '.join(cond.gu)}에는 실내 명소가 적어 가까운 다른 구까지 찾았습니다.")
        ids = {h["place_id"] for h in attr}
        hits = attr + [h for h in hits if h["place_id"] not in ids]
    food = config()["classify"]["food"]["category"]
    if not cond.categories and sum(h["category"] == food for h in hits) < 2 * min(cond.days, 3):
        # 의미 검색만으로는 식당이 하나도 안 들어올 수 있다("부산 전체 여행"): 점심용 식당 후보를 뒤에 보탠다
        ids = {h["place_id"] for h in hits}
        hits += [h for h in _query_fn(vec, replace(cond, categories=[food]), cfg["food_candidates_n"] * min(cond.days, 3))
                 if h["place_id"] not in ids]
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
