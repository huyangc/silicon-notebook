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
import time
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
    for name in ("dev.sh", "prod.sh", "backend.sh", "stop.sh", "extension_services.sh", "launcher_cleanup.sh"):
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
# TERM_EXITS / TERM_IGNORED: space-separated child names that record a SIGTERM in
# $FAKE_ROOT/<name>.term and then exit (a well-behaved service) or keep running
# (a service that ignores SIGTERM and only SIGKILL stops). The trap is armed
# before the pid file is published, so a reader that waits for the pid file can
# never signal a child that has not armed it yet.
trapped=
if [[ " ${TERM_EXITS:-} " == *" $1 "* ]]; then
  trapped=1; trap ': > "$FAKE_ROOT/$1.term"; exit 0' TERM
elif [[ " ${TERM_IGNORED:-} " == *" $1 "* ]]; then
  trapped=1; trap ': > "$FAKE_ROOT/$1.term"' TERM
fi
# Record whether the UI plugin sync had finished when this child started.
[[ -f "$FAKE_ROOT/sync.done" ]] && : > "$FAKE_ROOT/$1.after-sync"
# Publish the pid file by rename so a reader never sees it created but still empty.
echo "$$" > "$FAKE_ROOT/$1.pid.tmp" && mv "$FAKE_ROOT/$1.pid.tmp" "$FAKE_ROOT/$1.pid"
if [[ "${FAIL_MAIN:-}" == "$1" ]]; then
  # FAIL_AFTER: fail only once that other child has published its pid file.
  while [[ -n "${FAIL_AFTER:-}" && ! -f "$FAKE_ROOT/$FAIL_AFTER.pid" ]]; do /bin/sleep 0.02; done
  exit 8
fi
if [[ -n "$trapped" ]]; then
  while :; do /bin/sleep 0.05; done
fi
exec sleep 60
''')
    python_wrapper = '''#!/bin/bash
case "${1:-}" in
  */extension_services.py)
    if [[ "${2:-}" == start && "${WAIT_SERVICE:-}" == forked ]]; then
      # The controller exists but has not written a receipt or started anything.
      # argv[0] keeps the controller path so checkout_controllers() sees it.
      echo "$$" > "$FAKE_ROOT/controller.pid.tmp" && mv "$FAKE_ROOT/controller.pid.tmp" "$FAKE_ROOT/controller.pid"
      exec -a "$1" /bin/sleep 60
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
    # dev.sh runs `node scripts/sync-ui-plugins.mjs` (the frontend's predev hook)
    # from frontend/ before starting next. SYNC_MODE: ok (default, writes
    # sync.done), hang (waits until killed), fail (exit 3 once the backend has
    # published its pid, so a launcher failing early has already started it).
    executable(root / "fake-bin/node", '''#!/bin/bash
[[ "$1" == "scripts/sync-ui-plugins.mjs" ]] || exit 64
pwd -P > "$FAKE_ROOT/sync.cwd"
echo "$$" > "$FAKE_ROOT/sync.pid.tmp" && mv "$FAKE_ROOT/sync.pid.tmp" "$FAKE_ROOT/sync.pid"
case "${SYNC_MODE:-ok}" in
  hang) exec sleep 60 ;;
  fail)
    while [[ ! -f "$FAKE_ROOT/backend.pid" ]]; do /bin/sleep 0.02; done
    exit 3 ;;
esac
: > "$FAKE_ROOT/sync.done"
''')
    # RM_BLOCK=1 holds prod.sh in the window between forking its children and
    # handing them off: the first `rm` of the extension start receipt (the handoff
    # does it) publishes rm.blocked, waits for rm.release and then returns WITHOUT
    # removing anything, as if the launcher had been interrupted just before it.
    executable(root / "fake-bin/rm", '''#!/bin/bash
if [[ "${RM_BLOCK:-}" == 1 && "$*" == *silicon-extension-start* && ! -e "$FAKE_ROOT/rm.blocked" ]]; then
  : > "$FAKE_ROOT/rm.blocked"
  while [[ ! -e "$FAKE_ROOT/rm.release" ]]; do /bin/sleep 0.02; done
  exit 0
fi
exec /bin/rm "$@"
''')
    # SLEEP_MARKER_ARG=<n>: a `sleep <n>` publishes its pid in `sleeping` (by rename)
    # just before it execs the real sleep, so a test can wait until that pid shows
    # `/bin/sleep <n>` and then signal a launcher that is provably inside the sleep.
    executable(root / "fake-bin/sleep", '''#!/bin/bash
if [[ -n "${SLEEP_MARKER_ARG:-}" && "${1:-}" == "$SLEEP_MARKER_ARG" ]]; then
  echo "$$" > "$FAKE_ROOT/sleeping.tmp" && mv "$FAKE_ROOT/sleeping.tmp" "$FAKE_ROOT/sleeping"
fi
exec /bin/sleep "$@"
''')
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


def checkout_controllers(root: Path) -> list[int]:
    """Live (non-zombie) companion controllers started from this checkout."""
    needle = str(root / "scripts" / "extension_services.py")
    listing = subprocess.run(["ps", "-ax", "-o", "pid=,stat=,command="],
                             capture_output=True, text=True).stdout
    found = []
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and needle in parts[2] and not parts[1].startswith("Z"):
            found.append(int(parts[0]))
    return found


def reap_leaked_controllers(root: Path, timeout: float) -> list[int]:
    """Wait for this checkout's controllers to exit; SIGKILL and return any left.

    A backstop against accumulation, not a regression guard: whatever is still
    running once ``timeout`` passes is killed by exact pid (never by pattern)
    and reported, so a fake controller that would wait forever cannot pile up
    on the host. Launcher regressions are caught by the tests' own
    ``assert not alive(...)`` and by scripts/stress_launcher_signals.py.
    """
    deadline = time.monotonic() + timeout
    while (left := checkout_controllers(root)) and time.monotonic() < deadline:
        time.sleep(0.05)
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return left


def spawn(root: Path, env: dict[str, str], path: str, *args: str,
          new_session: bool = False, stderr=subprocess.DEVNULL) -> subprocess.Popen:
    """``new_session`` puts the launcher and everything it forks in a process
    group of their own, so a caller can kill even a child not yet exec'd.
    ``stderr`` is a file object when a test reads the launcher's diagnostics."""
    return subprocess.Popen(["bash", str(root / path), *args], cwd=root, env=env,
                            stdout=subprocess.DEVNULL, stderr=stderr,
                            preexec_fn=default_launcher_signals, start_new_session=new_session)


def kill_published(pid_file: Path) -> None:
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass
