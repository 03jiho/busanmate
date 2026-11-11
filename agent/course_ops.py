"""코스 직접 편집 (FR-012): 드래그로 순서 바꾸기·빼기·다른 곳으로 교체는 채팅을 거치지 않고 세션의 직전 코스를 바로
고친다. 순서는 사용자가 정한 그대로 두고 이동 시간만 다시 계산한다(택시는 카카오 실측). 공유 링크·저장한 코스는 DB에
두고(로그인 없음), 서버가 세션의 코스로 만든 값만 저장한다(사용자가 보낸 JSON을 그대로 저장하지 않는다)."""
import logging
import secrets
from dataclasses import asdict
from datetime import date

from psycopg.types.json import Jsonb

import db
from agent import session
from agent.explain import enrich
from agent.response import fallback_line, intro_line
from services.planner import detour_km, items_for, same_place
from services.search import Conditions, fetch_places, places_where, source_of
from settings import config
from tools.base import REGISTRY

log = logging.getLogger("agent")
SHARED_DDL = ("CREATE TABLE IF NOT EXISTS shared_course (share_id VARCHAR(16) PRIMARY KEY, payload JSONB NOT NULL, "
              "created_at TIMESTAMPTZ NOT NULL DEFAULT now())")


class EditError(ValueError):
    """사용자에게 그대로 보여 줄 수 있는 편집 오류."""


def _state(session_id: str) -> tuple[dict, dict, Conditions]:
    s = session.get(session_id)
    if not s["last_course"]:
        raise EditError("수정할 코스가 없습니다. 먼저 코스를 추천받아 주세요.")
    return s, s["last_course"], s["last_conditions"] or Conditions(query="")


def _sources(ids: list[int], places: dict[int, dict], course: dict) -> list[dict]:
    out = [source_of(places[i]) for i in ids if i in places]
    if any(d.get("rain_prob") is not None for d in course["days"]):  # 날씨를 반영했으면 기상청 출처도 함께
        w = config()["weather"]
        out.append({"label": w["source_label"], "url": None, "portal_url": w.get("portal_url"), "license": w["license"]})
    return out


def apply_order(session_id: str, days: list[list[int]]) -> dict:
    """날짜별 place_id 순서(드래그·빼기·교체 결과)로 코스를 다시 만든다. {course, sources, notices}."""
    s, old, cond = _state(session_id)
    if len(days) != len(old["days"]):
        raise EditError("일차 수가 직전 코스와 다릅니다.")
    ids = [i for d in days for i in d]
    if any(not d for d in days) or len(ids) != len(set(ids)):
        raise EditError("하루에 한 곳 이상, 같은 장소는 한 번만 넣을 수 있어요.")
    places = fetch_places(ids)
    if set(ids) - set(places):
        raise EditError("없거나 더 이상 운영하지 않는 장소가 있어요.")
    rows = [places[i] for i in ids]
    if any(same_place(a, b) for n, a in enumerate(rows) for b in rows[n + 1:]):
        raise EditError("같은 장소(이름이 같거나 같은 자리)가 두 번 들어갈 수 없어요.")
    reasons = {i["place_id"]: i.get("reason") for d in old["days"] for i in d["items"]}
    new_days = []
    for d_old, day_ids in zip(old["days"], days):
        items = items_for([places[i] for i in day_ids], cond.mode)  # 순서는 그대로, 이동 시간만 다시
        for it in items:
            it["reason"] = reasons.get(it["place_id"]) or intro_line(places[it["place_id"]]) \
                or fallback_line(places[it["place_id"]])
        new_days.append({"day": d_old["day"], "rain_prob": d_old.get("rain_prob"),
                         "start": d_old.get("start") or config()["course"]["day_start"], "items": items})
    course = {"days": new_days}
    res = REGISTRY["get_route_times"].call({}, {"course": course})  # 택시 구간 실측 + 점심 시간 맞춤
    course = res.updates.get("course", course)
    enrich(course, places, cond)
    s["last_course"] = course
    return {"course": course, "sources": _sources(ids, places, course), "notices": res.notices}


def alternatives(session_id: str, day: int, index: int, n: int = 3) -> list[dict]:
    """'다른 곳으로 교체' 후보: 같은 분류에서, 앞뒤 장소 사이에 넣을 때 덜 돌아가는 순서로 n곳 (코스에 없는 곳만)."""
    _, course, _ = _state(session_id)
    if not 1 <= day <= len(course["days"]) or not 0 <= index < len(course["days"][day - 1]["items"]):
        raise EditError("바꿀 장소를 찾지 못했어요.")
    items = course["days"][day - 1]["items"]
    cur = items[index]
    used = {i["place_id"] for d in course["days"] for i in d["items"]}
    prev, nxt = (items[index - 1] if index else None), (items[index + 1] if index + 1 < len(items) else None)

    def extra(p):
        return detour_km(prev, p, nxt) if (prev or nxt) else detour_km(cur, p, None)
    others = [i for d in course["days"] for i in d["items"] if i is not cur]
    # 코스에 이미 있는 곳과 같은 장소(원천 중복: 공백만 다른 같은 이름, 같은 좌표)는 후보에서 뺀다
    rows = sorted((p for p in places_where(category=cur["category"])
                   if p["place_id"] not in used and not any(same_place(p, q) for q in others)), key=extra)
    return [{"place_id": p["place_id"], "name": p["name"], "category": p["category"], "gu": p["gu"],
             "extra_km": round(extra(p), 1), "intro": intro_line(p) or fallback_line(p)} for p in rows[:n]]


def ensure_table() -> None:
    with db.connect() as conn:
        conn.execute(SHARED_DDL)


def share(session_id: str) -> str:
    """세션의 직전 코스를 공유용으로 저장하고 share_id(8자)를 돌려준다. 질문 원문은 저장하지 않는다."""
    s, course, cond = _state(session_id)
    ids = [i["place_id"] for d in course["days"] for i in d["items"]]
    places = fetch_places(ids)
    payload = {"course": course, "conditions": asdict(cond), "dates": [d.isoformat() for d in s["last_dates"] or []],
               "sources": _sources(ids, places, course)}
    share_id = secrets.token_urlsafe(6)[:8]
    with db.connect() as conn:
        conn.execute(SHARED_DDL)
        conn.execute("INSERT INTO shared_course (share_id, payload) VALUES (%s, %s)", (share_id, Jsonb(payload)))
    return share_id


def load(share_id: str) -> dict | None:
    with db.connect() as conn:
        conn.execute(SHARED_DDL)
        row = conn.execute("SELECT payload FROM shared_course WHERE share_id = %s", (share_id,)).fetchone()
    return row[0] if row else None


def load_into_session(session_id: str, share_id: str) -> dict | None:
    """저장한 코스를 불러와 세션의 직전 코스로 둔다(불러온 뒤에도 편집·수정 버튼이 동작하게)."""
    payload = load(share_id)
    if payload:
        s = session.get(session_id)
        s["last_course"] = payload["course"]
        s["last_conditions"] = Conditions(**payload["conditions"]) if payload.get("conditions") else None
        s["last_dates"] = [date.fromisoformat(x) for x in payload.get("dates") or []]
    return payload
