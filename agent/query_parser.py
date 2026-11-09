"""QueryParser (COMP-04, FR-001, AI-REQ-05): 자연어 → 구조화 조건. LLM 실패 시 규칙 기반 간이 추출 (SDD §7.4)."""
import logging
import re
from datetime import date, timedelta

from agent import prompt
from services.llm import get_llm
from services.search import Conditions
from settings import config

log = logging.getLogger("agent")

BUSAN_GU = ["중구", "서구", "동구", "영도구", "부산진구", "동래구", "남구", "북구", "해운대구", "사하구",
            "금정구", "강서구", "연제구", "수영구", "사상구", "기장군"]
WEEKDAYS = "월화수목금토일"
ASK_DATE = "언제 방문하실 예정인가요? 날짜를 알려 주시면 날씨와 축제까지 반영해 드릴게요. (예: 이번 토요일, 11월 20일부터 1박 2일)"
ASK_REGION = "어느 지역을 중심으로 볼까요? (예: 해운대, 남포동, 서면, 기장, 부산 전체)"
ASK_BOTH = "어느 지역을 중심으로, 언제 방문하실 예정인가요? (예: 이번 토요일 해운대, 11월 20일부터 1박 2일 부산 전체)"


def categories() -> list[str]:
    c = config()["classify"]
    return sorted({r["category"] for r in c["rules"]} | {c["food"]["category"], c["default"]["category"]})


def themes() -> list[str]:
    return list(config()["preferences"]["theme_categories"])


def schema() -> dict:
    return {"type": "object", "required": ["intent", "query"], "properties": {
        "intent": {"type": "string", "enum": ["search", "course", "edit", "chitchat"]},
        "gu": {"type": "array", "items": {"type": "string", "enum": BUSAN_GU}},
        "region_any": {"type": "boolean"},
        "categories": {"type": "array", "items": {"type": "string", "enum": categories()}},
        "theme": {"type": "string", "enum": themes()},
        "companion": {"type": "string"},
        "start_date": {"type": "string"},
        "days": {"type": "integer"},
        "hours": {"type": "number"},
        "mode": {"type": "string", "enum": ["walk", "taxi"]},
        "indoor": {"type": "boolean"},
        "query": {"type": "string"},
        "edit": {"type": "object", "required": ["op"], "properties": {
            "op": {"type": "string", "enum": ["replace", "add", "remove"]},
            "day": {"type": "integer"}, "index": {"type": "integer"}, "spec": {"type": "string"}}},
    }}


def _llm_parse(message: str, today: date) -> dict:
    system = prompt("parser").format(today=today.isoformat(), weekday=WEEKDAYS[today.weekday()],
                                     gu_list=", ".join(BUSAN_GU), categories=", ".join(categories()),
                                     themes=", ".join(themes()))
    return get_llm().json(system, message, schema())


def _rule_parse(message: str, has_course: bool) -> dict:
    """LLM 장애 시 간이 추출. 날짜는 읽지 않는다."""
    gu = [g for g in BUSAN_GU if g in message or (len(g) > 2 and g[:-1] in message)]  # "해운대" → 해운대구
    nights = re.search(r"(\d)\s*박", message)
    theme_words = {"바다": "바다|해변|해수욕장", "맛집": "맛집|먹|식당", "역사·문화": "역사|문화|박물관", "자연": "숲|산책|자연"}
    if has_course and re.search(r"빼|바꿔|교체|넣어|추가", message):
        intent = "edit"
    elif re.search(r"코스|일정|여행|당일|박", message):
        intent = "course"
    else:
        intent = "search"
    return {"intent": intent, "gu": gu, "query": message,
            "days": int(nights.group(1)) + 1 if nights else 1,
            "mode": "walk" if re.search(r"걸어|도보", message) else ("taxi" if "택시" in message else None),
            "indoor": True if "실내" in message else None,
            "theme": next((t for t, pat in theme_words.items() if re.search(pat, message)), None)}


def parse(message: str, today: date, prefs: dict | None = None, has_course: bool = False) -> dict:
    """{"intent", "conditions": Conditions, "dates": [date], "clarify": 되물을 질문|None, "edit": dict|None, "notices"}.
    선호 설정은 질의에 없는 항목만 채운다(질의 우선, UC-006)."""
    prefs, notices = prefs or {}, []
    try:
        raw, fallback = _llm_parse(message, today), False
    except Exception as e:
        log.warning("parser LLM 실패: %s", type(e).__name__)
        raw, fallback = _rule_parse(message, has_course), True
        notices.append("AI가 질문을 해석하지 못해 간단한 규칙으로 조건을 읽었습니다.")

    intent = raw.get("intent", "search")
    cond = Conditions(
        query=raw.get("query") or message,
        gu=[g for g in raw.get("gu") or [] if g in BUSAN_GU],
        categories=[c for c in raw.get("categories") or [] if c in categories()],
        indoor=raw.get("indoor"),
        days=max(1, int(raw.get("days") or 1)),
        hours=raw.get("hours"),
        mode=raw.get("mode") or prefs.get("mode") or config()["travel"]["default_mode"],
        theme=raw.get("theme") if raw.get("theme") in themes() else prefs.get("theme"),
        companion=raw.get("companion") or prefs.get("companion"))

    try:
        start = date.fromisoformat(raw["start_date"]) if raw.get("start_date") else None
    except ValueError:
        start = None
    clarify, edit = None, raw.get("edit") if intent == "edit" else None
    no_region = not cond.gu and not raw.get("region_any")  # "부산 전체", "어디든"이면 지역을 묻지 않는다
    if intent == "course" and fallback and start is None:
        start = today
        notices.append("방문 날짜를 읽지 못해 오늘 기준으로 일정을 짰습니다.")
    elif intent == "course" and (start is None or no_region):
        clarify = ASK_BOTH if start is None and no_region else (ASK_DATE if start is None else ASK_REGION)
    elif intent == "edit" and not has_course:
        clarify = "수정할 코스가 아직 없습니다. 먼저 코스를 추천받아 주세요."
    elif edit and edit.get("op") in ("replace", "remove") and not edit.get("index"):
        clarify = "몇 번째 장소를 바꿀지 알려 주세요. (예: 두 번째 장소 빼줘)"

    dates = [start + timedelta(days=i) for i in range(min(cond.days, config()["course"]["max_days"]))] if start else []
    return {"intent": intent, "conditions": cond, "dates": dates, "clarify": clarify, "edit": edit, "notices": notices}
