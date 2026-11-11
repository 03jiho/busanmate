"""API 서버 (COMP-02, SDD §6). 실행: uvicorn api.main:app --reload
요청 검증, 세션 식별, 관리자 토큰, 요청 한도, 오류 응답 표준화."""
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import requests
from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

import db
from agent import course_ops, session
from agent.orchestrator import handle_chat, handle_chat_stream
from agent.query_parser import themes
from pipeline import run as pipeline
from services import image
from settings import config, secret

log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app):
    # CLIP 모델을 미리 불러 첫 사진 검색이 늦지 않게 한다 (NFR-002). 서버 시작은 막지 않는다
    threading.Thread(target=image._clip, daemon=True).start()
    try:  # 공유 코스 테이블: 이미 떠 있는 DB(초기화 스크립트가 다시 돌지 않음)에도 만든다
        course_ops.ensure_table()
    except Exception as e:
        log.warning("shared_course 테이블 준비 실패: %s", type(e).__name__)
    yield


app = FastAPI(title="BusanMate API", lifespan=lifespan)
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


@app.exception_handler(requests.Timeout)
@app.exception_handler(httpx.TimeoutException)
async def _upstream_timeout(request, e: Exception):
    log.warning("외부 API 시간 초과 path=%s %s", request.url.path, type(e).__name__)  # ERR-005·006: URL(키 포함)은 남기지 않는다
    return _error(504, "UPSTREAM_TIMEOUT", "외부 서비스 응답이 늦어 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.")


@app.exception_handler(requests.RequestException)
@app.exception_handler(httpx.HTTPError)
@app.exception_handler(genai_errors.APIError)
async def _upstream_error(request, e: Exception):
    log.warning("외부 API 오류 path=%s %s", request.url.path, type(e).__name__)
    return _error(502, "UPSTREAM_ERROR", "외부 서비스 오류로 최신 정보를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.")


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
class EditIn(BaseModel):
    """화면의 "코스 고치기" 버튼: 문장 해석 없이 직전 코스를 제자리에서 고친다 (FR-012)."""
    kind: Literal["cafe", "indoor", "shorter"]
    day: int | None = Field(default=None, ge=1, le=3)


class ChatIn(BaseModel):
    session_id: str = Field(pattern=SESSION_ID)
    message: str = Field(min_length=1, max_length=config()["api"]["max_message_chars"])
    edit: EditIn | None = None


@app.post("/api/chat")
def chat(body: ChatIn):
    _rate_limit(body.session_id)
    log.info("chat session=%s", hashlib.sha256(body.session_id.encode()).hexdigest()[:10])
    if body.edit:
        return handle_chat(body.session_id, body.message.strip(), edit=body.edit.model_dump())
    return handle_chat(body.session_id, body.message.strip())


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn):
    """API-001 스트리밍(NDJSON): 진행 단계 {stage, phase, t}를 차례로 보내고 마지막 줄에 {result, t}(/api/chat과 같은 결과).
    화면이 "조건 해석 → 장소 검색 → 날씨 → 동선 계산 → 요약"을 실제 진행대로 보여 주는 데 쓴다."""
    _rate_limit(body.session_id)
    log.info("chat stream session=%s", hashlib.sha256(body.session_id.encode()).hexdigest()[:10])
    edit = body.edit.model_dump() if body.edit else None

    def lines():
        try:
            for ev in handle_chat_stream(body.session_id, body.message.strip(), edit=edit):
                yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"
        except Exception as e:  # 스트림 도중 오류: 상세는 서버 로그에만 (ERR-008)
            log.exception("chat stream 실패: %s", type(e).__name__)
            err = {"error": {"code": "INTERNAL", "message": "응답을 만드는 중 문제가 생겼습니다. 다시 시도해 주세요."}}
            yield json.dumps(err, ensure_ascii=False) + "\n"
    return StreamingResponse(lines(), media_type="application/x-ndjson")


# --- API-002 (FR-008, FR-009, NFR-006): 사진은 메모리에서만 처리하고 저장·로그하지 않는다 ---
@app.post("/api/search/image")
async def search_image(session_id: str = Form(pattern=SESSION_ID), file: UploadFile = File(...)):
    _rate_limit(session_id)
    data = await file.read(config()["image"]["max_bytes"] + 1)  # 한도+1까지만 읽어 초과 여부만 안다
    await file.close()
    log.info("image upload bytes=%d type=%s", len(data), file.content_type)  # ERR-003: 크기·형식만
    try:
        return await run_in_threadpool(image.search_by_image, data)
    except image.ImageError as e:
        raise ApiError(e.status, e.code, e.message)


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


# --- 코스 직접 편집·공유 (FR-012): 채팅을 거치지 않고 세션의 직전 코스를 바로 고친다 ---
class OrderIn(BaseModel):
    days: list[list[int]] = Field(min_length=1, max_length=3)


def _edit(fn, *args):
    try:
        return fn(*args)
    except course_ops.EditError as e:
        raise ApiError(400, "INVALID_INPUT", str(e))


@app.put("/api/session/{session_id}/course")
def put_course(session_id: str, body: OrderIn):
    """드래그 순서·빼기·교체 결과(날짜별 place_id 순서) → 이동 시간을 다시 계산한 코스 {course, sources, notices}."""
    _check_session(session_id)
    _rate_limit(session_id)
    return _edit(course_ops.apply_order, session_id, body.days)


@app.get("/api/session/{session_id}/alternatives")
def get_alternatives(session_id: str, day: int, index: int):
    """'다른 곳으로 교체' 후보 3곳(같은 분류, 동선상 덜 돌아가는 순)."""
    _check_session(session_id)
    return {"alternatives": _edit(course_ops.alternatives, session_id, day, index)}


@app.post("/api/session/{session_id}/share")
def post_share(session_id: str):
    """세션의 직전 코스를 저장하고 공유 id를 돌려준다(/?course=<id>로 읽기 전용 열람)."""
    _check_session(session_id)
    _rate_limit(session_id)
    return {"share_id": _edit(course_ops.share, session_id)}


@app.get("/api/course/{share_id}")
def get_shared(share_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,16}", share_id) or not (payload := course_ops.load(share_id)):
        raise ApiError(404, "NOT_FOUND", "코스를 찾지 못했어요. 링크를 다시 확인해 주세요.")
    return payload


@app.post("/api/session/{session_id}/load/{share_id}")
def post_load(session_id: str, share_id: str):
    """저장한 코스를 이 세션의 직전 코스로 불러온다(불러온 뒤에도 편집할 수 있게)."""
    _check_session(session_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,16}", share_id) or not (payload := course_ops.load_into_session(session_id, share_id)):
        raise ApiError(404, "NOT_FOUND", "코스를 찾지 못했어요.")
    return payload


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
