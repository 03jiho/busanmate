"""Scorer · RoutePlanner (COMP-06, FR-004~006, FR-014). LLM 없이 결정적으로 계산한다 (AI-REQ-04)."""
import itertools
import math
from collections import Counter

from services.route import estimate_leg, haversine_km, road_km
from settings import config


def weather_term(p: dict, rain_prob: float | None) -> float:
    """강수확률이 임계 이상이면 실내 1, 실외 0. 예보 없음·맑음·실내 불명은 중립."""
    c = config()
    if rain_prob is None or rain_prob < c["weather"]["rain_prob_threshold"] or p["indoor"] is None:
        return c["score"]["weather_neutral"]
    return 1.0 if p["indoor"] else 0.0


def _centroid(ps):
    return sum(p["lat"] for p in ps) / len(ps), sum(p["lng"] for p in ps) / len(ps)


def score(cands: list[dict], theme: str | None = None, rain_prob: float | None = None) -> list[dict]:
    """S(p) = w1·sim + w2·pref + w3·weather − w4·dist. dist는 유사도 1위 장소(앵커)에서의 거리, 0~1 정규화.
    앵커 기준이라 코스가 한 지역에 모인다. 점수 내림차순."""
    if not cands:
        return []
    s = config()["score"]
    prefer = set(config()["preferences"]["theme_categories"].get(theme, []))
    anchor = max(cands, key=lambda p: p["sim"])
    dist = [haversine_km(p["lat"], p["lng"], anchor["lat"], anchor["lng"]) for p in cands]
    dmax = max(dist) or 1.0
    out = [{**p, "score": s["w_sim"] * p["sim"] + s["w_pref"] * (p["category"] in prefer)
            + s["w_weather"] * weather_term(p, rain_prob) - s["w_dist"] * d / dmax}
           for p, d in zip(cands, dist)]
    return sorted(out, key=lambda p: -p["score"])


def places_per_day(hours: float) -> int:
    """K = clamp(floor(가용시간 / (평균 체류 + 평균 이동)), 2, 6)."""
    c = config()["course"]
    k = math.floor(hours * 60 / (c["default_stay_min"] + c["default_move_min"]))
    return max(c["places_per_day_min"], min(c["places_per_day_max"], k))


def select(scored: list[dict], k: int) -> list[dict]:
    """점수 순으로 고르되 같은 분류가 이미 뽑혔으면 그 수 × λ만큼 감점 (다양성 보정)."""
    lam = config()["score"]["diversity_penalty"]
    pool, chosen = list(scored), []
    while pool and len(chosen) < k:
        best = max(pool, key=lambda p: p["score"] - lam * sum(q["category"] == p["category"] for q in chosen))
        chosen.append(best)
        pool.remove(best)
    return chosen


def path_km(seq) -> float:
    return sum(road_km(a, b) for a, b in zip(seq, seq[1:]))


def _variety_ok(seq) -> bool:
    max_run, run = config()["course"]["max_same_category_run"], 1
    for a, b in zip(seq, seq[1:]):
        run = run + 1 if a["category"] == b["category"] else 1
        if run > max_run:
            return False
    return True


def best_order(places: list[dict], check_variety: bool) -> list[dict] | None:
    """모든 순열(K ≤ 6이므로 최대 720개) 중 총 이동거리가 가장 짧은 순서. 조건을 만족하는 순서가 없으면 None."""
    best = None
    for perm in itertools.permutations(places):
        if check_variety and not _variety_ok(perm):
            continue
        d = path_km(perm)
        if best is None or d < best[0]:
            best = (d, list(perm))
    return best[1] if best else None


def day_minutes(seq: list[dict], mode: str) -> int:
    stay = config()["course"]["default_stay_min"]
    return stay * len(seq) + sum(estimate_leg(a, b, mode)["minutes"] for a, b in zip(seq, seq[1:]))


