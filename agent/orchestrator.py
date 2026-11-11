"""AgentOrchestrator (COMP-03, AI-REQ-04, FR-011, FR-012): LangGraph로 질의 흐름과 도구 호출을 제어한다.
parse → (되묻기·잡담이면 바로 respond) → agent ⇄ tools (턴당 최대 6회) → respond.
LLM은 도구만 고르고, 점수화·경로·시간 계산은 도구 안의 코드가 한다."""
import json
import logging
import time
from dataclasses import asdict, replace
from datetime import date, datetime
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

import tools  # noqa: F401  (tools 패키지가 도구 모듈을 모두 등록한다)
from agent import prompt, session
from agent.query_parser import parse
from agent.response import build_response
from services.llm import ToolCall, get_llm
from services.search import Conditions
from settings import config
from tools.base import REGISTRY, ToolResult, declarations
from tools.weather import KST

log = logging.getLogger("agent")
# LLM이 도구를 고르지 못할 때 쓰는 기본 순서 (SDD §7.4)
DEFAULT_PLAN = {"search": ["search_places"],
                "course": ["search_places", "get_weather", "get_festivals", "build_course", "get_route_times"],
                "edit": ["edit_course", "get_route_times"]}


class State(TypedDict, total=False):
    session_id: str
    message: str
    today: date
    intent: str
    conditions: Any
    dates: list
    edit: dict | None
    clarify: str | None
    candidates: list
    course: dict | None
    rain_by_day: list | None
    festivals: list
    messages: list
    pending: list
    tool_calls: int
    tool_log: list
    llm_down: bool
    notices: list
    sources: list
    result: dict
    action: dict | None
    suggest: str | None


def _max_calls() -> int:
    return config()["agent"]["max_tool_calls_per_turn"]


EDIT_OPS = {"cafe": "add", "indoor": "replace", "shorter": "remove"}


def parse_node(state: State) -> dict:
    s = session.get(state["session_id"])
    fresh = {"candidates": [], "festivals": [], "rain_by_day": None, "messages": [], "pending": [], "tool_calls": 0,
             "tool_log": [], "sources": [], "llm_down": False}
    action = state.get("action")
    if action:  # "코스 고치기" 버튼: 문장을 해석하지 않고 직전 코스·조건·날짜로 제자리 수정 (FR-012)
        if not s["last_course"]:
            return {**fresh, "intent": "edit", "conditions": s["last_conditions"] or Conditions(query=state["message"]),
                    "dates": [], "edit": None, "notices": [], "course": None,
                    "clarify": "수정할 코스가 아직 없습니다. 먼저 코스를 추천받아 주세요."}
        return {**fresh, "intent": "edit", "conditions": s["last_conditions"], "dates": s["last_dates"],
                "edit": {"op": EDIT_OPS[action["kind"]], "kind": action["kind"], "day": action.get("day")},
                "clarify": None, "notices": [], "course": s["last_course"]}
    msg = f"{s['pending']} {state['message']}" if s["pending"] else state["message"]  # 되물은 뒤의 답이면 이어 붙인다
    p = parse(msg, state["today"], s["preferences"], has_course=bool(s["last_course"]))
    s["pending"] = msg if p["clarify"] else None
    cond, dates = p["conditions"], p["dates"]
    if p["intent"] == "edit" and s["last_conditions"]:  # 대화로 고칠 때도 직전 코스의 조건·날짜를 그대로 쓴다
        cond, dates = replace(s["last_conditions"], must=p["conditions"].must), s["last_dates"]
    return {**fresh, "intent": p["intent"], "conditions": cond, "dates": dates, "edit": p["edit"], "suggest": p.get("suggest"),
            "clarify": p["clarify"], "notices": p["notices"], "course": s["last_course"] if p["intent"] == "edit" else None}


def agent_node(state: State) -> dict:
    if state.get("action"):  # 버튼 수정은 할 일이 정해져 있다: LLM 없이 edit_course → get_route_times
        plan = [n for n in DEFAULT_PLAN["edit"] if n not in state["tool_log"]]
        return {"pending": [ToolCall(n, dict(state["edit"]) if n == "edit_course" else {}) for n in plan]}
    system = prompt("agent").format(
        intent=state["intent"], conditions=json.dumps(asdict(state["conditions"]), ensure_ascii=False),
        dates=", ".join(map(str, state["dates"])) or "없음",
        edit=json.dumps(state["edit"], ensure_ascii=False) if state["edit"] else "없음",
        has_course="있음" if state.get("course") else "없음", max_calls=_max_calls())
    messages = state["messages"] or [{"role": "user", "text": state["message"]}]
    try:
        reply = get_llm().chat(system, messages, declarations())
    except Exception as e:
        log.warning("agent LLM 실패: %s", type(e).__name__)
        plan = [n for n in DEFAULT_PLAN.get(state["intent"], []) if n not in state["tool_log"]]
        calls = [ToolCall(n, dict(state["edit"] or {}) if n == "edit_course" else {}) for n in plan]
        return {"pending": calls, "llm_down": True,
                "notices": state["notices"] + ["AI가 도구를 고르지 못해 기본 순서로 처리했습니다."]}
    return {"messages": messages + [{"role": "model", "reply": reply}], "pending": reply.tool_calls}


