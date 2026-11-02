"""The local aggregate gate must not silently drop a committed test root."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.process_contract


ROOT = Path(__file__).resolve().parents[2]


def _written_lane_pids(pid_dir: Path) -> list[int]:
    """已经把 pid 写完整的 lane 文件。

    lane 用 `printf … >file` 落 pid,而 shell 的 `>` 是**先创建/截断、再写入**两步。
    只要以「文件出现」为就绪信号,就可能在这两步之间把文件读成空串,`int('')` 直接抛
    ValueError —— CI 上三条泳道并发、runner 负载高时这个窗口被放大,本地几乎不复现。
    改以「读到完整的一行 pid」为就绪信号:要求内容以 printf 写的那个换行结尾(半写入的
    文件不会带它),再解析。尚未写完的文件一律当作未就绪,交给调用方继续轮询。
    """
    pids: list[int] = []
    for path in sorted(pid_dir.glob("*.pid")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not raw.endswith("\n"):
            continue
        try:
            pids.append(int(raw.strip()))
        except ValueError:
            continue
    return pids


@pytest.mark.parametrize("summary", [False, True])
def test_check_script_termination_reaps_lane_descendants(tmp_path, summary):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    check = scripts / "check.sh"
    check.write_bytes((ROOT / "scripts" / "check.sh").read_bytes())
    check.chmod(0o755)
    (scripts / "check_output_summary.py").write_bytes(
        (ROOT / "scripts" / "check_output_summary.py").read_bytes()
    )
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    # pid 文件**故意**分两步写:先创建空文件,握手后再落内容。真实 lane 用的
    # `printf … >file` 本来就是这两步(shell 先创建/截断再写),只是间隙短到本地几乎撞不上
    # —— 曾经因此在 CI 上偶发 `int('')` 而本地怎么跑都是绿的。把间隙固定放大,这个竞态就从
    # "偶发 flake" 变成"确定性用例":谁把就绪判据改回「文件出现即读」,这里必红。
    # release 文件保证 controller 已观察到空文件,无需固定 sleep 猜调度时机。
    lane_body = """#!/usr/bin/env bash
set -euo pipefail
sleep 60 &
child=$!
pid_file="$CHECK_TEST_PID_DIR/$CHECK_LANE_NAME.pid"
: >"$pid_file"
while [[ ! -f "$CHECK_TEST_RELEASE_DIR/$CHECK_LANE_NAME" ]]; do sleep 0.01; done
printf '%s\\n' "$child" >>"$pid_file"
wait "$child"
"""
    for lane in ("backend", "contracts", "frontend"):
        path = scripts / f"check_{lane}.sh"
        path.write_text(lane_body, encoding="utf-8")
        path.chmod(0o755)

    process = subprocess.Popen(
        [str(check), *(["--summary"] if summary else [])],
        cwd=tmp_path,
        env={
            **os.environ,
            "CHECK_TEST_PID_DIR": str(pid_dir),
            "CHECK_TEST_RELEASE_DIR": str(pid_dir),
            "PYTHON_BIN": sys.executable,
        },
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pids: list[int] = []
    try:
        deadline = time.monotonic() + 5
        pid_files: list[Path] = []
        while time.monotonic() < deadline:
            pid_files = sorted(pid_dir.glob("*.pid"))
            if len(pid_files) == 3 and all(
                path.read_text(encoding="utf-8") == "" for path in pid_files
            ):
                break
            time.sleep(0.01)
        assert len(pid_files) == 3
        for lane in ("backend", "contracts", "frontend"):
            (pid_dir / lane).touch()

        while time.monotonic() < deadline:
            child_pids = _written_lane_pids(pid_dir)
            if len(child_pids) == 3:
                break
            time.sleep(0.02)
        # 超时仍不足 3 个:要么 lane 没起来,要么 pid 一直没写完。把实际看到的内容带进
        # 失败信息,免得只留一个 "0 != 3" 让人猜。
        assert len(child_pids) == 3, (
            f"5s 内只等到 {len(child_pids)}/3 个写完整的 lane pid;"
            f" 目录内容={sorted(p.name for p in pid_dir.glob('*.pid'))}"
        )

        process.terminate()
        assert process.wait(timeout=5) == 143
        if summary:
            directories = list((tmp_path / ".local" / "check-logs").iterdir())
            assert len(directories) == 1
            assert {p.name for p in directories[0].glob("*.log")} == {
                "backend.log", "contracts.log", "frontend.log"
            }
            for lane in ("backend", "contracts", "frontend"):
                assert (directories[0] / f"{lane}.exit").read_text().strip() != "0"

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


@pytest.fixture
def fake_gate(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("check.sh", "check_output_summary.py", "check_timing_summary.py"):
        (scripts / name).write_bytes((ROOT / "scripts" / name).read_bytes())
    for lane in ("contracts", "backend", "frontend"):
        path = scripts / f"check_{lane}.sh"
        path.write_text(
            '#!/usr/bin/env bash\n'
            'echo "detail $CHECK_LANE_NAME"\n'
            'printf "%s=2\\n" "$CHECK_LANE_NAME" > "$CHECK_TIMING_FILE"\n'
            'if [[ "$CHECK_LANE_NAME" == backend ]]; then\n'
            '  echo "================== 3 passed in 0.01s =================="\n'
            'fi\n'
            'if [[ "$CHECK_LANE_NAME" == "${FAIL_LANE:-}" ]]; then\n'
            '  echo "AssertionError: important failure"\n'
            '  exit 7\n'
            'fi\n',
            encoding="utf-8",
        )
        path.chmod(0o755)

    def run(*args, **env):
        return subprocess.run(
            ["bash", str(scripts / "check.sh"), *args],
            env={**os.environ, "PYTHON_BIN": sys.executable, **env},
            cwd=tmp_path, capture_output=True, text=True, timeout=15,
        )

    return tmp_path, run


def test_summary_keeps_all_lanes_and_raw_logs_but_hides_success_details(fake_gate):
    root, run = fake_gate
    result = run("--summary")
    assert result.returncode == 0, result.stderr
    for lane in ("contracts", "backend", "frontend"):
        assert f"{lane}: exit=0; elapsed=2s" in result.stdout
    assert "3 passed in 0.01s" in result.stdout
    assert "tests (runner-reported): unavailable" in result.stdout
    assert "detail " not in result.stdout
    directories = list((root / ".local" / "check-logs").iterdir())
    assert len(directories) == 1
    for lane in ("contracts", "backend", "frontend"):
        log = directories[0] / f"{lane}.log"
        assert f"detail {lane}\n" in log.read_text()
        assert str(log) in result.stdout


def test_summary_preserves_failure_and_lane_selection(fake_gate):
    root, run = fake_gate
    result = run("--lane", "frontend", "--summary", FAIL_LANE="frontend")
    assert result.returncode == 1
    assert "frontend: exit=7" in result.stdout
    assert "AssertionError: important failure" in result.stdout
    assert "failure excerpt" in result.stdout
    assert "contracts:" not in result.stdout
    assert "backend:" not in result.stdout
    directory, = (root / ".local" / "check-logs").iterdir()
    assert not (directory / "backend.log").exists()


def test_default_still_prints_full_output_and_rejects_bad_arguments(fake_gate):
    _, run = fake_gate
    result = run("--lane", "backend")
    assert result.returncode == 0
    assert "detail backend" in result.stdout
    for args in (("--summary", "--lane"), ("--summary", "--lane", "bogus"), ("--bogus",)):
        result = run(*args)
        assert result.returncode == 2
        assert "detail " not in result.stdout
