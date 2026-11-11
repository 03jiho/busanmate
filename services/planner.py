"""Scorer · RoutePlanner (COMP-06, FR-004~006, FR-014). LLM 없이 결정적으로 계산한다 (AI-REQ-04)."""
import itertools
import math
from collections import Counter
from functools import partial

from services.route import estimate_leg, haversine_km, road_km
from settings import config


def weather_term(p: dict, rain_prob: float | None) -> float:
    """강수확률이 임계 이상이면 실내 1, 실외 0. 예보 없음·맑음·실내 불명은 중립."""
    c = config()
    if rain_prob is None or rain_prob < c["weather"]["rain_prob_threshold"] or p["indoor"] is None:
        return c["score"]["weather_neutral"]
    return 1.0 if p["indoor"] else 0.0


def is_outdoor(p: dict) -> bool:
    """야외 장소: 실내로 확인된 곳은 아니고, 해변·공원 분류이거나 이름(산책로·전망대 등)·설명(야경)에 야외 단서가 있다."""
    w = config()["weather"]
    if p.get("indoor"):
        return False
    return (p.get("indoor") is False or p["category"] in w["outdoor_categories"]
            or any(k in p["name"] for k in w["outdoor_name_keywords"])
            or any(k in (p.get("description") or "") for k in w["outdoor_desc_keywords"]))


def rainy(rain_prob: float | None) -> bool:
    return rain_prob is not None and rain_prob >= config()["weather"]["rain_prob_threshold"]


def _centroid(ps):
    return sum(p["lat"] for p in ps) / len(ps), sum(p["lng"] for p in ps) / len(ps)


def score(cands: list[dict], theme: str | None = None, rain_prob: float | None = None,
          near_points: list[tuple[float, float]] | None = None, extra_prefer: set | None = None) -> list[dict]:
    """S(p) = w1·sim + w2·pref + w3·weather − w4·dist. dist는 유사도 1위 장소(앵커)에서의 거리, 0~1 정규화.
    앵커 기준이라 코스가 한 지역에 모인다. 점수 내림차순.
    pref: 테마 분류이거나, config theme_near의 기준 분류 장소(near_points, 예: 해변) 근처면 1.
    extra_prefer: 테마 외에 가산점을 줄 분류(예: "실내"·"비 오는 날" 요청 → 문화시설)."""
    if not cands:
        return []
    s = config()["score"]
    prefs = config()["preferences"]
    prefer = set(prefs["theme_categories"].get(theme, [])) | (extra_prefer or set())
    near = (prefs.get("theme_near") or {}).get(theme, {})

    def pref(p):
        return (p["category"] in prefer or any(k in p["name"] for k in near.get("keywords", []))
                or any(haversine_km(p["lat"], p["lng"], *q) <= near.get("km", 0) for q in near_points or []))

    anchor = max(cands, key=lambda p: p["sim"])
    dist = [haversine_km(p["lat"], p["lng"], anchor["lat"], anchor["lng"]) for p in cands]
    dmax = max(dist) or 1.0
    rain_cut = config()["weather"]["rain_outdoor_penalty"] if rainy(rain_prob) else 0  # 비 오는 날 야외는 크게 깎는다
    out = [{**p, "score": s["w_sim"] * p["sim"] + s["w_pref"] * pref(p) - rain_cut * is_outdoor(p)
            + s["w_weather"] * weather_term(p, rain_prob) - s["w_dist"] * d / dmax}
           for p, d in zip(cands, dist)]
    return sorted(out, key=lambda p: -p["score"])


def places_per_day(hours: float) -> int:
    """K = clamp(floor(가용시간 / (평균 체류 + 평균 이동)), 2, 6)."""
    c = config()["course"]
    k = math.floor(hours * 60 / (c["default_stay_min"] + c["default_move_min"]))
    return max(c["places_per_day_min"], min(c["places_per_day_max"], k))


def _food() -> str:
    return config()["classify"]["food"]["category"]


