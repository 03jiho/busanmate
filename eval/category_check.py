"""카테고리-설명 모순 점검: 카페·음식점 장소를 뽑아 분류와 소개문(대표 메뉴)이 어긋나는지 본다.
실행: python -m eval.category_check [표본 수, 기본 20 | all]. 모순이 있으면 종료 코드 1.
(예: 본 앤 브레드 — 이름의 '브레드' 때문에 카페로 분류됐지만 대표 메뉴는 스테이크하우스, 2026-11-12 발견·수정)"""
import random
import re
import sys

from psycopg.rows import dict_row

import db
from settings import config


def contradiction(p: dict) -> str | None:
    """모순 이유 또는 None. 카페인데 메뉴·소개문이 식사, 음식점인데 메뉴가 카페 메뉴뿐."""
    c = config()["classify"]["cafe"]
    m = re.search(r"대표 메뉴: (.*)", p.get("description") or "")
    menu = m.group(1) if m else ""
    body = (p.get("description") or "").replace(p["name"], "")
    # 소개문의 식사 낱말(예: 브런치 카페의 '불고기파니니')은 대표 메뉴에 카페 메뉴가 없을 때만 모순으로 본다
    cafe_menu = any(k in menu for k in c["menu_keywords"])
    food_hits = [k for k in c["food_menu_keywords"] if k in menu or (k in body and not cafe_menu)]
    if p["category"] == c["category"] and food_hits:
        return f"카페인데 식사 메뉴·설명({', '.join(food_hits[:3])})"
    if p["category"] == config()["classify"]["food"]["category"] and menu \
            and any(k in menu for k in c["menu_keywords"]) and not any(k in menu for k in c["food_menu_keywords"]):
        return f"음식점인데 메뉴가 카페 메뉴({menu[:30]})"
    return None


def sample(n: int | None, seed: int = 0) -> list[dict]:
    """카페·음식점을 반씩(카페가 모자라면 음식점으로) 무작위로 뽑는다. n=None이면 전부."""
    cats = [config()["classify"]["cafe"]["category"], config()["classify"]["food"]["category"]]
    with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT place_id, name, category, description FROM place WHERE is_active AND category = ANY(%s)",
                    (cats,))
        rows = cur.fetchall()
    if n is None:
        return rows
    rnd = random.Random(seed)
    cafes = [r for r in rows if r["category"] == cats[0]]
    foods = [r for r in rows if r["category"] == cats[1]]
    picked = rnd.sample(cafes, min(len(cafes), n // 2))
    return picked + rnd.sample(foods, n - len(picked))


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "20"
    rows = sample(None if arg == "all" else int(arg))
    bad = 0
    print(f"{'id':>6} | {'분류':<4} | {'이름':<16} | 판정")
    for p in rows:
        why = contradiction(p)
        bad += bool(why)
        if why or arg != "all":
            print(f"{p['place_id']:>6} | {p['category']:<4} | {p['name'][:16]:<16} | {why or '일치'}")
    print(f"점검 {len(rows)}곳 · 모순 {bad}곳")
    sys.exit(1 if bad else 0)
