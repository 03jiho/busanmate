"""에이전트 흐름 (FR-011, FR-012, AI-REQ-01·02·04): 가짜 LLM과 합성 후보로 외부 호출 없이 검증한다."""
import uuid
from datetime import date, datetime

import pytest

from agent import orchestrator, query_parser, response, session
from agent.response import verify_places
from services.llm import FALLBACK_NOTICE, LLMReply, ToolCall
from tools import map as map_tool
from tools import places as places_tool
from tools import weather
from tools.base import REGISTRY, ToolResult

TODAY = date(2026, 11, 4)
CANDS = [{"place_id": i, "source": "busan_attraction", "name": f"장소{i}", "category": c, "gu": "중구",
          "lat": 35.10 + i * 0.002, "lng": 129.03 + i * 0.002, "indoor": c == "문화시설", "sim": 0.9 - i * 0.01,
          "description": f"설명{i}", "open_hours": None, "url": None, "license": "공공데이터"}
         for i, c in enumerate(["해변", "문화시설", "음식점", "공원·자연"] * 3)]
# 이름으로 콕 집어 말한 장소 처리용: 검색 결과(CANDS)에 없는 실제 장소 1곳과 비슷한 이름 후보
YONG = {**CANDS[3], "place_id": 100, "name": "용두산공원", "lat": 35.1007, "lng": 129.0324}
NAMES = [{"place_id": 200 + i, "name": n, "gu": g, "category": c} for i, (n, g, c) in enumerate(
    [("해운대", "해운대구", "해변"), ("오션블루 가덕휴게소", "강서구", "관광지"), ("대청스카이전망대", "중구", "공원·자연"),
     ("해운대 블루라인파크", "해운대구", "관광지"), ("스시 타카이", "해운대구", "음식점")])]


class FakeLLM:
    def __init__(self, parsed, plan=None, fail=False):
        self.parsed, self.plan, self.fail, self.prompts = parsed, list(plan or []), fail, []

    def json(self, system, prompt, schema, deadline=None):
        if self.fail:
            raise RuntimeError("down")
        self.prompts.append(prompt)
        if "intent" in schema["properties"]:
            return self.parsed
        if "steps" in schema["properties"]:  # 도구 선택(구조화 출력): 계획 한 번
            calls = self.plan.pop(0) if self.plan else []
            return {"steps": [{"name": n, **a} for n, a in calls]}
        return {"answer": "먼저 [[0]]에 갑니다. 이어서 [[999]]도 들릅니다. 마지막은 [[1]]입니다.",
                "reasons": [{"place_id": 0, "reason": "바다가 보여요"}, {"place_id": 999, "reason": "가짜"}]}

    def chat(self, system, messages, tools=None, deadline=None):
        if self.fail:
            raise RuntimeError("down")
        calls = self.plan.pop(0) if self.plan else []
        return LLMReply(text="" if calls else "완료", tool_calls=[ToolCall(n, a) for n, a in calls])


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(places_tool, "search", lambda cond, n=None: ([dict(c) for c in CANDS], []))
    grids = []
    monkeypatch.setattr(weather, "forecast", lambda nx, ny: grids.append((nx, ny)) or {date(2026, 11, 7): 80})
    monkeypatch.setattr(weather, "_grids_seen", grids, raising=False)
    monkeypatch.setattr(map_tool, "kakao_leg", lambda a, b: {"mode": "taxi", "minutes": 9, "meters": 3000,
                                                             "estimated": False, "taxi_fare": 5000})
    monkeypatch.setattr(REGISTRY["get_festivals"], "run", lambda args, ctx: ToolResult(ok=True, updates={"festivals": []}))
    monkeypatch.setattr(response, "fetch_places", lambda ids: {c["place_id"]: c for c in CANDS + [YONG] + CAFES + INDOOR if c["place_id"] in ids})
    monkeypatch.setattr(places_tool, "fetch_places", lambda ids: {c["place_id"]: c for c in CANDS + [YONG] + CAFES + INDOOR if c["place_id"] in ids})
    monkeypatch.setattr(places_tool, "places_where", lambda category=None, indoor=None: [])  # 실제 DB를 읽지 않게
    monkeypatch.setattr(query_parser, "all_place_names", lambda: [{"place_id": c["place_id"], "name": c["name"],
                                                                   "gu": c["gu"], "category": c["category"]}
                                                                  for c in CANDS + [YONG] + NAMES])

    def use(llm):
        for m in (query_parser, response, orchestrator):
            monkeypatch.setattr(m, "get_llm", lambda: llm)
        return llm
    return use


def chat(sid, msg):
    return orchestrator.handle_chat(sid, msg, today=TODAY)


def test_verify_places_drops_unknown_and_names_known():
    text, dropped = verify_places("먼저 [[1]]에 가요. 그다음 [[9]]도 좋아요.\n마지막은 [[2]].", {1: "A", 2: "B"})
    assert text == "먼저 A에 가요.\n마지막은 B." and dropped == [9]


