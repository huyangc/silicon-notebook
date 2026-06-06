from app.eval.report import render_quality_report


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
