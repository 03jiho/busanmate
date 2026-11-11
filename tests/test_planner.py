import random

import pytest

from services.planner import build_course, day_minutes, path_km, places_per_day, score
from services.route import estimate_leg
from services.search import Conditions
from settings import config

CATS = ["해변", "문화시설", "음식점", "공원·자연"]


def cands(n=12, spread=0.02, gu="중구"):
    """격자 모양 합성 후보. 분류는 돌아가며, 문화시설·음식점은 실내."""
    rnd = random.Random(1)
    return [{"place_id": i, "name": f"p{i}", "category": CATS[i % 4], "gu": gu,
             "lat": 35.10 + rnd.uniform(0, spread), "lng": 129.03 + rnd.uniform(0, spread),
             "indoor": CATS[i % 4] in ("문화시설", "음식점"), "sim": 0.9 - i * 0.01} for i in range(n)]


def test_places_per_day():
    assert places_per_day(9) == 6 and places_per_day(3) == 2 and places_per_day(1) == 2


def assert_fr006(seq: list[dict], hours: float) -> None:
    """FR-006 수용 조건: 2~6곳, 무작위 순서 100회 평균보다 짧은 동선, 가용 시간 이내, 같은 분류 3연속 없음."""
    assert 2 <= len(seq) <= 6
    rnd = random.Random(0)
    if len(seq) > 2:  # 2곳이면 순서와 무관하게 거리가 같다
        assert path_km(seq) < sum(path_km(rnd.sample(seq, len(seq))) for _ in range(100)) / 100
    assert day_minutes(seq, "auto") <= hours * 60
    cats = [p["category"] for p in seq]
    assert all(not (a == b == c) for a, b, c in zip(cats, cats[1:], cats[2:]))


@pytest.mark.parametrize("hours", [4, 6, 9])
@pytest.mark.parametrize("spread", [0.02, 0.08])
def test_tc006_course_rules(hours, spread):  # TC-006 (FR-006)
    assert_fr006(build_course(cands(spread=spread), Conditions(query="x", hours=hours))["days"][0]["items"], hours)


def test_no_three_in_a_row_even_when_one_category_dominates():  # FR-006: 후보가 한 분류에 몰려도 3연속 금지
    skewed = [{**p, "category": "공원·자연"} for p in cands(10)] + [{**p, "place_id": 100 + i, "sim": 0.3}
                                                                  for i, p in enumerate(cands(2))]
    seq = build_course(skewed, Conditions(query="x", hours=9))["days"][0]["items"]
    assert_fr006(seq, 9)


@pytest.mark.parametrize("spread", [0.02, 0.1, 0.4])
def test_walk_mode_has_no_long_walking_leg(spread):  # "걸어서"에 도보 106분 구간이 나오던 문제
    """도보 구간은 30분 이하, 걸어서 30분 넘는 구간은 택시(장소 수는 줄이지 않는다)."""
    seq = build_course(cands(12, spread=spread), Conditions(query="x", mode="walk", hours=9))["days"][0]["items"]
    legs = [i["leg_to_next"] for i in seq if i["leg_to_next"]]
    assert len(seq) == 6
    assert all(lg["minutes"] <= 30 if lg["mode"] == "walk" else lg["walk_minutes"] > 30 for lg in legs)


def test_course_is_pure_code(monkeypatch):  # AI-REQ-04: 점수화·경로는 LLM 없이
    def boom():
        raise AssertionError("LLM을 호출하면 안 된다")
    monkeypatch.setattr("services.llm.get_llm", boom)
    assert build_course(cands(), Conditions(query="x", days=2))["days"]


def test_time_limit_drops_places():
    far = cands(spread=0.4)  # 약 40km 범위
    seq = build_course(far, Conditions(query="x", hours=4))["days"][0]["items"]
    assert len(seq) >= 2 and (day_minutes(seq, "auto") <= 4 * 60 or len(seq) == 2)