@pytest.mark.parametrize("text, expected", [
    ("[[1]]과 [[2]]를 걷고", "해운대와 동백섬을 걷고"),
    ("[[2]]로 갔다가 [[3]]으로 간다", "동백섬으로 갔다가 초량 이바구길로 간다"),
    ("[[4]]은 [[1]]이 좋다", "부산타워는 해운대가 좋다"),
    ("[[5]]를 보고 [[6]]가 끝", "장림포구 (부네치아)를 보고 F1963이 끝"),
    ("[[1]]이다. [[1]]예요.", "해운대이다. 해운대예요."),
    ("[[2]]에서 쉰다", "동백섬에서 쉰다"),
])
def test_marker_josa(text, expected):
    names = {1: "해운대", 2: "동백섬", 3: "초량 이바구길", 4: "부산타워", 5: "장림포구 (부네치아)", 6: "F1963"}
    assert verify_places(text, names)[0] == expected


def test_weather_helpers():
    items = [{"category": "POP", "fcstDate": "20261107", "fcstTime": t, "fcstValue": v}
             for t, v in [("0600", "90"), ("1200", "60"), ("1800", "30")]]
    assert weather.daily_pop(items) == {date(2026, 11, 7): 60}  # 9~21시만
    assert weather.base_time(datetime(2026, 11, 4, 1, 0)) == ("20261103", "2300")
    assert weather.base_time(datetime(2026, 11, 4, 14, 20)) == ("20261104", "1400")


@pytest.mark.parametrize("lat, lng, grid", [(35.1798, 129.0750, (98, 76)), (37.5665, 126.9780, (60, 127)),
                                            (33.4996, 126.5312, (53, 38))])
def test_latlon_to_grid_matches_kma(lat, lng, grid):  # 부산시청·서울시청·제주시청의 기상청 공식 격자
    assert weather.latlon_to_grid(lat, lng) == grid


def test_build_course_before_search_is_rejected():
    res = REGISTRY["build_course"].call({}, {"conditions": None})
    assert not res.ok and "search_places" in res.data["error"]


def test_course_flow(fake):
    sid = str(uuid.uuid4())
    llm = fake(FakeLLM({"intent": "course", "query": "바다 산책", "gu": ["중구"], "start_date": "2026-11-07", "days": 1},
                       plan=[[("search_places", {}), ("get_weather", {}), ("get_festivals", {}), ("build_course", {})],
                             [("get_route_times", {})]]))
    r = chat(sid, "이번 토요일 바다 코스")
    assert r["tool_calls"] == ["search_places", "get_weather", "get_festivals", "build_course", "get_route_times"]
    items = r["course"]["days"][0]["items"]
    assert 2 <= len(items) <= 6 and all(i["name"].startswith("장소") for i in items)
    assert "[[" not in r["answer"] and "999" not in r["answer"] and "장소0" in r["answer"]  # 환각 문장 제거
    assert any(s["label"].startswith("기상청") for s in r["sources"])                       # 기상청 출처 표시
    assert {s["place_id"] for s in r["sources"] if "place_id" in s} == {i["place_id"] for i in items}
    assert r["course"]["days"][0]["rain_prob"] == 80 and session.get(sid)["last_course"]
    assert all(c is not None for c in llm.prompts)
    lat, lng = sum(c["lat"] for c in CANDS) / len(CANDS), sum(c["lng"] for c in CANDS) / len(CANDS)
    assert weather._grids_seen == [weather.latlon_to_grid(lat, lng)]  # 후보 중심 좌표의 격자로 예보 조회


@pytest.mark.parametrize("parsed, expected", [
    ({}, query_parser.ASK_BOTH),                                          # 지역·날짜 둘 다 없음
    ({"gu": ["수영구"]}, query_parser.ASK_DATE),                            # 날짜만 없음
    ({"start_date": "2026-11-07"}, query_parser.ASK_REGION),              # 지역만 없음
    ({"start_date": "2026-11-07", "region_any": True}, None),             # "부산 전체"면 묻지 않는다
])
def test_course_asks_for_missing_region_or_date(fake, parsed, expected):
    sid = str(uuid.uuid4())
    fake(FakeLLM({"intent": "course", "query": "바다", **parsed}, plan=[[("search_places", {})]]))
    r = chat(sid, "바다 코스 추천해줘")
    if expected:
        assert r["answer"] == expected and r["course"] is None
        assert session.get(sid)["pending"] == "바다 코스 추천해줘"  # 다음 답과 이어 붙여 다시 해석한다
    else:
        assert r["tool_calls"] and session.get(sid)["pending"] is None


def test_tool_call_limit(fake):
    sid = str(uuid.uuid4())
    fake(FakeLLM({"intent": "course", "query": "바다", "gu": ["중구"], "start_date": "2026-11-07"},
                 plan=[[("search_places", {})] * 20]))
    assert len(chat(sid, "토요일 바다 코스")["tool_calls"]) == 6  # 계획이 길어도 6회에서 멈춘다