def plan_day(scored: list[dict], k: int, hours: float, mode: str, check_variety: bool) -> tuple[list[dict], list[str]]:
    """K곳을 고르고 순서를 정한다. 가용 시간을 넘으면 최저 점수 장소를 빼고 다시 정한다."""
    c = config()["course"]
    min_k, notices = c["places_per_day_min"], []
    # 하루 동선이 흩어지지 않게 기준 장소 반경 안에서만 고른다.
    # 도보만이어도 장소 수는 줄이지 않는다: 걸어서 walk_max_leg_min을 넘는 구간은 estimate_leg가 택시로 바꾼다.
    group = scored
    anchor = scored[0]
    if mode == "walk":  # 도보: 1위가 외딴 곳이면 걸어서 닿는 이웃이 있는 장소 중 점수 1위를 기준으로 (도보 구간이 많아지게)
        dense = [p for p in scored
                 if sum("walk_minutes" not in estimate_leg(p, q, "walk") for q in scored if q is not p) >= min_k - 1]
        anchor = dense[0] if dense else anchor
    def dist(p):
        return haversine_km(p["lat"], p["lng"], anchor["lat"], anchor["lng"])

    near = [p for p in scored if dist(p) <= c["walk_radius_km" if mode == "walk" else "day_radius_km"]]
    if mode == "walk" and len(near) < k:  # 도보: 반경 안이 모자라면 장소 수를 줄이지 않고 가까운 순으로 채운다(먼 구간은 택시)
        near = sorted(scored, key=dist)[:k]
    if len(near) >= min_k:
        scored, k = near, min(k, len(near))
    seq = select(scored, k)
    while True:
        ordered = best_order(seq, check_variety)
        if ordered is None:
            # 한 분류가 많아 3연속을 피할 순서가 없다(FR-006): 그 분류의 최저점 장소를 지금 동선에 가장 가까운
            # 다른 분류 장소로 바꾸고, 없으면 뺀다. 2곳 이하는 항상 조건을 만족한다.
            top = Counter(p["category"] for p in seq).most_common(1)[0][0]
            seq.remove(min((p for p in seq if p["category"] == top), key=lambda p: p["score"]))
            clat, clng = _centroid(seq)
            alts = [p for p in group if p["category"] != top and p not in seq]
            if alts:
                seq.append(min(alts, key=lambda p: haversine_km(p["lat"], p["lng"], clat, clng)))
            continue
        if day_minutes(ordered, mode) <= hours * 60 or len(ordered) <= min_k:
            break
        seq.remove(min(seq, key=lambda p: p["score"]))
    if day_minutes(ordered, mode) > hours * 60:
        notices.append("가용 시간 안에 모두 둘러보기 빠듯한 일정입니다.")
    return ordered, notices


