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
    rc = RerankClient(_S())   # base_url = http://fake/v1
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
    s = _S(); s.rerank_api_style = "openai"
    rc = RerankClient(s)                       # base_url = http://fake/v1
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
    s = _S(); s.rerank_api_style = "openai"
    assert RerankClient(s).rerank("q", ["a", "b"]) == [0, 1]


def test_api_style_param_overrides_settings(monkeypatch):
    """构造参数 api_style 覆盖 settings(每用户 config 可传)。"""
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"results": [{"index": 0, "relevance_score": 1.0}]}

    monkeypatch.setattr("app.services.rerank_client.requests.post",
                        lambda url, **k: captured.update(url=url) or _Resp())
    s = _S(); s.rerank_api_style = "dashscope"          # settings says dashscope
    RerankClient(s, api_style="openai").rerank("q", ["a"])  # param overrides → openai
    assert captured["url"] == "http://fake/v1/rerank"


def test_connection_pool_matches_service_capacity():
    client = RerankClient(_S(), max_connections=6)
    assert client._session.adapters["https://"]._pool_maxsize == 6
    assert client._session.adapters["http://"]._pool_maxsize == 6
