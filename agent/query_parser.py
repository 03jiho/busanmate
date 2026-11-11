"""QueryParser (COMP-04, FR-001, AI-REQ-05): 자연어 → 구조화 조건. LLM 실패 시 규칙 기반 간이 추출 (SDD §7.4)."""
import logging
import re
from datetime import date, timedelta

from agent import prompt
from agent.response import with_josa
from services.llm import FALLBACK_NOTICE, STATS, fallback, get_llm
from services.search import Conditions, all_place_names, match_names
from settings import config
from tools.base import steps_schema

log = logging.getLogger("agent")

BUSAN_GU = ["중구", "서구", "동구", "영도구", "부산진구", "동래구", "남구", "북구", "해운대구", "사하구",
            "금정구", "강서구", "연제구", "수영구", "사상구", "기장군"]
WEEKDAYS = "월화수목금토일"
ASK_DATE = "언제 방문하실 예정인가요? 날짜를 알려 주시면 날씨와 축제까지 반영해 드릴게요. (예: 이번 토요일, 11월 20일부터 1박 2일)"
ASK_REGION = "어느 지역을 중심으로 볼까요? (예: 해운대, 남포동, 서면, 기장, 부산 전체)"
ASK_BOTH = "어느 지역을 중심으로, 언제 방문하실 예정인가요? (예: 이번 토요일 해운대, 11월 20일부터 1박 2일 부산 전체)"


def categories() -> list[str]:
    c = config()["classify"]
    return sorted({r["category"] for r in c["rules"]} | {c["food"]["category"], c["cafe"]["category"],
                                                          c["default"]["category"]})


def themes() -> list[str]:
    return list(config()["preferences"]["theme_categories"])


def schema() -> dict:
    return {"type": "object", "required": ["intent", "query"], "properties": {
        "intent": {"type": "string", "enum": ["search", "course", "edit", "chitchat", "outside"]},
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
        "places": {"type": "array", "items": {"type": "string"}},
        "edit": {"type": "object", "required": ["op"], "properties": {
            "op": {"type": "string", "enum": ["replace", "add", "remove"]},
            "day": {"type": "integer"}, "index": {"type": "integer"}, "spec": {"type": "string"}}},
        "steps": steps_schema(),  # 도구 계획도 같은 호출에서 받아 LLM 왕복을 한 번 줄인다 (응답 시간 예산)
    }}


def _llm_parse(message: str, today: date, deadline: float | None = None) -> dict:
    system = prompt("parser").format(today=today.isoformat(), weekday=WEEKDAYS[today.weekday()],
                                     gu_list=", ".join(BUSAN_GU), categories=", ".join(categories()),
                                     themes=", ".join(themes()))
    return get_llm().json(system, message, schema(), deadline=deadline)


# 부산이 아닌 지역(범위 밖). "대구탕", "대구뽈찜"처럼 음식 이름에 들어간 경우는 지역으로 보지 않는다
OUTSIDE = re.compile(r"서울|제주|경주|대구(?![탕뽈찜전])|인천|대전|광주|울산|강릉|속초|전주|여수|통영|거제|양양|가평|"
                     r"춘천|포항|안동|수원|도쿄|오사카|후쿠오카|교토|일본|중국|대만|홍콩|베트남|태국|싱가포르|미국|파리")
RAINY = re.compile(r"비\s*(?:오는|올\s*때|올\s*것|예보)|우천|장마")
NAMED_TRIGGER = re.compile(r"((?:\S+\s+){0,3}?\S+?)(?:은|는|을|를|도)?\s+(?:꼭\s*|반드시\s*)?(?:넣어|포함|추가|들러|가고\s*싶)")
NAMED_FILLER = re.compile(r"이번|다음|주|오늘|내일|모레|\S*요일\S*|\S*코스\S*|\S*일정\S*|\S*여행\S*|\d+\S*")


def _named_phrases(message: str) -> list[str]:
    """LLM 실패 시 장소명 추출: "○○ 꼭 넣어줘 / 포함 / 가고 싶어" 앞의 어절에서 날짜·"코스" 같은 말을 떼고,
    이랑·랑·하고·쉼표·및으로 나눈다. 예: "토요일 코스, 동백섬이랑 부산 아쿠아랜드 포함" → [동백섬, 부산 아쿠아랜드]."""
    m = NAMED_TRIGGER.search(message)
    if not m:
        return []
    words = m.group(1).split()
    cut = max((i + 1 for i, w in enumerate(words) if NAMED_FILLER.fullmatch(w)), default=0)  # 마지막 채움말 뒤만
    words = words[cut:] or words[-1:]
    while len(words) > 1 and (words[0] in BUSAN_GU or words[0] in REGION_WORDS or words[0] in ("바다", "맛집")):
        words.pop(0)  # 앞에 붙은 지역·테마 단어
    if not words:
        return []
    parts = re.split(r"\s*,\s*|\s+및\s+|(?<=[가-힣])(?:이랑|랑|하고)\s+", " ".join(words))
    return [re.sub(r"(은|는|을|를|도|에)$", "", p).strip() for p in parts if p.strip()]


