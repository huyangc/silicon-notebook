def test_settings_window_knobs(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.kg_window_min_chars == 4000
    assert s.kg_window_max_chars == 8000
    assert s.kg_window_target_chars == 0          # 0 = 自适应（默认）
    monkeypatch.setenv("KG_WINDOW_MAX_CHARS", "6000")
    assert Settings().kg_window_max_chars == 6000
