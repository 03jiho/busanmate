"""장소 이미지 임베딩 (COMP-12, DATA-02·06): 아직 임베딩하지 않은 place_image를 내려받아 CLIP 벡터로 Chroma에 넣는다.
실행: python -m pipeline.images   (pipeline.run 끝에도 자동으로 실행된다)"""
import io
import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image

import db
from services.image import embed_images
from settings import config

log = logging.getLogger("pipeline")


def _download(url: str) -> Image.Image | None:
    cfg = config()["image"]
    try:
        r = requests.get(url, timeout=cfg["download_timeout_s"], headers={"User-Agent": "BusanMate/0.1"})
        r.raise_for_status()
        if len(r.content) > cfg["download_max_bytes"]:
            return None
        img = Image.open(io.BytesIO(r.content))
        img.load()
        return img
    except Exception as e:
        log.warning("이미지 내려받기 실패: %s", type(e).__name__)
        return None


def embed_pending() -> dict:
    """멱등: embedding_id가 비어 있는(활성 장소의) 이미지만 처리한다. {new, skipped}."""
    with db.connect() as conn:
        rows = conn.execute("SELECT i.image_id, i.place_id, i.url FROM place_image i JOIN place p USING (place_id) "
                            "WHERE p.is_active AND i.embedding_id IS NULL ORDER BY i.image_id").fetchall()
    coll, size = db.collection("place_image"), config()["embedding"]["image_batch_size"]
    new = skipped = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i in range(0, len(rows), size):  # 배치마다 내려받고 바로 버려 메모리를 아낀다
            batch = rows[i:i + size]
            imgs = list(pool.map(_download, [u for _, _, u in batch]))
            ok = [(row, img) for row, img in zip(batch, imgs) if img is not None]
            skipped += len(batch) - len(ok)
            if not ok:
                continue
            coll.upsert(ids=[str(r[0]) for r, _ in ok], embeddings=embed_images([img for _, img in ok]),
                        metadatas=[{"place_id": r[1]} for r, _ in ok])
            with db.connect() as conn:
                conn.cursor().executemany("UPDATE place_image SET embedding_id = %s WHERE image_id = %s",
                                          [(str(r[0]), r[0]) for r, _ in ok])
            new += len(ok)
            log.info("이미지 임베딩 %d/%d", i + len(batch), len(rows))
    return {"new": new, "skipped": skipped}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(embed_pending())
