"""AgentOrchestrator (COMP-03, AI-REQ-04, FR-011, FR-012): LangGraph로 질의 흐름과 도구 호출을 제어한다.
parse → (되묻기·잡담이면 바로 respond) → agent ⇄ tools (턴당 최대 6회) → respond.
LLM은 도구만 고르고, 점수화·경로·시간 계산은 도구 안의 코드가 한다."""
import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

import tools  # noqa: F401  (tools 패키지가 도구 모듈을 모두 등록한다)
from agent import prompt, session
from agent.query_parser import parse
from agent.response import build_response
from services.llm import ToolCall, get_llm
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


def _max_calls() -> int:
    return config()["agent"]["max_tool_calls_per_turn"]


def parse_node(state: State) -> dict:
    s = session.get(state["session_id"])
    msg = f"{s['pending']} {state['message']}" if s["pending"] else state["message"]  # 되물은 뒤의 답이면 이어 붙인다
    p = parse(msg, state["today"], s["preferences"], has_course=bool(s["last_course"]))
    s["pending"] = msg if p["clarify"] else None
    return {"intent": p["intent"], "conditions": p["conditions"], "dates": p["dates"], "edit": p["edit"],
            "clarify": p["clarify"], "notices": p["notices"], "course": s["last_course"] if p["intent"] == "edit" else None,
            "candidates": [], "festivals": [], "rain_by_day": None, "messages": [], "pending": [], "tool_calls": 0,
            "tool_log": [], "sources": [], "llm_down": False}


def agent_node(state: State) -> dict:
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
    updates = {}
    for call in state["pending"]:
        if n >= _max_calls():
            break
        tool = REGISTRY.get(call.name)
        res = tool.call(call.args, ctx) if tool else ToolResult(ok=False, data={"error": f"없는 도구: {call.name}"})
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
        s["last_course"], s["last_conditions"] = result["course"], state["conditions"]
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
    g.add_conditional_edges("parse", lambda s: "respond" if s.get("clarify") or s["intent"] == "chitchat" else "agent",
                            ["respond", "agent"])
    g.add_conditional_edges("agent", lambda s: "tools" if s["pending"] and s["tool_calls"] < _max_calls() else "respond",
                            ["tools", "respond"])
    g.add_conditional_edges("tools", _after_tools, ["respond", "agent"])
    g.add_edge("respond", END)
    return g.compile()


GRAPH = _graph()


def handle_chat(session_id: str, message: str, today: date | None = None) -> dict:
    """API-001 응답: {answer, conditions, course, sources, notices, places, intent, tool_calls}."""
    out = GRAPH.invoke({"session_id": session_id, "message": message, "today": today or datetime.now(KST).date()})
    return {**out["result"], "conditions": asdict(out["conditions"]) if out.get("conditions") else None,
            "intent": out.get("intent"), "tool_calls": out.get("tool_log", [])}
