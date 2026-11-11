"""AC-007: 수집 2회 연속 실행 시 중복 0건, ingest_log 생성. PostgreSQL이 없으면 skip."""
import pytest

import db
from pipeline import run as pipeline
from settings import config

try:
    db.connect().close()
except Exception:
    pytest.skip("PostgreSQL 연결 불가 (docker compose up db)", allow_module_level=True)

SRC = "test_src"
ROWS = [{"UC_SEQ": i, "MAIN_TITLE": f"테스트공원{i}", "GUGUN_NM": "중구", "LAT": 35.1, "LNG": 129.0,
         "ADDR1": "부산 중구", "ITEMCNTNTS": "설명", "MAIN_IMG_NORMAL": f"https://example.org/{i}.jpg"} for i in range(3)]


class FakeCollection:
    def __init__(self):
        self.ids = set()

    def upsert(self, ids, **_):
        self.ids |= set(ids)

    def delete(self, ids):
        self.ids -= set(ids)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setitem(config()["sources"], SRC, {**config()["sources"]["busan_attraction"]})
    rows = list(ROWS)
    coll = FakeCollection()
    calls = []
    monkeypatch.setattr(pipeline.busan, "fetch", lambda source: rows)
    monkeypatch.setattr(pipeline.db, "collection", lambda name: coll)
    monkeypatch.setattr(pipeline, "embed_texts", lambda docs: calls.append(len(docs)) or [[0.0] * 3 for _ in docs])
    monkeypatch.setattr(pipeline, "_embed_images", lambda: {"source": "place_image", "status": "ok", "new": 0,
                                                            "updated": 0, "skipped": 0, "message": None})
    with db.connect() as conn:
        start = conn.execute("SELECT coalesce(max(log_id), 0) FROM ingest_log").fetchone()[0]
    yield rows, coll, calls
    with db.connect() as conn:
        conn.execute("DELETE FROM place WHERE source = %s", (SRC,))
        conn.execute("DELETE FROM ingest_log WHERE log_id > %s AND source IN (%s, 'place_image')", (start, SRC))


def _count():
    with db.connect() as conn:
        return conn.execute("SELECT count(*), count(*) FILTER (WHERE is_active) FROM place WHERE source = %s",
                            (SRC,)).fetchone()


def test_rerun_is_idempotent(env):
    rows, coll, calls = env
    assert pipeline.run([SRC])[0] | {"message": None} == \
        {"source": SRC, "status": "ok", "new": 3, "updated": 0, "skipped": 0, "message": None}
    second = pipeline.run([SRC])[0]
    assert (second["new"], second["updated"]) == (0, 0)
    assert _count() == (3, 3) and calls == [3]  # 두 번째 실행은 임베딩 호출 없음

    rows.pop()  # 원본에서 사라진 장소는 비활성화되고 벡터도 지운다
    pipeline.run([SRC])
    assert _count() == (3, 2) and len(coll.ids) == 2
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM ingest_log WHERE source = %s", (SRC,)).fetchone()[0] == 3


def test_seed_mode_is_idempotent_and_never_deactivates(env, monkeypatch):
    seed = pipeline.busan.load_seed("busan_attraction")  # 실제 시드 CSV 행을 테스트 소스로 적재
    monkeypatch.setattr(pipeline.busan, "load_seed", lambda source: seed)
    first = pipeline.run([SRC], seed=True)[0]
    assert first["status"] == "ok" and first["new"] == len(seed) and "시드" in first["message"]
    assert pipeline.run([SRC], seed=True)[0]["new"] == 0
    seed.pop()  # 시드는 일부만 담고 있으므로 빠진 행이 있어도 비활성화하지 않는다
    pipeline.run([SRC], seed=True)
    assert _count() == (len(seed) + 1, len(seed) + 1)