def test_search_stops_after_search_places(fake):
    sid = str(uuid.uuid4())
    fake(FakeLLM({"intent": "search", "query": "바다"}, plan=[[("search_places", {})]] * 20))
    r = chat(sid, "바다 보이는 곳")
    assert r["tool_calls"] == ["search_places"] and len(r["places"]) == 5


def test_llm_down_uses_rules_and_default_plan(fake):
    sid = str(uuid.uuid4())
    fake(FakeLLM({}, fail=True))
    r = chat(sid, "해운대 바다 1박 여행 코스")
    assert r["conditions"]["gu"] == ["해운대구"] and r["conditions"]["days"] == 2
    assert r["tool_calls"][0] == "search_places" and "build_course" in r["tool_calls"]
    assert r["course"]["days"] and r["notices"].count(FALLBACK_NOTICE) == 1  # 내부 용어 없이 한 줄만
    assert not any("AI" in n for n in r["notices"])
    items = r["course"]["days"][0]["items"]
    assert all(i["reason"] for i in items)  # LLM이 없어도 추천 이유는 DB 소개 문구로 채운다 (AC-002)

    before = [i["place_id"] for i in items]  # LLM이 없어도 "두 번째 장소 빼줘"가 동작한다
    after = [i["place_id"] for i in chat(sid, "두 번째 장소 빼줘")["course"]["days"][0]["items"]]  # 1일차 기준
    assert sorted(after) == sorted(before[:1] + before[2:])


# --- 이름으로 콕 집어 말한 장소 (없는 장소 안내, 있는 장소는 꼭 포함) ---
def test_match_names_threshold_and_region_prefix():
    from services.search import match_names
    rows = [{"place_id": c["place_id"], "name": c["name"], "gu": c["gu"]} for c in [YONG] + NAMES]
    found, missing = match_names(["용두산공원", "용두산", "해운대 블루오션 스카이타워"], rows, ["해운대구"],
                                 query_parser.REGION_WORDS)
    assert found["용두산공원"]["place_id"] == 100 and found["용두산"]["place_id"] == 100  # 앞부분만 말해도 같은 장소
    assert "해운대 블루오션 스카이타워" in missing                                        # 장소 "해운대"로 붙지 않는다
    assert "해운대" not in missing["해운대 블루오션 스카이타워"]  # 지역 단어를 뗀 이름으로 비슷한 곳을 찾는다
    found, missing = match_names(["해운대해수욕장", "광안리 해수욕장", "부산 아쿠아랜드"],
                                 rows + [{"place_id": 300, "name": "임랑해수욕장", "gu": "기장군"},
                                         {"place_id": 301, "name": "광안리해수욕장", "gu": "수영구"}],
                                 [], query_parser.REGION_WORDS)
    assert found["해운대해수욕장"]["name"] == "해운대"                    # DB 이름 + 일반 접미어 (임랑해수욕장 아님)
    assert found["광안리 해수욕장"]["place_id"] == 301
    assert missing["부산 아쿠아랜드"] == []                               # 비슷한 곳이 없으면 억지로 안내하지 않는다


def test_unknown_place_notice_on_top(fake):
    sid = str(uuid.uuid4())
    fake(FakeLLM({"intent": "course", "query": "바다", "gu": ["해운대구"], "start_date": "2026-11-07",
                  "places": ["해운대 블루오션 스카이타워"]},
                 plan=[[("search_places", {}), ("build_course", {})], [("get_route_times", {})]]))
    r = chat(sid, "이번 토요일 해운대 블루오션 스카이타워 꼭 넣어줘")
    first = r["notices"][0]
    assert first.startswith("'해운대 블루오션 스카이타워'는 공공데이터에서 찾지 못해 코스에서 제외했어요. 비슷한 장소: ")
    assert r["course"]["days"]  # 코스는 나머지 조건으로 계속 만든다


def test_known_place_is_always_in_course(fake):
    sid = str(uuid.uuid4())
    fake(FakeLLM({"intent": "course", "query": "산책", "start_date": "2026-11-07", "places": ["용두산공원"]},
                 plan=[[("search_places", {}), ("build_course", {})], [("get_route_times", {})]]))
    r = chat(sid, "이번 토요일 용두산공원 꼭 넣어줘")
    assert r["conditions"]["gu"] == ["중구"]  # 지역을 안 말해도 그 장소의 구로 짠다(되묻지 않음)
    assert 100 in {i["place_id"] for i in r["course"]["days"][0]["items"]}
    assert not any("찾지 못해" in n for n in r["notices"])


def test_llm_down_known_place_and_rule_summary(fake):
    """LLM이 모두 실패해도 메시지 속 실제 장소명은 코스에 넣고, 답변은 대체 문구 대신 규칙 기반 요약."""
    sid = str(uuid.uuid4())
    fake(FakeLLM({}, fail=True))
    r = chat(sid, "중구 바다 코스 용두산공원 꼭 넣어줘")
    names = [i["name"] for i in r["course"]["days"][0]["items"]]
    assert "용두산공원" in names and not any("찾지 못해" in n for n in r["notices"])
    assert "요청하신 조건으로" not in r["answer"] and " → ".join(names) in r["answer"]