def test_rain_prefers_indoor():  # FR-004
    def indoor_ratio(rain):
        items = build_course(cands(), Conditions(query="x", hours=5), [rain])["days"][0]["items"]
        return sum(p["category"] in ("문화시설", "음식점") for p in items) / len(items)
    assert indoor_ratio(90) > indoor_ratio(None)


def test_tc014_multi_day():  # TC-014 (FR-014): 날짜별 코스가 FR-006을 만족, 전체 중복 없음, 날짜별 예보 반영
    course = build_course(cands(36, spread=0.2), Conditions(query="x", days=3, hours=6), [None, 80, None])
    assert [d["day"] for d in course["days"]] == [1, 2, 3]
    ids = [p["place_id"] for d in course["days"] for p in d["items"]]
    assert len(ids) == len(set(ids))
    for d in course["days"]:
        assert_fr006(d["items"], 6)

    def indoor(d):
        return sum(p["category"] in ("문화시설", "음식점") for p in d["items"]) / len(d["items"])
    rainy, dry = course["days"][1], [course["days"][0], course["days"][2]]
    assert rainy["rain_prob"] == 80 and indoor(rainy) >= max(indoor(d) for d in dry)


def test_days_capped_at_three():
    course = build_course(cands(24, spread=0.2), Conditions(query="x", days=5))
    assert len(course["days"]) == 3 and any("최대 3일" in n for n in course["notices"])


def test_leg_mode_auto_walk_or_taxi():
    a = {"lat": 35.10, "lng": 129.03, "gu": "중구"}
    near, far = {**a, "lat": 35.105}, {**a, "lat": 35.20, "gu": "동래구"}
    assert estimate_leg(a, near)["mode"] == "walk"
    assert estimate_leg(a, far)["mode"] == "taxi"
    assert estimate_leg(a, near)["estimated"] is True


def test_walk_only_never_returns_long_walk():
    """도보만이어도 걸어서 walk_max_leg_min(30분)을 넘는 구간은 택시로 (UI에서 도보 145분이 나오던 문제)."""
    a = {"lat": 35.10, "lng": 129.03, "gu": "중구"}
    mid, far = {**a, "lat": 35.112}, {**a, "lat": 35.20, "gu": "동래구"}  # 보정 후 약 1.7km(26분), 16.7km
    assert estimate_leg(a, mid, "walk")["mode"] == "walk" and estimate_leg(a, mid)["mode"] == "taxi"
    leg = estimate_leg(a, far, "walk")
    assert leg["mode"] == "taxi" and leg["walk_minutes"] > 30 and leg["minutes"] < leg["walk_minutes"]
    # 하루 최소 2곳이라 장소를 더 뺄 수 없는 경우에도 긴 도보 구간이 남지 않는다
    course = build_course([{**a, "place_id": 1, "name": "A", "category": "해변", "sim": 0.9, "indoor": False},
                           {**far, "place_id": 2, "name": "B", "category": "음식점", "sim": 0.8, "indoor": True}],
                          Conditions(query="x", mode="walk"))
    legs = [i["leg_to_next"] for d in course["days"] for i in d["items"] if i["leg_to_next"]]
    assert legs and all(lg["mode"] == "taxi" or lg["minutes"] <= 30 for lg in legs)


def _mixed(n_food: int, n_other: int) -> list[dict]:
    """음식점이 유사도 상위를 차지하는 후보 ("맛집이랑 자연"에서 음식점이 줄줄이 뽑히던 상황)."""
    food = [{"place_id": i, "name": f"f{i}", "category": "음식점", "gu": "중구", "lat": 35.10 + i * 0.001,
             "lng": 129.03, "indoor": True, "sim": 0.95 - i * 0.01} for i in range(n_food)]
    other = [{"place_id": 100 + i, "name": f"o{i}", "category": CATS[i % 2], "gu": "중구", "lat": 35.10 + i * 0.001,
              "lng": 129.031, "indoor": False, "sim": 0.6 - i * 0.01} for i in range(n_other)]
    return food + other


