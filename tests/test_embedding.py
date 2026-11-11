from types import SimpleNamespace

from services import embedding


def test_batches_respect_count_and_char_limits(monkeypatch):
    sizes = []

    def embed_content(model, contents, config):
        sizes.append(len(contents))
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.0]) for _ in contents])

    fake = SimpleNamespace(models=SimpleNamespace(embed_content=embed_content))
    monkeypatch.setattr(embedding, "_gemini", lambda: fake)
    monkeypatch.setattr(embedding.time, "sleep", lambda s: None)
    cfg = embedding.config()["embedding"]
    monkeypatch.setitem(cfg, "batch_size", 3)
    monkeypatch.setitem(cfg, "batch_chars", 10)

    out = embedding.embed_texts(["aaaa"] * 5 + ["b"] * 4)
    assert len(out) == 9
    assert sizes == [2, 2, 3, 2]  # 글자 수 상한(10)으로 2개씩, 그다음 건수 상한(3)
