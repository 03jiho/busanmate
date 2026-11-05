"""API 서버 (COMP-02, SDD §6). 실행: uvicorn api.main:app --reload
요청 검증, 세션 식별, 관리자 토큰, 요청 한도, 오류 응답 표준화. API-002(사진)는 5단계."""
import hashlib
import hmac
import logging
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Literal

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

import db
from agent import session
from agent.orchestrator import handle_chat
from agent.query_parser import themes
from pipeline import run as pipeline
from settings import config, secret

log = logging.getLogger("api")
app = FastAPI(title="BusanMate API")
SESSION_ID = r"^[A-Za-z0-9_-]{8,64}$"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(ApiError)
async def _api_error(request, e: ApiError):
    return _error(e.status, e.code, e.message)


@app.exception_handler(RequestValidationError)
async def _invalid(request, e: RequestValidationError):
    fields = sorted({".".join(str(x) for x in err["loc"][1:]) for err in e.errors()})  # 값은 응답·로그에 넣지 않는다
    return _error(400, "INVALID_INPUT", f"입력값을 확인해 주세요: {', '.join(fields)}")


@app.exception_handler(HTTPException)
async def _http(request, e: HTTPException):
    return _error(e.status_code, "NOT_FOUND" if e.status_code == 404 else "HTTP_ERROR", str(e.detail))


@app.exception_handler(Exception)
async def _internal(request, e: Exception):
    log.exception("처리되지 않은 오류 path=%s", request.url.path)  # ERR-009: 상세는 서버 로그에만
    return _error(500, "INTERNAL", "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")


@app.middleware("http")
async def _access_log(request: Request, call_next):
    rid, t = uuid.uuid4().hex[:8], time.time()
    response = await call_next(request)
    log.info("req=%s %s %s %s %dms", rid, request.method, request.url.path, response.status_code,
             (time.time() - t) * 1000)  # 본문(질의·사진)은 남기지 않는다
    response.headers["X-Request-ID"] = rid
    return response


# --- 요청 한도: 세션당 분당 N회 (ERR-004) ---
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def _rate_limit(session_id: str) -> None:
    now, limit = time.time(), config()["api"]["rate_limit_per_min"]
    with _hits_lock:
        q = _hits[session_id]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            raise ApiError(429, "RATE_LIMITED", "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
        q.append(now)


def _check_session(session_id: str) -> None:
    if not re.fullmatch(SESSION_ID, session_id):
        raise ApiError(400, "INVALID_INPUT", "세션 ID 형식이 올바르지 않습니다.")


# --- API-001 ---
class ChatIn(BaseModel):
    session_id: str = Field(pattern=SESSION_ID)
    message: str = Field(min_length=1, max_length=config()["api"]["max_message_chars"])


@app.post("/api/chat")
def chat(body: ChatIn):
    _rate_limit(body.session_id)
    log.info("chat session=%s", hashlib.sha256(body.session_id.encode()).hexdigest()[:10])
    return handle_chat(body.session_id, body.message.strip())


# --- API-003, 004 (FR-005) ---
class Preferences(BaseModel):
    theme: str | None = None
    mode: Literal["auto", "walk", "taxi"] | None = None
    companion: str | None = Field(default=None, max_length=20)


@app.get("/api/session/{session_id}/preferences")
def get_preferences(session_id: str):
    _check_session(session_id)
    return Preferences(**session.get(session_id)["preferences"]).model_dump()


@app.put("/api/session/{session_id}/preferences")
def put_preferences(session_id: str, body: Preferences):
    _check_session(session_id)
    if body.theme is not None and body.theme not in themes():
        raise ApiError(400, "INVALID_INPUT", f"테마는 {', '.join(themes())} 중 하나여야 합니다.")
    session.get(session_id)["preferences"] = body.model_dump(exclude_none=True)
    return {"ok": True}


# --- API-005 ---
@app.get("/api/session/{session_id}/course")
def get_course(session_id: str):
    _check_session(session_id)
    return {"course": session.get(session_id)["last_course"]}


# --- API-006, 007 (FR-013): 관리자 수집 배치 ---
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _require_admin(token: str | None) -> None:
    if not token or not hmac.compare_digest(token.encode(), secret("ADMIN_TOKEN").encode()):
        log.warning("관리자 인증 실패")  # ERR-002: 토큰은 기록하지 않는다
        raise ApiError(401, "UNAUTHORIZED", "관리자 권한이 필요합니다.")


class IngestIn(BaseModel):
    sources: list[str] | None = None


def _run_job(run_id: str, sources: list[str] | None) -> None:
    try:
        results = pipeline.run(sources)
        status = "done" if all(r["status"] == "ok" for r in results) else "partial"
    except Exception as e:
        log.warning("수집 실패: %s", type(e).__name__)
        results, status = [], "failed"
    with _jobs_lock:
        _jobs[run_id].update(status=status, results=results, finished_at=time.time())


@app.post("/api/admin/ingest")
def start_ingest(body: IngestIn | None = None, x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    sources = body.sources if body else None
    unknown = [s for s in sources or [] if s not in config()["sources"]]
    if unknown:
        raise ApiError(400, "INVALID_INPUT", f"알 수 없는 소스: {', '.join(unknown)}")
    with _jobs_lock:
        if any(j["status"] == "running" for j in _jobs.values()):
            raise ApiError(409, "CONFLICT", "이미 수집이 진행 중입니다.")
        run_id = uuid.uuid4().hex
        _jobs[run_id] = {"status": "running", "results": [], "started_at": time.time()}
    threading.Thread(target=_run_job, args=(run_id, sources), daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/admin/ingest/{run_id}")
def ingest_status(run_id: str, x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    job = _jobs.get(run_id)
    if not job:
        raise ApiError(404, "NOT_FOUND", "수집 작업을 찾을 수 없습니다.")
    rs = job["results"]
    return {"status": job["status"], "new": sum(r["new"] for r in rs), "updated": sum(r["updated"] for r in rs),
            "skipped": sum(r["skipped"] for r in rs), "results": rs}


# --- API-008 (NFR-003) ---
@app.get("/api/health")
def health():
    checks = {}
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1")
        checks["db"] = True
    except Exception:
        checks["db"] = False
    try:
        checks["vector"] = db.collection("place_text").count() > 0
    except Exception:
        checks["vector"] = False
    return {"status": "ok" if all(checks.values()) else "degraded", **checks}