@pytest.mark.parametrize("msg, names", [
    ("이번 토요일 해운대 블루오션 스카이타워 꼭 넣어줘", ["블루오션 스카이타워"]),
    ("이번 토요일 해운대 코스, 동백섬이랑 부산 아쿠아랜드 포함", ["동백섬", "부산 아쿠아랜드"]),
    ("중구 바다 코스 용두산공원 꼭 넣어줘", ["용두산공원"]),
    ("해운대 바다 코스", []),
])
def test_rule_named_phrases(msg, names):  # LLM 실패 시 장소명 추출
    assert query_parser._named_phrases(msg) == names


def test_llm_down_mixed_known_and_unknown_places(fake):
    """있는 장소와 없는 장소를 함께 말하면: 있는 곳은 코스에, 없는 곳은 안내 (한 덩어리로 묶여 안내 없이 사라지던 문제)."""
    sid = str(uuid.uuid4())
    fake(FakeLLM({}, fail=True))
    r = chat(sid, "중구 코스, 용두산공원이랑 부산 아쿠아랜드 포함")
    assert r["notices"][0].startswith("'부산 아쿠아랜드'는 공공데이터에서 찾지 못해")
    assert "용두산공원" in [i["name"] for i in r["course"]["days"][0]["items"]]


# --- 코스 제자리 수정 ("코스 고치기" 버튼: edit.kind) ---
CAFES = [{**CANDS[2], "place_id": 400 + i, "name": f"카페{i}", "category": "카페", "indoor": True,
          "lat": 35.105 + i * 0.01, "lng": 129.035} for i in range(3)]
INDOOR = [{**CANDS[1], "place_id": 500 + n, "name": f"실내전시관{n}", "lat": 35.111 + n * 0.003, "lng": 129.041}
          for n in range(3)]


@pytest.fixture
def two_days(fake, monkeypatch):
    monkeypatch.setattr(places_tool, "places_where",
                        lambda category=None, indoor=None: CAFES if category == "카페" else INDOOR[:2] + CAFES)
    sid = str(uuid.uuid4())
    fake(FakeLLM({"intent": "course", "query": "역사", "gu": ["중구"], "start_date": "2026-11-07", "days": 2},
                 plan=[[("search_places", {}), ("get_weather", {}), ("get_festivals", {}), ("build_course", {})],
                       [("get_route_times", {})]]))
    first = chat(sid, "다음 주 토요일부터 1박 2일 중구 역사 여행")
    assert len(first["course"]["days"]) == 2
    return sid, first


def _ids(r):
    return [[i["place_id"] for i in d["items"]] for d in r["course"]["days"]]


def test_cafe_edit_keeps_two_days_and_places(two_days):
    """1박 2일에서 "카페 추가": 2일 구조와 기존 장소·순서는 그대로, 한 날에 카페 하나만 점심 무렵에 들어간다."""
    sid, first = two_days
    r = orchestrator.handle_chat(sid, "카페 추가", today=TODAY, edit={"kind": "cafe"})
    before, after = _ids(first), _ids(r)
    assert len(after) == 2
    changed = [d for d in range(2) if before[d] != after[d]]
    assert len(changed) == 1
    d = changed[0]
    added = [i for i in after[d] if i not in before[d]]
    assert len(added) == 1 and added[0] in {c["place_id"] for c in CAFES}
    assert [i for i in after[d] if i != added[0]] == before[d]          # 기존 장소 순서 유지
    from services.planner import lunch_slot
    day = first["course"]["days"][d]
    assert after[d].index(added[0]) == lunch_slot(day["items"], day.get("start"))  # 점심 무렵 자리
    assert after[1 - d] == before[1 - d]                                # 다른 날은 그대로
    assert r["conditions"] == first["conditions"] and r["dates"] == first["dates"]  # 조건 칩·일차 탭 그대로
    assert any("카페" in n or "넣었어요" in n for n in r["notices"])


def test_indoor_edit_replaces_one_outdoor_in_place(two_days, monkeypatch):
    sid, first = two_days
    monkeypatch.setattr(places_tool, "places_where", lambda category=None, indoor=None: INDOOR)  # 502는 코스에 없음
    r = orchestrator.handle_chat(sid, "실내로 바꾸기", today=TODAY, edit={"kind": "indoor"})
    before, after = _ids(first), _ids(r)
    diffs = [(d, k) for d in range(2) for k, (a, b) in enumerate(zip(before[d], after[d])) if a != b]
    assert len(diffs) == 1 and [len(x) for x in after] == [len(x) for x in before]  # 한 자리만, 같은 위치에서
    d, k = diffs[0]
    assert after[d][k] in {500, 501, 502} and after[d][k] not in sum(before, [])  # 코스에 없던 실내 장소로
    assert not next(c for c in CANDS + INDOOR if c["place_id"] == before[d][k])["indoor"]


