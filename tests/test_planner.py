import random

import pytest

from services.planner import build_course, day_minutes, path_km, places_per_day
from services.route import estimate_leg
from services.search import Conditions

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
