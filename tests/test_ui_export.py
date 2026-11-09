"""UI 일정 계산·공유 텍스트·PDF (COMP-01). 지도 타일은 받지 않는다(requests.get을 막음)."""
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
import export  # noqa: E402

DAY = {"day": 1, "start": "10:00", "items": [
    {"order": 1, "place_id": 1, "name": "해운대", "category": "해변", "gu": "해운대구", "lat": 35.158, "lng": 129.160,
     "stay_min": 60, "reason": "바다", "leg_to_next": {"mode": "walk", "minutes": 15, "estimated": True}},
    {"order": 2, "place_id": 2, "name": "동백섬", "category": "공원·자연", "gu": "해운대구", "lat": 35.153, "lng": 129.152,
     "stay_min": 90, "reason": None, "leg_to_next": {"mode": "taxi", "minutes": 20, "estimated": False, "taxi_fare": 9800}},
    {"order": 3, "place_id": 3, "name": "광안리", "category": "해변", "gu": "수영구", "lat": 35.153, "lng": 129.118,
     "stay_min": 60, "reason": None, "leg_to_next": None}]}
RESULT = {"course": {"days": [DAY]}, "dates": ["2026-11-14"],
          "sources": [{"place_id": 1, "label": "부산명소", "url": None, "license": "제한 없음"},
                      {"label": "기상청 단기예보", "url": None, "license": "공공누리 제1유형"}]}


def test_schedule_times_and_totals():
    s = export.schedule(DAY)
    assert s["times"] == [("10:00", "11:00"), ("11:15", "12:45"), ("13:05", "14:05")]
    assert (s["end"], s["total"], s["move"], s["count"]) == ("14:05", 245, 35, 3)


def test_share_text_and_pdf(monkeypatch):
    def no_network(*a, **k):
        raise requests.ConnectionError

    monkeypatch.setattr(export.requests, "get", no_network)
    text = export.share_text(RESULT)
    assert "11월 14일(토)" in text and "11:15 2. 동백섬" in text and "공공누리 제1유형" in text
    assert export.course_pdf(RESULT).startswith(b"%PDF")


@pytest.mark.parametrize("m,out", [(45, "45분"), (60, "1시간"), (245, "4시간 5분")])
def test_hm(m, out):
    assert export.hm(m) == out
