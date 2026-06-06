import json
from app.eval.speed import estimate_extract_seconds, parse_llm_log, plan_windows


def test_plan_windows_matches_product():
    # 生产配置 workers=1000:level=clamp(1M/1000=1000,4000,8000)=4000 -> 250 窗口,均分回 4000
    size, n = plan_windows(1_000_000, 1000, 4000, 8000)
    assert size == 4000 and n == 250
    # workers=100 时 level=clamp(10000,4000,8000)=8000 -> 125 窗口(对齐现有 test_adaptive_windows)
    assert plan_windows(1_000_000, 100, 4000, 8000) == (8000, 125)


def test_estimate_monotonic_and_formula():
    # 25 窗口、有效并发 16、单窗口 2s、固定开销 3s -> ceil(25/16)*2 + 3 = 7
    assert estimate_extract_seconds(n_windows=25, effective_concurrency=16,
                                    per_window_p50_s=2.0, fixed_overhead_s=3.0) == 7.0
    a = estimate_extract_seconds(10, 16, 2.0, 3.0)
    b = estimate_extract_seconds(100, 16, 2.0, 3.0)
    assert b > a


def test_parse_llm_log_filters_by_ts(tmp_path):
    p = tmp_path / "llm.jsonl"
    lines = [
        {"ts": "2026-06-06T10:00:00", "kind": "chat", "status": "ok",
         "latency_ms": 1000, "usage": {"total_tokens": 700}},
        {"ts": "2026-06-06T12:00:00", "kind": "chat", "status": "ok",
         "latency_ms": 2000, "usage": {"total_tokens": 500}},
        {"ts": "2026-06-06T12:00:01", "kind": "chat", "status": "retry",
         "latency_ms": 50},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines))
    stats = parse_llm_log(str(p), since_ts="2026-06-06T11:00:00")
    assert stats["calls"] == 1            # 只统计 ok,且 ts 在 since 之后
    assert stats["retries"] == 1
    assert stats["latency_p50_s"] == 2.0
    assert stats["total_tokens"] == 500
