"""LLM/VLM Client (COMP-11): 모델 호출 추상화, 모델 폴백, 응답 캐시, 호출 로그.
에이전트는 LLMClient 프로토콜에만 의존한다. 제공자는 .env의 LLM_PROVIDER로 고른다(현재 gemini)."""
import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

from google import genai
from google.genai import errors, types

from settings import config, secret

log = logging.getLogger("llm")
STATS: Counter = Counter()  # 호출·대체 횟수 (프로세스 메모리, /api/health에 노출). ponytail: 재시작하면 0, 오래 보려면 DB로
# LLM을 못 써서 규칙 기반으로 대신했을 때 사용자에게 보이는 유일한 안내 (내부 용어 없이 한 줄)
FALLBACK_NOTICE = "추천 이유를 간단히 표시했어요."
# 호출은 스레드에서 하고 질문 예산만큼만 기다린다(Gemini의 최소 기한 10초와 무관하게 예산을 지킨다).
# ponytail: 기다림을 포기한 호출은 HTTP 기한까지 스레드에 남는다, 동시 요청이 많아지면 async 클라이언트로
_POOL = ThreadPoolExecutor(max_workers=8)


class BudgetExceeded(TimeoutError):
    """질문 하나의 LLM 시간 예산이 모자라 호출하지 않았다."""


def error_cause(e: BaseException) -> str:
    """LLM 실패 원인 분류: timeout · 429 · auth(401·403) · model(404 모델 이름) · request(그 밖의 4xx) · 5xx · parse ·
    cooldown · budget · other (로그와 집계용)."""
    if isinstance(e, BudgetExceeded):
        return "budget"
    if isinstance(e, (json.JSONDecodeError, KeyError, TypeError)):
        return "parse"
    code = getattr(e, "code", None)
    if isinstance(code, int):
        if code == 429:
            return "429"
        if code in (401, 403):
            return "auth"
        if code == 404:
            return "model"
        if code >= 500:
            return "timeout" if code == 504 else "5xx"
        if code >= 400:
            return "request"
    text = f"{type(e).__name__} {e}".lower()
    if "timeout" in text or "timed out" in text or "응답 없음" in text:
        return "timeout"
    if "건너뜀" in str(e):
        return "cooldown"
    return "other"


