"""LLM 장애 대응 (AI-REQ-02, NFR-001): 폴백 모델까지 합친 총 대기 상한, 전부 실패 시 차단기."""
import time

import pytest
from google.genai import errors, types

from services import llm


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    c = llm.GeminiClient()
    c.cfg = {**c.cfg, "timeout_s": 0.4, "total_timeout_s": 0.6, "cooldown_s": 60}
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