def tools_node(state: State) -> dict:
    ctx = dict(state)
    notices, sources = list(state["notices"]), list(state["sources"])
    messages, n, tool_log = list(state["messages"]), state["tool_calls"], list(state["tool_log"])
    updates, emit = {}, get_stream_writer()  # emit: 스트리밍(/api/chat/stream)일 때만 단계 이벤트가 나간다
    for call in state["pending"]:
        if n >= _max_calls():
            break
        tool = REGISTRY.get(call.name)
        emit({"tool": call.name, "phase": "start"})
        res = tool.call(call.args, ctx) if tool else ToolResult(ok=False, data={"error": f"없는 도구: {call.name}"})
        emit({"tool": call.name, "phase": "done"})
        ctx.update(res.updates)
        updates.update(res.updates)
        notices += [x for x in res.notices if x not in notices]
        sources += [x for x in res.sources if x not in sources]
        data = res.data if isinstance(res.data, dict) else {"data": res.data}
        messages.append({"role": "tool", "name": call.name, "result": {"ok": res.ok, **data}})
        n += 1
        tool_log.append(call.name)
    return {**updates, "messages": messages, "tool_calls": n, "tool_log": tool_log, "notices": notices,
            "sources": sources, "pending": []}


def respond_node(state: State) -> dict:
    result = build_response(state)
    s = session.get(state["session_id"])
    if result.get("course"):
        s["last_course"], s["last_conditions"], s["last_dates"] = result["course"], state["conditions"], state["dates"]
    log.info("turn intent=%s tools=%s", state.get("intent"), state.get("tool_log"))  # 질의 원문은 남기지 않는다
    return {"result": result}


def _after_tools(s: State) -> str:
    """의도의 마지막 단계 도구(코스·수정은 get_route_times, 검색은 search_places)를 마쳤으면 LLM에 되묻지 않고
    바로 응답한다 (LLM 왕복 1회 절약, NFR-001)."""
    last_step = DEFAULT_PLAN.get(s["intent"], [None])[-1]
    done = bool(s["tool_log"]) and s["tool_log"][-1] == last_step
    return "respond" if s["llm_down"] or s["tool_calls"] >= _max_calls() or done else "agent"


def _graph():
    g = StateGraph(State)
    for name, fn in [("parse", parse_node), ("agent", agent_node), ("tools", tools_node), ("respond", respond_node)]:
        g.add_node(name, fn)
    g.add_edge(START, "parse")
    g.add_conditional_edges("parse", lambda s: "respond" if s.get("clarify") or s["intent"] in ("chitchat", "outside")
                            else "agent", ["respond", "agent"])
    g.add_conditional_edges("agent", lambda s: "tools" if s["pending"] and s["tool_calls"] < _max_calls() else "respond",
                            ["tools", "respond"])
    g.add_conditional_edges("tools", _after_tools, ["respond", "agent"])
    g.add_edge("respond", END)
    return g.compile()


GRAPH = _graph()


def _input(session_id: str, message: str, today: date | None, edit: dict | None) -> dict:
    return {"session_id": session_id, "message": message, "today": today or datetime.now(KST).date(), "action": edit}


def _result(out: dict) -> dict:
    return {**out["result"], "conditions": asdict(out["conditions"]) if out.get("conditions") else None,
            "dates": [d.isoformat() for d in out.get("dates") or []],  # UI 조건 칩용 (해석된 방문 날짜)
            "intent": out.get("intent"), "tool_calls": out.get("tool_log", [])}


def handle_chat(session_id: str, message: str, today: date | None = None, edit: dict | None = None) -> dict:
    """API-001 응답: {answer, conditions, course, sources, notices, places, intent, tool_calls}.
    edit({kind: cafe|indoor|shorter, day?})가 있으면 문장 해석 없이 직전 코스를 제자리에서 고친다."""
    return _result(GRAPH.invoke(_input(session_id, message, today, edit)))


# 화면의 진행 단계: 조건 해석 → 장소 검색 → 날씨 → 동선 계산 → 요약 (도구 → 단계)
TOOL_STAGE = {"search_places": "search", "get_weather": "weather", "get_festivals": "weather",
              "build_course": "route", "get_route_times": "route", "edit_course": "route"}


def handle_chat_stream(session_id: str, message: str, today: date | None = None, edit: dict | None = None):
    """API-001 스트리밍: 단계 이벤트 {stage, phase: start|done, t(초)}를 차례로 내보내고 마지막에 {result, t}.
    단계는 실제 그래프 진행(해석 노드, 도구 호출, 응답 노드)에서 나온다. 건너뛴 단계(장소 검색만 할 때의 날씨 등)는 나오지 않는다."""
    t0, last = time.monotonic(), None

    def ev(stage, phase):
        return {"stage": stage, "phase": phase, "t": round(time.monotonic() - t0, 1)}
    yield ev("parse", "start")
    for mode, data in GRAPH.stream(_input(session_id, message, today, edit), stream_mode=["updates", "custom", "values"]):
        if mode == "custom" and data.get("tool") in TOOL_STAGE:
            yield ev(TOOL_STAGE[data["tool"]], data["phase"])
        elif mode == "updates":
            node = next(iter(data))
            if node == "parse":
                yield ev("parse", "done")
            elif node == "respond":
                yield ev("respond", "done")
        elif mode == "values":
            last = data
    yield {"result": _result(last), "t": round(time.monotonic() - t0, 1)}
