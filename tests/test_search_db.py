"""FR-002 하이브리드 검색: 적재된 실제 데이터로 필터·조건 완화를 확인한다. 질의 벡터는 가짜(Gemini 호출 없음)."""
import pytest

import db
from services.search import Conditions, search

try:
    db.connect().close()
    assert db.collection("place_text").count() > 0
except Exception:
    pytest.skip("PostgreSQL·Chroma 또는 적재 데이터 없음", allow_module_level=True)


def fake_embed(texts, task):
    return [[0.01] * 768 for _ in texts]


def test_filters_are_applied():
    hits, notices = search(Conditions(query="박물관", gu=["영도구"], categories=["문화시설"]), embed=fake_embed)
    assert hits and not notices
    assert all(p["gu"] == "영도구" and p["category"] == "문화시설" for p in hits)
    assert all(p["license"] and p["lat"] for p in hits)  # 출처·좌표는 DB에서 채운다


def test_relaxes_indoor_then_gu():
    hits, notices = search(Conditions(query="해변", categories=["해변"], indoor=True), embed=fake_embed)
    assert len(hits) >= 2 and all(p["category"] == "해변" for p in hits)
    assert any("실내/실외" in n for n in notices)

    hits, notices = search(Conditions(query="해변", gu=["연제구"], categories=["해변"]), embed=fake_embed)
    assert len(hits) >= 2 and any("부산 전체" in n for n in notices)