def eating(p: dict) -> bool:
    """먹는 곳(식당·카페). 이 둘은 연달아 두지 않는다."""
    return p["category"] in (_food(), config()["classify"]["cafe"]["category"])


def same_place(a: dict, b: dict) -> bool:
    """같은 장소로 본다: 띄어쓰기만 다른 같은 이름(원천 중복 "오륙도 가원"/"오륙도가원"),
    또는 같은 좌표(합쳐진 이름을 나눠 저장한 "중앙공원"/"민주공원")."""
    return a["name"].replace(" ", "") == b["name"].replace(" ", "") or (a["lat"], a["lng"]) == (b["lat"], b["lng"])


def select(scored: list[dict], k: int, food_cap: int | None = None, mixed: bool = True,
           outdoor_cap: int | None = None) -> list[dict]:
    """점수 순으로 고르되 같은 분류가 이미 뽑혔으면 그 수 × λ만큼 감점 (다양성 보정).
    food_cap: 음식점은 이 수까지만 (다른 분류 후보가 남아 있는 동안). None이면 상한 없음.
    mixed: 먹는 곳(식당·카페)은 장소 수의 절반 이하, 카페는 cafe_per_day 이하 → 연달아 두지 않는 순서가 있게."""
    lam, c = config()["score"]["diversity_penalty"], config()["course"]
    cafe = config()["classify"]["cafe"]["category"]
    chosen = [p for p in scored if p.get("must")][:k]  # 사용자가 이름으로 콕 집은 장소는 먼저 넣는다
    pool = [p for p in scored if p not in chosen and not any(same_place(p, q) for q in chosen)]
    while pool and len(chosen) < k:
        drop = set()
        if food_cap is not None and sum(q["category"] == _food() for q in chosen) >= food_cap:
            drop.add(_food())
        if mixed and sum(q["category"] == cafe for q in chosen) >= c["cafe_per_day"]:
            drop.add(cafe)
        if mixed and sum(eating(q) for q in chosen) >= (k + 1) // 2:
            drop |= {_food(), cafe}
        share = max(1, int(k * c["max_category_share"]))  # 한 분류가 하루의 절반을 넘지 않게
        drop |= {cat for cat, n in Counter(q["category"] for q in chosen).items() if n >= share}
        if drop and any(p["category"] not in drop for p in pool):  # 다른 후보가 남아 있는 동안만
            pool = [p for p in pool if p["category"] not in drop]
        if outdoor_cap is not None and sum(map(is_outdoor, chosen)) >= outdoor_cap \
                and any(not is_outdoor(p) for p in pool):  # 비 오는 날 야외 상한 (실내 후보가 남아 있는 동안)
            pool = [p for p in pool if not is_outdoor(p)]
        best = max(pool, key=lambda p: p["score"] - lam * sum(q["category"] == p["category"] for q in chosen))
        chosen.append(best)
        pool = [p for p in pool if not same_place(p, best)]
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


def meals_ok(seq) -> bool:
    """식당·카페를 연달아 두지 않는다."""
    return not any(eating(a) and eating(b) for a, b in zip(seq, seq[1:]))


def lunch_ok(seq, mode: str, start: str | None = None) -> bool:
    """점심 시간대(lunch_window)에 도착하는 식당이 있다."""
    lo, hi = map(_minutes, config()["course"]["lunch_window"])
    t = _minutes(start or config()["course"]["day_start"])
    for a, b in zip(seq, list(seq[1:]) + [None]):
        if a["category"] == _food() and lo <= t <= hi:
            return True
        t += stay_min(a) + (estimate_leg(a, b, mode)["minutes"] if b else 0)
    return False


