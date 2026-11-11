"""예상 비용 (코스 맞춤 기능): 메뉴 가격 읽기, 택시 요금 추정(부산 요금표 초안), 날짜별 합계."""
from services.budget import course_budget, meal_price, taxi_fare


def test_meal_price_from_menu_or_default():
    assert meal_price({"category": "음식점", "description": "동삼정\n대표 메뉴: 한우육회 타르타르 ￦25,000"}) == (25000, False)
    assert meal_price({"category": "카페", "description": "대표 메뉴: RI 드립커피 6,500원"}) == (6500, False)
    assert meal_price({"category": "음식점", "description": "1964년 창업, 2층 50석"}) == (12000, True)  # 가격 없음 → 기본 단가
    assert meal_price({"category": "음식점", "description": "주차 1,000원 · 대표 메뉴 국밥 9,000원"}) == (9000, False)


def test_taxi_fare_measured_first_then_tariff():
    assert taxi_fare({"mode": "taxi", "taxi_fare": 7600, "meters": 3000}) == (7600, False)
    assert taxi_fare({"mode": "taxi", "meters": 1500}) == (4800, True)            # 기본거리 2km 안
    assert taxi_fare({"mode": "taxi", "meters": 2000 + 132 * 10}) == (5800, True)  # 132m마다 100원


def test_course_budget_sums_meals_and_taxi():
    items = [{"place_id": 1, "category": "해변", "leg_to_next": {"mode": "taxi", "meters": 2264, "taxi_fare": None}},
             {"place_id": 2, "category": "음식점", "leg_to_next": {"mode": "walk", "minutes": 5}},
             {"place_id": 3, "category": "카페", "leg_to_next": None}]
    places = {1: {"category": "해변"}, 2: {"category": "음식점", "description": "￦15,000"}, 3: {"category": "카페"}}
    course = {"days": [{"day": 1, "items": items}]}
    b = course_budget(course, places)
    assert (b["meals"], b["taxi"], b["total"], b["estimated"]) == (15000 + 6000, 5000, 26000, True)
    assert items[1]["price_won"] == 15000 and items[2]["price_estimated"] is True
