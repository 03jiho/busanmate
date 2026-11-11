"""사진 탐색 (FR-008, FR-009, NFR-006, API-002): CLIP·Chroma·VLM·DB는 가짜로 바꿔 판정 로직만 검증한다."""
import io
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import main
from services import image


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(buf, "JPEG")
    return buf.getvalue()


PLACES = {i: {"place_id": i, "source": "busan_attraction", "name": f"장소{i}", "category": "해변", "gu": "중구",
              "lat": 35.1, "lng": 129.0, "url": None, "license": "공공데이터"} for i in range(1, 10)}


@pytest.fixture
def fake(monkeypatch):
    state = {"distances": [], "metas": []}
    coll = SimpleNamespace(query=lambda **kw: {"distances": [state["distances"]], "metadatas": [state["metas"]]})
    monkeypatch.setattr(image, "embed_images", lambda imgs: [[0.0] * 4 for _ in imgs])
    monkeypatch.setattr(image.db, "collection", lambda name: coll)
    monkeypatch.setattr(image, "fetch_places", lambda ids: {i: PLACES[i] for i in ids if i in PLACES})
    monkeypatch.setattr(image, "search", lambda cond, n=None: ([{"place_id": 7, "sim": 0.6}, {"place_id": 8, "sim": 0.5}], []))
    return state


def test_validate():
    assert image.validate(jpeg()) == "image/jpeg"
    assert image.validate(b"\x89PNG\r\n\x1a\n" + b"0" * 10) == "image/png"
    with pytest.raises(image.ImageError) as e:
        image.validate(b"GIF89a....")
    assert e.value.status == 415
    with pytest.raises(image.ImageError) as e:
        image.validate(b"\xff\xd8\xff" + b"0" * (10 * 1024 * 1024))
    assert e.value.status == 413


def test_aggregate_takes_max_per_place():
    ranked = image.aggregate([0.1, 0.3, 0.2, 0.5], [{"place_id": 1}, {"place_id": 2}, {"place_id": 1}, {"place_id": 3}])
    assert ranked == [(1, 0.9), (2, 0.7), (3, 0.5)]


def test_recognized_when_above_threshold(fake):
    fake.update(distances=[0.1, 0.15, 0.4], metas=[{"place_id": 1}, {"place_id": 1}, {"place_id": 2}])
    r = image.search_by_image(jpeg())
    assert r["mode"] == "recognized" and [m["place_id"] for m in r["matches"]] == [1, 2]
    assert r["matches"][0]["similarity"] == 0.9 and r["sources"][0]["license"]


def test_similar_mood_uses_vlm_description(fake, monkeypatch):
    fake.update(distances=[0.5], metas=[{"place_id": 1}])
    monkeypatch.setattr(image, "get_llm", lambda: SimpleNamespace(describe_image=lambda d, m, p: "노을 지는 바닷가"))
    r = image.search_by_image(jpeg())
    assert r["mode"] == "similar" and r["description"] == "노을 지는 바닷가"
    assert [m["place_id"] for m in r["matches"]] == [7, 8]


def test_vlm_failure_falls_back_to_clip(fake, monkeypatch):
    fake.update(distances=[0.5, 0.6], metas=[{"place_id": 3}, {"place_id": 4}])

    def boom(*a):
        raise RuntimeError("down")
    monkeypatch.setattr(image, "get_llm", lambda: SimpleNamespace(describe_image=boom))
    r = image.search_by_image(jpeg())
    assert r["mode"] == "similar" and [m["place_id"] for m in r["matches"]] == [3, 4] and r["notices"]


def test_api_rejects_bad_files(monkeypatch):
    client, sid = TestClient(main.app), uuid.uuid4().hex
    r = client.post("/api/search/image", data={"session_id": sid}, files={"file": ("a.gif", b"GIF89a..", "image/gif")})
    assert r.status_code == 415 and r.json()["error"]["code"] == "FILE_UNSUPPORTED"
    big = b"\xff\xd8\xff" + b"0" * (10 * 1024 * 1024)
    r = client.post("/api/search/image", data={"session_id": sid}, files={"file": ("a.jpg", big, "image/jpeg")})
    assert r.status_code == 413 and r.json()["error"]["code"] == "FILE_TOO_LARGE"
    monkeypatch.setattr(main.image, "search_by_image", lambda data: {"mode": "recognized", "matches": []})
    r = client.post("/api/search/image", data={"session_id": sid}, files={"file": ("a.jpg", jpeg(), "image/jpeg")})
    assert r.json()["mode"] == "recognized"