@pytest.mark.parametrize("theme,cap", [("맛집", 3), ("자연", 2), (None, 2)])
def test_food_cap_and_no_three_meals_in_a_row(theme, cap):
    items = build_course(_mixed(8, 6), Conditions(query="x", theme=theme))["days"][0]["items"]
    cats = [i["category"] for i in items]
    assert len(items) == 6 and cats.count("음식점") <= cap
    assert all(not (a == b == c == "음식점") for a, b, c in zip(cats, cats[1:], cats[2:]))


def test_mixed_categories_keep_food_cap():  # 해석기가 "맛집이랑 자연"을 categories=[음식점, 공원·자연]으로 줄 때
    items = build_course(_mixed(8, 6), Conditions(query="x", theme="맛집", categories=["음식점", "공원·자연"]))
    cats = [i["category"] for i in items["days"][0]["items"]]
    assert cats.count("음식점") <= 3 and all(not (a == b == c == "음식점") for a, b, c in zip(cats, cats[1:], cats[2:]))


def test_same_name_with_spacing_is_picked_once():  # 원천 중복 "오륙도 가원" / "오륙도가원"
    cands_ = _mixed(0, 6) + [{**_mixed(1, 0)[0], "place_id": 50, "name": "오륙도 가원"},
                             {**_mixed(1, 0)[0], "place_id": 51, "name": "오륙도가원"}]
    names = [i["name"].replace(" ", "") for i in build_course(cands_, Conditions(query="x"))["days"][0]["items"]]
    assert names.count("오륙도가원") <= 1


def test_food_only_request_has_no_cap():  # "음식점만"처럼 분류를 직접 지정하면 상한 없음
    items = build_course(_mixed(8, 0), Conditions(query="x", categories=["음식점"]))["days"][0]["items"]
    assert [i["category"] for i in items].count("음식점") == 6


def test_sea_theme_credits_places_near_beach():
    """바다 테마: 해변 분류가 아니어도 해변 1km 안이면 가산점, 내륙은 없음 (해변이 적은 지역의 바다 코스)."""
    coast = {"place_id": 1, "name": "해안공원", "category": "공원·자연", "gu": "수영구", "lat": 35.155, "lng": 129.118,
             "indoor": False, "sim": 0.5}
    inland = {**coast, "place_id": 2, "name": "내륙공원", "lat": 35.19, "lng": 129.10}
    by_id = {p["place_id"]: p["score"] for p in score([coast, inland], "바다", near_points=[(35.153, 129.118)])}
    s = config()["score"]
    assert by_id[1] - by_id[2] > s["w_pref"] - s["w_dist"]  # 차이는 가산점(0.3)에서 거리항을 뺀 것보다 크다


def test_walk_course_keeps_places_and_taxis_far_legs():
    """도보: 장소 수를 줄이지 않는다. 걸어서 30분 넘는 구간(외딴 해운대수목원 같은 곳)만 택시로 가고 나머지는 걷는다."""
    lone = {"place_id": 1, "name": "외딴곳", "category": "공원·자연", "gu": "해운대구", "lat": 35.22, "lng": 129.13,
            "indoor": False, "sim": 0.95}
    cluster = [{"place_id": 10 + i, "name": f"c{i}", "category": CATS[i % 4], "gu": "해운대구",
                "lat": 35.158 + i * 0.002, "lng": 129.160, "indoor": False, "sim": 0.8 - i * 0.01} for i in range(4)]
    items = build_course([lone] + cluster, Conditions(query="x", mode="walk"))["days"][0]["items"]
    legs = [i["leg_to_next"] for i in items[:-1]]
    assert len(items) == 5  # 외딴 1위도 빼지 않는다
    assert [lg["mode"] for lg in legs].count("taxi") == 1 and next(lg for lg in legs if lg["mode"] == "taxi")["walk_minutes"] > 30
    assert all(lg["minutes"] <= 30 for lg in legs if lg["mode"] == "walk")


# --- 식사 배치: 체류 5시간 이상이면 11:30~13:30 점심 식당 1곳, 식당·카페 연달아 금지 ---
from services.planner import arrivals, eating, lunch_ok, meals_ok  # noqa: E402


