from app.services.embedding import FakeEmbedder, make_embedder
from app.core.config import Settings

def test_fake_embedder_is_deterministic_and_batched():
    e = FakeEmbedder(dim=8)
    v1 = e.embed_query("MOSFET")
    v2 = e.embed_query("MOSFET")
    assert v1 == v2 and len(v1) == 8           # deterministic, right dim
    batch = e.embed_texts(["a", "b", "MOSFET"])
    assert len(batch) == 3 and batch[2] == v1  # batch matches single

def test_factory_defaults_to_fake_when_unconfigured(monkeypatch):
    monkeypatch.delenv("EMBED_PROVIDER", raising=False)
    e = make_embedder(Settings())
    assert e.__class__.__name__ in ("FakeEmbedder", "LocalBGEEmbedder", "DashscopeEmbedder")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    assert make_embedder(Settings()).__class__.__name__ == "FakeEmbedder"

def test_dashscope_embedder_batches_and_no_retries(monkeypatch):
    import app.services.embedding_dashscope as mod
    captured = {}
    class _Emb:
        def create(self, model, input):
            captured["input"] = input
            data = [type("D", (), {"embedding": [0.1, 0.2]})() for _ in input]
            return type("R", (), {"data": data})()
    class _Client:
        embeddings = _Emb()
    def fake_openai(**kw):
        captured["kwargs"] = kw
        return _Client()
    monkeypatch.setattr(mod, "OpenAI", fake_openai)
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://x"); monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-v4")
    from app.core.config import Settings
    e = mod.DashscopeEmbedder(Settings())
    out = e.embed_texts(["a", "b", "c"])
    assert len(out) == 3 and captured["input"] == ["a", "b", "c"]   # ONE batched call
    assert captured["kwargs"].get("max_retries") == 0               # fail-fast

def test_local_bge_lazy_loads_once(monkeypatch):
    import app.services.embedding_local as mod
    loads = {"n": 0}
    class _Model:
        def encode(self, texts, **kw):
            return [[0.0] * 4 for _ in texts]
    def fake_loader(name):
        loads["n"] += 1
        return _Model()
    monkeypatch.setattr(mod, "_load_model", fake_loader)
    monkeypatch.setattr(mod.LocalBGEEmbedder, "_model", None)  # reset singleton across tests
    monkeypatch.setenv("EMBED_MODEL", "BAAI/bge-m3"); monkeypatch.setenv("EMBED_DIM", "4")
    from app.core.config import Settings
    e = mod.LocalBGEEmbedder(Settings())
    e.embed_query("a"); e.embed_texts(["b", "c"])
    assert loads["n"] == 1  # model loaded once, reused


def test_dashscope_chunks_at_most_10(monkeypatch):
    """Regression: text-embedding-v3/v4 reject batches > 10 items with a 400.
    embed_texts must sub-chunk so no single request exceeds the cap."""
    import app.services.embedding_dashscope as mod
    sizes = []
    class _Emb:
        def create(self, model, input):
            sizes.append(len(input))
            data = [type("D", (), {"embedding": [0.1, 0.2]})() for _ in input]
            return type("R", (), {"data": data})()
    class _Client:
        embeddings = _Emb()
    monkeypatch.setattr(mod, "OpenAI", lambda **kw: _Client())
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://x"); monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-v4")
    from app.core.config import Settings
    e = mod.DashscopeEmbedder(Settings())
    out = e.embed_texts([f"t{i}" for i in range(25)])
    assert len(out) == 25                # all embedded
    assert sizes and max(sizes) <= 10    # every request within dashscope's cap
