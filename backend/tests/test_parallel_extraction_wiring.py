def test_settings_concurrency_knobs(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.kg_job_concurrency == 8
    assert s.kg_ask_reserve == 64
    monkeypatch.setenv("KG_JOB_CONCURRENCY", "3")
    monkeypatch.setenv("KG_ASK_RESERVE", "16")
    s2 = Settings()
    assert s2.kg_job_concurrency == 3 and s2.kg_ask_reserve == 16