def _eat_mix(n_food=6, n_cafe=2, n_other=6, spread=0.004):
    cafe = [{"place_id": 300 + i, "name": f"카페{i}", "category": "카페", "gu": "중구", "lat": 35.10 + i * spread,
             "lng": 129.032, "indoor": True, "sim": 0.97 - i * 0.01} for i in range(n_cafe)]
    return _mixed(n_food, n_other) + cafe


def _lunch_food(items):
    lo, hi = (int(x[:2]) * 60 + int(x[3:]) for x in config()["course"]["lunch_window"])
    return [i for i, (a, _) in zip(items, arrivals(items)) if i["category"] == "음식점" and lo <= a <= hi]


@pytest.mark.parametrize("theme", [None, "맛집", "바다"])
@pytest.mark.parametrize("mode", ["auto", "walk"])
def test_meal_rules_full_day(theme, mode):
    """하루 9시간(6곳, 체류 6시간): 점심 시간대 식당 1곳 이상, 식당·카페가 연달아 오지 않는다."""
    items = build_course(_eat_mix(), Conditions(query="x", theme=theme, mode=mode))["days"][0]["items"]
    assert len(items) == 6 and meals_ok(items)
    assert _lunch_food(items), [(i["category"], a) for i, (a, _) in zip(items, arrivals(items))]


def test_lunch_added_when_no_food_selected():
    """먹는 곳 점수가 낮아 식당이 안 뽑혀도, 체류 5시간 이상이면 식당 1곳을 넣는다."""
    cands = _mixed(0, 10) + [{**_mixed(1, 0)[0], "place_id": 99, "name": "밥집", "sim": 0.1}]
    items = build_course(cands, Conditions(query="x"))["days"][0]["items"]
    assert any(i["category"] == "음식점" for i in items) and _lunch_food(items)


def test_short_day_does_not_force_lunch():  # 반나절(체류 3시간)은 점심 보장 대상이 아니다
    items = build_course(_mixed(0, 10), Conditions(query="x", hours=4))["days"][0]["items"]
    assert not any(eating(i) for i in items)


def test_meal_rule_functions():
    a, f, c = ({"category": x, "lat": 35.1, "lng": 129.03, "gu": "중구"} for x in ("관광지", "음식점", "카페"))
    assert meals_ok([a, f, a, c]) and not meals_ok([a, f, c]) and not meals_ok([c, f])
    # 10:00 관광지(60분) → 이동 → 11시 무렵 음식점: 11:30 전 도착이면 점심이 아니다
    assert not lunch_ok([a, f], "auto") and lunch_ok([a, a, f], "auto")


def test_food_only_request_skips_meal_rules():  # "음식점만"이면 연달아 금지·점심 보장을 적용하지 않는다
    items = build_course(_mixed(8, 0), Conditions(query="x", categories=["음식점"]))["days"][0]["items"]
    assert len(items) == 6


# --- 실내·비 오는 날 ---
def test_indoor_prefers_indoor_attractions_and_explains_shortage():
    """실내 요청: 실내 명소(문화시설)를 먼저 쓰고 식당·카페로만 채우지 않는다. 모자라면 이유를 알린다."""
    food = _eat_mix(8, 2, 0)
    attr = [{"place_id": 600 + i, "name": f"전시관{i}", "category": "문화시설", "gu": "중구", "lat": 35.101 + i * 0.002,
             "lng": 129.031, "indoor": True, "sim": 0.5} for i in range(2)]
    out = build_course(food + attr, Conditions(query="x", indoor=True, gu=["중구"]))
    items = out["days"][0]["items"]
    assert {600, 601} <= {i["place_id"] for i in items}            # 실내 명소는 모두 쓴다
    assert sum(map(eating, items)) <= (len(items) + 1) // 2 and meals_ok(items)
    assert any("실내 명소" in n and "2곳뿐" in n for n in out["notices"])


