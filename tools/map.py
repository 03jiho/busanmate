"""MapTool (FR-007): 택시 구간은 카카오모빌리티 자동차 길찾기로 실측(시간·거리·택시요금), 도보는 추정치.
이동시간은 약관상 저장·캐시하지 않는다. 조회에 실패한 구간은 추정치를 그대로 둔다 (NFR-003)."""
import copy
import logging
from concurrent.futures import ThreadPoolExecutor

import requests

from settings import config, secret
from tools.base import BaseTool, ToolResult, register

log = logging.getLogger("tools")
_pool = ThreadPoolExecutor(max_workers=6)


def kakao_leg(a: dict, b: dict) -> dict:
    r = requests.get(secret("KAKAO_MOBILITY_DIRECTIONS_URL"), timeout=config()["kakao"]["timeout_s"],
                     params={"origin": f"{a['lng']},{a['lat']}", "destination": f"{b['lng']},{b['lat']}"},
                     headers={"Authorization": f"KakaoAK {secret('KAKAO_REST_API_KEY')}"})
    r.raise_for_status()
    route = r.json()["routes"][0]
    if route["result_code"] != 0:
        raise RuntimeError(f"카카오 길찾기 결과 코드 {route['result_code']}")
    s = route["summary"]
    return {"mode": "taxi", "minutes": max(1, round(s["duration"] / 60)), "meters": s["distance"],
            "estimated": False, "taxi_fare": s.get("fare", {}).get("taxi")}


@register
class GetRouteTimes(BaseTool):
    name = "get_route_times"
    description = "코스의 구간별 이동 시간을 조회한다. 택시 구간은 실제 길찾기 시간과 예상 요금으로 바꾼다. build_course 다음에 호출한다."
    timeout_s = 8

    def run(self, args, ctx):
        course = copy.deepcopy(ctx.get("course"))
        if not course or not course["days"]:
            return ToolResult(ok=False, data={"error": "코스가 없습니다. build_course를 먼저 호출하세요."})
        legs = [(items[i], items[i + 1]) for d in course["days"] for items in [d["items"]]
                for i in range(len(items) - 1) if items[i]["leg_to_next"]["mode"] == "taxi"]

        def measure(pair):
            try:
                walk = pair[0]["leg_to_next"].get("walk_minutes")  # 도보만인데 멀어서 택시로 바꾼 구간 표시용
                pair[0]["leg_to_next"] = {**kakao_leg(*pair), **({"walk_minutes": walk} if walk else {})}
                return True
            except Exception as e:
                # URL·키는 남기지 않는다 (RuntimeError는 우리가 만든 결과 코드 메시지라 안전)
                log.warning("kakao 실패: %s", e if isinstance(e, RuntimeError) else type(e).__name__)
                return False

        ok = list(_pool.map(measure, legs))
        notices = []
        if not all(ok):
            notices.append("일부 택시 구간은 길찾기 조회에 실패해 직선거리 기반 예상치로 표시합니다.")
        if any(i["leg_to_next"] and i["leg_to_next"].get("walk_minutes") for d in course["days"] for i in d["items"]):
            notices.append(f"걸어서 {config()['course']['walk_max_leg_min']}분이 넘는 구간은 택시로 바꿨습니다.")
        if any(i["leg_to_next"] and i["leg_to_next"]["mode"] == "walk" for d in course["days"] for i in d["items"]):
            notices.append("도보 구간 시간은 직선거리 기반 예상치입니다.")
        return ToolResult(ok=True, data={"taxi_legs": len(legs), "measured": sum(ok)}, notices=notices,
                          updates={"course": course})

    def fallback(self, args, ctx, err):
        return ToolResult(ok=False, data={"error": "이동 시간 조회 실패"}, stale=True,
                          notices=["이동 시간은 직선거리 기반 예상치입니다."])
