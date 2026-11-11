"""API (SDD §6): 오류 형식, 요청 한도, 선호, 관리자 수집. 에이전트·파이프라인은 가짜로 바꾼다."""
import time
import uuid

import pytest
import requests
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


@pytest.mark.parametrize("exc, status, code", [
    (requests.Timeout("t"), 504, "UPSTREAM_TIMEOUT"),
    (requests.ConnectionError("c"), 502, "UPSTREAM_ERROR"),
    (RuntimeError("bug"), 500, "INTERNAL"),
])
def test_upstream_failures_map_to_502_504(monkeypatch, sid, exc, status, code):
    def boom(s, m):
        raise exc
    monkeypatch.setattr(main, "handle_chat", boom)
    r = TestClient(main.app, raise_server_exceptions=False).post("/api/chat", json={"session_id": sid, "message": "a"})
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_unknown_path_uses_error_format():
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_chat_stream_ndjson(sid, monkeypatch):
    """API-001 스트리밍: 단계 이벤트를 줄마다 보내고 마지막 줄이 결과. 도중 오류는 표준 오류 한 줄(상세는 로그에만)."""
    import json as _json

    def fake_stream(s, m, edit=None):
        yield {"stage": "parse", "phase": "start", "t": 0.0}
        yield {"stage": "parse", "phase": "done", "t": 0.5}
        yield {"result": {"answer": "ok", "intent": "search"}, "t": 0.6}
    monkeypatch.setattr(main, "handle_chat_stream", fake_stream)
    r = client.post("/api/chat/stream", json={"session_id": sid, "message": "해운대"})
    lines = [_json.loads(x) for x in r.text.splitlines() if x]
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert [x.get("stage") for x in lines[:2]] == ["parse", "parse"] and lines[-1]["result"]["answer"] == "ok"

    def boom(s, m, edit=None):
        yield {"stage": "parse", "phase": "start", "t": 0.0}
        raise RuntimeError("secret detail")
    monkeypatch.setattr(main, "handle_chat_stream", boom)
    lines = [_json.loads(x) for x in client.post("/api/chat/stream", json={"session_id": sid, "message": "a"}).text.splitlines()]
    assert lines[-1]["error"]["code"] == "INTERNAL" and "secret" not in lines[-1]["error"]["message"]
