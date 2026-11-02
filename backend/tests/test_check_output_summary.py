"""Gate reports keep runner counts and diagnostic excerpts honest."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location(
        "check_output_summary", ROOT / "scripts" / "check_output_summary.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_counts_use_runner_totals_and_ignore_progress():
    module = _module()
    assert module.test_counts("tests/test_a.py .... [100%]\n") == []
    assert module.test_counts(
        "\x1b[32m===== 3 passed, 2 deselected in 1.2s =====\x1b[0m\n"
        "# tests 4\n# pass 3\n# fail 1\n"
        "ℹ tests 7\nℹ pass 7\nℹ fail 0\n Tests  8 passed (8)\n"
    ) == [
        "===== 3 passed, 2 deselected in 1.2s =====",
        "# tests 4", "# pass 3", "# fail 1",
        "ℹ tests 7", "ℹ pass 7", "ℹ fail 0", "Tests  8 passed (8)",
    ]


def test_missing_status_is_not_reported_as_success(tmp_path):
    report = _module().lane_report(tmp_path, "backend")
    assert "exit=unavailable" in report
    assert "elapsed=unavailable" in report
    assert "tests (runner-reported): unavailable" in report
    assert "did not complete successfully" in report


def test_failure_excerpt_is_bounded_and_full_log_is_preserved(tmp_path):
    module = _module()
    raw = "first diagnostic\n" + "long output " * 10_000 + "\nlast diagnostic\n"
    log_path = tmp_path / "frontend.log"
    log_path.write_text(raw)
    (tmp_path / "frontend.exit").write_text("9\n")
    report = module.lane_report(tmp_path, "frontend")
    assert "exit=9" in report
    assert "first diagnostic" not in report
    assert "last diagnostic" in report
    assert "failure excerpt" in report
    assert len(report) < module.FAILURE_TAIL_CHARS + 1_000
    assert str(log_path) in report
    assert log_path.read_text() == raw
