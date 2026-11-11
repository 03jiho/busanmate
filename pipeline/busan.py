"""부산 명소·맛집·축제 API 수집과 DB 행 변환 (COMP-12, DATA-01·03)."""
import csv
import re
from datetime import date
from pathlib import Path

import requests

from settings import config, secret

_LANG_SUFFIX = re.compile(r"\s*\((?:[한영일,\s]|중간|중번)+\)\s*$")  # "(한,영,중간,중번,일)"
_DATE = re.compile(r"(?<!\d)(?:(\d{4})\s*[.년]\s*)?(\d{1,2})\s*[.월]\s*(\d{1,2})(?!\d)")


def fetch(source: str) -> list[dict]:
    """소스 전체를 페이지 단위로 가져온다. 실패하면 requests 예외를 그대로 올린다(호출부에서 키 없이 기록)."""
    src, ing = config()["sources"][source], config()["ingest"]
    url = secret(src["url_env"]).rstrip("/") + "/" + src["operation"]
    items, page = [], 1
    while True:
        r = requests.get(url, timeout=ing["timeout_s"], params={
            "serviceKey": secret("DATA_GO_KR_SERVICE_KEY"), "pageNo": page,
            "numOfRows": ing["page_size"], "resultType": "json"})
        r.raise_for_status()
        body = r.json()[src["operation"]]
        if body["header"]["code"] != "00":
            raise RuntimeError(f"{source}: API 오류 코드 {body['header']['code']}")
        items += body.get("item", [])
        if len(items) >= body["totalCount"] or not body.get("item"):
            return items
        page += 1


def load_seed(source: str) -> list[dict]:
    """API 장애·오프라인 시연용 시드 CSV(data/seed/). API 원본과 같은 필드 이름이고 source 열로 소스를 나눈다."""
    path = Path(__file__).resolve().parent.parent / config()["ingest"]["seed_file"]
    with open(path, encoding="utf-8", newline="") as f:
        return [{k: v for k, v in row.items() if k != "source"} for row in csv.DictReader(f) if row["source"] == source]


def clean_name(name: str) -> str:
    return _LANG_SUFFIX.sub("", name).strip()


def classify(name: str) -> tuple[str, bool | None]:
    cfg = config()["classify"]
    base = name.removesuffix("부산").strip()  # "턴투워드부산"이 suffix '산'에 걸리지 않게
    for rule in cfg["rules"]:
        if (name in rule.get("names", []) or any(k in name for k in rule.get("keywords", []))
                or base.endswith(tuple(rule.get("suffixes", [])))):
            return rule["category"], rule["indoor"]
    return cfg["default"]["category"], cfg["default"]["indoor"]


def _text(*parts) -> str:
    return "\n".join(p.strip() for p in parts if p and p.strip())


def to_place(row: dict, source: str) -> dict | None:
    """place 행. 좌표·이름·구가 없으면 None(적재 제외)."""
    name = clean_name(row.get("MAIN_TITLE", ""))
    if not (name and row.get("LAT") and row.get("LNG") and row.get("GUGUN_NM")):
        return None
    menu = row.get("RPRSNTV_MENU", "")
    if source == "busan_food":
        cafe = config()["classify"]["cafe"]
        is_cafe = any(k in name.lower() for k in cafe["keywords"]) or any(k in menu for k in cafe["menu_keywords"])
        kind = cafe if is_cafe else config()["classify"]["food"]
        category, indoor = kind["category"], kind["indoor"]
    else:
        category, indoor = classify(name)
    hours = {k: v.strip() for k, v in (("raw", row.get("USAGE_DAY_WEEK_AND_TIME", "")),
                                         ("holiday", row.get("HLDY_INFO", ""))) if v and v.strip()}
    return {
        "source": source, "source_id": str(row["UC_SEQ"]), "name": name,
        "category": category, "sub_category": None,
        "gu": row["GUGUN_NM"].strip(), "address": _text(row.get("ADDR1"), row.get("ADDR2")) or row["GUGUN_NM"],
        "lat": float(row["LAT"]), "lng": float(row["LNG"]),
        "description": _text(row.get("TITLE"), row.get("SUBTITLE"), menu and f"대표 메뉴: {menu}",
                             row.get("ITEMCNTNTS")) or None,
        "open_hours": hours or None, "indoor": indoor,
        "tel": (row.get("CNTCT_TEL") or "").strip() or None,
        "url": (row.get("HOMEPAGE_URL") or "").strip() or None,
        "license": config()["sources"][source]["license"],
        "image_url": (row.get("MAIN_IMG_NORMAL") or "").strip() or None,
    }


