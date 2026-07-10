def test_settings_concurrency_knobs(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.kg_job_concurrency == 8
    assert s.kg_ask_reserve == 64
    monkeypatch.setenv("KG_JOB_CONCURRENCY", "3")
    monkeypatch.setenv("KG_ASK_RESERVE", "16")
    s2 = Settings()
    assert s2.kg_job_concurrency == 3 and s2.kg_ask_reserve == 16


def test_merge_review_batch_size_setting(monkeypatch):
    from app.core.config import Settings
    assert Settings().kg_merge_review_batch_size == 30
    monkeypatch.setenv("KG_MERGE_REVIEW_BATCH_SIZE", "12")
    assert Settings().kg_merge_review_batch_size == 12


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


def test_upload_dispatches_via_submit_job(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.kg import scheduler

    calls = []
    monkeypatch.setattr(scheduler, "submit_job",
                        lambda fn, /, *a, **k: calls.append((fn, a)) or None)
    client = TestClient(app)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    r = client.post(f"/api/notebooks/{nb}/sources",
                    files=[("files", ("a.md", b"# Title\n\nsome text", "text/markdown"))])
    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0][1][0].startswith("src-")
    # Task 12: the scheduled callable stays the repository's process_source
    # delegate — upload dispatch keeps riding the KG job pool (never inline,
    # never a raw service method without the fresh compatibility hooks).
    assert getattr(calls[0][0], "__name__", "") == "process_source"
