"""사진 인식 평가 (AC-005, AI-REQ-06): Top-5 정확도 ≥ 70% 목표.
실행: python -m eval.photo_eval <폴더>      폴더에 사진과 labels.csv(file,place_id)를 둔다. 인덱스에 쓰지 않은 사진이어야 한다.
     python -m eval.photo_eval --sanity   색인 이미지의 썸네일 버전으로 연결만 점검(정확도 지표로 쓰지 말 것)"""
import csv
import io
import random
import sys
from pathlib import Path

import requests
from PIL import Image

import db
from services.image import rank_places
from settings import config


def evaluate(samples: list[tuple[Image.Image, int]]) -> dict:
    tau, top1, top5, recognized = config()["image"]["match_threshold"], 0, 0, 0
    for img, place_id in samples:
        ranked = rank_places(img)
        ids = [p for p, _ in ranked[:5]]
        top1 += bool(ids) and ids[0] == place_id
        top5 += place_id in ids
        recognized += bool(ranked) and ranked[0][1] >= tau
    n = len(samples) or 1
    return {"n": len(samples), "top1": top1 / n, "top5": top5 / n, "recognized_rate": recognized / n, "tau": tau}


def from_folder(folder: Path) -> list[tuple[Image.Image, int]]:
    with open(folder / "labels.csv", encoding="utf-8") as f:
        return [(Image.open(folder / row["file"]).convert("RGB"), int(row["place_id"])) for row in csv.DictReader(f)]


def sanity(n: int = 40) -> list[tuple[Image.Image, int]]:
    """visitbusan 썸네일(_thumbL)은 색인한 원본(_ttiel)의 축소본이다. 파이프라인 연결 점검용."""
    with db.connect() as conn:
        rows = conn.execute("SELECT place_id, url FROM place_image WHERE embedding_id IS NOT NULL").fetchall()
    out = []
    for place_id, url in random.Random(0).sample(rows, min(n, len(rows))):
        try:
            r = requests.get(url.replace("_ttiel", "_thumbL"), timeout=10, headers={"User-Agent": "BusanMate/0.1"})
            out.append((Image.open(io.BytesIO(r.content)).convert("RGB"), place_id))
        except Exception:
            continue
    return out


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--sanity"
    print(evaluate(sanity() if arg == "--sanity" else from_folder(Path(arg))))
