import pytest
from fastapi.testclient import TestClient

import settings
from api.main import app


def test_health():  # API-008: DB·벡터 상태를 함께 돌려준다
    body = TestClient(app).get("/api/health").json()
    assert body["status"] in ("ok", "degraded") and {"db", "vector"} <= set(body)


def test_config_has_draft_values():
    cfg = settings.config()
    assert cfg["score"]["w_sim"] + cfg["score"]["w_pref"] + cfg["score"]["w_weather"] == pytest.approx(0.9)
    assert cfg["image"]["match_threshold"] == 0.80
    assert cfg["agent"]["max_tool_calls_per_turn"] == 6


def test_secret_missing_names_variable_only(monkeypatch):
    monkeypatch.delenv("BUSANMATE_TEST_MISSING", raising=False)
    with pytest.raises(RuntimeError, match="BUSANMATE_TEST_MISSING"):
        settings.secret("BUSANMATE_TEST_MISSING")
