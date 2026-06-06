def test_settings_concurrency_knobs(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.kg_job_concurrency == 8
    assert s.kg_ask_reserve == 64
    monkeypatch.setenv("KG_JOB_CONCURRENCY", "3")
    monkeypatch.setenv("KG_ASK_RESERVE", "16")
    s2 = Settings()
    assert s2.kg_job_concurrency == 3 and s2.kg_ask_reserve == 16


def test_llm_client_connection_pool_sized(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "k")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "m")
    monkeypatch.setenv("KG_EXTRACT_WORKERS", "100")
    monkeypatch.setenv("KG_ASK_RESERVE", "16")
    from app.core.config import Settings
    from app.core.llm import OpenAICompatibleClient
    c = OpenAICompatibleClient(Settings())
    client = c.client()
    inner = client._client  # httpx.Client
    if hasattr(inner, "_limits"):
        # SDK exposes limits directly
        limits = inner._limits
        assert limits.max_connections == 116
        assert limits.max_keepalive_connections == 16
    else:
        # Fallback: inspect the transport pool (httpx internals)
        pool = inner._transport._pool
        assert pool._max_connections == 116
        assert pool._max_keepalive_connections == 16
    assert client.max_retries == 0