def test_split_names_same_coords_not_both_picked():  # "중앙공원, 민주공원"을 나눠 저장해도 한 코스에 같이 넣지 않는다
    a = {"place_id": 1, "name": "중앙공원", "category": "공원·자연", "gu": "중구", "lat": 35.11, "lng": 129.03,
         "indoor": False, "sim": 0.99}
    cands = [a, {**a, "place_id": 2, "name": "민주공원", "sim": 0.98}] + _mixed(0, 8)
    ids = {i["place_id"] for i in build_course(cands, Conditions(query="x"))["days"][0]["items"]}
    assert len(ids & {1, 2}) == 1


def test_fit_lunch_shifts_start_after_measured_taxi():
    """택시 실측이 추정보다 빨라 식당 도착이 11:14가 되면, 순서는 그대로 두고 출발을 10:20으로 옮겨 점심 시간대에 맞춘다."""
    from services.planner import fit_lunch
    a = {"category": "문화시설", "stay_min": 60, "leg_to_next": {"mode": "taxi", "minutes": 14}}
    f = {"category": "음식점", "stay_min": 60, "leg_to_next": {"mode": "taxi", "minutes": 10}}
    rest = [{"category": "문화시설", "stay_min": 60, "leg_to_next": {"mode": "walk", "minutes": 5}} for _ in range(3)]
    day = {"day": 1, "start": "10:00", "items": [a, f] + rest + [{"category": "관광지", "stay_min": 60, "leg_to_next": None}]}
    assert arrivals(day["items"], day["start"])[1][0] == 11 * 60 + 14  # 실측 반영 후 11:14 도착 (점심 시간대 밖)
    assert "10:20" in fit_lunch(day) and day["start"] == "10:20"
    assert arrivals(day["items"], day["start"])[1][0] == 11 * 60 + 34   # 11:34 도착 → 시간대 안
    assert fit_lunch(day) is None                                       # 이미 맞으면 그대로


def test_only_eating_candidates_do_not_shrink_below_minimum():  # 실내 명소 0곳(먹는 곳만) → 1곳짜리 코스가 되던 문제
    out = build_course(_eat_mix(8, 2, 0), Conditions(query="x", indoor=True, gu=["수영구"]))
    assert len(out["days"][0]["items"]) >= 2


def test_fit_lunch_rounds_earlier_shift_enough():  # 13:37 도착 → 5분이 아니라 10분 당겨 13:27 (반올림 오류로 13:32가 되던 문제)
    from services.planner import fit_lunch
    s = [{"category": "문화시설", "stay_min": 60, "leg_to_next": {"mode": "walk", "minutes": 17}} for _ in range(3)]
    day = {"day": 1, "start": "10:00", "items": s + [{"category": "음식점", "stay_min": 60, "leg_to_next": {"mode": "walk",
           "minutes": 5}}, {"category": "관광지", "stay_min": 60, "leg_to_next": None}]}
    assert arrivals(day["items"], "10:00")[3][0] == 13 * 60 + 51
    fit_lunch(day)
    assert day["start"] == "09:35" and arrivals(day["items"], day["start"])[3][0] == 13 * 60 + 26


def test_multi_day_lunch_never_repeats_place():  # 1박 2일에서 같은 식당이 두 날 모두 점심으로 들어가던 문제
    far = [{**p, "place_id": 700 + p["place_id"], "lat": p["lat"] + 0.08} for p in _mixed(0, 8)]
    cands = _mixed(0, 8) + far + [{**_mixed(1, 0)[0], "place_id": 900, "name": "유일한식당", "sim": 0.1, "lat": 35.14}]
    days = build_course(cands, Conditions(query="x", days=2))["days"]
    ids = [i["place_id"] for d in days for i in d["items"]]
    assert len(ids) == len(set(ids)) and ids.count(900) <= 1


# --- 비 오는 날 야외 제한, 식사 시간대 (프롬프트 10·11) ---
from services.planner import is_outdoor, slots_ok  # noqa: E402


