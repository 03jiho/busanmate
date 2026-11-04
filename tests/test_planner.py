import random

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


def test_route_shorter_than_random_and_rules_hold():  # FR-006
    course = build_course(cands(), Conditions(query="x", hours=9))
    seq = course["days"][0]["items"]
    assert 2 <= len(seq) <= 6
    rnd = random.Random(0)
    shuffled = [rnd.sample(seq, len(seq)) for _ in range(100)]
    assert path_km(seq) < sum(path_km(s) for s in shuffled) / 100
    cats = [p["category"] for p in seq]
    assert all(not (a == b == c) for a, b, c in zip(cats, cats[1:], cats[2:]))
    assert day_minutes(seq, "auto") <= 9 * 60


def test_time_limit_drops_places():
    far = cands(spread=0.4)  # 약 40km 범위
    seq = build_course(far, Conditions(query="x", hours=4))["days"][0]["items"]
    assert len(seq) >= 2 and (day_minutes(seq, "auto") <= 4 * 60 or len(seq) == 2)


def test_rain_prefers_indoor():  # FR-004
    def indoor_ratio(rain):
        items = build_course(cands(), Conditions(query="x", hours=5), [rain])["days"][0]["items"]
        return sum(p["category"] in ("문화시설", "음식점") for p in items) / len(items)
    assert indoor_ratio(90) > indoor_ratio(None)


def test_multi_day_no_duplicates():  # FR-014
    course = build_course(cands(24, spread=0.2), Conditions(query="x", days=3, hours=6), [None, 80, None])
    ids = [p["place_id"] for d in course["days"] for p in d["items"]]
    assert len(course["days"]) == 3 and len(ids) == len(set(ids))
    assert all(len(d["items"]) >= 2 for d in course["days"])


def test_days_capped_at_three():
    course = build_course(cands(24, spread=0.2), Conditions(query="x", days=5))
    assert len(course["days"]) == 3 and any("최대 3일" in n for n in course["notices"])


def test_leg_mode_auto_walk_or_taxi():
    a = {"lat": 35.10, "lng": 129.03, "gu": "중구"}
    near, far = {**a, "lat": 35.105}, {**a, "lat": 35.20, "gu": "동래구"}
    assert estimate_leg(a, near)["mode"] == "walk"
    assert estimate_leg(a, far)["mode"] == "taxi"
    assert estimate_leg(a, far, "walk")["mode"] == "walk"
    assert estimate_leg(a, near)["estimated"] is True
