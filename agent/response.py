"""ResponseBuilder (COMP-09, FR-003, FR-011, NFR-004): 설명 생성, verify_places 장소 검증, 출처 첨부.
LLM에는 이번 턴의 장소만 보여 주고 [[place_id]] 마커로만 인용하게 한 뒤, 마커를 DB와 대조해 이름으로 바꾼다."""
import json
import logging
import re

from agent import prompt
from agent.explain import enrich
from services.llm import FALLBACK_NOTICE, STATS, fallback, get_llm
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
CHITCHAT = "BusanMate는 부산 장소 찾기와 여행 코스만 도와드려요. 숙박·교통 예약이나 결제는 할 수 없어요. 아래처럼 물어봐 주세요."
OUTSIDE = "BusanMate는 부산 전용이에요. 부산에서 비슷한 곳을 찾아드릴까요?"
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


def _says_something(text: str | None, p: dict) -> bool:
    """장소명만 되풀이한 문구("동삼정")는 빈 설명으로 본다."""
    norm = re.sub(r"\W", "", text or "")
    return len(norm) >= 8 and norm != re.sub(r"\W", "", p["name"])


def clip(text: str | None, limit: int = 110) -> str | None:
    """긴 설명은 문장 단위로 자른다. 문장 끝(마침표 등)에서 끝나면 그대로, 문장 중간에서 잘렸을 때만 '…' 하나를 붙인다.
    첫 문장부터 길면 마지막 띄어쓰기에서 자른다."""
    if not text or len(text) <= limit:
        return text
    out = ""
    for sent in re.split(r"(?<=[.!?。])\s+", text.strip()):
        if len(out) + len(sent) + 1 > limit:
            break
        out = f"{out} {sent}".strip()
    if not out:
        out = text[:limit].rsplit(" ", 1)[0]
    out = out.rstrip(" ,·")
    return out if re.search(r"[.!?。…]$", out) else out + "…"


# 근거 검사에서 빼는 일반어(추천 문장에 흔한 서술·평가어). ponytail: 형태소 분석 없이 어절 앞부분 대조, 오탐이 잦으면 형태소 분석기로
_GENERIC = {"장소", "곳", "수", "있는", "있어요", "있습니다", "즐길", "즐기기", "즐길수", "좋은", "좋아요", "좋습니다", "추천", "추천해요",
            "방문", "방문하기", "들르기", "들러", "분위기", "여유롭게", "함께", "가볍게", "천천히", "잠시", "코스", "여행", "부산",
            "산책", "구경", "둘러보기", "곳이에요", "곳입니다", "만나", "느낄", "느껴", "경험", "시간", "보내기", "가기", "다양한"}
_JOSA_TAIL = re.compile(r"(으로|에서|에게|까지|부터|이에요|예요|입니다|이며|이고|하고|은|는|이|가|을|를|의|에|로|도|와|과|만|한|할|해)$")