def _rule_parse(message: str, has_course: bool) -> dict:
    """LLM 장애 시 간이 추출. 날짜는 읽지 않는다."""
    gu = [g for g in BUSAN_GU if g in message or (len(g) > 2 and g[:-1] in message)]  # "해운대" → 해운대구
    nights = re.search(r"(\d)\s*박", message)
    theme_words = {"바다": "바다|해변|해수욕장", "맛집": "맛집|먹|식당", "역사·문화": "역사|문화|박물관", "자연": "숲|산책|자연"}
    edit = None
    if has_course and re.search(r"빼|삭제|바꿔|교체|넣어|추가", message):
        intent = "edit"
        nth = re.search(r"(첫|두|세|네|다섯|여섯|\d+)\s*번째", message)
        day = re.search(r"(\d)\s*일차", message)
        op = "remove" if re.search(r"빼|삭제", message) and not re.search(r"넣어|추가|바꿔|교체", message) else \
            "replace" if re.search(r"바꿔|교체", message) or (nth and re.search(r"넣어|추가", message)) else "add"
        ordinals = {"첫": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6}
        edit = {"op": op, "index": (ordinals.get(nth.group(1)) or int(nth.group(1))) if nth else None,
                "day": int(day.group(1)) if day else 1, "spec": message}
    elif re.search(r"코스|일정|여행|당일|박", message):
        intent = "course"
    else:
        intent = "search"
    # 장소명: "○○ 꼭 넣어줘 / 포함 / 가고 싶어" 앞의 최대 세 어절 (지역 단어는 match_names가 떼어 본다)
    return {"intent": intent, "gu": gu, "query": message, "edit": edit,
            "places": _named_phrases(message) if intent != "edit" else [],
            "days": int(nights.group(1)) + 1 if nights else 1,
            "mode": "walk" if re.search(r"걸어|도보", message) else ("taxi" if "택시" in message else None),
            "indoor": True if "실내" in message or RAINY.search(message) else None,
            "theme": next((t for t, pat in theme_words.items() if re.search(pat, message)), None)}


REGION_WORDS = sorted({g[:-1] for g in BUSAN_GU if len(g) > 2} | {"부산", "서면", "광안리", "남포동", "송정", "기장"},
                      key=len, reverse=True)


def resolve_places(names: list[str], cond: Conditions, scan_message: str | None = None) -> list[str]:
    """말한 장소명을 DB와 대조한다. 찾은 곳은 cond.must에 넣고(지역을 안 말했으면 그 장소의 구를 지역으로),
    못 찾은 곳은 맨 위에 띄울 안내문으로 돌려준다. scan_message가 있으면(LLM 실패 시) 메시지 안의 DB 장소명도 찾는다."""
    names = [n.strip() for n in names if n and n.strip()]
    if not names and not scan_message:
        return []
    try:
        rows = all_place_names()
    except Exception as e:  # DB 장애: 대조를 건너뛴다 (코스는 검색 결과로 계속 만든다)
        log.warning("장소명 대조 실패: %s", type(e).__name__)
        return []
    found, missing = match_names(names, rows, cond.gu, REGION_WORDS)
    if scan_message:  # 규칙 기반일 때 놓친 실제 장소명(3자 이상, 지역 이름 제외)
        msg = re.sub(r"\s", "", scan_message)
        for r in rows:
            n = re.sub(r"\s", "", r["name"])
            if len(n) >= 3 and n in msg and n not in REGION_WORDS:
                found.setdefault(r["name"], r)
                # 같은 장소를 가리킨 미매칭만 지운다("용두산" → 용두산공원). 덩어리에 섞여 있어도 다른 이름은 남긴다
                missing = {k: v for k, v in missing.items() if not n.startswith(re.sub(r"\s", "", k))}
    cond.must = list(dict.fromkeys(r["place_id"] for r in found.values()))
    if found and not cond.gu:
        cond.gu = sorted({r["gu"] for r in found.values()})
    out = []
    for said, similar in missing.items():
        josa = with_josa(said, "은")[len(said):]
        hint = f" 비슷한 장소: {', '.join(similar)}" if similar else ""
        out.append(f"'{said}'{josa} 공공데이터에서 찾지 못해 코스에서 제외했어요.{hint}")
    return out