def _haeundae_mix():
    """'이번 토요일 해운대'처럼 해변·공원(야외)이 유사도 상위를 차지하는 후보 + 실내 명소·식당."""
    out = [{"place_id": 10 + i, "name": n, "category": c, "gu": "해운대구", "lat": 35.158 + i * 0.002, "lng": 129.16,
            "indoor": False if c == "해변" else None, "sim": 0.95 - i * 0.01, "description": ""}
           for i, (n, c) in enumerate([("해운대", "해변"), ("동백섬", "해변"), ("APEC나루공원", "공원·자연"),
                                       ("달맞이 산책로", "관광지"), ("청사포 전망대", "관광지"), ("송정해수욕장", "해변")])]
    inside = [{"place_id": 40 + i, "name": f"전시관{i}", "category": "문화시설", "gu": "해운대구",
               "lat": 35.16 + i * 0.002, "lng": 129.162, "indoor": True, "sim": 0.6, "description": ""} for i in range(4)]
    food = [{"place_id": 60 + i, "name": f"식당{i}", "category": "음식점", "gu": "해운대구", "lat": 35.161,
             "lng": 129.158 + i * 0.002, "indoor": True, "sim": 0.7, "description": ""} for i in range(3)]
    return out + inside + food


def test_rainy_day_outdoor_at_most_two():
    """강수 60% 시나리오: 야외 장소(해변·공원·산책로·전망대)는 2곳 이하 (실내 후보가 있으면)."""
    items = build_course(_haeundae_mix(), Conditions(query="해운대", gu=["해운대구"]), [60])["days"][0]["items"]
    assert len(items) >= 4 and sum(map(is_outdoor, items)) <= 2
    dry = build_course(_haeundae_mix(), Conditions(query="해운대", gu=["해운대구"]), [10])["days"][0]["items"]
    assert sum(map(is_outdoor, dry)) > 2  # 맑은 날은 제한하지 않는다


def test_is_outdoor_rules():
    base = {"category": "관광지", "indoor": None, "name": "x", "description": ""}
    assert is_outdoor({**base, "name": "달맞이 산책로"}) and is_outdoor({**base, "description": "부산 야경 명소"})
    assert is_outdoor({**base, "category": "해변"}) and not is_outdoor({**base, "category": "해변", "indoor": True})
    assert not is_outdoor(base)


def test_food_only_in_meal_slots_never_first():
    """식당은 11:30~13:30·17:30~19:30에 도착할 때만, 10:00 첫 장소는 식당이 아니다 (식당이 유사도 1위여도)."""
    cands = [{**f, "sim": 0.99} for f in _haeundae_mix() if f["category"] == "음식점"] + \
        [p for p in _haeundae_mix() if p["category"] != "음식점"]
    items = build_course(cands, Conditions(query="x", gu=["해운대구"]))["days"][0]["items"]
    assert items[0]["category"] != "음식점" and slots_ok(items, "auto") and meals_ok(items)
    times = {i["place_id"]: a for i, (a, _) in zip(items, arrivals(items))}
    assert all(11 * 60 + 30 <= times[i["place_id"]] <= 13 * 60 + 30 or 17 * 60 + 30 <= times[i["place_id"]] <= 19 * 60 + 30
               for i in items if i["category"] == "음식점")


