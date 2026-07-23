from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "backend.sh"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _env(tmp_path: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        PYTHON_BIN=sys.executable,
        HOST="127.0.0.1",
        PORT=str(port),
        LOG_FILE=str(tmp_path / "backend.log"),
        DATABASE_URL=f"sqlite:///{tmp_path / 'backend.db'}",
        SILICON_NOTEBOOK_STORAGE_DIR=str(tmp_path / "storage"),
        SILICON_NOTEBOOK_ENV_FILE="",
        SILICON_NOTEBOOK_AUTH_OPTIONAL="false",
        ALLOW_NO_ENV_FILE="1",
        LLM_LOG_ENABLED="false",
        EVENT_LOG_ENABLED="false",
        START_TIMEOUT_SECONDS="8",
    )
    return env


def _run(command: str, env: dict[str, str], *, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), command],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def test_backend_script_start_status_stop_with_default_authenticated_api(tmp_path):
    """Anonymous `/api/notebooks` is 401; readiness+OpenAPI still identify us."""
    port = _free_port()
    env = _env(tmp_path, port)
    try:
        started = _run("start", env, timeout=15)
        assert started.returncode == 0, started.stdout + started.stderr
        assert "启动成功" in started.stdout

        with pytest.raises(HTTPError) as captured:
            urlopen(f"http://127.0.0.1:{port}/api/notebooks", timeout=2)
        assert captured.value.code == 401

        status = _run("status", env)
        assert status.returncode == 0
        assert "silicon-notebook 后端" in status.stdout
        assert "notebooks=" not in status.stdout
    finally:
        _run("stop", env, timeout=10)
    assert _run("status", env).stdout.count("空闲") == 1


def test_backend_script_distinguishes_foreign_openapi_service(tmp_path):
    port = _free_port()
    env = _env(tmp_path, port)
    foreign = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(40):
            if foreign.poll() is not None:
                raise AssertionError("foreign server exited")
            try:
                urlopen(f"http://127.0.0.1:{port}/", timeout=0.2)
                break
            except OSError:
                time.sleep(0.05)
        status = _run("status", env)
        assert status.returncode == 0
        assert "但不是 silicon-notebook" in status.stdout
        refused = _run("start", env)
        assert refused.returncode != 0
        assert "别的服务占用" in refused.stdout
    finally:
        foreign.terminate()
        try:
            foreign.wait(timeout=5)
        except subprocess.TimeoutExpired:
            foreign.kill()
            foreign.wait(timeout=5)


def test_backend_start_timeout_terminates_the_exact_launched_process(tmp_path):
    port = _free_port()
    env = _env(tmp_path, port)
    env["START_TIMEOUT_SECONDS"] = "1"
    pid_file = tmp_path / "fake.pid"
    wrapper = tmp_path / "python-wrapper"
    wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exec \"$REAL_PYTHON\" \"$@\"; fi\n"
        "echo $$ > \"$FAKE_PID_FILE\"\n"
        "exec sleep 60\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env.update(
        PYTHON_BIN=str(wrapper),
        REAL_PYTHON=sys.executable,
        FAKE_PID_FILE=str(pid_file),
    )
    try:
        result = _run("start", env, timeout=8)
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
    wrapper.write_text(
        "#!/bin/sh\n"
        "echo 'driver leaked postgresql://raw-user:raw-password@db/private' >&2\n"
        "exit 17\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env = _env(tmp_path, _free_port())
    env["PYTHON_BIN"] = str(wrapper)
    env["START_TIMEOUT_SECONDS"] = "1"

    result = _run(command, env, timeout=4)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "数据库配置无效" in output
    assert "raw-user" not in output
    assert "raw-password" not in output


@pytest.mark.parametrize("command", ("status", "start", "stop"))
def test_backend_commands_reject_invalid_database_scheme_without_leaking_url(
    tmp_path, command
):
    env = _env(tmp_path, _free_port())
    env["DATABASE_URL"] = "mysql://raw-user:raw-password@db/private?token=raw-token"

    result = _run(command, env, timeout=4)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "数据库配置无效" in output
    assert "raw-user" not in output
    assert "raw-password" not in output
    assert "raw-token" not in output
