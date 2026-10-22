"""Real shell launchers with fake services/processes; never bind host ports."""
from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

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
def dev_launcher(root, env, **extra_env):
    """dev.sh running with both fake children published. Whatever the test does,
    both children are SIGKILLed by exact pid on the way out, so assertions about
    the launcher's own cleanup belong inside the ``with`` body."""
    with (root / "launcher.err").open("w") as stream:
        process = spawn(root, {**env, **extra_env}, "scripts/dev.sh", stderr=stream)
    with process:
        try:
            wait_until(lambda: (root / "backend.pid").exists() and (root / "frontend.pid").exists())
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            kill_published(root / "backend.pid")
            kill_published(root / "frontend.pid")


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
        # that process: a subshell or npm layer in between would make this ppid
        # differ and leave the real server running when the layer is signalled.
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
                      START_CLEANUP_GRACE_SECONDS="1",
                      _SCRIPT_TEST_CLEANUP_POLL_INTERVAL_SECONDS="0.05") as process:
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


@pytest.mark.parametrize("grace", ["0", "-1", "ten", "1.5"])
def test_dev_launcher_rejects_a_bad_grace_before_starting_anything(launcher, grace):
    root, env = launcher
    result = run(root, {**env, "START_CLEANUP_GRACE_SECONDS": grace}, "scripts/dev.sh")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "START_CLEANUP_GRACE_SECONDS 必须是正整数" in result.stderr
    assert not (root / "service-events.jsonl").exists()
    assert not (root / "backend.pid").exists()
    assert not (root / "frontend.pid").exists()


def _cleanup_function(script):
    lines = (ROOT / "scripts" / script).read_text(encoding="utf-8").splitlines()
    start = lines.index("cleanup() {")
    end = lines.index("}", start)
    return "\n".join(lines[start:end + 1])


def test_dev_launcher_cleanup_is_the_same_shape_as_prod_launcher_cleanup():
    """No shared function exists (extension_services.sh is the companion
    handshake's alone), so the two copies are pinned against drifting apart."""
    assert _cleanup_function("dev.sh") == _cleanup_function("prod.sh")


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
