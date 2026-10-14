"""scripts/check_timing_summary.py: the G1 gate's closing timing report.

It shows the gate's wall time, lane seconds and the slowest backend files and
tests, warns over the 60 s local target, and never fails the gate.
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_timing_summary.py"


def _module():
    spec = importlib.util.spec_from_file_location("check_timing_summary", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gate_outputs(tmp_path: Path) -> tuple[Path, Path]:
    timing = tmp_path / "timing"
    timing.mkdir()
    for lane, seconds in (("backend", 58), ("contracts", 20), ("frontend", 31)):
        (timing / f"{lane}.time").write_text(f"{lane}={seconds}\n", encoding="utf-8")
    cases = [
        ("tests.test_alpha", "test_one", 3.0),
        ("tests.test_alpha", "test_two", 2.5),
        ("tests.eval.test_beta.TestGroup", "test_three", 4.0),
        *[(f"tests.test_small_{index:02d}", "test_it", 0.1) for index in range(12)],
    ]
    junit = tmp_path / "backend-junit.xml"
    junit.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(
            f'<testcase classname="{classname}" name="{name}" time="{seconds}" />'
            for classname, name, seconds in cases
        )
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return timing, junit


def test_the_summary_ranks_files_and_tests_and_warns_over_the_target(tmp_path):
    timing, junit = _gate_outputs(tmp_path)
    text = _module().summarize(61, timing, junit, time.time() - 60, 60)
    lines = text.splitlines()

    assert "gate_wall=61 backend=58 contracts=20 frontend=31" in lines
    files = lines.index(
        "slowest backend test files (summed junit seconds, top 10):"
    )
    assert lines[files + 1].split() == ["5.50s", "tests/test_alpha.py"]
    assert lines[files + 2].split() == ["4.00s", "tests/eval/test_beta.py"]
    tests = lines.index("slowest backend tests (junit seconds, top 10):")
    assert lines[tests + 1].split() == [
        "4.00s", "tests.eval.test_beta.TestGroup::test_three"
    ]
    assert len(lines[files + 1 : tests]) == 10
    assert "WARNING: the G1 gate took 61 s, over the 60 s local target" in lines


def test_no_warning_at_or_under_the_target(tmp_path):
    timing, junit = _gate_outputs(tmp_path)
    text = _module().summarize(60, timing, junit, time.time() - 60, 60)

    assert "WARNING" not in text


def test_a_report_older_than_this_run_is_not_ranked(tmp_path):
    timing, junit = _gate_outputs(tmp_path)
    old = time.time() - 3600
    os.utime(junit, (old, old))
    text = _module().summarize(10, timing, junit, time.time() - 60, 60)

    assert "backend junit backend-junit.xml: not written by this run" in text
    assert "slowest" not in text


def test_it_never_fails_the_gate_even_without_its_inputs(tmp_path, capsys):
    status = _module().main([
        "--wall", "500",
        "--timing-dir", str(tmp_path / "missing"),
        "--junit", str(tmp_path / "missing.xml"),
        "--started-at", "0",
    ])

    out = capsys.readouterr().out
    assert status == 0
    assert "gate_wall=500 backend=missing contracts=missing frontend=missing" in out
    assert "WARNING: the G1 gate took 500 s" in out


def test_check_sh_reports_it_without_letting_it_change_the_exit_status():
    source = (ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    call = source.index('"$ROOT_DIR/scripts/check_timing_summary.py"')
    tail = source[call:]

    assert '--target 60 || true\n\nexit "$status"\n' in tail
    assert source.index('cat "$TMP_DIR/$lane.log"') < call
