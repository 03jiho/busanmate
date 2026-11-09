"""TC-010 (FR-010): 방문 기간과 겹치는 축제만 돌려주고, 기간 밖 행사는 돌려주지 않는다. DB가 없으면 skip."""
from datetime import date, timedelta

import pytest

import db
import tools  # noqa: F401
from tools.base import REGISTRY

try:
    with db.connect() as conn:
        START, END = conn.execute("SELECT start_date, end_date FROM festival ORDER BY start_date DESC LIMIT 1").fetchone()
except Exception:
    pytest.skip("PostgreSQL 또는 축제 데이터 없음", allow_module_level=True)


def festivals(dates):
    return REGISTRY["get_festivals"].run({}, {"dates": dates}).updates["festivals"]


def test_returns_only_overlapping_festivals():
    found = festivals([START])
    assert found and all(f["start_date"] <= START <= f["end_date"] for f in found)


def test_out_of_period_returns_nothing():
    far = END + timedelta(days=3650)
    assert festivals([far, far + timedelta(days=1)]) == []
    assert festivals([date(2000, 1, 1)]) == []
