from app.eval.report import render_quality_report, render_speed_report


def test_render_quality_report_has_sections():
    per_book = {
        "Razavi": {"concept": {"total": 1500, "suspect_non_atomic": 200,
                               "suspect_non_atomic_rate": 0.13,
                               "probe_counts": {"symbol": 78, "reference": 159},
                               "orphans": 50, "enumerated_groups": 5,
                               "near_duplicate_groups": 3,
                               "enumerated_samples": {"level # model": ["Level 1 Model", "Level 2 Model"]},
                               "samples": {"symbol": ["Vb1", "R_0"]}},
                   "claim": {"total": 2000, "degraded": 120, "degraded_rate": 0.06,
                             "samples": ["cascode"]}},
    }
    md = render_quality_report(per_book)
    assert "# KG 抽取质量报告" in md
    assert "Razavi" in md
    assert "疑似非原子" in md
    assert "Vb1" in md          # 样例出现
    assert "Level 1 Model" in md


def test_render_speed_report():
    measured = [
        {"chars": 5000, "n_windows": 1, "wall_s": 6.0,
         "latency_p50_s": 3.0, "latency_p95_s": 5.0, "total_tokens": 1500,
         "retries": 0, "effective_concurrency": 1},
        {"chars": 100000, "n_windows": 13, "wall_s": 9.0,
         "latency_p50_s": 4.0, "latency_p95_s": 8.0, "total_tokens": 30000,
         "retries": 2, "effective_concurrency": 13},
    ]
    extrapolated = [{"chars": 500000, "n_windows": 63, "est_s": 21.0}]
    md = render_speed_report(measured, extrapolated, recommended_max_chars=250000,
                             target_seconds=120)
    assert "# KG 抽取速度报告" in md
    assert "100000" in md or "100,000" in md
    assert "推荐文档上限" in md
    assert "250" in md
