from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "backend.sh"
pytestmark = pytest.mark.xdist_group("backend_script_contract")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        PYTHON_BIN=sys.executable,
        HOST="127.0.0.1",
        PORT="59993",
        LOG_FILE=str(tmp_path / "backend.log"),
        DATABASE_URL=f"sqlite:///{tmp_path / 'backend.db'}",
        SILICON_NOTEBOOK_STORAGE_DIR=str(tmp_path / "storage"),
        SILICON_NOTEBOOK_ENV_FILE="",
        ALLOW_NO_ENV_FILE="1",
        LLM_LOG_ENABLED="false",
        EVENT_LOG_ENABLED="false",
        START_TIMEOUT_SECONDS="1",
    )
    return env


def _run(
    command: str, env: dict[str, str], *, timeout: float = 8
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), command],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def test_backend_start_timeout_terminates_the_exact_launched_process(tmp_path):
    """Timeout cleanup is process-level, but needs no listener or ambient service."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "lsof", "#!/bin/sh\nexit 0\n")
    _write_executable(fake_bin / "curl", "#!/bin/sh\nexit 7\n")
    pid_file = tmp_path / "launched.pid"
    wrapper = fake_bin / "python-wrapper"
    _write_executable(
        wrapper,
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exec \"$REAL_PYTHON\" \"$@\"; fi\n"
        "printf '%s\\n' \"$$\" > \"$FAKE_PID_FILE\"\n"
        "exec sleep 60\n",
    )
    env = _env(tmp_path)
    env.update(
        PATH=f"{fake_bin}:{env['PATH']}",
        PYTHON_BIN=str(wrapper),
        REAL_PYTHON=sys.executable,
        FAKE_PID_FILE=str(pid_file),
    )

    try:
        result = _run("start", env)

        assert result.returncode != 0
        assert "超时" in result.stdout
        pid = int(pid_file.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if pid_file.exists():
            pid = int(pid_file.read_text(encoding="utf-8"))
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("command", ("status", "start", "stop"))
def test_backend_commands_fail_closed_when_database_helper_fails(
    tmp_path, command
):
    wrapper = tmp_path / "broken-python"
    _write_executable(
        wrapper,
        "#!/bin/sh\n"
        "echo 'driver leaked postgresql://raw-user:raw-password@db/private' >&2\n"
        "exit 17\n",
    )
    env = _env(tmp_path)
    env["PYTHON_BIN"] = str(wrapper)

    result = _run(command, env)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "数据库配置无效" in output
    assert "raw-user" not in output
    assert "raw-password" not in output


@pytest.mark.parametrize("command", ("status", "start", "stop"))
def test_backend_commands_reject_invalid_database_scheme_without_leaking_url(
    tmp_path, command
):
    env = _env(tmp_path)
    env["DATABASE_URL"] = (
        "mysql://raw-user:raw-password@db/private?token=raw-token"
    )

    result = _run(command, env)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "数据库配置无效" in output
    assert "raw-user" not in output
    assert "raw-password" not in output
    assert "raw-token" not in output
