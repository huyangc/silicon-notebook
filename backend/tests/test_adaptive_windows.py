def test_settings_window_knobs(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.kg_window_min_chars == 4000
    assert s.kg_window_max_chars == 8000
    assert s.kg_window_target_chars == 0          # 0 = 自适应（默认）
    monkeypatch.setenv("KG_WINDOW_MAX_CHARS", "6000")
    assert Settings().kg_window_max_chars == 6000


def test_plan_window_size():
    from app.services.kg_ingest import plan_window_size
    import math
    assert plan_window_size(3000, 100, 4000, 8000) == 3000
    assert plan_window_size(9000, 100, 4000, 8000) == 3000
    assert plan_window_size(1_000_000, 100, 4000, 8000) == 8000
    n = plan_window_size(129206, 100, 4000, 8000)
    N = math.ceil(129206 / n)
    assert N == 33 and n <= 8000 and N * n >= 129206
    assert plan_window_size(129206, 100, 4000, 8000, override=9000) == 9000
    # edge cases: empty doc -> 1 (never 0); workers=0 guarded (no ZeroDivision)
    assert plan_window_size(0, 16, 4000, 8000) == 1
    assert plan_window_size(100000, 0, 4000, 8000) >= 4000
