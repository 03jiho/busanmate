from datetime import date

import pytest

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


def test_to_festival():
    f = to_festival({**ROW, "USAGE_DAY_WEEK_AND_TIME": "2026. 10. 16. ~ 10. 18."}, "busan_festival")
    assert (f["start_date"], f["end_date"]) == (date(2026, 10, 16), date(2026, 10, 18))
    assert to_festival({**ROW, "USAGE_DAY_WEEK_AND_TIME": "", "USAGE_DAY": ""}, "busan_festival") is None
