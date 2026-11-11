"""근거 있는 코스 (FR-003, AI-REQ-01): 카드마다 "왜 이곳?"(요청 조건과 맞은 항목), 출처 확인 수, 예상 시간 계산 방식,
예상 비용을 코스에 붙인다. 모두 DB 값과 조건으로 계산한다(LLM 아님)."""
from services.budget import course_budget
from services.planner import arrivals, is_outdoor, rainy
from services.route import haversine_km
from services.search import theme_points
from settings import config


def estimate_basis() -> str:
    """이동시간 '예상' 툴팁: 계산 방식 (config 값 그대로)."""
    t = config()["travel"]
    f, s = t["detour_factor"], t["speed_kmh"]
    return (f"직선거리 × 보정계수(같은 구 {f['same_gu']}, 다른 구 {f['adjacent_gu']}, 바다·다리 구간 {f['sea_or_bridge']})"
            f" ÷ 평균 속도(도보 {s['walk']}km/h, 택시 {s['taxi']}km/h)로 계산한 추정치예요. "
            "택시는 카카오 길찾기 실측이 실패한 구간만 추정치예요.")


def why(p: dict, cond, rain: float | None, arrive: int, prev_leg: dict | None, must: set,
        near_points: list | None = None) -> list[str]:
    """요청 조건과 맞은 항목: 요청한 장소 · 테마 · 실내/날씨 · 점심 시간 · 동선(앞 장소에서 걸어서).
    테마 판정은 점수(planner.score의 pref)와 같다: 테마 분류, 이름의 키워드, 기준 분류 장소(해변) 근처."""
    prefs, c = config()["preferences"], config()["course"]
    theme = getattr(cond, "theme", None)
    out = []
    if p["place_id"] in must:
        out.append("요청한 장소")
    gu = getattr(cond, "gu", None) or []
    if p.get("gu") in gu:
        out.append(f"요청한 지역({p['gu']})")
    near = (prefs.get("theme_near") or {}).get(theme, {})
    close = any(haversine_km(p["lat"], p["lng"], *q) <= near.get("km", 0) for q in near_points or [])
    if theme and (p["category"] in prefs["theme_categories"].get(theme, [])
                  or any(k in p["name"] for k in near.get("keywords", [])) or close):
        out.append(f"테마 '{theme}'와 맞음")
    if p.get("indoor") and getattr(cond, "indoor", None):
        out.append("실내 요청")
    elif p.get("indoor") and rain is not None and rain >= config()["weather"]["rain_prob_threshold"]:
        out.append(f"비 올 확률 {rain:.0f}%라 실내")
    elif rain is not None and rain < config()["weather"]["rain_prob_threshold"] and is_outdoor(p):
        out.append(f"비 올 확률 {rain:.0f}%로 야외 둘러보기 좋음")
    for key, label in (("lunch_window", "점심"), ("dinner_window", "저녁")):
        lo, hi = (int(x[:2]) * 60 + int(x[3:]) for x in c[key])
        if p["category"] == config()["classify"]["food"]["category"] and lo <= arrive <= hi:
            out.append(f"{label} 시간에 맞춤")
    if prev_leg and prev_leg["mode"] == "walk":
        out.append(f"앞 장소에서 걸어서 {prev_leg['minutes']}분")
    return out  # 맞은 조건이 없으면 비워 둔다(화면은 '왜 이곳?' 줄을 생략)


def enrich(course: dict, places: dict[int, dict], cond) -> None:
    """코스에 근거를 붙인다: item.why, course.sourced(출처 확인된 장소 수/전체), estimate_basis, budget."""
    must = set(getattr(cond, "must", None) or [])
    try:  # 테마 기준 장소 좌표(바다 → 해변). DB를 못 읽으면 분류·키워드로만 판정한다
        near_points = theme_points(getattr(cond, "theme", None))
    except Exception:
        near_points = []
    total = sourced = 0
    for d in course["days"]:
        times = arrivals(d["items"], d.get("start"))
        for k, item in enumerate(d["items"]):
            p = places.get(item["place_id"])
            total += 1
            if not p:
                continue
            sourced += bool(p.get("license"))  # 출처(제공처·이용 조건)가 DB에 있는 장소
            prev = d["items"][k - 1]["leg_to_next"] if k else None
            item["why"] = why(p, cond, d.get("rain_prob"), times[k][0], prev, must, near_points)
            item["umbrella"] = rainy(d.get("rain_prob")) and is_outdoor(p)  # 비 오는 날 불가피하게 넣은 야외 장소
            leg = item.get("leg_to_next")
            nxt = places.get(d["items"][k + 1]["place_id"]) if k + 1 < len(d["items"]) else None
            # 비 오는 날 야외 장소로 가거나 나오는 도보 구간은 짧아도 택시를 먼저 권한다
            wet = rainy(d.get("rain_prob")) and (is_outdoor(p) or bool(nxt and is_outdoor(nxt)))
            c = config()["course"]  # 택시 제안: 도보 10분 이상, 비 오는 날 야외 장소 앞뒤는 5분 넘으면 (5분 이하는 제안 없음)
            if leg and leg["mode"] == "walk" and (leg["minutes"] >= c["walk_taxi_hint_min"]
                                                  or (wet and leg["minutes"] > c["walk_no_taxi_max_min"])):
                taxi = config()["travel"]["speed_kmh"]["taxi"]
                leg["taxi_hint_min"] = max(1, round((leg.get("meters") or 0) / 1000 / taxi * 60))
                leg["rain_taxi"] = wet
    course["sourced"] = {"count": sourced, "total": total}
    course["estimate_basis"] = estimate_basis()
    course["budget"] = course_budget(course, places)
