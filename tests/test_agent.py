"""에이전트 흐름 (FR-011, FR-012, AI-REQ-01·02·04): 가짜 LLM과 합성 후보로 외부 호출 없이 검증한다."""
import uuid
from datetime import date, datetime

import pytest

from agent import orchestrator, query_parser, response, session
from agent.response import verify_places
from services.llm import LLMReply, ToolCall
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
NAMES = [{"place_id": 200 + i, "name": n, "gu": g} for i, (n, g) in enumerate(
    [("해운대", "해운대구"), ("오션블루 가덕휴게소", "강서구"), ("대청스카이전망대", "중구"), ("해운대 블루라인파크", "해운대구")])]


class FakeLLM:
    def __init__(self, parsed, plan=None, fail=False):
        self.parsed, self.plan, self.fail, self.prompts = parsed, list(plan or []), fail, []

    def json(self, system, prompt, schema):
        if self.fail:
            raise RuntimeError("down")
        self.prompts.append(prompt)
        if "intent" in schema["properties"]:
            return self.parsed
        return {"answer": "먼저 [[0]]에 갑니다. 이어서 [[999]]도 들릅니다. 마지막은 [[1]]입니다.",
                "reasons": [{"place_id": 0, "reason": "바다가 보여요"}, {"place_id": 999, "reason": "가짜"}]}

    def chat(self, system, messages, tools=None):
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
    monkeypatch.setattr(query_parser, "all_place_names", lambda: [{"place_id": c["place_id"], "name": c["name"],
                                                                   "gu": c["gu"]} for c in CANDS + [YONG] + NAMES])

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
                 plan=[[("search_places", {})]] * 20))
    assert len(chat(sid, "토요일 바다 코스")["tool_calls"]) == 6  # 마지막 단계에 닿지 않아도 6회에서 멈춘다


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
    assert r["course"]["days"] and any("AI" in n for n in r["notices"])
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
INDOOR = [{**CANDS[1], "place_id": 500, "name": "실내전시관", "lat": 35.111, "lng": 129.041}]


@pytest.fixture
def two_days(fake, monkeypatch):
    monkeypatch.setattr(places_tool, "places_where",
                        lambda category=None, indoor=None: CAFES if category == "카페" else INDOOR + CAFES)
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


def test_indoor_edit_replaces_one_outdoor_in_place(two_days):
    sid, first = two_days
    r = orchestrator.handle_chat(sid, "실내로 바꾸기", today=TODAY, edit={"kind": "indoor"})
    before, after = _ids(first), _ids(r)
    diffs = [(d, k) for d in range(2) for k, (a, b) in enumerate(zip(before[d], after[d])) if a != b]
    assert len(diffs) == 1 and [len(x) for x in after] == [len(x) for x in before]  # 한 자리만, 같은 위치에서
    d, k = diffs[0]
    assert after[d][k] == 500 and not next(c for c in CANDS if c["place_id"] == before[d][k])["indoor"]


def test_shorter_edit_removes_one_and_keeps_order(two_days):
    sid, first = two_days
    r = orchestrator.handle_chat(sid, "더 짧게", today=TODAY, edit={"kind": "shorter"})
    before, after = _ids(first), _ids(r)
    assert len(after) == 2 and sum(map(len, before)) - sum(map(len, after)) == 1
    for b, a in zip(before, after):
        assert [i for i in b if i in a] == a  # 남은 장소 순서 그대로


def test_edit_button_without_course_asks_first(fake):
    fake(FakeLLM({}))
    r = orchestrator.handle_chat(str(uuid.uuid4()), "카페 추가", today=TODAY, edit={"kind": "cafe"})
    assert "먼저 코스를" in r["answer"] and r["course"] is None
