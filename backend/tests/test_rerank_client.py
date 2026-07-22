import pytest

from app.services.rerank_client import RerankClient, normalize_rerank_api_style


class _S:
    rerank_max_docs = 500
    openai_compat_timeout_seconds = 30


def _client(*, api_style="dashscope", **kwargs):
    return RerankClient(
        _S(), model="qwen3-rerank", base_url="http://fake/v1",
        api_key="k", api_style=api_style, **kwargs,
    )


def test_unconfigured_identity():
    rc = RerankClient(_S())
    assert not rc.configured and rc.rerank("q", ["a", "b", "c"]) == [0, 1, 2]


def test_orders_by_score(monkeypatch):
    rc = _client()
    monkeypatch.setattr(rc, "_rerank_batch", lambda q, d: [
        {"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5},
        {"index": 1, "relevance_score": 0.1}])
    assert rc.rerank("q", ["a", "b", "c"]) == [2, 0, 1]


def test_failure_identity(monkeypatch):
    rc = _client()
    monkeypatch.setattr(rc, "_rerank_batch", lambda q, d: (_ for _ in ()).throw(RuntimeError()))
    assert rc.rerank("q", ["a", "b"]) == [0, 1]


def test_native_dashscope_request_shape(monkeypatch):
    """锁定原生 DashScope text-rerank 形状:URL=/services/rerank/text-rerank/text-rerank,
    body=input{query,documents}+parameters,响应解析 output.results。"""
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"output": {"results": [
                {"index": 1, "relevance_score": 0.7}, {"index": 0, "relevance_score": 0.2}]}}

    def _post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json)
        return _Resp()

    monkeypatch.setattr("app.services.rerank_client.requests.post", _post)
    rc = _client()
    order = rc.rerank("q", ["a", "b"])
    assert captured["url"] == "http://fake/v1/services/rerank/text-rerank/text-rerank"
    assert captured["json"]["model"] == "qwen3-rerank"
    assert captured["json"]["input"] == {"query": "q", "documents": ["a", "b"]}
    assert "parameters" in captured["json"]
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert order == [1, 0]   # 由 output.results 的 relevance_score 重排


def test_openai_vllm_request_shape(monkeypatch):
    """RERANK_API_STYLE=openai(vLLM/Cohere 等兼容):URL=/rerank,扁平 body
    {model,query,documents}(无 input/parameters 嵌套),响应解析顶层 results。"""
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"results": [
                {"index": 1, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.3}]}

    def _post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json)
        return _Resp()

    monkeypatch.setattr("app.services.rerank_client.requests.post", _post)
    rc = _client(api_style="openai")
    order = rc.rerank("q", ["a", "b"])
    assert captured["url"] == "http://fake/v1/rerank"
    assert captured["json"]["model"] == "qwen3-rerank"
    assert captured["json"]["query"] == "q"
    assert captured["json"]["documents"] == ["a", "b"]
    assert "input" not in captured["json"] and "parameters" not in captured["json"]
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert order == [1, 0]


def test_openai_score_field_fallback(monkeypatch):
    """有的兼容实现把分数键叫 score(非 relevance_score),也要能解析。"""
    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"results": [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.1}]}

    monkeypatch.setattr("app.services.rerank_client.requests.post", lambda *a, **k: _Resp())
    assert _client(api_style="openai").rerank("q", ["a", "b"]) == [0, 1]


def test_api_style_param_overrides_settings(monkeypatch):
    """构造参数 api_style 覆盖 settings(每用户 config 可传)。"""
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"results": [{"index": 0, "relevance_score": 1.0}]}

    monkeypatch.setattr("app.services.rerank_client.requests.post",
                        lambda url, **k: captured.update(url=url) or _Resp())
    _client(api_style="openai").rerank("q", ["a"])
    assert captured["url"] == "http://fake/v1/rerank"


def test_connection_pool_matches_service_capacity():
    client = _client(max_connections=6)
    assert client._session.adapters["https://"]._pool_maxsize == 6
    assert client._session.adapters["http://"]._pool_maxsize == 6


def test_raw_config_is_explicit_and_whitespace_is_unconfigured():
    client = RerankClient(
        _S(), model=" model ", base_url="   ", api_key=" secret ",
    )
    assert client.model == "model"
    assert client.base_url == ""
    assert client.api_key == "secret"
    assert client.configured is False


@pytest.mark.parametrize("alias", ["openai", "vllm", "cohere", "compatible"])
def test_openai_protocol_aliases_are_normalized(alias):
    assert normalize_rerank_api_style(alias) == "openai"


def test_unknown_protocol_normalizes_to_dashscope():
    assert normalize_rerank_api_style("unknown") == "dashscope"
