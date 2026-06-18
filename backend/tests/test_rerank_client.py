from app.services.rerank_client import RerankClient


class _S:
    rerank_model = "qwen3-rerank"; rerank_base_url = "http://fake/v1"
    rerank_api_key = "k"; rerank_max_docs = 500; embed_concurrency = 8
    openai_compat_timeout_seconds = 30


def test_unconfigured_identity():
    s = _S(); s.rerank_model = ""
    rc = RerankClient(s)
    assert not rc.configured and rc.rerank("q", ["a", "b", "c"]) == [0, 1, 2]


def test_orders_by_score(monkeypatch):
    rc = RerankClient(_S())
    monkeypatch.setattr(rc, "_rerank_batch", lambda q, d: [
        {"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5},
        {"index": 1, "relevance_score": 0.1}])
    assert rc.rerank("q", ["a", "b", "c"]) == [2, 0, 1]


def test_failure_identity(monkeypatch):
    rc = RerankClient(_S())
    monkeypatch.setattr(rc, "_rerank_batch", lambda q, d: (_ for _ in ()).throw(RuntimeError()))
    assert rc.rerank("q", ["a", "b"]) == [0, 1]
