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
    monkeypatch.setattr(response, "fetch_places", lambda ids: {c["place_id"]: c for c in CANDS if c["place_id"] in ids})

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