def slots_ok(seq, mode: str, start: str | None = None) -> bool:
    """식당은 점심(lunch_window)·저녁(dinner_window) 시간대에 도착할 때만 둔다. 그래서 첫 장소(10:00)는 식당이 아니다."""
    c = config()["course"]
    windows = [tuple(map(_minutes, c[w])) for w in ("lunch_window", "dinner_window")]
    t = _minutes(start or c["day_start"])
    for a, b in zip(seq, list(seq[1:]) + [None]):
        if a["category"] == _food() and not any(lo <= t <= hi for lo, hi in windows):
            return False
        t += stay_min(a) + (estimate_leg(a, b, mode)["minutes"] if b else 0)
    return True


def stay_of(p: dict) -> int:
    """분류·이름별 기본 체류 시간(분): 이름 키워드(미술관·박물관·전망대) > 분류 표 > default_stay_min."""
    c = config()["course"]
    named = [m for kw, m in (c.get("stay_min_by_name") or {}).items() if kw in (p.get("name") or "")]
    return named[0] if named else (c.get("stay_min_by_category") or {}).get(p.get("category"), c["default_stay_min"])


def stay_min(p: dict) -> int:
    """코스 항목에 정해진 체류 시간(더 짧게로 줄였을 수 있음), 없으면 기본값."""
    return p.get("stay_min") or stay_of(p)


