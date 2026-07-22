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
    assert e.__class__.__name__ in ("FakeEmbedder", "DashscopeEmbedder")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    assert make_embedder(Settings()).__class__.__name__ == "FakeEmbedder"

def test_dashscope_provider_without_credentials_is_unconfigured(monkeypatch):
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "")
    monkeypatch.setenv("EMBED_API_KEY", "")
    monkeypatch.setenv("EMBED_MODEL", "")
    settings = Settings()
    assert settings.embedder_configured is False
    assert make_embedder(settings).__class__.__name__ == "FakeEmbedder"

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


def test_dashscope_connection_pool_matches_service_capacity(monkeypatch):
    import app.services.embedding_dashscope as mod

    captured = {}

    class _HttpClient:
        def __init__(self, *, timeout, limits):
            captured["limits"] = limits

    monkeypatch.setattr(mod.httpx, "Client", _HttpClient)
    monkeypatch.setattr(mod, "OpenAI", lambda **kwargs: captured.update(openai=kwargs) or object())
    settings = Settings(
        _env_file=None,
        embed_base_url="https://safe.example/v1",
        embed_api_key="key",
        embed_model="model",
    )
    embedder = mod.DashscopeEmbedder(settings, max_connections=5)

    embedder._ensure()

    assert captured["limits"].max_connections == 5
    assert captured["limits"].max_keepalive_connections == 5
