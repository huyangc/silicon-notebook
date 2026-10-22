"""Fake deployment checkout for the real shell launchers (tests and stress).

Shared by ``test_extension_service_launchers.py`` and
``scripts/stress_launcher_signals.py``. It copies the real launchers next to
fake services, a fake companion controller and fake application children, and
never binds a host port.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_TRAPPED_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)


def default_launcher_signals() -> None:
    """``preexec_fn``: the launcher starts with default dispositions.

    A non-interactive shell cannot trap a signal that was ignored when it
    started (POSIX). A gate run under ``nohup`` or a detached harness ignores
    SIGHUP, and a backgrounded lane ignores SIGINT; inheriting either would
    hand the launcher a cancellation it cannot see, and the test would judge
    the test runner's environment instead of the launcher.
    """
    for signum in LAUNCHER_TRAPPED_SIGNALS:
        signal.signal(signum, signal.SIG_DFL)


def executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def alive(pid: int) -> bool:
    """A zombie counts as exited: it has run to completion, only unreaped."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def build_fake_checkout(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "checkout"
    for directory in ("scripts", "backend", "frontend/node_modules/.bin", "fake-bin", ".venv/bin", "node/bin"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for name in ("dev.sh", "prod.sh", "backend.sh", "stop.sh", "extension_services.sh"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    for name in ("start.sh", "stop.sh"):
        shutil.copy2(ROOT / "packaging" / name, root / name)
    (root / "scripts/autotune.sh").write_text("# no host configuration\n")
    (root / ".env").write_text("# isolated deployment\n")
    (root / "frontend/server.js").write_text("// fake node does not evaluate this\n")
    (root / "scripts/extension_services.py").write_text('''
import json, os, pathlib, signal, sys, time
action = sys.argv[1]
receipt = pathlib.Path(sys.argv[sys.argv.index("--receipt") + 1]) if "--receipt" in sys.argv else None
if action == "start" and receipt:
    receipt.write_text(json.dumps({"run_id": "owned-test-run", "owned": True}))
record = {"action": action, "receipt": str(receipt) if receipt else None}
if "--launcher-pid" in sys.argv:
    record["launcher_pid"] = int(sys.argv[sys.argv.index("--launcher-pid") + 1])
if receipt and receipt.exists() and receipt.read_text():
    record["ownership"] = json.loads(receipt.read_text())
with open(os.environ["SERVICE_EVENTS"], "a") as stream:
    stream.write(json.dumps(record) + "\\n")
if action == "start" and os.environ.get("WAIT_SERVICE") in ("1", "misses-first-term"):
    missed = pathlib.Path(os.environ["FAKE_ROOT"], "term-missed")
    def cancelled(signum, frame):
        # misses-first-term: the launcher's first TERM is lost, as it is when it
        # lands before the forked controller has exec'd.
        if os.environ["WAIT_SERVICE"] == "misses-first-term" and not missed.exists():
            missed.write_text("")
            return
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sys.exit(130)
    signal.signal(signal.SIGTERM, cancelled)
    staged = pathlib.Path(os.environ["FAKE_ROOT"], "controller.pid.tmp")
    staged.write_text(str(os.getpid()))
    os.replace(staged, staged.with_suffix(""))
    while True:
        time.sleep(1)
sys.exit(7 if action == "start" and os.environ.get("FAIL_SERVICE") == "1" else 0)
''')
    executable(root / "fake_child.sh", '''#!/bin/bash
# Publish the pid file by rename so a reader never sees it created but still empty.
echo "$$" > "$FAKE_ROOT/$1.pid.tmp" && mv "$FAKE_ROOT/$1.pid.tmp" "$FAKE_ROOT/$1.pid"
[[ "${FAIL_MAIN:-}" == "$1" ]] && exit 8
exec sleep 60
''')
    python_wrapper = '''#!/bin/bash
case "${1:-}" in
  */extension_services.py)
    if [[ "${2:-}" == start && "${WAIT_SERVICE:-}" == forked ]]; then
      # The controller exists but has not written a receipt or started anything.
      echo "$$" > "$FAKE_ROOT/controller.pid.tmp" && mv "$FAKE_ROOT/controller.pid.tmp" "$FAKE_ROOT/controller.pid"
      exec /bin/sleep 60
    fi
    exec "$REAL_PYTHON" "$@" ;;
  */python_env.py)
    if [[ "${2:-}" == "-c" ]]; then
      [[ "${FAIL_DATABASE:-0}" == 1 ]] && exit 2
      echo "sqlite test deployment"; exit 0
    fi
    exec /bin/bash "$FAKE_ROOT/fake_child.sh" backend ;;
  -c) exec "$REAL_PYTHON" "$@" ;;
esac
exit 0
'''
    executable(root / "fake-bin/python", python_wrapper)
    executable(root / ".venv/bin/python", python_wrapper)
    frontend_wrapper = '#!/bin/bash\nexec /bin/bash "$FAKE_ROOT/fake_child.sh" frontend\n'
    for path in ("fake-bin/npm", "frontend/node_modules/.bin/next", "node/bin/node"):
        executable(root / path, frontend_wrapper)
    for name in ("ss", "lsof", "fuser"):
        executable(root / "fake-bin" / name, "#!/bin/sh\nexit 0\n")
    executable(root / "fake-bin/curl", '''#!/bin/bash
case "$*" in
  */openapi.json*) echo '{"info":{"title":"silicon-notebook API"}}' ;;
  */api/ready*)
    if [[ -f "$FAKE_ROOT/backend.pid" ]]; then echo '{"ready":true}'
    else echo '{"ready":false}'; fi ;;
esac
''')
    env = {
        "PATH": f"{root / 'fake-bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "REAL_PYTHON": sys.executable, "PYTHON_BIN": str(root / "fake-bin/python"),
        "FAKE_ROOT": str(root), "SERVICE_EVENTS": str(root / "service-events.jsonl"),
        "SKIP_INSTALL": "1", "SKIP_BUILD": "1", "START_TIMEOUT_SECONDS": "3",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return root, env


def stop_fake_children(root: Path) -> None:
    for name in ("backend", "frontend"):
        path = root / f"{name}.pid"
        if path.exists():
            try:
                os.kill(int(path.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass


def spawn(root: Path, env: dict[str, str], path: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(["bash", str(root / path), *args], cwd=root, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            preexec_fn=default_launcher_signals)


def kill_published(pid_file: Path) -> None:
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass
