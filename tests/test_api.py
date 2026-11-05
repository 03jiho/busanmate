"""API (SDD §6): 오류 형식, 요청 한도, 선호, 관리자 수집. 에이전트·파이프라인은 가짜로 바꾼다."""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from api import main

client = TestClient(main.app)


@pytest.fixture
def sid():
    return uuid.uuid4().hex


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(main, "handle_chat", lambda s, m: {"answer": "ok", "course": None, "sources": [], "notices": []})
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(main.pipeline, "run", lambda sources: [
        {"source": s, "status": "ok", "new": 1, "updated": 2, "skipped": 0, "message": None} for s in sources or ["a"]])
    main._jobs.clear()


def test_chat_and_validation_error_format(sid):
    assert client.post("/api/chat", json={"session_id": sid, "message": "해운대"}).json()["answer"] == "ok"
    r = client.post("/api/chat", json={"session_id": "x", "message": ""})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_INPUT"
    assert "message" in r.json()["error"]["message"] and "session_id" in r.json()["error"]["message"]


def test_rate_limit(sid):
    for _ in range(20):
        client.post("/api/chat", json={"session_id": sid, "message": "a"})
    r = client.post("/api/chat", json={"session_id": sid, "message": "a"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"


def test_preferences(sid):
    assert client.put(f"/api/session/{sid}/preferences", json={"theme": "바다", "mode": "walk"}).json() == {"ok": True}
    assert client.get(f"/api/session/{sid}/preferences").json() == {"theme": "바다", "mode": "walk", "companion": None}
    assert client.put(f"/api/session/{sid}/preferences", json={"theme": "우주"}).status_code == 400
    assert client.put(f"/api/session/{sid}/preferences", json={"mode": "bus"}).status_code == 400
    assert client.get(f"/api/session/{sid}/course").json() == {"course": None}
    assert client.get("/api/session/bad!/course").status_code == 400


def test_admin_requires_token():
    assert client.post("/api/admin/ingest").status_code == 401
    r = client.post("/api/admin/ingest", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"


def test_admin_ingest_flow():
    h = {"X-Admin-Token": "test-admin-token"}
    assert client.post("/api/admin/ingest", json={"sources": ["nope"]}, headers=h).status_code == 400
    run_id = client.post("/api/admin/ingest", json={"sources": ["busan_food"]}, headers=h).json()["run_id"]
    for _ in range(50):
        body = client.get(f"/api/admin/ingest/{run_id}", headers=h).json()
        if body["status"] != "running":
            break
        time.sleep(0.05)
    assert (body["status"], body["new"], body["updated"], body["skipped"]) == ("done", 1, 2, 0)
    assert client.get("/api/admin/ingest/unknown", headers=h).json()["error"]["code"] == "NOT_FOUND"


def test_unknown_path_uses_error_format():
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