def test_shorter_edit_scales_stay_then_removes(two_days):
    """'더 짧게': 먼저 그날 장소별 체류 시간을 비율로 줄이고(장소·순서 그대로), 모두 하한이면 한 곳을 뺀다."""
    sid, first = two_days
    r = orchestrator.handle_chat(sid, "더 짧게", today=TODAY, edit={"kind": "shorter"})
    assert _ids(r) == _ids(first) and any("체류 시간" in n for n in r["notices"])
    stays = lambda res: [i["stay_min"] for d in res["course"]["days"] for i in d["items"]]  # noqa: E731
    assert sum(stays(r)) < sum(stays(first))
    for _ in range(8):  # 하한(20분)까지 줄인 뒤에는 한 곳을 뺀다
        r = orchestrator.handle_chat(sid, "더 짧게", today=TODAY, edit={"kind": "shorter"})
    before, after = _ids(first), _ids(r)
    assert sum(map(len, before)) - sum(map(len, after)) >= 1
    for b, a in zip(before, after):
        assert [i for i in b if i in a] == a  # 남은 장소 순서 그대로


def test_edit_button_without_course_asks_first(fake):
    fake(FakeLLM({}))
    r = orchestrator.handle_chat(str(uuid.uuid4()), "카페 추가", today=TODAY, edit={"kind": "cafe"})
    assert "먼저 코스를" in r["answer"] and r["course"] is None


def test_empty_reason_card_gets_category_address_line(fake):
    """LLM이 추천 이유를 빠뜨린 카드는 분류·주소로 만든 한 줄로 채운다 (빈 카드 없음)."""
    sid = str(uuid.uuid4())
    fake(FakeLLM({"intent": "course", "query": "바다", "gu": ["중구"], "start_date": "2026-11-07"},
                 plan=[[("search_places", {}), ("build_course", {})], [("get_route_times", {})]]))
    items = chat(sid, "이번 토요일 중구 코스")["course"]["days"][0]["items"]
    assert all(i["reason"] for i in items)
    assert any(i["reason"] == f"중구에 있는 {i['category']}입니다." for i in items if i["place_id"] != 0)


@pytest.mark.parametrize("msg", ["이번 토요일 중구 비 오는 날 코스", "토요일 중구 우천 시 갈 곳 코스"])
def test_rainy_words_mean_indoor(fake, msg):
    fake(FakeLLM({"intent": "course", "query": "x", "gu": ["중구"], "start_date": "2026-11-07"}))
    assert query_parser.parse(msg, TODAY)["conditions"].indoor is True


@pytest.mark.parametrize("msg, cats", [("이번 토요일 서면 맛집 투어 코스", []), ("이번 토요일 서면 식당만 도는 코스", ["음식점"])])
def test_theme_like_category_is_theme_unless_only(fake, msg, cats):
    """"맛집 투어"를 categories=[음식점]으로 읽어도 테마로만 둔다(식당만 나오지 않게). '식당만'이면 분류 필터."""
    fake(FakeLLM({"intent": "course", "query": "x", "gu": ["부산진구"], "start_date": "2026-11-07", "theme": "맛집",
                  "categories": ["음식점"]}))
    assert query_parser.parse(msg, TODAY)["conditions"].categories == cats


def test_reason_that_only_repeats_name_is_replaced():  # 소개 첫 줄이 제목(=장소명)이라 카드에 "동삼정"만 뜨던 문제
    p = {"name": "동삼정", "category": "음식점", "gu": "중구", "address": "부산광역시 중구 광복로 1\n2층",
         "description": "동삼정\n대표 메뉴: 밀면\n50년 전통의 밀면집으로 담백한 육수가 특징이다."}
    assert response.intro_line(p) == "50년 전통의 밀면집으로 담백한 육수가 특징이다."  # 짧은 메뉴 줄도 건너뛴다
    assert response.intro_line({**p, "description": "동삼정"}) is None
    assert response.fallback_line(p) == "중구 광복로 1에 있는 음식점입니다."


# --- 진행 단계 스트리밍, 범위 밖 질문 ---
def test_stream_stages_in_order_and_same_result(fake):
    """스트리밍 단계는 실제 진행 순서(조건 해석 → 장소 검색 → 날씨 → 동선 계산 → 요약)이고, 결과는 handle_chat과 같은 모양."""
    plan = [[("search_places", {}), ("get_weather", {}), ("get_festivals", {}), ("build_course", {})], [("get_route_times", {})]]
    fake(FakeLLM({"intent": "course", "query": "바다", "gu": ["중구"], "start_date": "2026-11-07"}, plan=plan))
    evs = list(orchestrator.handle_chat_stream(str(uuid.uuid4()), "이번 토요일 중구 코스", today=TODAY))
    stages = [e["stage"] for e in evs if "stage" in e]
    order = list(dict.fromkeys(stages))
    assert order == ["parse", "search", "weather", "route", "respond"]
    assert all(a["t"] <= b["t"] for a, b in zip(evs, evs[1:]) if "t" in a and "t" in b)
    res = evs[-1]["result"]
    assert res["course"]["days"] and res["intent"] == "course" and set(res) >= {"answer", "conditions", "dates", "sources"}


