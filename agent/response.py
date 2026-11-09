"""ResponseBuilder (COMP-09, FR-003, FR-011, NFR-004): 설명 생성, verify_places 장소 검증, 출처 첨부.
LLM에는 이번 턴의 장소만 보여 주고 [[place_id]] 마커로만 인용하게 한 뒤, 마커를 DB와 대조해 이름으로 바꾼다."""
import json
import logging
import re

from agent import prompt
from services.llm import get_llm
from services.search import fetch_places, source_of

log = logging.getLogger("agent")
MARKER = re.compile(r"\[\[(\d+)\]\]")
# 마커 뒤 조사. 긴 것부터, '이'는 서술격(이다·이며 등)이면 조사가 아니므로 제외
MARKER_JOSA = re.compile(r"\[\[(\d+)\]\](이에요|예요|으로|이나|이랑|은|는|이(?![다며고지라었에])|가|을|를|과|와|로|나|랑)?")
_JOSA = {"은": ("은", "는"), "는": ("은", "는"), "이": ("이", "가"), "가": ("이", "가"), "을": ("을", "를"),
         "를": ("을", "를"), "과": ("과", "와"), "와": ("과", "와"), "이나": ("이나", "나"), "나": ("이나", "나"),
         "이랑": ("이랑", "랑"), "랑": ("이랑", "랑"), "이에요": ("이에요", "예요"), "예요": ("이에요", "예요")}
_DIGIT_JONG = {"0": 21, "1": 8, "2": 0, "3": 16, "4": 0, "5": 0, "6": 1, "7": 8, "8": 8, "9": 0}  # 영 일 이 삼 …


def _jong(word: str) -> int:
    """마지막 글자의 받침 번호(0=없음, 8=ㄹ). 괄호 등 기호는 건너뛴다.
    ponytail: 영문으로 끝나면 받침 없음으로 본다, 틀리는 이름이 나오면 발음 표 추가"""
    for ch in reversed(word):
        if "가" <= ch <= "힣":
            return (ord(ch) - 0xAC00) % 28
        if ch in _DIGIT_JONG:
            return _DIGIT_JONG[ch]
        if ch.isalpha():
            return 0
    return 0


def with_josa(name: str, josa: str | None) -> str:
    """장소명에 받침에 맞는 조사를 붙인다. 예: 해운대+과 → 해운대와, 동백섬+로 → 동백섬으로."""
    if not josa:
        return name
    jong = _jong(name)
    if josa in ("으로", "로"):
        return name + ("로" if jong in (0, 8) else "으로")
    return name + _JOSA[josa][0 if jong else 1]
CHITCHAT = ("저는 부산 장소 검색과 여행 코스 추천을 도와드려요. 숙박·교통 예약이나 결제는 도와드리지 못합니다. "
            "예: \"이번 토요일 부모님과 걸어서 돌기 좋은 바다 코스\", \"해운대 근처 비 오는 날 갈 만한 곳\"")
SCHEMA = {"type": "object", "required": ["answer", "reasons"], "properties": {
    "answer": {"type": "string"},
    "reasons": {"type": "array", "items": {"type": "object", "required": ["place_id", "reason"], "properties": {
        "place_id": {"type": "integer"}, "reason": {"type": "string"}}}}}}


def verify_places(text: str, allowed: dict[int, str]) -> tuple[str, list[int]]:
    """허용되지 않은 마커가 든 문장은 지우고, 나머지 마커는 DB 장소명으로 바꾼다. (검증된 문장, 걸러진 id)"""
    parts = re.split(r"((?<=[.!?])\s+|\n+)", text)
    out, dropped = [], []
    for i, part in enumerate(parts):
        if i % 2:  # 구분자
            out.append(part)
            continue
        bad = [int(x) for x in MARKER.findall(part) if int(x) not in allowed]
        if bad:
            dropped += bad
            if out:
                out.pop()  # 지운 문장 앞의 구분자도 뺀다
            continue
        out.append(MARKER_JOSA.sub(lambda m: with_josa(allowed[int(m.group(1))], m.group(2)), part))
    return "".join(out).strip(), dropped


def build_response(state: dict) -> dict:
    notices, intent = list(state.get("notices") or []), state.get("intent")
    if state.get("clarify"):
        return {"answer": state["clarify"], "course": None, "sources": [], "notices": notices}
    if intent == "chitchat":
        return {"answer": CHITCHAT, "course": None, "sources": [], "notices": notices}

    course = state.get("course") if intent in ("course", "edit") else None
    if course and course["days"]:
        ids = [i["place_id"] for d in course["days"] for i in d["items"]]
    else:
        course, ids = None, [p["place_id"] for p in (state.get("candidates") or [])[:5]]
    places = fetch_places(ids)
    # 검증에서 빠진(비활성) 장소는 코스에서도 뺀다
    if course:
        for d in course["days"]:
            d["items"] = [i for i in d["items"] if i["place_id"] in places]
    ids = [i for i in ids if i in places]
    if not ids:
        return {"answer": "조건에 맞는 장소를 찾지 못했습니다. 지역이나 테마를 조금 넓혀서 다시 물어봐 주세요.",
                "course": None, "sources": [], "notices": notices}

    allowed = {i: places[i]["name"] for i in ids}
    context = {
        "request": state.get("message"), "intent": intent,
        "places": [{"id": i, "name": places[i]["name"], "category": places[i]["category"], "gu": places[i]["gu"],
                    "summary": (places[i]["description"] or "")[:200]} for i in ids],
        "course": [{"day": d["day"], "order": [i["place_id"] for i in d["items"]]} for d in course["days"]] if course else None,
        "weather": state.get("rain_by_day"),
        "festivals": [f["name"] for f in state.get("festivals") or []],
    }
    try:
        out = get_llm().json(prompt("response"), json.dumps(context, ensure_ascii=False, default=str), SCHEMA)
        answer, dropped = verify_places(out["answer"], allowed)
        reasons = {}
        for r in out.get("reasons", []):
            if r["place_id"] in allowed:
                text, bad = verify_places(r["reason"], allowed)
                reasons[r["place_id"]], dropped = text, dropped + bad
        if dropped:
            log.warning("verify_places 제거 id=%s", dropped)  # ERR-007
    except Exception as e:
        log.warning("response LLM 실패: %s", type(e).__name__)
        answer = "요청하신 조건으로 코스를 만들었습니다." if course else "조건에 맞는 장소를 찾았습니다."
        # 추천 이유는 DB 설명의 첫 줄(공공데이터 장소 소개 문구)로 대신한다. 근거는 여전히 DB뿐이다 (AI-REQ-01·02)
        reasons = {i: (places[i]["description"] or "").split("\n")[0][:80] or None for i in ids}
        notices.append("AI 설명을 만들지 못해 추천 이유를 장소 소개 문구로 대신합니다.")

    if course:
        for d in course["days"]:
            for item in d["items"]:
                item["name"], item["reason"] = places[item["place_id"]]["name"], reasons.get(item["place_id"])
    sources = [source_of(places[i]) for i in ids] + list(state.get("sources") or [])
    return {"answer": answer, "course": course, "sources": sources, "notices": notices,
            "places": None if course else [{**source_of(places[i]), "category": places[i]["category"],
                                            "gu": places[i]["gu"], "reason": reasons.get(i)} for i in ids]}
