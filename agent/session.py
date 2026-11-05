"""SessionStore (COMP-10, FR-005, FR-012): 서버 메모리 dict, TTL. DB에 저장하지 않는다 (ADR-007, NFR-006)."""
import threading
import time

from settings import config

_lock = threading.Lock()
_store: dict[str, dict] = {}


def get(session_id: str) -> dict:
    """세션 상태 dict(같은 객체를 돌려주므로 직접 수정한다). 만료된 세션은 이때 정리한다."""
    now, ttl = time.time(), config()["session"]["ttl_s"]
    with _lock:
        for k in [k for k, s in _store.items() if now - s["touched"] > ttl]:
            del _store[k]
        s = _store.setdefault(session_id, {"preferences": {}, "last_course": None, "last_conditions": None,
                                           "pending": None})
        s["touched"] = now
        return s