def kmeans(points: list[dict], k: int, iters: int = 20) -> list[list[dict]]:
    """위·경도 k-means(그룹 크기 상한 = ⌈n/k⌉). 첫 중심은 점수 1위, 이후는 기존 중심에서 가장 먼 점(결정적).
    초기 중심은 점수 상위 절반에서만 고르고, 상한을 둬서 외곽 한 곳이 하루를 차지하는 [1, 23] 같은 쏠림을 막는다."""
    seeds = points[:max(k, len(points) // 2)]
    centers = [(points[0]["lat"], points[0]["lng"])]
    while len(centers) < k:
        centers.append(max(((p["lat"], p["lng"]) for p in seeds),
                           key=lambda c: min(haversine_km(*c, *m) for m in centers)))
    cap = math.ceil(len(points) / k)
    for _ in range(iters):
        groups = [[] for _ in centers]
        for p in points:  # 점수 순으로, 가장 가까운 중심 중 자리가 남은 곳에
            order = sorted(range(k), key=lambda i: haversine_km(p["lat"], p["lng"], *centers[i]))
            groups[next(i for i in order if len(groups[i]) < cap)].append(p)
        new = [_centroid(g) if g else centers[i] for i, g in enumerate(groups)]
        if new == centers:
            break
        centers = new
    return groups


def items_for(seq: list[dict], mode: str) -> list[dict]:
    return [{"order": i + 1, "place_id": p["place_id"], "name": p["name"], "category": p["category"],
             "gu": p["gu"], "lat": p["lat"], "lng": p["lng"], "reason": None,
             "stay_min": config()["course"]["default_stay_min"],
             "leg_to_next": estimate_leg(p, seq[i + 1], mode) if i + 1 < len(seq) else None}
            for i, p in enumerate(seq)]


def build_course(cands: list[dict], cond, rain_by_day: list[float | None] | None = None) -> dict:
    """후보 → 날짜별 코스. cond는 services.search.Conditions. rain_by_day[i]는 i일차 강수확률(없으면 None)."""
    c = config()["course"]
    days, notices = max(1, min(cond.days, c["max_days"])), []
    if cond.days > c["max_days"]:
        notices.append(f"일정은 최대 {c['max_days']}일까지 짤 수 있어 {c['max_days']}일로 줄였습니다.")
    if len(cands) < c["places_per_day_min"]:
        return {"days": [], "notices": notices + ["후보가 부족해 코스를 만들 수 없습니다. 조건을 넓혀 볼까요?"]}
    rain = (list(rain_by_day or []) + [None] * days)[:days]
    hours = cond.hours or c["default_available_hours"]
    k = places_per_day(hours)
    # 사용자가 분류를 지정했으면(테마가 분류 하나에만 대응하는 경우 포함, 예: 맛집 → 음식점) 연속 제한을 두지 않는다 (FR-006)
    theme_cats = config()["preferences"]["theme_categories"].get(cond.theme, [])
    check_variety = not cond.categories and len(theme_cats) != 1

    if days == 1:
        groups = [cands]
    else:
        pool = score(cands, cond.theme)[:days * k * 2]  # 필요 수의 2배: 그룹이 작아지지 않고 저점수 외곽은 빠지게
        clusters = kmeans(pool, days)
        for g in clusters:  # 너무 작은 그룹은 가장 큰 그룹에서 가까운 장소를 빌려 채운다
            while len(g) < c["places_per_day_min"]:
                donor = max(clusters, key=len)
                if len(donor) <= c["places_per_day_min"]:
                    break
                clat, clng = _centroid(g) if g else _centroid(donor)
                near = min(donor, key=lambda p: haversine_km(p["lat"], p["lng"], clat, clng))
                donor.remove(near)
                g.append(near)
        # 그룹 1위 장소 반경 안의 남은 후보를 보탠다 (외곽 그룹도 하루치를 채우게, 중복 없이)
        assigned = {p["place_id"] for g in clusters for p in g}
        extra = [p for p in score(cands, cond.theme) if p["place_id"] not in assigned]
        for g in clusters:
            if not g:
                continue
            top = max(g, key=lambda p: p["score"])
            add = [p for p in extra
                   if haversine_km(p["lat"], p["lng"], top["lat"], top["lng"]) <= c["day_radius_km"]]
            g += add
            extra = [p for p in extra if p not in add]
        # 비 올 확률이 높은 날일수록 실내 비율이 높은 그룹을 배정 (FR-014)
        by_rain = sorted(range(days), key=lambda d: -(rain[d] if rain[d] is not None else -1))
        by_indoor = sorted(clusters, key=lambda g: -sum(bool(p["indoor"]) for p in g) / max(len(g), 1))
        groups = [None] * days
        for d, g in zip(by_rain, by_indoor):
            groups[d] = g

    out_days = []
    for d, group in enumerate(groups):
        if len(group) < c["places_per_day_min"]:
            notices.append(f"{d + 1}일차는 후보가 부족해 코스를 만들지 못했습니다.")
            continue
        seq, n = plan_day(score(group, cond.theme, rain[d]), min(k, len(group)), hours, cond.mode, check_variety)
        notices += [f"{d + 1}일차: {m}" if days > 1 else m for m in n]
        if rain[d] is not None and rain[d] >= config()["weather"]["rain_prob_threshold"]:
            notices.append(f"{d + 1}일차 강수확률 {rain[d]:.0f}%라 실내 장소를 우선했습니다.")
        out_days.append({"day": d + 1, "rain_prob": rain[d], "start": c["day_start"], "items": items_for(seq, cond.mode)})
    return {"days": out_days, "notices": notices}
