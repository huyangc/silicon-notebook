from app.services.embedding import embed_in_chunks


def test_failed_chunk_does_not_lose_others():
    texts = [f"t{i}" for i in range(25)]

    def embed_fn(batch):
        if "t10" in batch:                 # 第二个 chunk(10..19) 整批失败
            raise RuntimeError("boom")
        return [[float(len(t))] for t in batch]

    out = embed_in_chunks(embed_fn, texts, chunk_size=10)
    assert len(out) == 25                  # 与输入对齐
    assert out[0] == [2.0]                 # chunk0 成功
    assert out[10] is None and out[19] is None   # chunk1 整批失败 -> None
    assert out[20] == [3.0]                # chunk2 成功（'t20' 长度3）


def test_all_success():
    out = embed_in_chunks(lambda b: [[1.0] for _ in b], ["a", "b", "c"], chunk_size=2)
    assert out == [[1.0], [1.0], [1.0]]


def test_dashscope_batch_size_from_config(monkeypatch):
    import app.services.embedding_dashscope as mod
    sizes = []
    class _Emb:
        def create(self, model, input):
            sizes.append(len(input))
            return type("R", (), {"data": [type("D", (), {"embedding": [0.1]})() for _ in input]})()
    class _Client:
        embeddings = _Emb()
    monkeypatch.setattr(mod, "OpenAI", lambda **kw: _Client())
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://x"); monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-v4")
    monkeypatch.setenv("EMBED_BATCH_SIZE", "5")
    from app.core.config import Settings
    e = mod.DashscopeEmbedder(Settings())
    e.embed_texts([f"t{i}" for i in range(12)])
    assert sizes and max(sizes) <= 5