@pytest.mark.parametrize("msg, outside", [("서울 한강 산책 코스 추천해줘", True), ("제주도 맛집 알려줘", True),
                                          ("해운대 대구탕 맛집", False), ("부산 서면 맛집", False)])
def test_outside_busan(fake, msg, outside):
    """부산이 아닌 지역은 범위 밖: 검색하지 않고 "부산 전용" 안내 + 지역만 '부산'으로 바꾼 제안. 대구탕은 지역이 아니다."""
    fake(FakeLLM({"intent": "search", "query": "x"}))
    r = chat(str(uuid.uuid4()), msg)
    assert (r["intent"] == "outside") is outside
    if outside:
        assert r["answer"] == response.OUTSIDE and r["tool_calls"] == [] and "부산" in r["suggest"]


# --- 코스 직접 편집(드래그·빼기·교체), 근거 표시 ---
def _course_session(fake):
    sid = str(uuid.uuid4())
    fake(FakeLLM({"intent": "course", "query": "바다", "gu": ["중구"], "start_date": "2026-11-07", "theme": "바다"},
                 plan=[[("search_places", {}), ("build_course", {})], [("get_route_times", {})]]))
    return sid, chat(sid, "이번 토요일 중구 바다 코스")


def test_response_has_why_sourced_basis_budget(fake):
    _, r = _course_session(fake)
    course = r["course"]
    assert course["sourced"]["count"] == course["sourced"]["total"] == sum(len(d["items"]) for d in course["days"])
    assert all(i["why"] for d in course["days"] for i in d["items"])
    assert any("테마 '바다'" in w for d in course["days"] for i in d["items"] for w in i["why"])  # 해변 분류
    assert "보정계수" in course["estimate_basis"] and course["budget"]["total"] >= 0


def test_drag_reorder_and_remove_recompute_without_chat(fake, monkeypatch):
    from agent import course_ops
    monkeypatch.setattr(course_ops, "fetch_places", lambda ids: {c["place_id"]: c for c in CANDS if c["place_id"] in ids})
    sid, r = _course_session(fake)
    ids = [i["place_id"] for i in r["course"]["days"][0]["items"]]
    rev = course_ops.apply_order(sid, [list(reversed(ids))])        # 드래그: 순서를 뒤집는다
    items = rev["course"]["days"][0]["items"]
    assert [i["place_id"] for i in items] == list(reversed(ids))     # 사용자가 정한 순서 그대로
    assert [i["order"] for i in items] == list(range(1, len(ids) + 1)) and items[-1]["leg_to_next"] is None
    assert session.get(sid)["last_course"]["days"][0]["items"][0]["place_id"] == ids[-1]  # 세션에도 반영
    out = course_ops.apply_order(sid, [list(reversed(ids))[1:]])     # X: 한 곳 빼기
    assert len(out["course"]["days"][0]["items"]) == len(ids) - 1 and all(i["reason"] for i in out["course"]["days"][0]["items"])
    with pytest.raises(course_ops.EditError):
        course_ops.apply_order(sid, [[ids[0], ids[0]]])              # 같은 장소 두 번은 안 된다


def test_alternatives_same_category_not_in_course(fake, monkeypatch):
    from agent import course_ops
    sid, r = _course_session(fake)
    cur = r["course"]["days"][0]["items"][0]
    extra = [{**c, "place_id": 800 + n, "name": f"대안{n}", "lat": c["lat"] + 0.001} for n, c in enumerate(CANDS)]
    monkeypatch.setattr(course_ops, "places_where", lambda category=None, indoor=None:
                        [c for c in CANDS + extra if c["category"] == category])
    alts = course_ops.alternatives(sid, 1, 0)
    used = {i["place_id"] for i in r["course"]["days"][0]["items"]}
    assert 1 <= len(alts) <= 3 and all(a["category"] == cur["category"] and a["place_id"] not in used for a in alts)
    assert alts == sorted(alts, key=lambda a: a["extra_km"])
    dup = {**next(c for c in CANDS if c["place_id"] in used - {cur["place_id"]}), "place_id": 999}  # 다른 코스 장소의 원천 중복
    monkeypatch.setattr(course_ops, "places_where", lambda category=None, indoor=None: [dup])
    assert all(a["place_id"] != 999 for a in course_ops.alternatives(sid, 1, 0))



def test_similar_uses_context_category():
    """비슷한 장소: '스카이타워'면 이름이 비슷한 횟집이 아니라 전망대·관광지가 나오고, 점수가 낮으면 안내하지 않는다."""
    from services.search import match_names
    rows = [{"place_id": c["place_id"], "name": c["name"], "gu": c["gu"], "category": c["category"]} for c in NAMES]
    _, missing = match_names(["블루오션 스카이타워", "이기대"], rows, ["해운대구"], query_parser.REGION_WORDS)
    assert "대청스카이전망대" in missing["블루오션 스카이타워"] and "스시 타카이" not in missing["블루오션 스카이타워"]
    assert missing["이기대"] == []