def grounded(reason: str, p: dict) -> bool:
    """추천 이유의 핵심 낱말이 원문(이름·분류·주소·소개문)에 있는가. 절반 넘게 원문에 없으면 근거 없는 문장으로 본다."""
    src = " ".join(str(p.get(k) or "") for k in ("name", "category", "gu", "address", "description"))
    words = [_JOSA_TAIL.sub("", w) for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", reason)]
    keys = [w for w in words if len(w) >= 2 and w not in _GENERIC]
    if not keys:
        return True
    found = sum(any(w[:n] in src for n in range(len(w), 1, -1)) for w in keys)
    return found * 2 >= len(keys)


def intro_line(p: dict) -> str | None:
    """LLM이 실패했을 때의 추천 이유: DB 소개 문구 중 장소명이 아닌 첫 줄(원천 설명의 첫 줄은 제목인 경우가 많다)."""
    return next((clip(line.strip(), 80) for line in (p.get("description") or "").split("\n")
                 if _says_something(line, p)), None)


def fallback_line(p: dict) -> str:
    """설명이 빈 카드용 한 줄: "중구 용두산길 37-55에 있는 공원·자연입니다." (주소가 없으면 구)."""
    addr = re.sub(r"^부산(광역시)?\s*", "", (p.get("address") or "").split("\n")[0]).strip() or p.get("gu") or "부산"
    return f"{addr}에 있는 {p['category']}입니다."


def rule_summary(course: dict | None, names: list[str], dates: list | None = None) -> str:
    """LLM이 실패했을 때의 요약: 장소 순서와 이동 방법을 코드로 쓴다 (DB 이름만 쓰므로 근거는 그대로 DB)."""
    if not course:
        return f"조건에 맞는 장소 {len(names)}곳을 찾았습니다: {', '.join(names)}."
    lines = []
    for d in course["days"]:
        legs = [i["leg_to_next"] for i in d["items"] if i.get("leg_to_next")]
        walk, taxi = sum(lg["mode"] == "walk" for lg in legs), sum(lg["mode"] == "taxi" for lg in legs)
        how = " · ".join(x for x in [f"도보 {walk}구간" if walk else "", f"택시 {taxi}구간" if taxi else ""] if x)
        when = f"{dates[d['day'] - 1].month}월 {dates[d['day'] - 1].day}일 " if dates and d["day"] <= len(dates) else ""
        head = f"{d['day']}일차 {when}" if len(course["days"]) > 1 else when
        lines.append(f"{head}코스: {' → '.join(i['name'] for i in d['items'])} (장소 {len(d['items'])}곳"
                     + (f", {how})" if how else ")"))
    return "\n\n".join(lines)


def build_response(state: dict) -> dict:
    notices, intent = list(state.get("notices") or []), state.get("intent")
    if state.get("clarify"):
        return {"answer": state["clarify"], "course": None, "sources": [], "notices": notices}
    if intent == "chitchat":
        return {"answer": CHITCHAT, "course": None, "sources": [], "notices": notices}
    if intent == "outside":  # 범위 밖(부산이 아닌 지역): 화면이 "부산에서 찾기"(suggest)와 예시 질문 버튼을 함께 보여 준다
        return {"answer": OUTSIDE, "course": None, "sources": [], "notices": notices, "suggest": state.get("suggest")}

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
                    "address": places[i].get("address"),
                    "summary": (places[i]["description"] or "")[:120]} for i in ids],
        "course": [{"day": d["day"], "order": [i["place_id"] for i in d["items"]]} for d in course["days"]] if course else None,
        "weather": state.get("rain_by_day"),
        "festivals": [f["name"] for f in state.get("festivals") or []],
    }
    try:
        STATS["response_calls"] += 1
        out = get_llm().json(prompt("response"), json.dumps(context, ensure_ascii=False, default=str), SCHEMA,
                             deadline=state.get("deadline"))
        answer, dropped = verify_places(out["answer"], allowed)
        reasons = {}
        for r in out.get("reasons", []):
            if r["place_id"] in allowed:
                text, bad = verify_places(r["reason"], allowed)
                if not grounded(text, places[r["place_id"]]):  # 원문에 없는 사실(메뉴·시설·역사)을 지어냈다 → 원문 한 문장으로
                    STATS["reason_ungrounded"] += 1
                    log.warning("추천 이유 근거 없음 id=%s", r["place_id"])
                    text = intro_line(places[r["place_id"]])
                reasons[r["place_id"]], dropped = text, dropped + bad
        if dropped:
            log.warning("verify_places 제거 id=%s", dropped)  # ERR-007
    except Exception as e:
        fallback("response", e)  # 원인·본문 앞부분(키는 가림)을 로그와 집계에. 질의 원문은 남기지 않는다 (SDD §8)
        answer = rule_summary(course, [places[i]["name"] for i in ids], state.get("dates"))
        # 추천 이유는 DB 설명의 첫 줄(공공데이터 장소 소개 문구)로 대신한다. 근거는 여전히 DB뿐이다 (AI-REQ-01·02)
        reasons = {i: intro_line(places[i]) for i in ids}
        notices.append(FALLBACK_NOTICE)
    # 추천 이유가 빈 카드(LLM이 빠뜨렸거나 소개 문구가 없음)는 분류와 주소로 만든 한 줄로 채운다 (DB 정보만 씀)
    reasons = {i: clip(reasons.get(i)) if _says_something(reasons.get(i), places[i]) else fallback_line(places[i])
               for i in ids}

    if course:
        for d in course["days"]:
            for item in d["items"]:
                item["name"], item["reason"] = places[item["place_id"]]["name"], reasons.get(item["place_id"])
        enrich(course, places, state.get("conditions"))  # 왜 이곳?, 출처 확인 수, 예상 시간 계산 방식, 예상 비용
    sources = [source_of(places[i]) for i in ids] + list(state.get("sources") or [])
    notices = list(dict.fromkeys(notices))  # 단계마다 같은 대체 안내가 붙어도 한 줄만
    return {"answer": answer, "course": course, "sources": sources, "notices": notices,
            "places": None if course else [{**source_of(places[i]), "category": places[i]["category"],
                                            "gu": places[i]["gu"], "reason": reasons.get(i)} for i in ids]}