def split_title(name: str) -> list[str]:
    """원본 제목의 쉼표 처리. 세 어절 이상인 쪽은 홍보 문구로 보고 뗀다("철새와 함께하는 …, 을숙도" → [을숙도]).
    남은 이름이 둘 이상이고 모두 분류 규칙에 걸리면 장소 여럿으로 나눈다("중앙공원, 민주공원" → 둘).
    하나라도 규칙에 안 걸리면 한 시설로 보고 그대로 둔다("40계단, 문화관", "남포, 광복지하도상가")."""
    parts = [p.strip() for p in name.split(",") if p.strip()]
    if len(parts) < 2:
        return [name]
    places = [p for p in parts if len(p.split()) < 3]
    if len(places) == 1:
        return places
    default = config()["classify"]["default"]["category"]
    if len(places) >= 2 and all(classify(p)[0] != default for p in places):
        return places
    return [name]


def to_places(row: dict, source: str) -> list[dict]:
    """to_place + 합쳐진 이름 나누기(명소만). 나눈 둘째부터는 원본 id에 '-2', '-3'을 붙이고, 대표 이미지는 첫째만 쓴다.
    좌표·설명·출처는 원본 행 그대로이므로 플래너는 같은 좌표의 장소를 한 코스에 두 번 넣지 않는다."""
    p = to_place(row, source)
    if not p:
        return []
    names = [p["name"]] if source == "busan_food" else split_title(p["name"])
    if names == [p["name"]]:
        return [p]
    out = []
    for j, n in enumerate(names):
        category, indoor = classify(n)
        out.append({**p, "name": n, "category": category, "indoor": indoor,
                    "source_id": p["source_id"] if j == 0 else f"{p['source_id']}-{j + 1}",
                    "image_url": p["image_url"] if j == 0 else None})
    return out


def parse_period(text: str) -> tuple[date, date] | None:
    """'2025. 9. 26.(금) ~ 2025. 9. 28.(일)' 같은 자유 형식에서 (시작, 종료). 연도가 하나도 없으면 None."""
    dates, year = [], None
    for y, m, d in _DATE.findall(text or ""):
        year = int(y) if y else year
        if year is None:
            continue
        try:
            dates.append(date(year, int(m), int(d)))
        except ValueError:
            continue
    # ponytail: 시작~종료를 min/max로 잡는다. 종료 연도 없이 해를 넘기는 표기("12.20 ~ 1.5")는 틀어짐, 나오면 처리 추가
    return (min(dates), max(dates)) if dates else None


def to_festival(row: dict, source: str) -> dict | None:
    """festival 행. 기간을 읽을 수 없으면 None(적재 제외)."""
    period = parse_period(row.get("USAGE_DAY_WEEK_AND_TIME", "")) or parse_period(row.get("USAGE_DAY", ""))
    if not period:
        return None
    return {
        "source_id": str(row["UC_SEQ"]), "name": clean_name(row.get("MAIN_TITLE", "")),
        "description": _text(row.get("TITLE"), row.get("SUBTITLE"), row.get("MAIN_PLACE"),
                             row.get("ITEMCNTNTS")) or None,
        "start_date": period[0], "end_date": period[1],
        "lat": row.get("LAT") or None, "lng": row.get("LNG") or None,
        "address": _text(row.get("ADDR1"), row.get("ADDR2")) or None,
        "url": (row.get("HOMEPAGE_URL") or "").strip() or None,
        "license": config()["sources"][source]["license"],
    }
