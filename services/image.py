"""ImageService (COMP-08, FR-008·009, NFR-006, AI-REQ-03·06): 사진 검증 → CLIP 임베딩 → place_image 유사도 검색.
최고 유사도 ≥ τ이면 인식, 미만이면 VLM 장면 설명으로 비슷한 분위기의 장소를 찾는다.
업로드 사진은 메모리에서만 처리하고 버린다(저장·로그 없음)."""
import io
import logging
from functools import lru_cache

from PIL import Image

import db
from services.llm import get_llm
from services.search import Conditions, fetch_places, search, source_of
from settings import config

log = logging.getLogger("image")
MAGIC = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png"}
SCENE_PROMPT = ("이 사진의 장면을 여행지 검색에 쓸 수 있게 한국어 한두 문장으로 묘사해 줘. "
                "장소 유형(바다, 산, 거리, 건물, 음식점 등), 분위기, 눈에 띄는 사물 위주로 쓰고 장소 이름은 추측하지 마.")


class ImageError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def validate(data: bytes) -> str:
    """용량과 형식(확장자가 아니라 매직바이트)을 검사하고 MIME을 돌려준다 (ERR-003)."""
    if len(data) > config()["image"]["max_bytes"]:
        raise ImageError(413, "FILE_TOO_LARGE", "사진은 10MB 이하만 올릴 수 있습니다.")
    for magic, mime in MAGIC.items():
        if data.startswith(magic):
            return mime
    raise ImageError(415, "FILE_UNSUPPORTED", "JPG 또는 PNG 사진만 올릴 수 있습니다.")


@lru_cache
def _clip():
    import open_clip  # 무거운 의존성이라 처음 쓸 때 불러온다
    import torch
    cfg = config()["embedding"]
    model, _, preprocess = open_clip.create_model_and_transforms(cfg["image_model"], pretrained=cfg["image_pretrained"])
    model.eval()
    return model, preprocess, torch


def embed_images(images: list[Image.Image]) -> list[list[float]]:
    """L2 정규화한 CLIP 이미지 벡터."""
    model, preprocess, torch = _clip()
    with torch.no_grad():
        v = model.encode_image(torch.stack([preprocess(im.convert("RGB")) for im in images]))
        v = v / v.norm(dim=-1, keepdim=True)
    return v.tolist()


def aggregate(distances: list[float], metadatas: list[dict]) -> list[tuple[int, float]]:
    """이미지 단위 결과 → place_id별 최대 유사도(1 − cosine 거리), 내림차순."""
    best: dict[int, float] = {}
    for d, m in zip(distances, metadatas):
        pid = int(m["place_id"])
        best[pid] = max(best.get(pid, -1.0), 1 - d)
    return sorted(best.items(), key=lambda x: -x[1])


def rank_places(img: Image.Image) -> list[tuple[int, float]]:
    """CLIP 유사도 상위 이미지 → 장소별 (place_id, 유사도) 내림차순. 평가 스크립트도 이 함수를 쓴다."""
    vec = embed_images([img])[0]
    r = db.collection("place_image").query(query_embeddings=[vec], n_results=config()["image"]["top_images"],
                                           include=["distances", "metadatas"])
    return aggregate(r["distances"][0], r["metadatas"][0])


def search_by_image(data: bytes) -> dict:
    """API-002 응답: {mode: recognized|similar, matches, description, sources, notices}."""
    cfg = config()["image"]
    mime = validate(data)
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ImageError(415, "FILE_UNSUPPORTED", "사진 파일을 읽을 수 없습니다.")
    ranked = rank_places(img)
    del img
    notices, description = [], None
    if ranked and ranked[0][1] >= cfg["match_threshold"]:
        mode, picks = "recognized", ranked[:cfg["result_count"]]
    else:  # 유사한 명소가 없거나 부산 밖 사진: 장면 설명으로 비슷한 분위기의 장소 (FR-009)
        mode = "similar"
        try:
            description = get_llm().describe_image(data, mime, SCENE_PROMPT)
            hits, _ = search(Conditions(query=description), n=cfg["result_count"])
            picks = [(h["place_id"], h["sim"]) for h in hits]
        except Exception as e:
            log.warning("VLM 폴백 실패: %s", type(e).__name__)
            picks = ranked[:cfg["result_count"]]
            notices.append("사진 설명 없이 유사한 장소만 보여 드립니다.")
    places = fetch_places([p for p, _ in picks])
    picks = [(p, s) for p, s in picks if p in places]
    return {"mode": mode, "description": description, "notices": notices,
            "matches": [{**source_of(places[p]), "category": places[p]["category"], "gu": places[p]["gu"],
                         "lat": places[p]["lat"], "lng": places[p]["lng"], "similarity": round(s, 3)}
                        for p, s in picks],
            "sources": [source_of(places[p]) for p, _ in picks]}
