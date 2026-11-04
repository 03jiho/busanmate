"""구간 이동 추정 (FR-007): 직선거리 × 구간 보정계수 ÷ 수단 속도. 결과는 저장·캐시하지 않는다."""
import math

from settings import config


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def road_km(a: dict, b: dict) -> float:
    """보정계수를 곱한 예상 이동 거리."""
    t = config()["travel"]
    f = t["detour_factor"]
    if a["gu"] == b["gu"]:
        factor = f["same_gu"]
    elif {a["gu"], b["gu"]} & set(t["sea_bridge_gu"]):
        factor = f["sea_or_bridge"]
    else:
        factor = f["adjacent_gu"]
    return haversine_km(a["lat"], a["lng"], b["lat"], b["lng"]) * factor


def estimate_leg(a: dict, b: dict, mode: str = "auto") -> dict:
    """mode: auto(가까우면 도보, 멀면 택시) / walk / taxi."""
    t = config()["travel"]
    km = road_km(a, b)
    if mode == "auto":
        mode = "walk" if km <= t["walk_max_km"] else "taxi"
    return {"mode": mode, "minutes": max(1, round(km / t["speed_kmh"][mode] * 60)),
            "meters": round(km * 1000), "estimated": True}
