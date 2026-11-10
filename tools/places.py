"""장소 검색·코스 구성·코스 수정 도구 (FR-002, FR-006, FR-012). 계산은 모두 코드가 한다 (AI-REQ-04)."""
import copy
import logging
import re
from dataclasses import replace

from services.planner import arrivals, build_course, day_minutes, detour_km, items_for, lunch_slot, removal_saving
from services.search import fetch_places, places_where, search, theme_points
from settings import config
from tools.base import BaseTool, ToolResult, register

log = logging.getLogger("tools")


def _brief(p: dict) -> dict:
    return {"id": p["place_id"], "name": p["name"], "category": p["category"], "gu": p["gu"]}


@register
class SearchPlaces(BaseTool):
    name = "search_places"
    description = "사용자 조건(지역·분류·테마)에 맞는 부산 장소 후보를 DB에서 검색한다. 코스를 만들기 전에 먼저 호출한다."
    args_schema = {"type": "object", "properties": {
        "query": {"type": "string", "description": "검색 문장을 바꾸고 싶을 때만 지정"}}}
    timeout_s = 15

    def run(self, args, ctx):
        cond = replace(ctx["conditions"], query=args["query"]) if args.get("query") else ctx["conditions"]
        hits, notices = search(cond)
        if cond.must:  # 이름으로 콕 집은 장소는 검색 결과에 없어도 맨 앞에 넣고 코스에 꼭 넣게 표시한다
            rows = fetch_places(cond.must)
            hits = [{**rows[i], "sim": 1.0, "must": True} for i in cond.must if i in rows] + \
                   [h for h in hits if h["place_id"] not in rows]
        return ToolResult(ok=bool(hits), data={"count": len(hits), "top": [_brief(p) for p in hits[:8]]},
                          notices=notices, updates={"candidates": hits})

    def fallback(self, args, ctx, err):
        return ToolResult(ok=False, data={"error": "장소 검색 실패"}, stale=True,
                          notices=["장소 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."])


@register
class BuildCourse(BaseTool):
    name = "build_course"
    description = ("search_places로 찾은 후보로 날짜별 방문 코스를 만든다(점수화·장소 수·방문 순서). "
                   "날씨를 반영하려면 get_weather를 먼저 호출한다.")
    timeout_s = 10

    def run(self, args, ctx):
        if not ctx.get("candidates"):
            return ToolResult(ok=False, data={"error": "후보가 없습니다. search_places를 먼저 호출하세요."})
        try:  # 테마 기준 장소(바다 → 해변) 좌표. 못 읽으면 분류만으로 가산점을 준다
            near = theme_points(ctx["conditions"].theme)
        except Exception as e:
            log.warning("theme_points 실패: %s", type(e).__name__)
            near = []
        course = build_course(ctx["candidates"], ctx["conditions"], ctx.get("rain_by_day"), near)
        summary = [{"day": d["day"], "places": [i["name"] for i in d["items"]]} for d in course["days"]]
        return ToolResult(ok=bool(course["days"]), data={"days": summary}, notices=course["notices"],
                          updates={"course": course})