@pytest.mark.parametrize("text, out", [
    ("짧은 설명", "짧은 설명"),
    ("첫 문장이에요. 두 번째 문장은 꽤 길어서 한도를 넘깁니다. 세 번째.", "첫 문장이에요."),   # 문장 끝에서 잘림: … 없음
    ("띄어쓰기는 있지만 마침표 없이 아주 길게 이어지는 소개 문구라서 중간에서 잘립니다", "띄어쓰기는 있지만 마침표 없이 아주 길게 이어지는…"),
    ("한 문장... 그리고 아주 길게 이어지는 두 번째 문장이 한도를 넘깁니다", "한 문장..."),  # 이미 말줄임표: 하나만
])
def test_clip_by_sentence(text, out):
    assert response.clip(text, 30) == out


def test_umbrella_badge_and_taxi_hint():
    """비 오는 날 불가피하게 넣은 야외 장소엔 우산 배지, 15분 넘는 도보 구간엔 택시 제안 시간."""
    from agent.explain import enrich
    from services.search import Conditions as C
    beach = {"place_id": 1, "name": "해운대", "category": "해변", "gu": "해운대구", "lat": 35.158, "lng": 129.16,
             "indoor": False, "license": "공공데이터", "description": ""}
    museum = {**beach, "place_id": 2, "name": "전시관", "category": "문화시설", "indoor": True}
    course = {"days": [{"day": 1, "rain_prob": 70, "start": "10:00", "items": [
        {"place_id": 1, "category": "해변", "stay_min": 60, "leg_to_next": {"mode": "walk", "minutes": 25, "meters": 1700}},
        {"place_id": 2, "category": "문화시설", "stay_min": 60, "leg_to_next": None}]}]}
    enrich(course, {1: beach, 2: museum}, C(query="x"))
    a, b = course["days"][0]["items"]
    assert a["umbrella"] is True and b["umbrella"] is False
    assert a["leg_to_next"]["taxi_hint_min"] == 4  # 1.7km ÷ 25km/h
    assert a["leg_to_next"]["rain_taxi"] is True  # 비 오는 날 야외 장소에서 나오는 구간: 택시 추천



def test_rainy_day_adds_indoor_attractions_for_swap(fake, monkeypatch):
    """비 오는 날: 검색 후보에 실내 명소가 없어도 같은 구의 실내 명소를 보태 야외 상한(2곳)을 지킨다."""
    sid = str(uuid.uuid4())
    outdoor = [{**c, "place_id": 700 + n, "name": f"해변{n}", "category": "해변", "indoor": False, "sim": 0.95 - n * 0.01}
               for n, c in enumerate(CANDS[:8])]
    museums = [{**CANDS[1], "place_id": 900 + n, "name": f"박물관{n}", "lat": 35.105 + n * 0.002, "indoor": True}
               for n in range(4)]
    monkeypatch.setattr(places_tool, "search", lambda cond, n=None: ([dict(c) for c in outdoor], []))
    monkeypatch.setattr(places_tool, "places_where", lambda category=None, indoor=None: museums if indoor else [])
    monkeypatch.setattr(response, "fetch_places", lambda ids: {c["place_id"]: c for c in outdoor + museums if c["place_id"] in ids})
    fake(FakeLLM({"intent": "course", "query": "바다", "gu": ["중구"], "start_date": "2026-11-07"},
                 plan=[[("search_places", {}), ("get_weather", {}), ("build_course", {})], [("get_route_times", {})]]))
    items = chat(sid, "이번 토요일 중구 바다 코스")["course"]["days"][0]["items"]  # 가짜 예보: 강수 80%
    assert sum(i["category"] == "해변" for i in items) <= 2 and any(i["place_id"] >= 900 for i in items)


def test_parser_steps_skip_separate_tool_call(fake):
    """해석 단계 JSON에 도구 계획(steps)이 있으면 도구 선택 LLM을 따로 부르지 않는다 (왕복 1회 절약)."""
    steps = [{"name": n} for n in ["search_places", "get_weather", "get_festivals", "build_course", "get_route_times"]]
    llm = fake(FakeLLM({"intent": "course", "query": "바다", "gu": ["중구"], "start_date": "2026-11-07", "steps": steps}))
    llm.json_schemas = []
    orig = llm.json
    llm.json = lambda system, prompt, schema, deadline=None: llm.json_schemas.append(list(schema["properties"])) or orig(
        system, prompt, schema, deadline)
    r = chat(str(uuid.uuid4()), "이번 토요일 바다 코스")
    assert r["tool_calls"] == [s["name"] for s in steps] and r["course"]
    assert not any(k == ["steps"] for k in llm.json_schemas)  # 도구 선택 전용 호출 없음