def scrub(text: str, limit: int = 200) -> str:
    """로그용 응답 본문 앞부분: API 키 값과 key=·token= 형태는 가린다 (키는 로그에 절대 남기지 않는다)."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        text = text.replace(key, "***")
    return re.sub(r"(?i)(key|token|authorization)\s*[=:]\s*\S+", r"\1=***", text)[:limit]


def fallback(stage: str, e: BaseException) -> str:
    """단계(parser·agent·response)의 LLM 실패를 집계하고 원인·본문 앞부분을 로그에 남긴다. 원인을 돌려준다."""
    cause = error_cause(e)
    STATS[f"{stage}_fallback"] += 1
    STATS[f"{stage}_fail_{cause}"] += 1
    log.warning("%s LLM 실패 → 규칙 기반 cause=%s type=%s code=%s 본문=%s", stage, cause, type(e).__name__,
                getattr(e, "code", None), scrub(str(e)))
    return cause


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class LLMReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None  # 제공자 고유 응답(다음 턴에 그대로 돌려줘야 하는 정보 포함). 에이전트는 들여다보지 않는다


class LLMClient(Protocol):
    def chat(self, system: str, messages: list[dict], tools: list[dict] | None = None,
             deadline: float | None = None) -> LLMReply:
        """messages: {"role": "user", "text"} | {"role": "model", "reply": LLMReply} | {"role": "tool", "name", "result"}.
        tools: [{"name", "description", "parameters"(JSON schema)}]."""

    def json(self, system: str, prompt: str, schema: dict, deadline: float | None = None) -> dict:
        """schema를 따르는 JSON 응답. 같은 입력은 캐시에서 돌려준다. deadline(time.monotonic 기준)을 넘기지 않는다."""

    def describe_image(self, image: bytes, mime: str, prompt: str) -> str:
        """VLM(SDD §7.2 VLMClient): 이미지를 글로 묘사한다. 이미지는 저장·캐시하지 않는다."""


class GeminiClient:
    def __init__(self):
        self.cfg = config()["llm"]
        self.client = genai.Client(api_key=secret("GEMINI_API_KEY"),
                                   http_options=types.HttpOptions(timeout=self.cfg["timeout_s"] * 1000))
        self._cache: dict[str, tuple[float, dict]] = {}
        self._down_until = 0.0

    def _budget(self, start: float, deadline: float | None) -> float:
        """이번 호출에 쓸 수 있는 남은 초: 한 번의 호출 상한(total_timeout_s)과 질문 하나의 예산(deadline) 중 작은 쪽."""
        left = self.cfg["total_timeout_s"] - (time.monotonic() - start)
        # 질문 예산에서 LLM 뒤에 남은 일(응답 조립·전송)을 위해 reserve_s를 남긴다 → 전체 응답이 예산을 넘지 않게
        return min(left, deadline - time.monotonic() - self.cfg["reserve_s"]) if deadline else left

    def _generate(self, contents, conf: types.GenerateContentConfig, deadline: float | None = None):
        # 모든 모델이 방금 실패했으면 잠시 바로 실패시켜 규칙 기반 대체로 빨리 넘어가게 한다 (AI-REQ-02, NFR-001)
        if time.time() < self._down_until:
            raise RuntimeError("LLM 일시 장애로 호출을 건너뜀")
        last, start, retried = None, time.monotonic(), False
        models = list(self.cfg["models"])
        while models:
            model = models[0]
            remaining = self._budget(start, deadline)
            if remaining < self.cfg["min_wait_s"]:  # 시간이 모자람(느린 응답·질문 예산)은 장애가 아니다: 차단기 없이 이번만 건너뛴다
                if last is not None and error_cause(last) != "timeout":
                    break  # 앞 모델들이 429·5xx로 실패했다 → 아래에서 차단기
                STATS["llm_skipped_budget"] += 1
                raise BudgetExceeded(f"남은 시간 {remaining:.1f}초로 LLM 호출을 건너뜀"
                                     + (f" (앞 실패: {error_cause(last)})" if last else ""))
            wait = min(self.cfg["timeout_s"], remaining)
            # Gemini는 10초보다 짧은 HTTP 기한을 400으로 거부한다: 기한은 min_timeout_s 이상으로 주고, 기다리는 건 wait까지만
            conf.http_options = types.HttpOptions(timeout=int(max(self.cfg["min_timeout_s"], wait) * 1000))
            t = time.time()
            STATS["llm_calls"] += 1
            try:
                r = _POOL.submit(self.client.models.generate_content, model=model, contents=contents,
                                 config=conf).result(timeout=wait)
            except Exception as e:  # 429·5xx·타임아웃·연결 오류는 다음 모델로, 그 밖의 4xx(요청 오류)는 그대로 올린다
                if isinstance(e, TimeoutError) and not str(e):  # 기다림 포기(concurrent.futures)
                    e = TimeoutError(f"{model} {wait:.1f}초 안에 응답 없음")
                cause = error_cause(e)
                STATS[f"llm_fail_{cause}"] += 1
                if isinstance(e, errors.APIError) and e.code not in (429, 500, 503, 504):
                    log.warning("llm %s 요청 오류 cause=%s code=%s 본문=%s", model, cause, e.code, scrub(str(e)))
                    raise e
                last = e
                backoff = self.cfg["retry_429_backoff_s"]
                if cause in ("429", "5xx") and not retried and self._budget(start, deadline) - backoff >= self.cfg["min_wait_s"]:
                    # 한도 초과(429)·일시 과부하(503)는 잠깐 기다리면 풀리는 경우가 많고, 다음 모델은 더 느리다:
                    # 같은 모델로 한 번만 더 (지수 백오프 1단계)
                    retried = True
                    log.warning("llm %s %s, %.1f초 뒤 다시 시도 본문=%s", model, cause, backoff, scrub(str(e)))
                    time.sleep(backoff)
                    continue
                log.warning("llm %s 실패 cause=%s ms=%d 본문=%s, 다음 모델 시도", model, cause,
                            (time.time() - t) * 1000, scrub(str(e)))
                models.pop(0)
                continue
            u = r.usage_metadata
            log.info("llm model=%s in=%s out=%s ms=%d", model, u and u.prompt_token_count,
                     u and u.candidates_token_count, (time.time() - t) * 1000)  # 프롬프트 원문은 남기지 않는다
            return r
        if error_cause(last) != "timeout":  # 시도한 모델이 모두 429·5xx로 실패: 잠시 LLM을 건너뛴다 (타임아웃만이면 다음엔 다시 시도)
            self._down_until = time.time() + self.cfg["cooldown_s"]
        raise last

    def chat(self, system, messages, tools=None, deadline: float | None = None) -> LLMReply:
        contents, prev = [], None
        for m in messages:
            if m["role"] == "user":
                contents.append(types.Content(role="user", parts=[types.Part(text=m["text"])]))
            elif m["role"] == "model":
                contents.append(m["reply"].raw)  # thought signature 등을 보존하려고 원본 그대로
            else:  # 도구 결과: role은 user, 연속된 결과는 한 메시지로 묶는다
                part = types.Part.from_function_response(name=m["name"], response={"result": m["result"]})
                if prev == "tool":
                    contents[-1].parts.append(part)
                else:
                    contents.append(types.Content(role="user", parts=[part]))
            prev = m["role"]
        conf = types.GenerateContentConfig(
            system_instruction=system, temperature=self.cfg["temperature"],
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
                name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]) for t in tools])]
            if tools else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        r = self._generate(contents, conf, deadline)
        calls = [ToolCall(c.name, dict(c.args or {})) for c in (r.function_calls or [])]
        text = "".join(p.text or "" for p in r.candidates[0].content.parts if not p.function_call) if r.candidates else ""
        return LLMReply(text=text.strip(), tool_calls=calls, raw=r.candidates[0].content if r.candidates else None)

    def json(self, system, prompt, schema, deadline: float | None = None) -> dict:
        key = hashlib.sha256(json.dumps([self.cfg["models"], system, prompt, schema], ensure_ascii=False,
                                        sort_keys=True).encode()).hexdigest()
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cfg["cache_ttl_s"]:
            return hit[1]
        r = self._generate(prompt, types.GenerateContentConfig(
            system_instruction=system, temperature=self.cfg["temperature"],
            response_mime_type="application/json", response_json_schema=schema), deadline)
        try:
            out = json.loads(r.text)
        except (json.JSONDecodeError, TypeError):
            log.warning("llm JSON 파싱 실패 본문=%s", scrub(r.text or ""))
            raise
        self._cache[key] = (time.time(), out)  # ponytail: 프로세스 메모리 캐시, 여러 워커면 Redis 등으로
        return out

    def describe_image(self, image, mime, prompt) -> str:
        r = self._generate([types.Part.from_bytes(data=image, mime_type=mime), prompt],
                           types.GenerateContentConfig(temperature=self.cfg["temperature"]))
        return (r.text or "").strip()


@lru_cache
def get_llm() -> LLMClient:
    provider = os.environ.get("LLM_PROVIDER", "gemini")
    if provider == "gemini":
        return GeminiClient()
    raise ValueError(f"지원하지 않는 LLM_PROVIDER: {provider}")
