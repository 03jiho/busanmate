"""장소 검색·코스 구성·코스 수정 도구 (FR-002, FR-006, FR-012). 계산은 모두 코드가 한다 (AI-REQ-04)."""
import copy
import logging
from dataclasses import replace

from services.planner import best_order, build_course, items_for
from services.route import haversine_km
from services.search import fetch_places, search, theme_points
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
    description = "직전 코스의 장소 하나를 교체·추가·삭제한다. 나머지 장소는 그대로 두고 순서와 이동 시간만 다시 계산한다."
    args_schema = {"type": "object", "required": ["op"], "properties": {
        "op": {"type": "string", "enum": ["replace", "add", "remove"]},
        "day": {"type": "integer", "description": "며칠째 코스인지 (1부터)"},
        "index": {"type": "integer", "description": "몇 번째 장소인지 (1부터), replace·remove에 필요"},
        "spec": {"type": "string", "description": "새로 넣을 장소 설명 (예: 카페, 실내 전시)"}}}
    timeout_s = 15

    def run(self, args, ctx):
        course = copy.deepcopy(ctx.get("course"))
        if not course or not course["days"]:
            return ToolResult(ok=False, data={"error": "수정할 코스가 없습니다."})
        day_no, op = args.get("day") or 1, args["op"]
        if not 1 <= day_no <= len(course["days"]):
            return ToolResult(ok=False, data={"error": f"{day_no}일차 코스가 없습니다."})
        day = course["days"][day_no - 1]
        places, idx = list(day["items"]), (args.get("index") or len(day["items"])) - 1
        if op != "add" and not 0 <= idx < len(places):
            return ToolResult(ok=False, data={"error": f"{idx + 1}번째 장소가 없습니다."})

        if op == "remove":
            if len(places) <= config()["course"]["places_per_day_min"]:
                return ToolResult(ok=False, notices=["하루 최소 장소 수라 더 뺄 수 없습니다."],
                                  data={"error": "최소 장소 수"})
            places.pop(idx)
        else:
            anchor = places[idx] if op == "replace" else places[-1]
            used = {i["place_id"] for d in course["days"] for i in d["items"]}
            cond = replace(ctx["conditions"], query=args.get("spec") or ctx["conditions"].query,
                           gu=[], categories=[], indoor=None)
            named = [i for i in ctx["conditions"].must if i not in used]  # "○○ 코스에 넣어줘": 말한 그 장소를 쓴다
            hits = list(fetch_places(named[:1]).values()) if named else []
            if not hits:
                hits, _ = search(cond, n=20)
                hits = [h for h in hits if h["place_id"] not in used]
            if not hits:
                return ToolResult(ok=False, data={"error": "조건에 맞는 장소 없음"},
                                  notices=["조건에 맞는 장소를 찾지 못해 코스를 바꾸지 않았습니다."])
            radius = config()["course"]["day_radius_km"]
            near = [h for h in hits if haversine_km(h["lat"], h["lng"], anchor["lat"], anchor["lng"]) <= radius]
            pick = (near or hits)[0]
            if op == "replace":
                places[idx] = pick
            else:
                places.append(pick)
        day["items"] = items_for(best_order(places, False), ctx["conditions"].mode)
        return ToolResult(ok=True, updates={"course": course},
                          data={"day": day_no, "places": [i["name"] for i in day["items"]]})
