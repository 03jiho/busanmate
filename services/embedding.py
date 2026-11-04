"""텍스트 임베딩 (SDD §7.2 EmbeddingProvider). 제공자는 config.yaml embedding.text_provider."""
import time
from functools import lru_cache

from google import genai
from google.genai import types

from settings import config, secret


@lru_cache
def _gemini() -> genai.Client:
    return genai.Client(api_key=secret("GEMINI_API_KEY"), http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=5, initial_delay=2, http_status_codes=[429, 500, 503])))


def embed_texts(texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """task: 문서는 RETRIEVAL_DOCUMENT, 질의는 RETRIEVAL_QUERY."""
    cfg = config()["embedding"]
    if cfg["text_provider"] != "gemini":
        raise ValueError(f"지원하지 않는 임베딩 제공자: {cfg['text_provider']}")
    # 분당 건수·토큰 한도 안에 들도록 배치를 나눈다 (토큰 수는 글자 수로 넉넉히 추정)
    batches, cur, chars = [], [], 0
    for t in texts:
        if cur and (len(cur) >= cfg["batch_size"] or chars + len(t) > cfg["batch_chars"]):
            batches.append(cur)
            cur, chars = [], 0
        cur.append(t)
        chars += len(t)
    if cur:
        batches.append(cur)

    out = []
    for i, batch in enumerate(batches):
        if i:
            time.sleep(cfg["batch_interval_s"])  # ponytail: 고정 대기로 분당 한도 회피, 유료 등급이면 0으로
        r = _gemini().models.embed_content(
            model=cfg["text_model"], contents=batch,
            config=types.EmbedContentConfig(task_type=task, output_dimensionality=cfg["text_dim"]))
        out += [e.values for e in r.embeddings]
    return out