def test_multi_day_food_heavy_candidates_keep_each_day_full():
    """식당이 대부분인 후보로 2박 3일: 어느 날도 식당만으로 차서 1~2곳이 되지 않는다(먹는 곳 아닌 장소를 나눠 갖는다)."""
    food = [{"place_id": i, "name": f"식당{i}", "category": "음식점", "gu": "해운대구", "lat": 35.16 + (i % 6) * 0.004,
             "lng": 129.16 + (i // 6) * 0.004, "indoor": True, "sim": 0.95 - i * 0.005} for i in range(36)]
    sights = [{"place_id": 100 + i, "name": f"명소{i}", "category": CATS[i % 4 if i % 4 != 2 else 0], "gu": "해운대구",
               "lat": 35.20 + (i % 3) * 0.004, "lng": 129.20 + (i // 3) * 0.004, "indoor": False, "sim": 0.5} for i in range(12)]
    days = build_course(food + sights, Conditions(query="x", days=3))["days"]
    assert len(days) == 3 and all(len(d["items"]) >= 4 for d in days)
    assert all(meals_ok(d["items"]) for d in days)


def test_one_category_at_most_half_of_day():
    """문화시설이 대부분인 후보: 한 분류는 하루 장소의 절반까지, 다른 분류가 섞인다(3연속 없음)."""
    museums = [{"place_id": i, "name": f"전시관{i}", "category": "문화시설", "gu": "중구", "lat": 35.10 + i * 0.002,
                "lng": 129.03, "indoor": True, "sim": 0.9 - i * 0.01} for i in range(8)]
    others = [{"place_id": 50 + i, "name": f"기타{i}", "category": c, "gu": "중구", "lat": 35.101 + i * 0.002,
               "lng": 129.031, "indoor": c != "공원·자연", "sim": 0.3} for i, c in enumerate(["시장", "거리·마을", "카페", "음식점"])]
    day = build_course(museums + others, Conditions(query="x"))["days"][0]["items"]
    n = sum(i["category"] == "문화시설" for i in day)
    assert n <= len(day) // 2 and len(day) >= 4


def test_category_overflow_explained_when_no_alternatives():
    museums = [{"place_id": i, "name": f"전시관{i}", "category": "문화시설", "gu": "중구", "lat": 35.10 + i * 0.002,
                "lng": 129.03, "indoor": True, "sim": 0.9 - i * 0.01} for i in range(6)]
    course = build_course(museums, Conditions(query="x"))
    assert any("다른 종류 후보가 부족" in n for n in course["notices"])


def test_rainy_outdoor_goes_to_driest_hour():
    """비 오는 날 시간별 예보: 오전에 비가 오고 오후에 그치면 야외 장소(해변)는 오후로 간다."""
    from services.planner import arrivals
    museums = [{"place_id": i, "name": f"실내{i}", "category": c, "gu": "중구", "lat": 35.100 + i * 0.002,
                "lng": 129.030, "indoor": True, "sim": 0.8} for i, c in enumerate(["문화시설", "시장", "관광지", "문화시설", "시장"])]
    beach = {"place_id": 9, "name": "송도해수욕장", "category": "해변", "gu": "중구", "lat": 35.101, "lng": 129.031,
             "indoor": False, "sim": 0.9}
    wet_morning = {h: (90 if h < 13 else 10) for h in range(24)}
    day = build_course(museums + [beach], Conditions(query="x", mode="walk"), [80], rain_hours_by_day=[wet_morning])["days"][0]
    times = dict(zip([i["place_id"] for i in day["items"]], [a for a, _ in arrivals(day["items"], day["start"])]))
    assert times[9] >= 13 * 60  # 강수확률 낮은 오후


def test_stay_minutes_by_category_and_name():
    from services.planner import stay_of
    assert stay_of({"name": "해운대", "category": "해변"}) == 35
    assert stay_of({"name": "부산시립미술관", "category": "문화시설"}) == 90
    assert stay_of({"name": "KF아세안문화원", "category": "문화시설"}) == 75
    assert stay_of({"name": "황령산 전망대", "category": "공원·자연"}) == 40
    assert stay_of({"name": "어딘가", "category": "카페"}) == 40


def test_rainy_day_starts_indoors_when_possible():
    """비 오는 날(시간별 예보 없어도): 실내 장소가 있으면 첫 장소는 실내."""
    m = [{"place_id": i, "name": f"실내{i}", "category": c, "gu": "중구", "lat": 35.100 + i * 0.002, "lng": 129.030,
          "indoor": True, "sim": 0.6} for i, c in enumerate(["문화시설", "시장", "관광지", "문화시설"])]
    beach = {"place_id": 9, "name": "송도해수욕장", "category": "해변", "gu": "중구", "lat": 35.0995, "lng": 129.0295,
             "indoor": False, "sim": 0.95}
    items = build_course(m + [beach], Conditions(query="x", mode="walk"), [80])["days"][0]["items"]
    assert 9 in {i["place_id"] for i in items} and items[0]["place_id"] != 9
