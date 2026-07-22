def test_only_business_job_concurrency_remains(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.kg_job_concurrency == 8
    monkeypatch.setenv("KG_JOB_CONCURRENCY", "3")
    s2 = Settings()
    assert s2.kg_job_concurrency == 3
    assert not hasattr(s2, "kg_extract_workers")
    assert not hasattr(s2, "kg_ask_reserve")
    assert not hasattr(s2, "embed_concurrency")


def test_merge_review_batch_size_setting(monkeypatch):
    from app.core.config import Settings
    assert Settings().kg_merge_review_batch_size == 30
    monkeypatch.setenv("KG_MERGE_REVIEW_BATCH_SIZE", "12")
    assert Settings().kg_merge_review_batch_size == 12


def test_llm_client_connection_pool_uses_explicit_service_capacity():
    from app.core.config import Settings
    from app.core.llm import OpenAICompatibleClient
    c = OpenAICompatibleClient(
        Settings(_env_file=None), base_url="https://example.invalid",
        api_key="k", model="m", max_connections=7,
    )
    client = c.client()
    inner = client._client  # httpx.Client
    if hasattr(inner, "_limits"):
        # SDK exposes limits directly
        limits = inner._limits
        assert limits.max_connections == 7
        assert limits.max_keepalive_connections == 7
    else:
        # Fallback: inspect the transport pool (httpx internals)
        pool = inner._transport._pool
        assert pool._max_connections == 7
        assert pool._max_keepalive_connections == 7
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
