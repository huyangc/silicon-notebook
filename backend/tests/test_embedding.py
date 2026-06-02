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
