"""공유 링크·저장한 코스 (FR-012): DB에 저장하고 id로 읽기 전용 열람, 세션으로 불러오기. 실제 PostgreSQL이 필요하다."""
import uuid
from datetime import date

import pytest

import db
from agent import course_ops, session
from services.search import Conditions

try:
    db.connect().close()
except Exception:
    pytest.skip("PostgreSQL 없음", allow_module_level=True)


def test_share_then_load_readonly_and_into_session(monkeypatch):
    monkeypatch.setattr(course_ops, "fetch_places", lambda ids: {})
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())
    s = session.get(sid)
    course = {"days": [{"day": 1, "rain_prob": None, "start": "10:00", "items": [{"place_id": 1, "name": "A"}]}]}
    s["last_course"], s["last_conditions"], s["last_dates"] = course, Conditions(query="x", gu=["중구"]), [date(2026, 11, 14)]
    share_id = course_ops.share(sid)
    payload = course_ops.load(share_id)
    assert payload["course"] == course and payload["dates"] == ["2026-11-14"] and payload["conditions"]["gu"] == ["중구"]
    assert "message" not in payload  # 질문 원문은 저장하지 않는다
    course_ops.load_into_session(other, share_id)
    assert session.get(other)["last_course"] == course and session.get(other)["last_dates"] == [date(2026, 11, 14)]
    assert course_ops.load("nope1234") is None
