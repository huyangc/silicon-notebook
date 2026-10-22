"""Real shell launchers with fake services/processes; never bind host ports."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.child_env_testkit import BYTECODE_ENV
from tests.launcher_checkout_testkit import (
    alive,
    build_fake_checkout,
    default_launcher_signals,
    executable,
    kill_published,
    reap_leaked_controllers,
    spawn,
    stop_fake_children,
)

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.xdist_group("extension_launcher_lifecycle"), pytest.mark.process_contract]
# A backstop for a broken run, never the judgement: every wait below ends on a
# handshake (a pid file published by rename, or the launcher's own exit).
LIFECYCLE_CAP_SECONDS = 20


@contextmanager
def runner_disposition(signum: int, disposition):
    """Give the test runner itself ``disposition`` for ``signum`` while it spawns."""
    previous = signal.signal(signum, disposition)
    try:
        yield
    finally:
        signal.signal(signum, previous)


def wait_until(predicate, timeout: float = LIFECYCLE_CAP_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("fake process did not reach expected lifecycle state")
        time.sleep(0.02)


@pytest.fixture
def launcher(tmp_path):
    root, env = build_fake_checkout(tmp_path)
    yield root, env
    stop_fake_children(root)
    # Backstop against accumulation, not a regression guard: an orphaned fake
    # controller waits forever, and hundreds once piled up on a gate host. The
    # cases' own `assert not alive(...)` and scripts/stress_launcher_signals.py
    # are what catch a launcher that leaves its controller behind.
    leaked = reap_leaked_controllers(root, LIFECYCLE_CAP_SECONDS)
    assert not leaked, f"controllers left running after the test (killed): {leaked}"


def run(root, env, path, *args):
    return subprocess.run(["bash", str(root / path), *args], cwd=root, env=env,
                          capture_output=True, text=True, timeout=LIFECYCLE_CAP_SECONDS,
                          preexec_fn=default_launcher_signals)


def events(root):
    return [json.loads(line) for line in (root / "service-events.jsonl").read_text().splitlines()]


def assert_owned_cleanup(root):
    records = events(root)
    assert [record["action"] for record in records] == ["start", "stop"]
    assert records[0]["receipt"] == records[1]["receipt"]
    assert records[1]["ownership"] == {"run_id": "owned-test-run", "owned": True}
    assert not Path(records[0]["receipt"]).exists()


@pytest.mark.parametrize("path,args", [
    ("scripts/dev.sh", ()), ("scripts/prod.sh", ()),
    ("scripts/backend.sh", ("start",)), ("start.sh", ()),
])
def test_service_start_failure_prevents_application_start(launcher, path, args):
    root, env = launcher
    result = run(root, {**env, "FAIL_SERVICE": "1"}, path, *args)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (root / "backend.pid").exists()
    assert not (root / "frontend.pid").exists()
    assert_owned_cleanup(root)


@pytest.mark.parametrize("path,args", [
    ("scripts/dev.sh", ()), ("scripts/prod.sh", ()),
    ("scripts/backend.sh", ("start",)), ("start.sh", ()),
])
@pytest.mark.parametrize("interruption", [signal.SIGTERM, signal.SIGHUP])
@pytest.mark.parametrize("runner", [signal.SIG_DFL, signal.SIG_IGN], ids=["runner-default", "runner-ignores"])
def test_launcher_forwards_cancellation_while_companions_start(launcher, path, args, interruption, runner):
    """``runner-ignores`` is the gate run under ``nohup``: the runner ignoring
    the signal must not make the launcher deaf to it."""
    root, env = launcher
    controller_file = root / "controller.pid"
    with runner_disposition(interruption, runner):
        process = spawn(root, {**env, "WAIT_SERVICE": "1"}, path, *args)
    with process:
        try:
            # Handshake: the controller publishes its pid only after arming its
            # SIGTERM handler, recording the start and writing the receipt.
            wait_until(controller_file.exists)
            process.send_signal(interruption)
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) != 0
            assert not alive(int(controller_file.read_text()))
        finally:
            if process.poll() is None:
                process.kill()
            kill_published(controller_file)
    assert_owned_cleanup(root)
    # The controller watches the launcher itself (a SIGKILLed one runs no trap).
    assert events(root)[0]["launcher_pid"] == process.pid
    assert not (root / "backend.pid").exists()
    assert not (root / "frontend.pid").exists()


@pytest.mark.parametrize("path,args", [
    ("scripts/dev.sh", ()), ("scripts/prod.sh", ()),
    ("scripts/backend.sh", ("start",)), ("start.sh", ()),
])
@pytest.mark.parametrize("interruption", [signal.SIGTERM, signal.SIGHUP])
def test_launcher_cancellation_before_controller_starts_anything(launcher, path, args, interruption):
    """Cancelled while the controller exists but has written no receipt: it is
    stopped, nothing is rolled back (nothing was started) and the receipt goes."""
    root, env = launcher
    controller_file = root / "controller.pid"
    with spawn(root, {**env, "WAIT_SERVICE": "forked"}, path, *args) as process:
        try:
            wait_until(controller_file.exists)
            process.send_signal(interruption)
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) != 0
            assert not alive(int(controller_file.read_text()))
        finally:
            if process.poll() is None:
                process.kill()
            kill_published(controller_file)
    assert not (root / "service-events.jsonl").exists()
    assert not list(Path(env["TMPDIR"]).glob("silicon-extension-start.*"))
    assert not (root / "backend.pid").exists()
    assert not (root / "frontend.pid").exists()


@pytest.mark.parametrize("path,args", [
    ("scripts/dev.sh", ()), ("scripts/prod.sh", ()),
    ("scripts/backend.sh", ("start",)), ("start.sh", ()),
])
@pytest.mark.parametrize("interruption", [signal.SIGTERM, signal.SIGHUP])
def test_launcher_repeats_cancellation_the_controller_missed(launcher, path, args, interruption):
    """A TERM that reaches the controller between fork and exec is lost (the
    fork still runs the launcher's traps); the launcher keeps forwarding until
    the controller is gone instead of waiting for it to finish starting."""
    root, env = launcher
    controller_file = root / "controller.pid"
    with spawn(root, {**env, "WAIT_SERVICE": "misses-first-term"}, path, *args) as process:
        try:
            wait_until(controller_file.exists)
            process.send_signal(interruption)
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) != 0
            assert not alive(int(controller_file.read_text()))
        finally:
            if process.poll() is None:
                process.kill()
            kill_published(controller_file)
    assert (root / "term-missed").exists()
    assert_owned_cleanup(root)
    assert not (root / "backend.pid").exists()
    assert not (root / "frontend.pid").exists()


def test_development_signal_exit_cleans_owned_receipt(launcher):
    root, env = launcher
    with spawn(root, env, "scripts/dev.sh") as process:
        try:
            wait_until(lambda: (root / "backend.pid").exists() and (root / "frontend.pid").exists())
            process.terminate()
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) != 0
        finally:
            if process.poll() is None:
                process.kill()
    assert_owned_cleanup(root)
    for name in ("backend", "frontend"):
        pid = int((root / f"{name}.pid").read_text())
        wait_until(lambda: not alive(pid))


@contextmanager
def dev_launcher(root, env, *, group=False, **extra_env):
    """dev.sh running with both fake children published. Whatever the test does,
    both children are SIGKILLed by exact pid on the way out, so assertions about
    the launcher's own cleanup belong inside the ``with`` body. ``group`` starts
    it as the leader of a session of its own, so a test can signal the whole
    process group as a terminal does."""
    with (root / "launcher.err").open("w") as stream:
        process = spawn(root, {**env, **extra_env}, "scripts/dev.sh", stderr=stream, new_session=group)
    with process:
        try:
            wait_until(lambda: (root / "backend.pid").exists() and (root / "frontend.pid").exists())
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            if group:
                kill_group(process)
            kill_published(root / "backend.pid")
            kill_published(root / "frontend.pid")


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def published_pid(root, name):
    return int((root / f"{name}.pid").read_text())


def parent_pid(pid):
    return int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                              capture_output=True, text=True).stdout.strip())


@pytest.mark.parametrize("interruption,status", [
    (signal.SIGTERM, 143), (signal.SIGHUP, 129), (signal.SIGINT, 130),
])
def test_dev_launcher_stops_both_children_with_term_and_never_needs_kill(launcher, interruption, status):
    root, env = launcher
    with dev_launcher(root, env, TERM_EXITS="backend frontend") as process:
        pids = {name: published_pid(root, name) for name in ("backend", "frontend")}
        # TERM only reaches uvicorn / next themselves if the launcher's child IS
        # that process. For the backend, whose subshell holds two commands, dropping
        # `exec` leaves a subshell in between and this ppid differs. For `( one
        # simple command ) &` (next, the sync) bash does not fork a subshell at all,
        # so dropping its `exec` is not observable here; adding a second command to
        # that subshell is (see the sync cancellation case).
        assert {name: parent_pid(pid) for name, pid in pids.items()} == {"backend": process.pid, "frontend": process.pid}
        process.send_signal(interruption)
        assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == status
        for name, pid in pids.items():
            assert (root / f"{name}.term").exists(), f"{name} never received SIGTERM"
            assert not alive(pid)
        assert "sending SIGKILL" not in (root / "launcher.err").read_text()
    assert_owned_cleanup(root)


def test_dev_launcher_kills_a_child_that_ignores_term_after_the_grace_window(launcher):
    root, env = launcher
    with dev_launcher(root, env, TERM_EXITS="backend", TERM_IGNORED="frontend",
                      START_CLEANUP_GRACE_SECONDS="2",
                      _SCRIPT_TEST_CLEANUP_POLL_INTERVAL_SECONDS="0.05") as process:
        # The well-behaved backend leaves on its first poll, far inside the window
        # (the deadline counts whole $SECONDS, so a window of 1 can be 0.0x s long).
        backend, frontend = published_pid(root, "backend"), published_pid(root, "frontend")
        process.terminate()
        # Without the SIGKILL fallback the launcher would wait forever on `wait`.
        assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == 143
        assert (root / "frontend.term").exists(), "SIGTERM must come before SIGKILL"
        assert (root / "backend.term").exists()
        assert not alive(frontend)
        assert not alive(backend)
        err = (root / "launcher.err").read_text()
        assert f"startup child PID {frontend} did not stop after SIGTERM; sending SIGKILL" in err
        assert f"PID {backend} did not stop" not in err, "only the survivor is killed"
    assert_owned_cleanup(root)


def test_dev_launcher_syncs_ui_plugins_in_frontend_before_next_starts(launcher):
    root, env = launcher
    with dev_launcher(root, env) as process:
        assert (root / "sync.done").exists()
        assert (root / "frontend.after-sync").exists(), "next started before the sync finished"
        assert (root / "sync.cwd").read_text().strip() == str((root / "frontend").resolve())
        process.terminate()
        assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == 143


def test_dev_launcher_starts_no_frontend_and_stops_the_backend_when_the_sync_fails(launcher):
    root, env = launcher
    result = run(root, {**env, "SYNC_MODE": "fail", "TERM_EXITS": "backend"}, "scripts/dev.sh")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "前端插件同步失败(退出码 3)" in result.stderr
    assert not (root / "frontend.pid").exists()
    assert (root / "backend.term").exists(), "the backend was not stopped"
    assert not alive(published_pid(root, "backend"))
    assert_owned_cleanup(root)


@pytest.mark.parametrize("interruption,status", [
    (signal.SIGTERM, 143), (signal.SIGHUP, 129), (signal.SIGINT, 130),
])
def test_dev_launcher_cancelled_during_the_sync_leaves_nothing_running(launcher, interruption, status):
    root, env = launcher
    with (root / "launcher.err").open("w") as stream:
        process = spawn(root, {**env, "SYNC_MODE": "hang", "TERM_EXITS": "backend"},
                        "scripts/dev.sh", stderr=stream)
    with process:
        try:
            wait_until(lambda: (root / "backend.pid").exists() and (root / "sync.pid").exists())
            sync = published_pid(root, "sync")
            backend = published_pid(root, "backend")
            # The sync is the launcher's own child, so the signal reaches it directly
            # (no npm or subshell layer to leave a node behind).
            assert parent_pid(sync) == process.pid
            process.send_signal(interruption)
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == status
            assert not alive(sync)
            assert not alive(backend)
            assert not (root / "frontend.pid").exists()
        finally:
            if process.poll() is None:
                process.kill()
            kill_published(root / "sync.pid")
            kill_published(root / "backend.pid")
    assert_owned_cleanup(root)


def test_dev_launcher_follows_a_backend_that_exits_during_the_sync(launcher):
    root, env = launcher
    # The sync would run for a minute; the backend fails once the sync is running.
    result = run(root, {**env, "SYNC_MODE": "hang", "FAIL_MAIN": "backend", "FAIL_AFTER": "sync"},
                 "scripts/dev.sh")
    assert result.returncode == 8, result.stdout + result.stderr
    assert not alive(published_pid(root, "sync")), "the sync was left running"
    assert not (root / "frontend.pid").exists()
    assert_owned_cleanup(root)


def test_dev_launcher_runs_every_hook_that_npm_run_dev_would_run():
    """dev.sh starts next itself, so each lifecycle hook `npm run dev` ran must be
    in dev.sh by hand; a hook added to package.json fails here until it is."""
    frontend = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))["scripts"]
    assert frontend["predev"] == "npm run sync:ui-plugins"
    assert frontend["sync:ui-plugins"] == "node scripts/sync-ui-plugins.mjs"
    assert frontend["dev"] == "next dev"
    for hook in ("postdev", "presync:ui-plugins", "postsync:ui-plugins"):
        assert hook not in frontend
    dev_sh = (ROOT / "scripts/dev.sh").read_text(encoding="utf-8")
    assert "( exec node scripts/sync-ui-plugins.mjs ) &" in dev_sh
    assert '( exec "$ROOT_DIR/frontend/node_modules/.bin/next" dev ) &' in dev_sh


def test_dev_launcher_returns_the_failed_childs_status_and_stops_the_other(launcher):
    root, env = launcher
    result = run(root, {**env, "FAIL_MAIN": "backend", "FAIL_AFTER": "frontend",
                        "TERM_EXITS": "frontend"}, "scripts/dev.sh")
    assert result.returncode == 8, result.stdout + result.stderr
    assert (root / "frontend.term").exists()
    assert not alive(published_pid(root, "frontend"))
    assert_owned_cleanup(root)


@pytest.mark.parametrize("path", ["scripts/dev.sh", "scripts/prod.sh"])
@pytest.mark.parametrize("interruption,status", [
    (signal.SIGTERM, 143), (signal.SIGHUP, 129), (signal.SIGINT, 130),
])
def test_dev_and_prod_launchers_exit_with_the_same_status_per_signal(launcher, path, interruption, status):
    root, env = launcher
    controller_file = root / "controller.pid"
    with spawn(root, {**env, "WAIT_SERVICE": "1"}, path) as process:
        try:
            wait_until(controller_file.exists)
            process.send_signal(interruption)
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == status
        finally:
            if process.poll() is None:
                process.kill()
            kill_published(controller_file)
    assert_owned_cleanup(root)


@pytest.mark.parametrize("path", ["scripts/dev.sh", "scripts/prod.sh"])
@pytest.mark.parametrize("grace", ["0", "-1", "ten", "1.5"])
def test_dev_and_prod_launchers_reject_a_bad_grace_before_starting_anything(launcher, path, grace):
    root, env = launcher
    result = run(root, {**env, "START_CLEANUP_GRACE_SECONDS": grace}, path)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "START_CLEANUP_GRACE_SECONDS 必须是正整数" in result.stderr
    assert not (root / "service-events.jsonl").exists()
    assert not (root / "backend.pid").exists()
    assert not (root / "frontend.pid").exists()


@pytest.mark.parametrize("script", ["dev.sh", "prod.sh"])
def test_dev_and_prod_launchers_use_the_one_shared_wind_down(script):
    """The wind-down lives in scripts/launcher_cleanup.sh only (extension_services.sh
    stays the companion handshake's alone); the launchers must source it, install
    its traps, and bracket every child fork so a pid left unassigned by a pending
    trap is still found."""
    text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert body.index('source "$ROOT_DIR/scripts/extension_services.sh"') < body.index(
        'source "$ROOT_DIR/scripts/launcher_cleanup.sh"')
    assert "launcher_install_traps" in body
    assert "launcher_check_grace_seconds" in body
    for private in ("cleanup() {", "kill -9", "SIGKILL"):
        assert private not in body, f"{script} carries its own wind-down: {private!r}"
    # prod.sh alone clears the traps at its hand-off; nothing else installs one.
    assert [line.strip() for line in body.splitlines() if "trap " in line] in (
        [], ["trap - EXIT INT TERM HUP"])
    lines = [line for line in body.splitlines() if line.strip()]
    marks = [i for i, line in enumerate(lines) if line.startswith("launcher_fork_mark ")]
    assert marks
    assert len(marks) == sum(1 for line in lines if re.fullmatch(r"\w+=\$!", line))
    assert len(marks) == sum(1 for line in lines if line == "launcher_fork_done")
    for start in marks:
        variable = lines[start].split()[1]
        # mark / one `( ... ) &` fork / VAR=$! / done, nothing in between: a mark
        # moved earlier would name a pid that is not the child's (it could adopt the
        # companion controller), a done moved earlier or later leaves the fork
        # unprotected or protected for too long.
        assert lines[start + 1].startswith("("), f"{script}: {lines[start + 1]!r} after the mark"
        end = next(i for i in range(start + 1, len(lines)) if lines[i] == f"{variable}=$!")
        block = lines[start + 1:end]
        assert [line for line in block if line.endswith(" &")] == [block[-1]]
        assert not [line for line in block if line.startswith("launcher_fork_")]
        assert lines[end + 1] == "launcher_fork_done"


def group_members(pgid):
    listing = subprocess.run(["ps", "-ax", "-o", "pid=,pgid=,stat="],
                             capture_output=True, text=True).stdout
    members = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 3 and int(parts[1]) == pgid and not parts[2].startswith("Z"):
            members.append(int(parts[0]))
    return members


@pytest.mark.parametrize("interruption,status", [(signal.SIGTERM, 143), (signal.SIGHUP, 129)])
def test_a_child_whose_pid_a_pending_trap_left_unassigned_is_still_stopped(tmp_path, interruption, status):
    """The window is reproduced deterministically: `kill` runs the trap right after
    the background fork returned and before `PID=$!`, the state a signal pending at
    the fork leaves on bash 3.2."""
    script = tmp_path / "unassigned.sh"
    script.write_text(f'''set -euo pipefail
ROOT_DIR={ROOT}
PYTHON_BIN=true
source "$ROOT_DIR/scripts/extension_services.sh"
source "$ROOT_DIR/scripts/launcher_cleanup.sh"
launcher_install_traps
launcher_fork_mark BACKEND_PID
( exec sleep 60 ) &
kill -{interruption.name[3:]} $$
BACKEND_PID=$!
launcher_fork_done
exit 3
''')
    with subprocess.Popen(["/bin/bash", str(script)], env={"PATH": "/usr/bin:/bin", "START_CLEANUP_GRACE_SECONDS": "5", **BYTECODE_ENV},
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          preexec_fn=default_launcher_signals, start_new_session=True) as process:
        try:
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == status
            wait_until(lambda: not group_members(process.pid), timeout=5)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("target", ["$$", "-- -$$"], ids=["launcher", "group"])
def test_a_signal_that_cuts_the_reaping_short_does_not_let_the_companion_stop_run_early(tmp_path, target):
    """A third signal lands while the launcher waits for a SIGKILLed child to be
    reaped: `wait` returns early with the child still there. The reaping must go on
    until the child is gone, and only then are the companions stopped. The harness
    delays the SIGKILL's effect (a `kill` function: the child dies a second after
    `kill -9`) and sends the signal in between; the stand-in for the companion stop
    records whether the child was still alive when it ran."""
    stop_state = tmp_path / "stop.state"
    stub = tmp_path / "python-stub"
    stub.write_text(f'''#!/bin/bash
if [[ "$2" == stop ]]; then
  if kill -0 "$(cat {tmp_path}/child.pid)" 2>/dev/null; then echo alive; else echo dead; fi > {stop_state}
fi
''')
    stub.chmod(0o755)
    script = tmp_path / "reaping.sh"
    script.write_text(f'''set -euo pipefail
ROOT_DIR={ROOT}
PYTHON_BIN={stub}
source "$ROOT_DIR/scripts/extension_services.sh"
source "$ROOT_DIR/scripts/launcher_cleanup.sh"
launcher_install_traps
START_CLEANUP_GRACE_SECONDS=1
_SCRIPT_TEST_CLEANUP_POLL_INTERVAL_SECONDS=0.05
kill() {{
  if [[ "${{1:-}}" == "-9" ]]; then
    ( trap '' INT TERM HUP; /bin/sleep 1; builtin kill -9 "$2" ) &
    ( trap '' INT TERM HUP; /bin/sleep 0.3; builtin kill -TERM {target} ) &
    return 0
  fi
  builtin kill "$@"
}}
EXTENSION_SERVICES_RECEIPT={tmp_path}/receipt
echo started > "$EXTENSION_SERVICES_RECEIPT"
( trap '' TERM; exec /bin/sleep 60 ) &
BACKEND_PID=$!
echo "$BACKEND_PID" > {tmp_path}/child.pid
kill -TERM $$
exit 3
''')
    with subprocess.Popen(["/bin/bash", str(script)], env={"PATH": "/usr/bin:/bin", **BYTECODE_ENV},
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          preexec_fn=default_launcher_signals, start_new_session=True) as process:
        try:
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == 143
            assert stop_state.read_text().strip() == "dead"
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("path", ["scripts/dev.sh", "scripts/prod.sh"])
@pytest.mark.parametrize("second", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_a_second_cancellation_in_the_grace_wait_kills_now_and_still_stops_the_companions(launcher, path, second):
    """uvicorn prints "CTRL+C to force quit" while it winds down and the user
    obeys. That second signal used to hit the launcher's default disposition: it
    died with the signal, the receipt stayed and the companions kept running."""
    root, env = launcher
    extra = {"TERM_EXITS": "backend", "TERM_IGNORED": "frontend",
             "START_CLEANUP_GRACE_SECONDS": "30",
             "_SCRIPT_TEST_CLEANUP_POLL_INTERVAL_SECONDS": "0.05"}
    if path.endswith("prod.sh"):
        # Hold prod.sh just before its hand-off, children running (see the fake rm).
        extra["RM_BLOCK"] = "1"
    with (root / "launcher.err").open("w") as stream:
        process = spawn(root, {**env, **extra}, path, stderr=stream)
    with process:
        try:
            wait_until(lambda: (root / "backend.pid").exists() and (root / "frontend.pid").exists())
            if path.endswith("prod.sh"):
                wait_until((root / "rm.blocked").exists)
            backend, frontend = published_pid(root, "backend"), published_pid(root, "frontend")
            process.send_signal(signal.SIGTERM)
            (root / "rm.release").touch()
            # Both children have had SIGTERM, so the launcher is in its grace wait
            # (30 s) with the frontend ignoring it.
            wait_until(lambda: (root / "backend.term").exists() and (root / "frontend.term").exists())
            process.send_signal(second)
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == 143
            assert not alive(frontend)
            assert not alive(backend)
            assert f"startup child PID {frontend} did not stop after SIGTERM; sending SIGKILL" in (
                root / "launcher.err").read_text()
        finally:
            if process.poll() is None:
                process.kill()
            kill_published(root / "backend.pid")
            kill_published(root / "frontend.pid")
    assert_owned_cleanup(root)


# A terminal sends Ctrl-C (and a closing terminal SIGHUP) to the whole foreground
# process group: the launcher, its foreground `sleep`, and every child still in the
# group. The cases above signal the launcher's pid alone; these start it as a session
# leader and signal the group, the way a terminal does.
SIGNAL_STATUS = [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)]


@pytest.mark.parametrize("interruption,status", SIGNAL_STATUS)
def test_a_group_signal_stops_both_children_and_keeps_the_exit_status(launcher, interruption, status):
    root, env = launcher
    with dev_launcher(root, env, group=True, TERM_EXITS="backend frontend") as process:
        pids = {name: published_pid(root, name) for name in ("backend", "frontend")}
        os.killpg(process.pid, interruption)
        assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == status
        for name, pid in pids.items():
            if interruption != signal.SIGHUP:  # a group SIGHUP kills the fake children itself
                assert (root / f"{name}.term").exists(), f"{name} never received SIGTERM"
            assert not alive(pid)
    assert_owned_cleanup(root)


@pytest.mark.parametrize("interruption,status", SIGNAL_STATUS)
def test_a_group_signal_during_the_sync_leaves_nothing_running(launcher, interruption, status):
    root, env = launcher
    with (root / "launcher.err").open("w") as stream:
        process = spawn(root, {**env, "SYNC_MODE": "hang", "TERM_EXITS": "backend"},
                        "scripts/dev.sh", stderr=stream, new_session=True)
    with process:
        try:
            wait_until(lambda: (root / "backend.pid").exists() and (root / "sync.pid").exists())
            sync, backend = published_pid(root, "sync"), published_pid(root, "backend")
            os.killpg(process.pid, interruption)
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == status
            assert not alive(sync)
            assert not alive(backend)
            assert not (root / "frontend.pid").exists()
        finally:
            kill_group(process)
    assert_owned_cleanup(root)


@pytest.mark.parametrize("path", ["scripts/dev.sh", "scripts/prod.sh"])
@pytest.mark.parametrize("interruption,status", SIGNAL_STATUS)
def test_a_group_signal_while_companions_start_rolls_everything_back(launcher, path, interruption, status):
    root, env = launcher
    controller_file = root / "controller.pid"
    with spawn(root, {**env, "WAIT_SERVICE": "1"}, path, new_session=True) as process:
        try:
            wait_until(controller_file.exists)
            os.killpg(process.pid, interruption)
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == status
            assert not alive(int(controller_file.read_text()))
        finally:
            kill_group(process)
            kill_published(controller_file)
    assert_owned_cleanup(root)
    assert not (root / "backend.pid").exists()


@pytest.mark.parametrize("path", ["scripts/dev.sh", "scripts/prod.sh"])
@pytest.mark.parametrize("second", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_a_second_group_signal_in_the_grace_wait_still_kills_reaps_and_stops_the_companions(launcher, path, second):
    """The second Ctrl-C of a real terminal also interrupts the launcher's own
    foreground `sleep`; under `set -e` that failed command must not end the wind-down
    before the SIGKILL, the reaping and the companion stop."""
    root, env = launcher
    extra = {"TERM_EXITS": "backend", "TERM_IGNORED": "frontend",
             "START_CLEANUP_GRACE_SECONDS": "30",
             "_SCRIPT_TEST_CLEANUP_POLL_INTERVAL_SECONDS": "0.05"}
    if path.endswith("prod.sh"):
        extra["RM_BLOCK"] = "1"
    with (root / "launcher.err").open("w") as stream:
        process = spawn(root, {**env, **extra}, path, stderr=stream, new_session=True)
    with process:
        try:
            wait_until(lambda: (root / "backend.pid").exists() and (root / "frontend.pid").exists())
            if path.endswith("prod.sh"):
                wait_until((root / "rm.blocked").exists)
            backend, frontend = published_pid(root, "backend"), published_pid(root, "frontend")
            os.killpg(process.pid, signal.SIGINT)
            (root / "rm.release").touch()
            wait_until(lambda: (root / "backend.term").exists() and (root / "frontend.term").exists())
            os.killpg(process.pid, second)
            assert process.wait(timeout=LIFECYCLE_CAP_SECONDS) == 130
            assert not alive(frontend), "the child that ignores TERM was left running"
            assert not alive(backend)
            assert f"startup child PID {frontend} did not stop after SIGTERM; sending SIGKILL" in (
                root / "launcher.err").read_text()
        finally:
            kill_group(process)
            kill_published(root / "backend.pid")
            kill_published(root / "frontend.pid")
    assert_owned_cleanup(root)


@pytest.mark.parametrize("path,args", [
    ("scripts/prod.sh", ()), ("scripts/backend.sh", ("start",)), ("start.sh", ()),
])
def test_background_success_hands_off_without_stopping_services(launcher, path, args):
    root, env = launcher
    result = run(root, env, path, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [record["action"] for record in events(root)] == ["start"]
    assert not Path(events(root)[0]["receipt"]).exists()
    wait_until(lambda: (root / "backend.pid").exists())
    assert alive(int((root / "backend.pid").read_text()))


@pytest.mark.parametrize("failed", ["backend", "frontend"])
def test_packaged_application_failure_rolls_back_other_child_and_services(launcher, failed):
    root, env = launcher
    # The real launcher has a fixed startup observation interval. Synchronize
    # that observation with fake-child entry, so heavily loaded xdist workers
    # test a process that failed, rather than one not scheduled to execute yet.
    executable(root / "fake-bin/sleep", '''#!/bin/bash
if [[ "$1" == "2" ]]; then
  for ((attempt=0; attempt<500; attempt++)); do
    if [[ -f "$FAKE_ROOT/backend.pid" && -f "$FAKE_ROOT/frontend.pid" ]]; then
      exec /bin/sleep 0.1
    fi
    /bin/sleep 0.02
  done
  exit 1
fi
exec /bin/sleep "$@"
''')
    result = run(root, {**env, "FAIL_MAIN": failed}, "start.sh")
    assert result.returncode != 0, result.stdout + result.stderr
    assert_owned_cleanup(root)
    for name in ("backend", "frontend"):
        pid = int((root / f"{name}.pid").read_text())
        wait_until(lambda: not alive(pid))
        assert not (root / ".local/run" / f"{name}.pid").exists()


@pytest.mark.parametrize("path,args", [
    ("scripts/stop.sh", ()), ("scripts/backend.sh", ("stop",)), ("stop.sh", ()),
])
def test_stop_without_host_process_still_stops_companion_services(launcher, path, args):
    root, env = launcher
    result = run(root, env, path, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events(root) == [{"action": "stop", "receipt": None}]


@pytest.mark.parametrize("action", ["stop", "status"])
def test_backend_diagnostics_failure_does_not_block_saved_service_controls(launcher, action):
    root, env = launcher
    result = run(root, {**env, "FAIL_DATABASE": "1"}, "scripts/backend.sh", action)
    assert result.returncode != 0
    assert events(root) == [{"action": action, "receipt": None}]


PACK_HELPERS = (
    "extension_services.sh", "extension_services.py", "extension_service_runtime.py",
    "extension_service_worker.py", "check_cli_extensions.py", "python_env.py",
)
PINNED_NODE_MAJOR = (ROOT / ".nvmrc").read_text(encoding="utf-8").strip()


def _pack_fixture(root, env, *, archive_node_major):
    """A checkout pack.sh can run without a real build, whose portable node
    archive declares ``archive_node_major`` in include/node/node_version.h (no
    header at all when it is None) while the build host's node and NODE_VERSION
    both report the pinned major."""
    for name in (*PACK_HELPERS, "pack.sh", "node_version.sh", "migrate_legacy_model_env.py"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    shutil.copy2(ROOT / ".nvmrc", root / ".nvmrc")
    shutil.copytree(ROOT / "packaging", root / "packaging")
    for name in (".env.example", "model-services.example.toml"):
        shutil.copy2(ROOT / name, root / name)
    (root / "backend/app").mkdir()
    (root / "backend/app/__init__.py").write_text("")
    (root / "backend/requirements.txt").write_text("")
    for directory in ("standalone", "static"):
        (root / "frontend/.next" / directory).mkdir(parents=True)
    (root / "frontend/.next/standalone/server.js").write_text("// prebuilt fixture\n")
    executable(root / "fake-bin/npm", "#!/bin/sh\nexit 0\n")
    executable(root / "fake-bin/node", f"#!/bin/sh\necho v{PINNED_NODE_MAJOR}.0.0\n")
    header = root / "node_version.h"
    header.write_text(
        "#ifndef SRC_NODE_VERSION_H_\n"
        f"#define NODE_MAJOR_VERSION {archive_node_major}\n"
        "#define NODE_MINOR_VERSION 0\n",
        encoding="utf-8",
    )
    node_tar = root / "node-fixture.tar"
    with tarfile.open(node_tar, "w") as archive:
        archive.add(root / "fake-bin/node", arcname="node-fixture/bin/node")
        if archive_node_major is not None:
            archive.add(header, arcname="node-fixture/include/node/node_version.h")
    return run(root, {
        **env, "PACK_PYTHON": sys.executable, "VERSION": "fixture",
        "NODE_TARBALL": str(node_tar), "NODE_VERSION": f"v{PINNED_NODE_MAJOR}.0.0",
        "SKIP_WHEELHOUSE": "1",
    }, "scripts/pack.sh")


def test_pack_refuses_a_portable_node_archive_of_another_major(launcher):
    """NODE_TARBALL can hold any release whatever NODE_VERSION says, so pack.sh
    checks the archive's own header (the binary may be for another OS/arch)."""
    root, env = launcher
    result = _pack_fixture(root, env, archive_node_major=22)
    assert result.returncode != 0
    assert (
        f"the portable node archive {root / 'node-fixture.tar'}) needs Node.js "
        f"{PINNED_NODE_MAJOR}.x (pinned in .nvmrc); found 22"
    ) in result.stderr
    assert not list((root / "dist").glob("silicon_notebook_*.tar.gz"))


def test_pack_refuses_a_portable_node_archive_without_a_version_header(launcher):
    root, env = launcher
    result = _pack_fixture(root, env, archive_node_major=None)
    assert result.returncode != 0
    assert "便携 node 包里没有 include/node/node_version.h" in result.stderr
    assert not list((root / "dist").glob("silicon_notebook_*.tar.gz"))


def test_offline_archive_ships_executable_service_runtime_without_a_real_build(launcher):
    root, env = launcher
    helpers = PACK_HELPERS
    result = _pack_fixture(root, env, archive_node_major=PINNED_NODE_MAJOR)
    assert result.returncode == 0, result.stdout + result.stderr
    [release] = (root / "dist").glob("silicon_notebook_*.tar.gz")
    with tarfile.open(release) as archive:
        for helper in helpers:
            [member] = [entry for entry in archive.getmembers() if entry.name.endswith("/scripts/" + helper)]
            assert archive.extractfile(member).read() == (ROOT / "scripts" / helper).read_bytes()
    [stage] = (root / "dist/stage").iterdir()
    help_result = subprocess.run(
        [sys.executable, str(stage / "scripts/extension_services.py"), "--help"],
        cwd=stage, env=env, capture_output=True, text=True, timeout=5,
    )
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert "validate" in help_result.stdout
