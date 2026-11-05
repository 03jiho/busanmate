"""WeatherTool (FR-004, DATA-04): 기상청 단기예보 강수확률. 출처 표시 필수(공공누리 제1유형), 1시간 캐시."""
import time
from datetime import date, datetime, timedelta, timezone

import requests

from settings import config, secret
from tools.base import BaseTool, ToolResult, register

KST = timezone(timedelta(hours=9))
_BASE_HOURS = [2, 5, 8, 11, 14, 17, 20, 23]  # 단기예보 발표 시각
_cache: dict[tuple, tuple[float, dict]] = {}


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


def forecast() -> dict[date, int]:
    w = config()["weather"]
    bd, bt = base_time(datetime.now(KST))
    hit = _cache.get((bd, bt))
    if hit and time.time() - hit[0] < w["cache_ttl_s"]:
        return hit[1]
    r = requests.get(secret("KMA_FORECAST_URL").rstrip("/") + "/" + w["operation"], timeout=5, params={
        "serviceKey": secret("DATA_GO_KR_SERVICE_KEY"), "pageNo": 1, "numOfRows": 1000, "dataType": "JSON",
        "base_date": bd, "base_time": bt, "nx": w["grid"]["nx"], "ny": w["grid"]["ny"]})
    r.raise_for_status()
    body = r.json()["response"]
    if body["header"]["resultCode"] != "00":
        raise RuntimeError(f"기상청 응답 코드 {body['header']['resultCode']}")
    pop = daily_pop(body["body"]["items"]["item"])
    _cache[(bd, bt)] = (time.time(), pop)
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
        pop = forecast()
        rain = [pop.get(d) for d in dates]
        notices = [f"{d.month}월 {d.day}일은 예보 범위 밖이라 날씨를 반영하지 않았습니다."
                   for d, r in zip(dates, rain) if r is None]
        w = config()["weather"]
        return ToolResult(ok=True, data={"rain_prob_by_day": rain}, notices=notices,
                          sources=[{"label": w["source_label"], "url": None, "license": w["license"]}]
                          if any(r is not None for r in rain) else [],
                          updates={"rain_by_day": rain})

    def fallback(self, args, ctx, err):
        return ToolResult(ok=False, data={"error": "날씨 조회 실패"}, stale=True,
                          notices=["날씨 정보를 가져오지 못해 반영하지 않았습니다."], updates={"rain_by_day": None})
