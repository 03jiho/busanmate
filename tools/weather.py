"""WeatherTool (FR-004, DATA-04): 기상청 단기예보 강수확률. 출처 표시 필수(공공누리 제1유형), 1시간 캐시."""
import math
import time
from datetime import date, datetime, timedelta, timezone

import requests

from settings import config, secret
from tools.base import BaseTool, ToolResult, register

KST = timezone(timedelta(hours=9))
_BASE_HOURS = [2, 5, 8, 11, 14, 17, 20, 23]  # 단기예보 발표 시각
_cache: dict[tuple, tuple[float, dict]] = {}
HOURLY: dict[tuple, dict[date, dict[int, int]]] = {}  # 격자별 시간별 강수확률 (forecast가 채운다)


def base_time(now: datetime) -> tuple[str, str]:
    """가장 최근 발표분(발표 후 약 10분 뒤 제공되므로 15분 여유)."""
    t = now - timedelta(minutes=15)
    hours = [h for h in _BASE_HOURS if h <= t.hour]
    if not hours:
        t, hours = t - timedelta(days=1), [23]
    return t.strftime("%Y%m%d"), f"{hours[-1]:02d}00"


def daily_pop(items: list[dict]) -> dict[date, int]:
    """시간별 POP에서 낮 시간대(config day_hours) 최댓값을 그날 강수확률로."""
    lo, hi = config()["weather"]["day_hours"]
    out: dict[date, int] = {}
    for i in items:
        if i["category"] == "POP" and lo <= int(i["fcstTime"][:2]) <= hi:
            d = datetime.strptime(i["fcstDate"], "%Y%m%d").date()
            out[d] = max(out.get(d, 0), int(i["fcstValue"]))
    return out


def hourly_pop(items: list[dict]) -> dict[date, dict[int, int]]:
    """날짜별 {시: 강수확률} (비 오는 날 야외 장소를 강수확률이 낮은 시간대에 두는 데 쓴다)."""
    out: dict[date, dict[int, int]] = {}
    for i in items:
        if i["category"] == "POP":
            d = datetime.strptime(i["fcstDate"], "%Y%m%d").date()
            out.setdefault(d, {})[int(i["fcstTime"][:2])] = int(i["fcstValue"])
    return out


def latlon_to_grid(lat: float, lng: float) -> tuple[int, int]:
    """위경도 → 기상청 동네예보 격자(nx, ny). 기상청 공개 Lambert 정각원추도법(격자 5km) 변환식."""
    re, deg = 6371.00877 / 5.0, math.pi / 180
    slat1, slat2, olon, olat = 30.0 * deg, 60.0 * deg, 126.0 * deg, 38.0 * deg
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi / 4 + slat2 / 2) / math.tan(math.pi / 4 + slat1 / 2))
    sf = math.tan(math.pi / 4 + slat1 / 2) ** sn * math.cos(slat1) / sn
    ro = re * sf / math.tan(math.pi / 4 + olat / 2) ** sn
    ra = re * sf / math.tan(math.pi / 4 + lat * deg / 2) ** sn
    theta = (lng * deg - olon + math.pi) % (2 * math.pi) - math.pi
    theta *= sn
    return int(ra * math.sin(theta) + 43 + 0.5), int(ro - ra * math.cos(theta) + 136 + 0.5)


def forecast(nx: int, ny: int) -> dict[date, int]:
    """격자의 날짜별 강수확률. 같은 발표분·격자는 1시간 캐시한다(DATA-04 허용 범위)."""
    w = config()["weather"]
    bd, bt = base_time(datetime.now(KST))
    hit = _cache.get((bd, bt, nx, ny))
    if hit and time.time() - hit[0] < w["cache_ttl_s"]:
        return hit[1]
    r = requests.get(secret("KMA_FORECAST_URL").rstrip("/") + "/" + w["operation"], timeout=5, params={
        "serviceKey": secret("DATA_GO_KR_SERVICE_KEY"), "pageNo": 1, "numOfRows": 1000, "dataType": "JSON",
        "base_date": bd, "base_time": bt, "nx": nx, "ny": ny})
    r.raise_for_status()
    body = r.json()["response"]
    if body["header"]["resultCode"] != "00":
        raise RuntimeError(f"기상청 응답 코드 {body['header']['resultCode']}")
    pop = daily_pop(body["body"]["items"]["item"])
    HOURLY[(nx, ny)] = hourly_pop(body["body"]["items"]["item"])
    _cache[(bd, bt, nx, ny)] = (time.time(), pop)
    return pop


@register
class GetWeather(BaseTool):
    name = "get_weather"
    description = "방문일별 기상청 강수확률을 조회한다. 코스를 만들 때 build_course보다 먼저 호출한다."
    timeout_s = 6

    def run(self, args, ctx):
        dates = ctx.get("dates") or []
        if not dates:
            return ToolResult(ok=False, data={"error": "방문 날짜가 없습니다."})
        # 검색된 후보들의 중심 좌표의 격자로 조회한다 (후보가 없으면 config 기본 위치)
        cands = ctx.get("candidates") or []
        loc = config()["weather"]["default_location"]
        lat, lng = ((sum(p["lat"] for p in cands) / len(cands), sum(p["lng"] for p in cands) / len(cands))
                    if cands else (loc["lat"], loc["lng"]))
        grid = latlon_to_grid(lat, lng)
        pop = forecast(*grid)
        rain = [pop.get(d) for d in dates]
        by_hour = HOURLY.get(grid, {})
        notices = [f"{d.month}월 {d.day}일은 예보 범위 밖이라 날씨를 반영하지 않았습니다."
                   for d, r in zip(dates, rain) if r is None]
        w = config()["weather"]
        return ToolResult(ok=True, data={"rain_prob_by_day": rain}, notices=notices,
                          sources=[{"label": w["source_label"], "url": None, "portal_url": w.get("portal_url"), "license": w["license"]}]
                          if any(r is not None for r in rain) else [],
                          updates={"rain_by_day": rain, "rain_hours_by_day": [by_hour.get(d) for d in dates]})

    def fallback(self, args, ctx, err):
        return ToolResult(ok=False, data={"error": "날씨 조회 실패"}, stale=True,
                          notices=["날씨 정보를 가져오지 못해 반영하지 않았습니다."],
                          updates={"rain_by_day": None, "rain_hours_by_day": None})