def test_reason_grounding():
    """원문에 없는 사실을 지어낸 추천 이유는 걸러진다 (본 앤 브레드: 카페 분류에 한우 설명 사례)."""
    cafe = {"name": "브레드 공방", "category": "카페", "gu": "중구", "address": "중구 광복로 1",
            "description": "브레드 공방\n대표 메뉴: 커피, 케이크\n갓 구운 빵과 커피를 파는 동네 빵집이다."}
    assert response.grounded("갓 구운 빵과 커피를 즐길 수 있는 곳", cafe)
    assert not response.grounded("최상급 한우를 숙성해 내는 스테이크", cafe)


def test_summary_must_mention_a_condition():
    from services.search import Conditions as C
    cond = C(query="x", theme="바다", gu=["해운대구"])
    course = {"days": [{"day": 1, "rain_prob": 20, "items": [
        {"name": "가", "leg_to_next": {"mode": "walk", "minutes": 5}}, {"name": "나", "leg_to_next": None}]}]}
    ok = response.summary_ok
    assert ok("해운대 바다를 따라 걷는 하루예요.", cond, course)            # 테마 + 동선 (강수 20%라 날씨는 해당 없음)
    assert not ok("해운대 바다가 보이는 곳들이에요.", cond, course)         # 테마 1개뿐
    assert ok("가까운 곳끼리 걸어서 잇는 일정이에요.", C(query="x"), course)  # 해당 항목이 동선 하나뿐이면 1개로 충분
    wet = {"days": [{**course["days"][0], "rain_prob": 70}]}
    assert not ok("바다를 보며 걷는 코스예요.", cond, wet)                  # 비 예보인데 우산·실내 언급 없음
    assert ok("비 예보가 있어 실내 위주로, 바다 근처는 짧게 걸어요.", cond, wet)
    text = response.rule_summary(course, ["가", "나"], None, cond)
    assert "'바다' 테마" in text and "강수확률 20%" in text and "걸어서" in text and ok(text, cond, course)
    text = response.rule_summary(wet, ["가", "나"], None, cond)
    assert "우산" in text and ok(text, cond, wet)


def test_why_lists_concrete_conditions_or_nothing():
    from agent.explain import why
    from services.search import Conditions as C
    p = {"place_id": 1, "name": "해운대", "category": "해변", "gu": "해운대구", "lat": 35.158, "lng": 129.16, "indoor": False}
    got = why(p, C(query="x", theme="바다", gu=["해운대구"]), 10, 600, None, set())
    assert "요청한 지역(해운대구)" in got and any("바다" in w for w in got) and any("야외" in w for w in got)
    museum = {**p, "category": "문화시설", "gu": "중구", "indoor": True}
    assert why(museum, C(query="x"), None, 600, None, set()) == []  # 맞은 조건이 없으면 '왜 이곳?' 생략


@pytest.mark.parametrize("minutes, rain, hint", [(4, 80, False), (7, 80, True), (7, 10, False), (12, 10, True)])
def test_taxi_hint_thresholds(minutes, rain, hint):
    """택시 제안: 도보 10분 이상, 비 오는 날 야외 장소 앞뒤는 5분 넘으면. 5분 이하는 제안 없음. 늘어나는 택시비 합계."""
    from agent.explain import enrich
    from services.search import Conditions as C
    beach = {"place_id": 1, "name": "해운대", "category": "해변", "gu": "해운대구", "lat": 35.158, "lng": 129.16,
             "indoor": False, "license": "x", "description": ""}
    museum = {**beach, "place_id": 2, "name": "전시관", "category": "문화시설", "indoor": True}
    course = {"days": [{"day": 1, "rain_prob": rain, "start": "10:00", "items": [
        {"place_id": 1, "category": "해변", "stay_min": 35,
         "leg_to_next": {"mode": "walk", "minutes": minutes, "meters": minutes * 70}},
        {"place_id": 2, "category": "문화시설", "stay_min": 75, "leg_to_next": None}]}]}
    enrich(course, {1: beach, 2: museum}, C(query="x"))
    assert bool(course["days"][0]["items"][0]["leg_to_next"].get("taxi_hint_min")) is hint
    b = course["budget"]
    assert b["taxi_hint_legs"] == int(hint) and (b["taxi_hint_extra"] > 0) is hint


def test_alternatives_skip_far_detours(fake, monkeypatch):
    """'다른 곳으로 교체' 후보는 우회 거리가 alt_max_detour_km 안인 곳만 (다른 구로 35km 돌아가는 후보 금지)."""
    from agent import course_ops
    sid, r = _course_session(fake)
    cur = r["course"]["days"][0]["items"][1]
    far = {**cur, "place_id": 950, "name": "먼곳", "lat": cur["lat"] + 0.3, "lng": cur["lng"] + 0.3}
    near = {**cur, "place_id": 951, "name": "가까운곳", "lat": cur["lat"] + 0.001}
    monkeypatch.setattr(course_ops, "places_where", lambda category=None, indoor=None: [far, near])
    assert [a["place_id"] for a in course_ops.alternatives(sid, 1, 1)] == [951]
