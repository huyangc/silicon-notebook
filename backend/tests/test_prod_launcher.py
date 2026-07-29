from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _prepare_launcher(tmp_path: Path) -> tuple[Path, Path, Path, Path, dict[str, str]]:
    repository_root = Path(__file__).resolve().parents[2]
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    backend = root / "backend"
    fake_bin = root / "fake-bin"
    next_bin = root / "frontend" / "node_modules" / ".bin"
    for directory in (scripts, backend, fake_bin, next_bin):
        directory.mkdir(parents=True, exist_ok=True)

    (scripts / "prod.sh").write_text(
        (repository_root / "scripts" / "prod.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (scripts / "autotune.sh").write_text("# test stub\n", encoding="utf-8")
    (root / ".env").write_text("# test env\n", encoding="utf-8")
    (backend / "requirements.txt").write_text("# test requirements\n", encoding="utf-8")

    _write_executable(
        fake_bin / "python",
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then
  printf 'python %s\n' "$*" >>"$INSTALL_CALLS_FILE"
  exit 0
fi
if [[ "${1:-}" == "-c" ]]; then
  exec "$REAL_PYTHON" "$@"
fi
exec sleep 30
""",
    )
    _write_executable(
        fake_bin / "npm",
        """#!/usr/bin/env bash
printf 'npm %s\n' "$*" >>"$INSTALL_CALLS_FILE"
""",
    )
    _write_executable(
        next_bin / "next",
        """#!/usr/bin/env bash
exec sleep 30
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
url=""
for arg in "$@"; do
  case "$arg" in http://*) url="$arg" ;; esac
done
printf '%s\n' "$url" >>"$CURL_CALLS_FILE"
if [[ "$url" == */api/ready ]]; then
  printf '{"ready":true}\n'
fi
""",
    )
    _write_executable(fake_bin / "ss", "#!/usr/bin/env bash\nexit 0\n")

    calls_file = root / "curl-calls.txt"
    install_calls_file = root / "install-calls.txt"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PYTHON_BIN": str(fake_bin / "python"),
        "REAL_PYTHON": sys.executable,
        "CURL_CALLS_FILE": str(calls_file),
        "INSTALL_CALLS_FILE": str(install_calls_file),
        "SKIP_BUILD": "1",
        "START_TIMEOUT_SECONDS": "5",
        "PORT": "18800",
        "FRONTEND_PORT": "18801",
    }
    return root, scripts, fake_bin, next_bin, env


def test_prod_launcher_detaches_and_waits_for_curl_readiness(tmp_path: Path) -> None:
    root, scripts, _fake_bin, _next_bin, env = _prepare_launcher(tmp_path)
    calls_file = Path(env["CURL_CALLS_FILE"])
    install_calls_file = Path(env["INSTALL_CALLS_FILE"])
    backend = root / "backend"
    launched_pids: list[int] = []
    try:
        completed = subprocess.run(
            ["bash", str(scripts / "prod.sh")],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "background services will keep running" in completed.stdout
        assert "stop them with: npm run stop" in completed.stdout

        launched_pids = [int(value) for value in re.findall(r"\(PID (\d+),", completed.stdout)]
        assert len(launched_pids) == 2
        assert all(_pid_is_alive(pid) for pid in launched_pids)

        install_calls = install_calls_file.read_text(encoding="utf-8").splitlines()
        assert install_calls == [
            f"python -m pip install --disable-pip-version-check -r {backend / 'requirements.txt'}",
            f"npm ci --prefix {root / 'frontend'}",
        ]

        calls = calls_file.read_text(encoding="utf-8").splitlines()
        assert calls.count("http://127.0.0.1:18800/api/ready") >= 2
        assert calls.count("http://127.0.0.1:18801/") >= 2
    finally:
        for pid in launched_pids:
            if _pid_is_alive(pid):
                os.kill(pid, signal.SIGTERM)
        time.sleep(0.05)


def test_failed_start_escalates_and_reaps_term_ignoring_children(tmp_path: Path) -> None:
    root, scripts, fake_bin, next_bin, env = _prepare_launcher(tmp_path)
    backend_pid_file = root / "backend.pid"
    frontend_pid_file = root / "frontend.pid"
    _write_executable(
        fake_bin / "python",
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then exit 0; fi
if [[ "${1:-}" == "-c" ]]; then exec "$REAL_PYTHON" "$@"; fi
printf '%s\n' "$$" >"$BACKEND_PID_FILE"
trap '' TERM
exec sleep 30
""",
    )
    _write_executable(
        next_bin / "next",
        """#!/usr/bin/env bash
printf '%s\n' "$$" >"$FRONTEND_PID_FILE"
trap '' TERM
exec sleep 30
""",
    )
    _write_executable(fake_bin / "curl", "#!/usr/bin/env bash\nexit 22\n")
    env.update(
        BACKEND_PID_FILE=str(backend_pid_file),
        FRONTEND_PID_FILE=str(frontend_pid_file),
        START_TIMEOUT_SECONDS="1",
        START_CLEANUP_GRACE_SECONDS="1",
    )

    launched_pids: list[int] = []
    try:
        completed = subprocess.run(
            ["bash", str(scripts / "prod.sh")],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        assert completed.returncode == 1, completed.stdout + completed.stderr
        launched_pids = [
            int(backend_pid_file.read_text(encoding="utf-8")),
            int(frontend_pid_file.read_text(encoding="utf-8")),
        ]
        assert not any(_pid_is_alive(pid) for pid in launched_pids)
        assert completed.stderr.count("sending SIGKILL") == 2
    finally:
        for pid in launched_pids:
            if _pid_is_alive(pid):
                os.kill(pid, signal.SIGKILL)


def test_port_preflight_detects_ss_listener_without_visible_pid(tmp_path: Path) -> None:
    root, scripts, fake_bin, _next_bin, env = _prepare_launcher(tmp_path)
    _write_executable(
        fake_bin / "ss",
        """#!/usr/bin/env bash
printf 'LISTEN 0 4096 0.0.0.0:18800 0.0.0.0:*\n'
""",
    )

    completed = subprocess.run(
        ["bash", str(scripts / "prod.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "backend 端口 :18800 已被占用(PID unavailable)" in completed.stderr
    assert not Path(env["INSTALL_CALLS_FILE"]).exists()