def parse(message: str, today: date, prefs: dict | None = None, has_course: bool = False,
          deadline: float | None = None) -> dict:
    """{"intent", "conditions": Conditions, "dates": [date], "clarify": 되물을 질문|None, "edit": dict|None, "notices"}.
    선호 설정은 질의에 없는 항목만 채운다(질의 우선, UC-006)."""
    prefs, notices = prefs or {}, []
    STATS["parser_calls"] += 1
    try:
        raw, used_rules = _llm_parse(message, today, deadline), False
    except Exception as e:
        fallback("parser", e)
        raw, used_rules = _rule_parse(message, has_course), True
        notices.append(FALLBACK_NOTICE)

    intent = raw.get("intent", "search")
    if intent == "search" and raw.get("start_date") and re.search(r"코스|일정|하루|걸어서|여행|나들이", message):
        intent = "course"  # 방문 날짜 + 동선 표현이면 장소 검색이 아니라 코스 요청 (평가셋 오분류 4건)
    cond = Conditions(
        query=raw.get("query") or message,
        gu=[g for g in raw.get("gu") or [] if g in BUSAN_GU],
        categories=[c for c in raw.get("categories") or [] if c in categories()],
        indoor=True if RAINY.search(message) and raw.get("indoor") is None else raw.get("indoor"),  # "비 오는 날" → 실내
        days=max(1, int(raw.get("days") or 1)),
        hours=raw.get("hours"),
        mode=raw.get("mode") or prefs.get("mode") or config()["travel"]["default_mode"],
        theme=raw.get("theme") if raw.get("theme") in themes() else prefs.get("theme"),
        companion=raw.get("companion") or prefs.get("companion"))
    theme_cats = config()["preferences"]["theme_categories"].get(cond.theme, [])
    if cond.categories and set(cond.categories) == set(theme_cats) and not re.search(r"(음식점|식당|카페|맛집)\s*만", message):
        # "맛집 투어"를 분류 필터(음식점만)로 읽으면 하루 내내 식당만 나온다: 테마와 같은 분류는 테마로만 둔다.
        # "식당만", "카페만"처럼 '만'을 붙였을 때만 분류 필터로 쓴다.
        cond.categories = []
    notices = resolve_places(raw.get("places") or [], cond, message if used_rules else None) + notices

    try:
        start = date.fromisoformat(raw["start_date"]) if raw.get("start_date") else None
    except ValueError:
        start = None
    clarify, edit = None, raw.get("edit") if intent == "edit" else None
    no_region = not cond.gu and not raw.get("region_any")  # "부산 전체", "어디든"이면 지역을 묻지 않는다
    if intent == "course" and used_rules and start is None:
        start = today
        notices.append("방문 날짜를 읽지 못해 오늘 기준으로 일정을 짰습니다.")
    elif intent == "course" and (start is None or no_region):
        clarify = ASK_BOTH if start is None and no_region else (ASK_DATE if start is None else ASK_REGION)
    elif intent == "edit" and not has_course:
        clarify = "수정할 코스가 아직 없습니다. 먼저 코스를 추천받아 주세요."
    elif edit and edit.get("op") in ("replace", "remove") and not edit.get("index"):
        clarify = "몇 번째 장소를 바꿀지 알려 주세요. (예: 두 번째 장소 빼줘)"

    suggest = None
    if intent != "edit" and not cond.gu and not cond.must and "부산" not in message and             (intent == "outside" or OUTSIDE.search(message)):
        # 범위 밖(부산이 아닌 지역): 지역 이름만 '부산'으로 바꾼 질문을 "부산에서 찾기" 제안으로 돌려준다
        intent, clarify = "outside", None
        suggest = re.sub(r"\s+", " ", OUTSIDE.sub("부산", message)).strip() if OUTSIDE.search(message) else None
    elif intent == "outside":  # 부산 지역·장소를 말했는데 LLM이 범위 밖으로 본 경우
        intent = "search"
    dates = [start + timedelta(days=i) for i in range(min(cond.days, config()["course"]["max_days"]))] if start else []
    return {"intent": intent, "conditions": cond, "dates": dates, "clarify": clarify, "edit": edit, "notices": notices,
            "suggest": suggest, "steps": None if used_rules else raw.get("steps")}
