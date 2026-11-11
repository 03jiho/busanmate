"""LLM 장애 대응 (AI-REQ-02, NFR-001): 폴백 모델까지 합친 총 대기 상한, 전부 실패 시 차단기."""
import time

import pytest
from google.genai import errors, types

from services import llm


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    c = llm.GeminiClient()
    c.cfg = {**c.cfg, "timeout_s": 0.4, "min_timeout_s": 0.1, "total_timeout_s": 0.6, "cooldown_s": 60,
             "retry_429_backoff_s": 0.05, "reserve_s": 0}
    return c


def test_total_wait_is_capped_across_fallback_models(client):
    calls = []

    def slow(model, contents, config):
        calls.append(model)
        if len(calls) == 1:
            raise errors.ClientError(429, {})  # 한도 초과 → 다음 모델
        time.sleep(config.http_options.timeout / 1000)  # 타임아웃까지 매달림
        raise TimeoutError("read timeout")

    client.client.models.generate_content = slow
    t = time.time()
    with pytest.raises(TimeoutError):
        client._generate("x", types.GenerateContentConfig())
    assert time.time() - t < 0.9  # 모델 수 × 타임아웃(1.2초)이 아니라 총 상한(0.6초) 근처에서 끝난다
    assert calls[0] == calls[1]  # 429는 잠깐 기다렸다가 같은 모델로 한 번 더(지수 백오프 1단계)

    t = time.time()
    with pytest.raises(RuntimeError):  # 차단기: 다음 호출은 기다리지 않고 바로 실패 → 규칙 기반으로
        client._generate("x", types.GenerateContentConfig())
    assert time.time() - t < 0.05 and len(calls) <= 3


def test_request_errors_are_not_retried(client):
    calls = []

    def bad(model, contents, config):
        calls.append(model)
        raise errors.ClientError(400, {})

    client.client.models.generate_content = bad
    with pytest.raises(errors.ClientError):
        client._generate("x", types.GenerateContentConfig())
    assert calls == [client.cfg["models"][0]]  # 요청 자체가 잘못된 400은 다른 모델로 넘기지 않는다


def test_429_retry_once_then_success(client):
    """429(한도)면 백오프 후 같은 모델로 한 번 더 시도해 성공하면 그 응답을 쓴다."""
    calls = []

    class Ok:
        usage_metadata = None

    def flaky(model, contents, config):
        calls.append(model)
        if len(calls) == 1:
            raise errors.ClientError(429, {})
        return Ok()

    client.client.models.generate_content = flaky
    assert isinstance(client._generate("x", types.GenerateContentConfig()), Ok)
    assert calls == [client.cfg["models"][0]] * 2


def test_request_budget_skips_call(client):
    """질문 하나의 시간 예산이 모자라면 호출하지 않고 바로 실패 → 규칙 기반 (전체 응답 시간 상한)."""
    client.client.models.generate_content = lambda **kw: pytest.fail("호출하면 안 된다")
    with pytest.raises(llm.BudgetExceeded):
        client._generate("x", types.GenerateContentConfig(), deadline=time.monotonic() + 0.05)
    assert llm.error_cause(llm.BudgetExceeded("x")) == "budget"


@pytest.mark.parametrize("err, cause", [(errors.ClientError(429, {}), "429"), (TimeoutError("read timeout"), "timeout"),
                                        (ValueError("x"), "other"), (KeyError("answer"), "parse")])
def test_error_cause(err, cause):
    assert llm.error_cause(err) == cause