def rain_cost(seq, mode: str, pop: dict[int, int], start: str | None = None) -> int:
    """야외 장소에 머무는 시간대의 강수확률 합(시 단위 최댓값). 비 오는 날 순서를 정할 때 작을수록 좋다."""
    c = config()["course"]
    t, total = _minutes(start or c["day_start"]), 0
    for a, b in zip(seq, list(seq[1:]) + [None]):
        s = stay_min(a)
        if is_outdoor(a):
            total += max(pop.get(h, 0) for h in range(t // 60, (t + s) // 60 + 1))
        t += s + (estimate_leg(a, b, mode)["minutes"] if b else 0)
    return total


def needs_lunch(seq) -> bool:
    """하루 체류 합이 lunch_min_stay_min(5시간) 이상이면 점심 식당이 필요하다."""
    c = config()["course"]
    return sum(map(stay_min, seq)) >= c["lunch_min_stay_min"]


def best_order(places: list[dict], check_variety: bool, meals: bool = False,
               lunch: str | None = None, slots: str | None = None,
               rain: tuple[str, dict[int, int]] | None = None) -> list[dict] | None:
    """모든 순열(K ≤ 6이므로 최대 720개) 중 총 이동거리가 가장 짧은 순서. 조건을 만족하는 순서가 없으면 None.
    meals: 식당·카페 연달아 금지. lunch: 이동수단(mode)을 주면 점심 시간대 식당 도착을 요구한다.
    rain: (mode, {시: 강수확률})이면 야외 장소를 강수확률이 낮은 시간대에 두도록 그 시간대 강수확률을 거리로 더한다."""
    best = None
    for perm in itertools.permutations(places):
        if check_variety and not _variety_ok(perm):
            continue
        if meals and not meals_ok(perm):
            continue
        if lunch and not lunch_ok(perm, lunch):
            continue
        if slots and not slots_ok(perm, slots):
            continue
        d = path_km(perm)
        if rain:
            d += config()["weather"]["rain_hour_weight_km"] * rain_cost(perm, rain[0], rain[1]) / 100
            if is_outdoor(perm[0]) and not all(map(is_outdoor, perm)):  # 가능하면 실내로 시작
                d += config()["weather"]["rain_first_outdoor_km"]
        if best is None or d < best[0]:
            best = (d, list(perm))
    return best[1] if best else None


def day_minutes(seq: list[dict], mode: str) -> int:
    return sum(map(stay_min, seq)) + sum(estimate_leg(a, b, mode)["minutes"] for a, b in zip(seq, seq[1:]))


def plan_day(scored: list[dict], k: int, hours: float, mode: str, check_variety: bool,
             food_cap: int | None = None, meals: bool = True,
             lunch_pool: list[dict] | None = None, outdoor_cap: int | None = None,
             rain_hours: dict[int, int] | None = None) -> tuple[list[dict], list[str]]:
    """K곳을 고르고 순서를 정한다. 가용 시간을 넘으면 최저 점수 장소를 빼고 다시 정한다."""
    # 비 오는 날 시간별 예보가 있으면 야외 장소를 강수확률이 낮은 시간대에 (순서 비용에 반영)
    order = partial(best_order, rain=(mode, rain_hours) if rain_hours is not None else None)
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
    must = [p for p in scored if p.get("must")]
    anchor = must[0] if must else anchor  # 이름으로 콕 집은 장소가 있으면 그곳을 기준으로 동선을 짠다

    def dist(p):
        return haversine_km(p["lat"], p["lng"], anchor["lat"], anchor["lng"])

    near = [p for p in scored if dist(p) <= c["walk_radius_km" if mode == "walk" else "day_radius_km"]]
    if mode == "walk" and len(near) < k:  # 도보: 반경 안이 모자라면 장소 수를 줄이지 않고 가까운 순으로 채운다(먼 구간은 택시)
        near = sorted(scored, key=dist)[:k]
    if food_cap is not None:  # 반경 안에 음식점 아닌 곳이 모자라면 반경 밖에서 가까운 순으로 보탠다 (음식점 상한을 지키려고)
        food = config()["classify"]["food"]["category"]
        lack = (k - food_cap) - sum(p["category"] != food for p in near)
        if lack > 0:
            near += sorted((p for p in group if p["category"] != food and p not in near), key=dist)[:lack]
    near += [p for p in must if p not in near]
    if len(near) >= min_k:
        scored, k = near, min(k, len(near))
    seq = select(scored, k, food_cap, mixed=meals, outdoor_cap=outdoor_cap)
    food = _food()
    if meals and needs_lunch(seq) and not any(p["category"] == food for p in seq):
        # 체류 5시간 이상인데 식당이 없다: 가까운 식당 중 점수가 높은 곳을 넣는다(먹는 곳이 아닌 최저점 장소와 바꿈)
        foods = sorted((p for p in group if p["category"] == food and not any(same_place(p, q) for q in seq)),
                       key=lambda p: (dist(p) > c["day_radius_km"], -p["score"]))
        if not foods:  # 다일 일정은 그날 묶음에 식당이 없을 수 있다: 전체 후보 중 가까운 식당(동선 반경 2배 안)
            foods = sorted((p for p in lunch_pool or [] if p["category"] == food and dist(p) <= 2 * c["day_radius_km"]
                            and not any(same_place(p, q) for q in seq)), key=dist)
        if foods:
            victims = [p for p in seq if not p.get("must") and not eating(p)]
            if len(seq) >= k and victims:
                seq.remove(min(victims, key=lambda p: p["score"]))
            seq.append(foods[0])
        else:
            notices.append("점심 식당 후보가 없어 점심 시간을 비워 뒀습니다.")
    lunch_failed = slots_off = False
    while True:
        if outdoor_cap is not None:  # 비 오는 날 야외 상한: 바꾸기 단계에서 다시 늘어난 야외도 실내로 바꾼다
            outs = [p for p in seq if is_outdoor(p) and not p.get("must")]
            alts = [p for p in group if p not in seq and not is_outdoor(p) and not eating(p)
                    and not any(same_place(p, q) for q in seq)]
            if len(outs) > outdoor_cap and alts:
                seq.remove(min(outs, key=lambda p: p["score"]))
                clat, clng = _centroid(seq)
                seq.append(min(alts, key=lambda p: haversine_km(p["lat"], p["lng"], clat, clng)))
                continue
            if len(outs) > outdoor_cap and not any("우산" in n for n in notices):
                notices.append(f"비 예보가 있지만 근처 실내 명소가 부족해 야외 장소가 {len(outs)}곳 들어갔어요(우산 표시).")
        want_lunch = meals and needs_lunch(seq) and any(p["category"] == food for p in seq)
        slot_mode = mode if meals and not slots_off else None  # 식당은 점심·저녁 시간대에만 (먹는 곳만 고른 요청은 예외)
        ordered = order(seq, check_variety, meals, mode if want_lunch else None, slot_mode)
        lunch_failed = False
        if ordered is None and want_lunch:  # 점심 시간에 맞는 순서가 없으면 연속·시간대 규칙만 지키고 알린다
            ordered = order(seq, check_variety, meals, None, slot_mode)
            lunch_failed = ordered is not None
        if ordered is None and slot_mode and order(seq, check_variety, meals) is not None:
            # 식당이 많아 어느 순서로도 점심·저녁 시간대에 맞출 수 없다: 점수가 가장 낮은 식당을 먹는 곳이 아닌 곳으로 바꾼다
            foods = [p for p in seq if p["category"] == food and not p.get("must")]
            if not foods:
                meals = False
                continue
            victim = min(foods, key=lambda p: p["score"])
            seq.remove(victim)
            alts = [p for p in group if p not in seq and p is not victim and not eating(p)
                    and not any(same_place(p, q) for q in seq)]
            if alts:
                clat, clng = _centroid(seq)
                seq.append(min(alts, key=lambda p: haversine_km(p["lat"], p["lng"], clat, clng)))
            elif len(seq) < min_k:  # 바꿀 곳이 없는데 하루 최소 장소 수 밑으로 줄면 식당을 되돌리고 시간대 규칙을 푼다
                seq.append(victim)
                slots_off = True
                notices.append("근처에 식당 말고 다른 후보가 부족해 식사 시간대가 아닌 때에도 식당이 들어갔어요.")
            continue
        if ordered is None:
            # 순서 규칙(같은 분류 3연속 금지 FR-006, 식당·카페 연달아 금지)을 지킬 수 없다: 넘치는 쪽의 최저점 장소를
            # 지금 동선에 가장 가까운 다른 장소로 바꾸고, 없으면 뺀다. 2곳 이하는 항상 조건을 만족한다.
            eat_over = meals and sum(map(eating, seq)) > (len(seq) + 1) // 2
            top = None if eat_over else Counter(p["category"] for p in seq).most_common(1)[0][0]
            removable = [p for p in seq if not p.get("must") and (eating(p) if eat_over else p["category"] == top)]
            if not removable:  # 콕 집은 장소들끼리라 뺄 수 없다: 순서 규칙보다 사용자 지정이 우선
                check_variety = meals = False
                continue
            victim = min(removable, key=lambda p: p["score"])
            seq.remove(victim)
            clat, clng = _centroid(seq)
            full = food_cap is not None and sum(p["category"] == food for p in seq) >= food_cap  # 음식점 상한 유지
            alts = [p for p in group if p not in seq and not any(same_place(p, q) for q in seq) and p is not victim
                    and (not eating(p) if eat_over else p["category"] != top) and not (full and p["category"] == food)]
            if not alts and len(seq) < min_k:  # 바꿀 후보가 없어 하루 최소보다 줄어든다: 줄이지 않고 순서 규칙을 푼다
                seq.append(victim)
                check_variety = meals = False
                notices.append("다른 종류의 후보가 부족해 같은 종류(식당·카페 등)가 이어지는 구간이 있습니다.")
                continue
            if alts:
                seq.append(min(alts, key=lambda p: haversine_km(p["lat"], p["lng"], clat, clng)))
            continue
        sole = [p for p in seq if p["category"] == food]
        keep = sole[0] if want_lunch and len(sole) == 1 else None  # 하나뿐인 점심 식당은 시간이 넘쳐도 빼지 않는다
        others = [p for p in seq if not p.get("must") and p is not keep]
        if day_minutes(ordered, mode) <= hours * 60 or len(ordered) <= min_k or not others:
            break
        seq.remove(min(others, key=lambda p: p["score"]))
    if lunch_failed:
        lo, hi = config()["course"]["lunch_window"]
        notices.append(f"점심 시간({lo}~{hi})에 식당이 오도록 배치하지 못했습니다.")
    if day_minutes(ordered, mode) > hours * 60:
        notices.append("가용 시간 안에 모두 둘러보기 빠듯한 일정입니다.")
    share = max(1, int(len(ordered) * config()["course"]["max_category_share"]))
    for cat, n in Counter(p["category"] for p in ordered).items():
        if n > share and not eating({"category": cat}):  # 먹는 곳 쏠림은 식사 규칙 쪽 안내로
            notices.append(f"근처에 다른 종류 후보가 부족해 {cat}이(가) {n}곳 들어갔어요.")
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


def _minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def arrivals(items: list[dict], start: str | None = None) -> list[tuple[int, int]]:
    """장소별 (도착, 출발) 분. 출발 시각 + 체류 + 구간 이동 (화면의 방문 예정 시각과 같은 계산)."""
    t, out = _minutes(start or config()["course"]["day_start"]), []
    for i in items:
        leave = t + stay_min(i)
        out.append((t, leave))
        t = leave + (i["leg_to_next"]["minutes"] if i.get("leg_to_next") else 0)
    return out


def fit_lunch(day: dict, max_shift: int = 60) -> str | None:
    """택시 구간을 실측하면 계획 때 추정과 시각이 달라져 점심 식당이 시간대를 벗어날 수 있다.
    순서는 그대로 두고 그날 출발 시각을 5분 단위로 옮겨(최대 ±max_shift분) 맞춘다. 바꿨으면 안내문을 돌려준다."""
    items, food = day["items"], _food()
    if not needs_lunch(items) or not any(i["category"] == food for i in items):
        return None
    lo, hi = map(_minutes, config()["course"]["lunch_window"])
    start = _minutes(day.get("start") or config()["course"]["day_start"])
    arrive = [a for i, (a, _) in zip(items, arrivals(items, day.get("start"))) if i["category"] == food]
    if any(lo <= a <= hi for a in arrive):
        return None
    shift = min(((lo - a) if a < lo else (hi - a) for a in arrive), key=abs)
    shift = (1 if shift > 0 else -1) * ((abs(shift) + 4) // 5 * 5)  # 시간대 안으로 들어가게 5분 단위(크기는 올림)
    if abs(shift) > max_shift:
        return None
    day["start"] = f"{(start + shift) // 60:02d}:{(start + shift) % 60:02d}"
    return f"{day['day']}일차는 점심 시간에 맞추려고 출발을 {day['start']}로 조정했어요."


def lunch_slot(items: list[dict], start: str | None = None) -> int:
    """카페를 넣을 자리(이 인덱스 앞에 넣는다): 앞뒤가 식당·카페가 아닌 틈 중, 앞 장소를 떠나는 시각이
    점심 시간대 끝(점심 직후)에 가장 가까운 곳. 그런 틈이 없으면 점심 시간대 가운데에 가장 가까운 장소 다음."""
    lo, hi = map(_minutes, config()["course"]["lunch_window"])
    times = arrivals(items, start)
    free = [i for i in range(1, len(items) + 1) if not eating(items[i - 1]) and (i == len(items) or not eating(items[i]))]
    if free:
        return min(free, key=lambda i: abs(times[i - 1][1] - hi))
    return min(range(1, len(items) + 1), key=lambda i: abs(times[i - 1][1] - (lo + hi) / 2))


def detour_km(prev: dict | None, p: dict, nxt: dict | None) -> float:
    """prev와 nxt 사이에 p를 넣을 때 늘어나는 이동거리."""
    if prev and nxt:
        return road_km(prev, p) + road_km(p, nxt) - road_km(prev, nxt)
    return road_km(prev or nxt, p) if (prev or nxt) else 0.0


def removal_saving(items: list[dict], i: int, mode: str) -> int:
    """i번째 장소를 빼면 줄어드는 분(체류 + 앞뒤 구간 - 새로 생기는 구간)."""
    prev, nxt = items[i - 1] if i > 0 else None, items[i + 1] if i + 1 < len(items) else None
    saved = stay_min(items[i])
    saved += sum(estimate_leg(a, b, mode)["minutes"] for a, b in ((prev, items[i]), (items[i], nxt)) if a and b)
    return saved - (estimate_leg(prev, nxt, mode)["minutes"] if prev and nxt else 0)


def items_for(seq: list[dict], mode: str) -> list[dict]:
    return [{"order": i + 1, "place_id": p["place_id"], "name": p["name"], "category": p["category"],
             "gu": p["gu"], "lat": p["lat"], "lng": p["lng"], "reason": None,
             "stay_min": stay_min(p),
             "leg_to_next": estimate_leg(p, seq[i + 1], mode) if i + 1 < len(seq) else None}
            for i, p in enumerate(seq)]


def build_course(cands: list[dict], cond, rain_by_day: list[float | None] | None = None,
                 near_points: list[tuple[float, float]] | None = None,
                 rain_hours_by_day: list[dict | None] | None = None) -> dict:
    """후보 → 날짜별 코스. cond는 services.search.Conditions. rain_by_day[i]는 i일차 강수확률(없으면 None).
    near_points: 테마 기준 분류 장소 좌표(예: 바다 → 해변). 호출 측이 DB에서 읽어 넘긴다(여기는 순수 계산)."""
    c = config()["course"]
    days, notices = max(1, min(cond.days, c["max_days"])), []
    if cond.days > c["max_days"]:
        notices.append(f"일정은 최대 {c['max_days']}일까지 짤 수 있어 {c['max_days']}일로 줄였습니다.")
    if len(cands) < c["places_per_day_min"]:
        return {"days": [], "notices": notices + ["후보가 부족해 코스를 만들 수 없습니다. 조건을 넓혀 볼까요?"]}
    rain = (list(rain_by_day or []) + [None] * days)[:days]
    rain_hours = (list(rain_hours_by_day or []) + [None] * days)[:days]
    hours = cond.hours or c["default_available_hours"]
    k = places_per_day(hours)
    # 사용자가 분류를 지정했으면(테마가 분류 하나에만 대응하는 경우 포함, 예: 맛집 → 음식점) 연속 제한을 두지 않는다 (FR-006)
    theme_cats = config()["preferences"]["theme_categories"].get(cond.theme, [])
    food = config()["classify"]["food"]["category"]
    food_theme = theme_cats == [food]
    # 맛집 테마는 예외: 끼니를 연달아 먹지 않으므로 3연속 제한을 두고, 음식점 수에 상한을 둔다.
    # 분류를 하나만 직접 지정했으면(예: "음식점만") 연속 제한이 없고, 그게 음식점이면 상한도 없다.
    # 분류를 여러 개 지정했으면(예: "맛집이랑 자연" → 음식점·공원·자연) 섞어 달라는 뜻이므로 둘 다 적용한다.
    if cond.categories:
        check_variety = len(cond.categories) > 1
    else:
        check_variety = len(theme_cats) != 1 or food_theme
    caps = c["food_per_day"]
    food_cap = None if cond.categories == [food] else caps["food_theme" if food_theme or food in cond.categories
                                                            else "default"]
    # 먹는 곳만 고른 요청("음식점만", "카페만")이 아니면 점심 식당 보장·식당·카페 연달아 금지를 지킨다
    meals = not cond.categories or not set(cond.categories) <= {food, config()["classify"]["cafe"]["category"]}
    indoor_cats = set(config()["search"]["indoor_categories"]) if cond.indoor else set()  # "실내"·"비 오는 날"

    def sc(group, rain_prob=None):
        return score(group, cond.theme, rain_prob, near_points, indoor_cats)

    if days == 1:
        groups = [cands]
    else:
        pool = sc(cands)[:days * k * 2]  # 필요 수의 2배: 그룹이 작아지지 않고 저점수 외곽은 빠지게
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
        extra = [p for p in sc(cands) if p["place_id"] not in assigned]
        for g in clusters:
            if not g:
                continue
            top = max(g, key=lambda p: p["score"])
            add = [p for p in extra
                   if haversine_km(p["lat"], p["lng"], top["lat"], top["lng"]) <= c["day_radius_km"]]
            g += add
            extra = [p for p in extra if p not in add]
        # 식당이 많은 검색 결과면 한 날 묶음이 식당만으로 찰 수 있다(그날 1~2곳이 되던 문제): 먹는 곳이 아닌 장소가
        # 하루 필요 수(장소의 절반)보다 적은 날은, 여유가 있는 다른 날에서 가까운 순으로 옮겨 온다
        need = k - (k + 1) // 2
        for g in clusters:
            while g and sum(not eating(p) for p in g) < need:
                donors = [h for h in clusters if h is not g and sum(not eating(p) for p in h) > need]
                if not donors:
                    break
                clat, clng = _centroid(g)
                pick, donor = min(((p, h) for h in donors for p in h if not eating(p)),
                                  key=lambda x: haversine_km(x[0]["lat"], x[0]["lng"], clat, clng))
                donor.remove(pick)
                g.append(pick)
        # 비 올 확률이 높은 날일수록 실내 비율이 높은 그룹을 배정 (FR-014)
        by_rain = sorted(range(days), key=lambda d: -(rain[d] if rain[d] is not None else -1))
        by_indoor = sorted(clusters, key=lambda g: -sum(bool(p["indoor"]) for p in g) / max(len(g), 1))
        groups = [None] * days
        for d, g in zip(by_rain, by_indoor):
            groups[d] = g

    out_days, used = [], set()
    for d, group in enumerate(groups):
        if len(group) < c["places_per_day_min"]:
            notices.append(f"{d + 1}일차는 후보가 부족해 코스를 만들지 못했습니다.")
            continue
        others = {p["place_id"] for g in groups if g is not group for p in g} | used  # 점심 식당도 다른 날과 겹치지 않게
        seq, n = plan_day(sc(group, rain[d]), min(k, len(group)), hours, cond.mode, check_variety, food_cap, meals,
                          lunch_pool=[p for p in sc(cands) if p["place_id"] not in others],
                          outdoor_cap=config()["weather"]["rain_outdoor_max"] if rainy(rain[d]) else None,
                          rain_hours=(rain_hours[d] or {}) if rainy(rain[d]) else None)  # 시간별 예보가 없어도 실내로 시작
        used |= {p["place_id"] for p in seq}
        notices += [f"{d + 1}일차: {m}" if days > 1 else m for m in n]
        if rain[d] is not None and rain[d] >= config()["weather"]["rain_prob_threshold"]:
            notices.append(f"{d + 1}일차 강수확률 {rain[d]:.0f}%라 실내 장소를 우선했습니다.")
        out_days.append({"day": d + 1, "rain_prob": rain[d], "start": c["day_start"], "items": items_for(seq, cond.mode)})
    if indoor_cats:  # 실내 요청인데 먹는 곳이 아닌 자리를 실내 명소로 다 채우지 못했으면 이유를 알린다
        items = [i for day in out_days for i in day["items"]]
        n_attr = len({p["name"] for p in cands if p["category"] in indoor_cats})
        short = n_attr < days * (k - (k + 1) // 2)  # 먹는 곳이 아닌 자리(하루 절반)를 채울 만큼 없다 → 장소 수가 준다
        if short or sum(i["category"] in indoor_cats for i in items) < sum(not eating(i) for i in items):
            where = ", ".join(cond.gu) or "부산"
            notices.append(f"{where}에서 조건에 맞는 실내 명소(박물관·전시관 등) 후보가 {n_attr}곳뿐이라 "
                           "일부는 다른 장소로 채우거나 장소 수를 줄였습니다.")
    return {"days": out_days, "notices": notices}
