from datetime import date

import pytest

from pipeline import busan
from pipeline.busan import classify, clean_name, parse_period, to_festival, to_place

ROW = {"UC_SEQ": 1, "MAIN_TITLE": "국립해양박물관(한,영,중간,중번,일)", "GUGUN_NM": "영도구",
       "LAT": 35.07, "LNG": 129.08, "ADDR1": "부산광역시 영도구 해양로 301", "TITLE": "바다 박물관",
       "ITEMCNTNTS": "설명", "USAGE_DAY_WEEK_AND_TIME": "09:00~18:00", "HLDY_INFO": "월요일",
       "MAIN_IMG_NORMAL": "https://example.org/a.jpg", "CNTCT_TEL": "", "HOMEPAGE_URL": ""}


def test_clean_name():
    assert clean_name("부산바다축제(한,영, 중간,중번,일)") == "부산바다축제"
    assert clean_name("흰여울문화마을") == "흰여울문화마을"


@pytest.mark.parametrize("name, expected", [
    ("광안리해수욕장", ("해변", False)), ("해운대", ("해변", False)), ("부산시립미술관", ("문화시설", True)),
    ("태종대", ("공원·자연", False)), ("롯데월드 어드벤처 부산", ("관광지", None)), ("해동용궁사", ("사찰·종교", False)),
    ("감천문화마을", ("거리·마을", False)), ("충무동 해안시장", ("시장", None)),
])
def test_classify(name, expected):
    assert classify(name) == expected


@pytest.mark.parametrize("text, expected", [
    ("2025. 8. 1. ~ 8. 3.", (date(2025, 8, 1), date(2025, 8, 3))),
    ("2025. 9. 26.(금) ~ 2025. 9. 28.(일)", (date(2025, 9, 26), date(2025, 9, 28))),
    ("2025년 11월 29일 ~ 2026년 1월 18일 / 점등시간 : 18:00~23:00", (date(2025, 11, 29), date(2026, 1, 18))),
    ("2026.04.17.~04.19.", (date(2026, 4, 17), date(2026, 4, 19))),
    ("2025.2.12", (date(2025, 2, 12), date(2025, 2, 12))),
    ("2025.07. ~ 2025.10.", None), ("매년 11월", None), ("", None),
])
def test_parse_period(text, expected):
    assert parse_period(text) == expected


def test_to_place():
    p = to_place(ROW, "busan_attraction")
    assert (p["name"], p["category"], p["indoor"], p["source_id"]) == ("국립해양박물관", "문화시설", True, "1")
    assert p["open_hours"] == {"raw": "09:00~18:00", "holiday": "월요일"}
    assert p["tel"] is None and p["license"]
    assert to_place({**ROW, "LAT": ""}, "busan_attraction") is None  # 좌표 없으면 제외
    assert to_place(ROW, "busan_food")["category"] == "음식점"


@pytest.mark.parametrize("missing", ["MAIN_TITLE", "LAT", "LNG", "GUGUN_NM"])
def test_clean_drops_rows_missing_required(missing):
    from pipeline.run import clean
    assert clean([ROW, {**ROW, "UC_SEQ": 2, missing: ""}], "busan_attraction") == [to_place(ROW, "busan_attraction")]


def test_dedup_keeps_last_per_source_id():
    from pipeline.run import dedup
    a, b = to_place(ROW, "busan_attraction"), to_place({**ROW, "TITLE": "새 설명"}, "busan_attraction")
    assert dedup([a, b]) == [b]


def test_seed_csv_is_usable():
    from pipeline.run import clean, dedup
    places = [p for s in ("busan_attraction", "busan_food") for p in dedup(clean(busan.load_seed(s), s))]
    fests = clean(busan.load_seed("busan_festival"), "busan_festival")
    assert 30 <= len(places) <= 50 and len(fests) >= 1
    assert all(p["license"] and -90 < p["lat"] < 90 for p in places)


def test_to_festival():
    f = to_festival({**ROW, "USAGE_DAY_WEEK_AND_TIME": "2026. 10. 16. ~ 10. 18."}, "busan_festival")
    assert (f["start_date"], f["end_date"]) == (date(2026, 10, 16), date(2026, 10, 18))
    assert to_festival({**ROW, "USAGE_DAY_WEEK_AND_TIME": "", "USAGE_DAY": ""}, "busan_festival") is None


@pytest.mark.parametrize("name, menu, cat", [("모모스커피", "", "카페"), ("올드머그", "아메리카노, 라떼", "카페"),
                                             ("연화제과", "", "카페"), ("양평칼국수 본점", "칼국수", "음식점")])
def test_food_rows_split_cafe(name, menu, cat):  # 코스 수정 "카페 추가"용: 맛집 데이터 중 카페를 음식점과 구분
    assert to_place({**ROW, "MAIN_TITLE": name, "RPRSNTV_MENU": menu}, "busan_food")["category"] == cat


@pytest.mark.parametrize("name, parts", [
    ("중앙공원, 민주공원", ["중앙공원", "민주공원"]),                   # 장소 둘 → 나눈다
    ("송정해수욕장, 죽도공원", ["송정해수욕장", "죽도공원"]),
    ("철새와 함께하는 아름다운 문화의 향연, 을숙도", ["을숙도"]),        # 홍보 문구는 뗀다
    ("40계단, 문화관", ["40계단, 문화관"]),                            # 한 시설 → 그대로
    ("남포, 광복지하도상가", ["남포, 광복지하도상가"]),
])
def test_split_title(name, parts):
    from pipeline.busan import split_title
    assert split_title(name) == parts


def test_to_places_splits_with_suffixed_ids():
    from pipeline.busan import to_places
    out = to_places({**ROW, "MAIN_TITLE": "송정해수욕장, 죽도공원"}, "busan_attraction")
    assert [(p["name"], p["category"], p["source_id"]) for p in out] == \
        [("송정해수욕장", "해변", "1"), ("죽도공원", "공원·자연", "1-2")]
    assert out[1]["image_url"] is None and out[0]["lat"] == out[1]["lat"]


def test_cafe_name_with_meal_menu_is_restaurant():
    """이름에 '브레드'가 있어도 대표 메뉴가 식사(스테이크)면 음식점 (본 앤 브레드 오분류)."""
    row = {"MAIN_TITLE": "본 앤 브레드", "LAT": "35.16", "LNG": "129.16", "GUGUN_NM": "해운대구", "UC_SEQ": 1,
           "RPRSNTV_MENU": "스테이크하우스"}
    assert to_place(row, "busan_food")["category"] == "음식점"
    assert to_place({**row, "MAIN_TITLE": "브레드 공방", "RPRSNTV_MENU": "케이크, 커피"}, "busan_food")["category"] == "카페"


def test_category_check_flags_cafe_with_meal_description():
    from eval.category_check import contradiction
    steak = {"name": "본 앤 브레드", "category": "카페", "description": "본 앤 브레드\n대표 메뉴: 스테이크하우스\n최고급 한우를 숙성한다."}
    assert "카페인데" in contradiction(steak)
    assert contradiction({**steak, "category": "음식점"}) is None
    assert contradiction({"name": "빵집", "category": "카페", "description": "빵집\n대표 메뉴: 커피, 케이크"}) is None
