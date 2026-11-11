"""예상 비용 (코스 맞춤 기능): 식비(1인)와 택시비(1대). 결정적 계산이다 (AI-REQ-04).
식비는 공공데이터 맛집 설명의 대표 메뉴 가격("￦25,000", "12,000원")을 쓰고, 없으면 config 기본 단가.
택시비는 카카오 실측 요금이 있으면 그것, 없으면 부산 택시 요금표(초안)로 거리에서 추정한다."""
import math
import re

from settings import config

_PRICE = re.compile(r"[￦₩]\s*([\d,]{4,8})|([\d,]{4,8})\s*원")


def meal_price(p: dict) -> tuple[int, bool]:
    """(1인 가격, 추정 여부). 설명 속 첫 번째 그럴듯한 가격(3천~20만 원)을 대표 메뉴 가격으로 본다."""
    for a, b in _PRICE.findall(p.get("description") or ""):
        won = int((a or b).replace(",", ""))
        if 3_000 <= won <= 200_000:
            return won, False
    return config()["budget"]["meal_default"].get(p["category"], 0), True


def taxi_fare(leg: dict) -> tuple[int, bool]:
    """(1대 요금, 추정 여부). 카카오 실측 요금이 없으면 기본요금 + 거리요금으로 추정한다."""
    if leg.get("taxi_fare"):
        return int(leg["taxi_fare"]), False
    t = config()["budget"]["taxi"]
    extra = max(0, (leg.get("meters") or 0) - t["base_m"])
    return t["base_won"] + math.ceil(extra / t["step_m"]) * t["step_won"], True


def course_budget(course: dict, places: dict[int, dict]) -> dict:
    """날짜별 {day, meals, taxi, total} + 합계. 먹는 곳 카드에는 price_won·price_estimated를 붙인다."""
    eat = {config()["classify"]["food"]["category"], config()["classify"]["cafe"]["category"]}
    days, estimated = [], False
    for d in course["days"]:
        meals = taxi = 0
        for item in d["items"]:
            if item["category"] in eat and item["place_id"] in places:
                item["price_won"], item["price_estimated"] = meal_price(places[item["place_id"]])
                meals += item["price_won"]
                estimated |= item["price_estimated"]
            leg = item.get("leg_to_next")
            if leg and leg["mode"] == "taxi":
                fare, est = taxi_fare(leg)
                taxi += fare
                estimated |= est
        days.append({"day": d["day"], "meals": meals, "taxi": taxi, "total": meals + taxi})
    return {"days": days, "meals": sum(x["meals"] for x in days), "taxi": sum(x["taxi"] for x in days),
            "total": sum(x["total"] for x in days), "estimated": estimated}
