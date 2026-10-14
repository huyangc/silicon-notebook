"""scripts/check_frontend.sh runs and reports its steps correctly on both paths.

The lane is run for real against a fake ``npm`` that records when each script
starts and ends, so the ordering, concurrency, failure and interruption
reporting are checked as behaviour, not as text. CI runners take the
sequential path; the parallel path is forced here (and runs for real in the
daily ``level-2-frontend-parallel`` job).
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]

# `npm run <script>`: record start/end, then behave as FAKE_NPM_<SCRIPT> says.
# `test:node` waits for `test:component` to start when FAKE_NPM_OVERLAP is set,
# which only the parallel path can satisfy.
FAKE_NPM = r"""#!/usr/bin/env bash
script="$2"
var="FAKE_NPM_$(printf '%s' "$script" | tr ':a-z-' '_A-Z_')"
printf 'start %s\n' "$script" >>"$FAKE_NPM_EVENTS"
echo "output of $script"
if [[ "$script" == test:node && -n "${FAKE_NPM_OVERLAP:-}" ]]; then
  for _ in $(seq 1 200); do
    grep -q '^start test:component$' "$FAKE_NPM_EVENTS" && break
    sleep 0.05
  done
  grep -q '^start test:component$' "$FAKE_NPM_EVENTS" || exit 7
fi
case "${!var:-}" in
  fail) printf 'end %s\n' "$script" >>"$FAKE_NPM_EVENTS"; exit 1 ;;
  kill) kill -KILL "$PPID"; exit 0 ;;
esac
printf 'end %s\n' "$script" >>"$FAKE_NPM_EVENTS"
"""


@pytest.fixture
def lane(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "check_frontend.sh", root / "scripts" / "check_frontend.sh")
    (root / "frontend" / "node_modules").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text(FAKE_NPM, encoding="utf-8")
    npm.chmod(0o755)
    events = tmp_path / "events"

    def run(**overrides: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        events.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(tmp_path),
            "FAKE_NPM_EVENTS": str(events),
            **overrides,
        }
        env.pop("CHECK_TIMING_FILE", None)
        env.pop("CHECK_LANE_NAME", None)
        result = subprocess.run(
            ["bash", str(root / "scripts" / "check_frontend.sh")],
            env=env, capture_output=True, text=True, timeout=60,
        )
        return result, events.read_text(encoding="utf-8").splitlines()

    return run


def _headers(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("----- frontend ")]


def test_parallel_runs_the_groups_concurrently_and_typechecks_after_the_build(lane):
    result, events = lane(FRONTEND_LANE_PARALLEL="1", FAKE_NPM_OVERLAP="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert events[0:2] == ["start sync:ui-plugins", "end sync:ui-plugins"]
    assert events.count("start sync:ui-plugins") == 1
    # test:node only finishes once test:component has started: they overlapped.
    assert "end test:node" in events
    assert events.index("start lint") > events.index("end build")
    assert [header.split(":")[0] for header in _headers(result.stdout)] == [
        "----- frontend node-tests",
        "----- frontend component-tests",
        "----- frontend build",
        "----- frontend typecheck",
    ]
    assert all(" exit=0 " in header for header in _headers(result.stdout))
    assert "output of test:component" in result.stdout
    assert "frontend step failed" not in result.stderr


def test_parallel_build_failure_skips_only_the_typecheck(lane):
    result, events = lane(FRONTEND_LANE_PARALLEL="1", FAKE_NPM_BUILD="fail")

    assert result.returncode == 1
    assert "start lint" not in events
    assert "end test:node" in events and "end test:component" in events
    assert "----- frontend typecheck: not run (build failed) -----" in result.stdout
    assert "frontend step failed: build (exit 1)" in result.stderr
    assert "typecheck" not in result.stderr


def test_parallel_reports_a_killed_step_as_interrupted_not_as_skipped(lane):
    result, _events = lane(
        FRONTEND_LANE_PARALLEL="1", FAKE_NPM_TEST_COMPONENT="kill"
    )

    assert result.returncode == 1
    assert (
        "----- frontend component-tests: no exit status recorded (interrupted) -----"
        in result.stdout
    )
    assert "not run (build failed)" not in result.stdout
    assert "frontend step failed: component-tests (interrupted)" in result.stderr
    assert "----- frontend typecheck: exit=0" in result.stdout


def test_sequential_runs_in_order_and_stops_at_the_first_failure(lane):
    result, events = lane(FRONTEND_LANE_PARALLEL="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert events == [
        f"{edge} {script}"
        for script in ("sync:ui-plugins", "test:node", "test:component", "build", "lint")
        for edge in ("start", "end")
    ]

    result, events = lane(FRONTEND_LANE_PARALLEL="0", FAKE_NPM_TEST_NODE="fail")
    assert result.returncode != 0
    assert events == [
        "start sync:ui-plugins", "end sync:ui-plugins",
        "start test:node", "end test:node",
    ]
