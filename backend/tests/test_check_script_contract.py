"""The local aggregate gate must not silently drop a committed test root."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]


def test_check_script_runs_every_committed_test_root():
    check = (ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    lanes = "\n".join(
        (ROOT / "scripts" / f"check_{name}.sh").read_text(encoding="utf-8")
        for name in ("backend", "contracts", "frontend")
    )
    assert 'check_${lane}.sh' in check
    assert '"$ROOT_DIR/backend/tests"' in lanes
    assert '"$ROOT_DIR/fangan/testcases/harness/tests"' in lanes
    assert "npm run test" in lanes

    package = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    frontend_test = package["scripts"]["test"]
    assert "test:node" in frontend_test
    assert "test:component" in frontend_test


def test_check_script_termination_reaps_lane_descendants(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    check = scripts / "check.sh"
    check.write_bytes((ROOT / "scripts" / "check.sh").read_bytes())
    check.chmod(0o755)
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    lane_body = """#!/usr/bin/env bash
set -euo pipefail
sleep 60 &
child=$!
printf '%s\\n' "$child" >"$CHECK_TEST_PID_DIR/$CHECK_LANE_NAME.pid"
wait "$child"
"""
    for lane in ("backend", "contracts", "frontend"):
        path = scripts / f"check_{lane}.sh"
        path.write_text(lane_body, encoding="utf-8")
        path.chmod(0o755)

    process = subprocess.Popen(
        [str(check)],
        cwd=tmp_path,
        env={
            **os.environ,
            "CHECK_TEST_PID_DIR": str(pid_dir),
            "PYTHON_BIN": sys.executable,
        },
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pids: list[int] = []
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pid_files = sorted(pid_dir.glob("*.pid"))
            if len(pid_files) == 3:
                child_pids = [
                    int(path.read_text(encoding="utf-8")) for path in pid_files
                ]
                break
            time.sleep(0.02)
        assert len(child_pids) == 3

        process.terminate()
        assert process.wait(timeout=5) == 143

        deadline = time.monotonic() + 3
        remaining = set(child_pids)
        while remaining and time.monotonic() < deadline:
            for pid in tuple(remaining):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    remaining.remove(pid)
            if remaining:
                time.sleep(0.02)
        assert remaining == set()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for pid in child_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