@register
class EditCourse(BaseTool):
    name = "edit_course"
    description = ("직전 코스를 제자리에서 고친다: 장소 하나를 교체·추가·삭제하고, 나머지 장소·순서·일차 구조는 그대로 둔 채 "
                   "이동 시간만 다시 계산한다. kind=cafe(점심 무렵에 카페 추가), indoor(실외 한 곳을 실내로), "
                   "shorter(한 곳을 빼서 짧게).")
    args_schema = {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["replace", "add", "remove"]},
        "kind": {"type": "string", "enum": ["cafe", "indoor", "shorter"]},
        "day": {"type": "integer", "description": "며칠째 코스인지 (1부터)"},
        "index": {"type": "integer", "description": "몇 번째 장소인지 (1부터), replace·remove에 필요"},
        "spec": {"type": "string", "description": "새로 넣을 장소 설명 (예: 카페, 실내 전시)"}}}
    timeout_s = 15

    def run(self, args, ctx):
        course = copy.deepcopy(ctx.get("course"))
        if not course or not course["days"]:
            return ToolResult(ok=False, data={"error": "수정할 코스가 없습니다."})
        days, mode, c = course["days"], ctx["conditions"].mode, config()["course"]
        kind = args.get("kind") or _infer_kind(args)
        op = {"cafe": "add", "indoor": "replace", "shorter": "remove"}.get(kind) or args.get("op") or "add"
        used = {i["place_id"] for d in days for i in d["items"]}
        day_no = args.get("day") or _pick_day(kind, days, mode)
        if not 1 <= day_no <= len(days):
            return ToolResult(ok=False, data={"error": f"{day_no}일차 코스가 없습니다."})
        day = days[day_no - 1]
        items = list(day["items"])
        idx = (args.get("index") or 0) - 1

        def fail(msg):
            return ToolResult(ok=False, notices=[msg], data={"error": msg})

        if op == "remove":
            if len(items) <= c["places_per_day_min"]:
                return fail("하루 최소 장소 수라 더 뺄 수 없습니다.")
            if not 0 <= idx < len(items):  # 몇 번째인지 안 정했으면 빼서 가장 많이 줄어드는 곳
                idx = max(range(len(items)), key=lambda i: removal_saving(items, i, mode))
            saved, gone = removal_saving(items, idx, mode), items.pop(idx)
            note = f"{day_no}일차에서 '{gone['name']}'을(를) 빼서 약 {saved}분 줄였어요."
        else:
            if kind == "indoor" and not 0 <= idx < len(items):  # 실외 장소 중 마지막 곳을 바꾼다
                info = fetch_places([i["place_id"] for i in items])
                outdoor = [i for i, it in enumerate(items) if (info.get(it["place_id"]) or {}).get("indoor") is False]
                if not outdoor:
                    return fail(f"{day_no}일차에는 바꿀 실외 장소가 없어요.")
                idx = outdoor[-1]
            pool = _candidates(kind, args, ctx, used)
            if not pool:
                return fail("조건에 맞는 장소를 찾지 못해 코스를 바꾸지 않았습니다.")
            if op == "replace":
                if not 0 <= idx < len(items):
                    return fail("몇 번째 장소를 바꿀지 알려 주세요.")
                prev, nxt = items[idx - 1] if idx > 0 else None, items[idx + 1] if idx + 1 < len(items) else None
                pick = min(pool, key=lambda p: detour_km(prev, p, nxt))
                old, items[idx] = items[idx], pick
                note = f"{day_no}일차 '{old['name']}' 자리를 '{pick['name']}'(으)로 바꿨어요."
            else:  # add: 카페는 점심 무렵, 그 밖에는 동선이 가장 덜 늘어나는 자리
                if kind == "cafe":
                    pos = lunch_slot(items, day.get("start"))
                    pick = min(pool, key=lambda p: detour_km(items[pos - 1] if pos else None, p,
                                                             items[pos] if pos < len(items) else None))
                else:
                    pick = pool[0]
                    pos = min(range(len(items) + 1), key=lambda i: detour_km(items[i - 1] if i else None, pick,
                                                                             items[i] if i < len(items) else None))
                items.insert(pos, pick)
                after = f"'{items[pos - 1]['name']}' 다음" if pos else "맨 앞"
                note = f"{day_no}일차 {after}에 '{pick['name']}'을(를) 넣었어요."
        day["items"] = items_for(items, mode)  # 순서는 그대로, 이동 시간만 다시 계산 (택시 실측은 get_route_times)
        if op == "add":
            arrive = arrivals(day["items"], day.get("start"))[pos][0]
            note = note.replace("에 '", f"({arrive // 60:02d}:{arrive % 60:02d} 무렵)에 '", 1)
        return ToolResult(ok=True, updates={"course": course}, notices=[note],
                          data={"day": day_no, "places": [i["name"] for i in day["items"]]})


def _infer_kind(args: dict) -> str | None:
    """대화로 고칠 때(kind 없음) 설명에서 종류를 읽는다."""
    spec = args.get("spec") or ""
    if re.search(r"카페|커피|디저트", spec) and args.get("op", "add") == "add":
        return "cafe"
    if re.search(r"실내", spec) and args.get("op") in (None, "replace") and not args.get("index"):
        return "indoor"
    if re.search(r"짧게|줄여", spec) and not args.get("index"):
        return "shorter"
    return None


def _pick_day(kind: str | None, days: list[dict], mode: str) -> int:
    """며칠째를 정하지 않았을 때: 카페는 카페가 없는 날 중 장소가 가장 적은 날, 실내는 비 올 확률이 가장 높은 날,
    짧게는 가장 긴 날. 그 밖에는 1일차."""
    if kind == "cafe":
        cafe = config()["classify"]["cafe"]["category"]
        return min(days, key=lambda d: (any(i["category"] == cafe for i in d["items"]), len(d["items"]), d["day"]))["day"]
    if kind == "indoor":
        return max(days, key=lambda d: (d.get("rain_prob") or -1, -d["day"]))["day"]
    if kind == "shorter":
        return max(days, key=lambda d: (day_minutes(d["items"], mode), -d["day"]))["day"]
    return 1


def _candidates(kind: str | None, args: dict, ctx: dict, used: set) -> list[dict]:
    cls = config()["classify"]
    if kind == "cafe":
        rows = places_where(category=cls["cafe"]["category"])
    elif kind == "indoor":
        rows = [p for p in places_where(indoor=True)
                if p["category"] not in (cls["food"]["category"], cls["cafe"]["category"])]
    else:
        named = [i for i in ctx["conditions"].must if i not in used]  # "○○ 코스에 넣어줘": 말한 그 장소를 쓴다
        if named:
            return list(fetch_places(named[:1]).values())
        cond = replace(ctx["conditions"], query=args.get("spec") or ctx["conditions"].query,
                       gu=[], categories=[], indoor=None)
        rows, _ = search(cond, n=20)
    return [p for p in rows if p["place_id"] not in used]
