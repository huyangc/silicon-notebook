from app.eval.report import render_quality_report, render_speed_report, render_inference_report


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
    assert "cascode" in md      # claim 退化样例出现


def test_render_speed_report():
    measured = [
        {"chars": 5000, "n_windows": 1, "wall_s": 6.0,
         "latency_p50_s": 3.0, "latency_p95_s": 5.0, "total_tokens": 1500,
         "retries": 0, "effective_concurrency": 1},
        {"chars": 100000, "n_windows": 13, "wall_s": 9.0,
         "latency_p50_s": 4.0, "latency_p95_s": 8.0, "total_tokens": 30000,
         "retries": 2, "effective_concurrency": 13},
        {"chars": 200000, "n_windows": 50, "wall_s": 14.0,
         "latency_p50_s": 0.0, "latency_p95_s": 0.0, "total_tokens": 0,
         "retries": 110, "effective_concurrency": 50},
    ]
    extrapolated = [{"chars": 500000, "n_windows": 63, "est_s": 21.0}]
    md = render_speed_report(measured, extrapolated, recommended_max_chars=250000,
                             target_seconds=120)
    assert "# KG 抽取速度报告" in md
    assert "100000" in md or "100,000" in md
    assert "推荐文档上限" in md
    assert "250" in md
    assert "失败" in md         # 失败档(tokens=0)被标注


def test_render_inference_report():
    rows = [
        {"id": "q01", "level": "L1", "question": "什么是 cascode?",
         "answer": "...[k1]", "evidence_level": "grounded",
         "judge": {"correctness": 2, "inference_quality": 2,
                   "grounding_consistency": True, "fabricated_citation": False,
                   "reason": "准确"}},
        {"id": "q16", "level": "L3", "question": "为何...摆幅?",
         "answer": "...", "evidence_level": "overview",
         "judge": {"correctness": 1, "inference_quality": 1,
                   "grounding_consistency": True, "fabricated_citation": False,
                   "reason": "部分综合"}},
    ]
    md = render_inference_report(rows)
    assert "# 推断问答评测报告" in md
    assert "L1" in md and "L3" in md
    assert "落差" in md      # L3 vs L1 落差
    assert "q16" in md
