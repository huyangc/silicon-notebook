"""Hermetic lifecycle integration: only self-contained child processes, no ports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import extension_services as manager
import extension_service_runtime as runtime


# Lifecycle cases already exercise real process/thread concurrency. Keep unrelated
# supervisors from competing for startup deadlines across xdist workers.
pytestmark = pytest.mark.xdist_group("extension_service_lifecycle")


@pytest.fixture
def directory(tmp_path):
    directory = tmp_path / "runtime"
    runtime.ensure_state(directory)
    yield directory
    manager.stop(directory, None)


def service(tmp_path, *, key="demo/main", command=None, probe=None, mode="managed"):
    return {
        "key": key, "mode": mode,
        "command": command or [sys.executable, "-c", "import time; time.sleep(60)"],
        "cwd": str(tmp_path), "env": dict(os.environ),
        "healthcheck_url": "", "healthcheck_command": probe or [sys.executable, "-c", "pass"],
        "startup_timeout_seconds": 10.0, "shutdown_timeout_seconds": 0.1,
        "healthcheck_timeout_seconds": 2.0,
    }


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_start_reuses_only_matching_ready_session_and_receipt_owns_cleanup(directory, tmp_path):
    own = tmp_path / "own.json"
    other = tmp_path / "other.json"
    assert manager.start(directory, [service(tmp_path)], "same", own) == {"state": "ready", "reused": False}
    assert manager.status(directory)["services"]["demo/main"]["state"] == "ready"
    assert manager.start(directory, [service(tmp_path)], "same", other)["reused"]
    assert manager.stop(directory, other)["state"] == "not_owned"
    assert runtime.running(directory)
    with pytest.raises(runtime.ServiceError, match="configuration_changed"):
        manager.start(directory, [service(tmp_path)], "changed", None)
    assert manager.stop(directory, own)["state"] == "stopped"
    assert not runtime.running(directory)
    assert own.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700


def test_stop_ignores_environment_and_deleted_configuration(directory, tmp_path):
    manager.start(directory, [service(tmp_path)], "same", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "extension_services.py"), "stop", "--state-dir", str(directory)],
        env={**os.environ, "SILICON_NOTEBOOK_ENV_FILE": str(tmp_path / "missing"), "EXTENSIONS_CONFIG": "missing"},
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0
    assert not runtime.running(directory)


@pytest.mark.parametrize("kind", ["timeout", "exit"])
def test_start_failure_cleans_previously_started_services(directory, tmp_path, kind):
    failing = service(tmp_path, key="demo/fail", probe=[sys.executable, "-c", "raise SystemExit(1)"])
    failing["startup_timeout_seconds"] = 0.3
    if kind == "exit":
        failing["command"] = [sys.executable, "-c", "raise SystemExit(3)"]
    with pytest.raises(runtime.ServiceError, match="readiness_timeout|process_exited"):
        manager.start(directory, [service(tmp_path), failing], "failure", None)
    assert not runtime.running(directory)
    assert manager.status(directory)["state"] == "failed"


def test_post_ready_exit_stops_remaining_session(directory, tmp_path):
    exit_now = tmp_path / "exit-now"
    delayed = service(tmp_path, key="demo/delayed", command=[sys.executable, "-c", "import pathlib,sys,time\nwhile not pathlib.Path(sys.argv[1]).exists(): time.sleep(.02)", str(exit_now)])
    manager.start(directory, [service(tmp_path), delayed], "exit", None)
    exit_now.touch()
    eventually(lambda: not runtime.running(directory))
    assert manager.status(directory)["reason"] == "demo/delayed:process_exited"


def test_external_is_checked_but_no_worker_is_spawned(directory, tmp_path):
    manager.start(directory, [service(tmp_path, mode="external")], "external", None)
    assert manager.status(directory)["services"]["demo/main"]["pid"] is None
    assert list(directory.glob("*-0.json")) == []


def test_empty_plan_no_daemon_or_owned_receipt(directory, tmp_path):
    receipt = tmp_path / "receipt"
    assert manager.start(directory, [], "", receipt)["state"] == "disabled"
    assert runtime.read_json(receipt)["owned"] is False
    assert not runtime.running(directory)


def test_lifecycle_logs_and_state_exclude_configuration_and_process_output(directory, tmp_path):
    secret = "never-display-this-secret"
    command = [sys.executable, "-c", f"import time; print({secret!r}, flush=True); time.sleep(60)"]
    item = service(tmp_path, command=command)
    item["env"]["SECRET"] = secret
    manager.start(directory, [item], "opaque", None)
    manager.stop(directory, None)
    for path in directory.iterdir():
        assert secret not in path.read_text()
        assert str(tmp_path) not in path.read_text()


def test_unsafe_state_symlink_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(runtime.ServiceError, match="unsafe_state"):
        runtime.ensure_state(link / "children")


def _replace_after_next_open(monkeypatch, path, value):
    open_file = runtime.os.open
    opened = []

    def open_then_replace(candidate, *args, **kwargs):
        fd = open_file(candidate, *args, **kwargs)
        if Path(candidate) == path and not opened:
            opened.append(fd)
            runtime.atomic_json(path, value)
            assert os.fstat(fd).st_nlink == 0
        return fd

    monkeypatch.setattr(runtime.os, "open", open_then_replace)
    return opened


def test_state_read_keeps_open_snapshot_during_atomic_replacement(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    before = {"run_id": "old", "state": "starting"}
    after = {"run_id": "new", "state": "ready"}
    runtime.atomic_json(path, before)
    opened = _replace_after_next_open(monkeypatch, path, after)

    assert runtime.read_json(path) == before
    assert runtime.read_json(path) == after
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize("operation", ["read", "lock", "event"])
def test_hardlinked_state_files_are_rejected(tmp_path, operation):
    path = tmp_path / "events.jsonl"
    runtime.atomic_json(path, {"state": "ready"})
    (tmp_path / "other-link").hardlink_to(path)

    with pytest.raises(runtime.ServiceError, match="unsafe_state_file"):
        if operation == "read":
            runtime.read_json(path)
        elif operation == "lock":
            with runtime.lock(path):
                pytest.fail("hardlinked lock acquired")
        else:
            runtime.event(tmp_path, "run", "ready")


@pytest.mark.parametrize("operation", ["lock", "event", "writable_opt_in"])
def test_unlinked_state_writers_and_locks_are_rejected(tmp_path, monkeypatch, operation):
    path = tmp_path / "events.jsonl"
    runtime.atomic_json(path, {"state": "starting"})
    after = {"state": "ready"}
    opened = _replace_after_next_open(monkeypatch, path, after)

    with pytest.raises(runtime.ServiceError, match="unsafe_state_file"):
        if operation == "lock":
            with runtime.lock(path):
                pytest.fail("unlinked lock acquired")
        elif operation == "event":
            runtime.event(tmp_path, "run", "ready")
        else:
            fd = runtime.safe_open(path, os.O_RDWR, allow_unlinked=True)
            os.close(fd)
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert runtime.read_json(path) == after


def test_locks_exclude_second_owner(directory):
    with runtime.lock(directory / "runtime.lock"):
        assert runtime.running(directory)
        with pytest.raises(BlockingIOError):
            with runtime.lock(directory / "runtime.lock", blocking=False):
                pytest.fail("second owner acquired lock")


def test_http_probe_disables_proxy_redirects_and_never_reads_body(monkeypatch, tmp_path):
    observed = []
    class Response:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Opener:
        def open(self, url, timeout):
            observed.append((url, timeout))
            return Response()
    def build(*handlers):
        assert any(isinstance(h, runtime.NoRedirect) for h in handlers)
        assert any(getattr(h, "proxies", None) == {} for h in handlers)
        return Opener()
    monkeypatch.setattr(runtime.urllib.request, "build_opener", build)
    item = service(tmp_path)
    item["healthcheck_url"] = "http://127.0.0.1:1234/ready"
    assert runtime.http_probe(item["healthcheck_url"], 0.2)
    assert observed == [(item["healthcheck_url"], 0.2)]


def test_guard_cleans_descendants_if_supervisor_dies(directory, tmp_path):
    sentinel = tmp_path / "alive"
    code = "import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile True:\n p.write_text(str(time.monotonic()))\n time.sleep(.02)"
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(60)"
    item = service(tmp_path, command=[sys.executable, "-c", parent, code, str(sentinel)])
    manager.start(directory, [item], "orphan", None)
    eventually(sentinel.exists)
    state = runtime.read_json(directory / "state.json")
    # Test-only deliberate daemon crash. The production controller never uses a
    # persisted PID as signalling authority.
    os.kill(state["pid"], signal.SIGKILL)
    eventually(lambda: not runtime.running(directory))
    assert manager.status(directory)["state"] == "failed"
    assert manager.status(directory)["reason"] == "supervisor_exited"
    time.sleep(0.35)
    last = sentinel.read_text()
    time.sleep(0.15)
    assert sentinel.read_text() == last


def test_readiness_command_descendants_are_reaped_even_after_success(tmp_path):
    sentinel = tmp_path / "probe-child"
    child = "import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile True:\n p.write_text(str(time.monotonic()))\n time.sleep(.01)"
    probe = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(.1)"
    item = service(tmp_path, probe=[sys.executable, "-c", probe, child, str(sentinel)])
    assert runtime.healthy(item, 2)
    assert sentinel.exists()
    last = sentinel.read_text()
    time.sleep(0.1)
    assert sentinel.read_text() == last


def test_stale_record_never_signals_unrelated_process(directory):
    runtime.atomic_json(directory / "state.json", {"run_id": "stale", "pid": os.getpid(), "state": "ready"})
    assert manager.stop(directory, None)["state"] == "stopped"
    assert manager.status(directory)["pid"] is None


def test_receipt_from_older_run_cannot_stop_new_session(directory, tmp_path):
    receipt = tmp_path / "old-receipt"
    manager.start(directory, [service(tmp_path)], "first", receipt)
    manager.stop(directory, None)
    manager.start(directory, [service(tmp_path)], "second", None)
    assert manager.stop(directory, receipt)["state"] == "not_owned"
    assert runtime.running(directory)


def test_status_cli_works_without_importing_dotenv(directory, tmp_path):
    result = subprocess.run(
        [sys.executable, "-S", str(SCRIPTS / "extension_services.py"), "status", "--state-dir", str(directory)],
        env={**os.environ, "SILICON_NOTEBOOK_ENV_FILE": str(tmp_path / "missing")},
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["state"] == "stopped"


def test_another_terminal_can_inspect_and_cancel_startup(directory, tmp_path):
    item = service(tmp_path, probe=[sys.executable, "-c", "raise SystemExit(1)"])
    item["startup_timeout_seconds"] = 30
    with ThreadPoolExecutor() as executor:
        future = executor.submit(manager.start, directory, [item], "pending", None)
        eventually(lambda: manager.status(directory)["state"] == "starting")
        began = time.monotonic()
        assert manager.stop(directory, None)["state"] == "stopped"
        assert time.monotonic() - began < 3
        with pytest.raises(runtime.ServiceError, match="startup_cancelled"):
            future.result(timeout=3)


def test_guard_retains_lease_after_supervisor_crash_preventing_overlap(directory, tmp_path):
    item = service(tmp_path)
    item["shutdown_timeout_seconds"] = 0.7
    manager.start(directory, [item], "same", None)
    state = runtime.read_json(directory / "state.json")
    os.kill(state["pid"], signal.SIGKILL)
    eventually(lambda: not runtime.running(directory, "supervisor.lock"))
    assert runtime.running(directory)
    with pytest.raises(runtime.ServiceError, match="configuration_changed"):
        manager.start(directory, [item], "same", None)
    eventually(lambda: not runtime.running(directory))


def test_http_whole_probe_timeout_is_bounded(monkeypatch):
    observed = []
    class Process:
        stdin = None
        def communicate(self, payload, timeout):
            observed.append(timeout)
            raise subprocess.TimeoutExpired("hidden", timeout)
        def kill(self): observed.append("killed")
        def wait(self): observed.append("waited")
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: Process())
    assert not runtime.http_healthy("http://unused.invalid", 0.1)
    assert observed == [0.1, "killed", "waited"]


def test_http_probe_owner_watcher_exits_during_blocked_io(monkeypatch):
    from threading import Event
    unblock = Event()
    monkeypatch.setattr(runtime, "http_probe", lambda *args: unblock.wait())
    try:
        assert not runtime.guarded_http_probe({"url": "http://unused.invalid", "timeout": 30, "owner": -1})
    finally:
        unblock.set()


def test_old_run_cancellation_cannot_overwrite_new_run_request(directory):
    old_run = "a" * 32
    new_run = "b" * 32
    runtime.request_stop(directory, new_run)
    runtime.request_stop(directory, old_run)
    assert runtime.read_json(runtime.stop_path(directory, new_run))["run_id"] == new_run


def test_configuration_searches_relative_path_from_service_cwd(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import python_env
    cwd = tmp_path / "plugin"
    binary = cwd / "bin" / "serve"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    config = tmp_path / "extensions.toml"
    config.write_text('[extensions.demo]\nbundle="unused:BUNDLE"\n[extensions.demo.services.main]\ncwd="plugin"\ncommand=["serve"]\nhealthcheck_command=["serve"]\n[extensions.demo.services.main.env]\nPATH="bin"\n')
    monkeypatch.setattr(python_env, "build_python_environment", lambda **kwargs: {"EXTENSIONS_CONFIG": str(config)})
    monkeypatch.setattr(manager.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.chdir(tmp_path.parent)
    services, _ = manager.configuration(manager.ROOT)
    assert services[0]["cwd"] == str(cwd)
    assert services[0]["env"]["PATH"] == "bin"


@pytest.mark.parametrize("file_key,environment_key", [
    (None, "extensions_config"), (None, "ExTeNsIoNs_CoNfIg"),
    ("extensions_config", None), ("EXTENSIONS_CONFIG", "extensions_config"),
    ("extensions_config", "EXTENSIONS_CONFIG"),
])
def test_configuration_matches_settings_case_insensitive_selection(
    tmp_path, monkeypatch, file_key, environment_key,
):
    from types import SimpleNamespace
    from app.core.config import Settings

    for name in list(os.environ):
        if name.lower() == "extensions_config":
            monkeypatch.delenv(name)
    config = tmp_path / "extensions.toml"
    config.write_text(
        '[extensions.demo]\nbundle="unused:BUNDLE"\n'
        '[extensions.demo.services.main]\nmode="external"\n'
        f'healthcheck_command={json.dumps([sys.executable, "-c", "pass"])}\n'
    )
    dotenv = tmp_path / ".env"
    file_value = str(config) if environment_key is None else str(tmp_path / "not-selected.toml")
    dotenv.write_text(f'{file_key}={json.dumps(file_value)}\n' if file_key else "")
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", str(dotenv))
    if environment_key:
        monkeypatch.setenv(environment_key, str(config))
    monkeypatch.setattr(manager.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert Settings(_env_file=dotenv).extensions_config == str(config)
    services, _ = manager.configuration(manager.ROOT)
    assert [item["key"] for item in services] == ["demo/main"]


@pytest.mark.parametrize("interruption", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_interrupted_launcher_rolls_back_its_unready_session(directory, tmp_path, interruption):
    item = service(tmp_path, probe=[sys.executable, "-c", "raise SystemExit(1)"])
    item["startup_timeout_seconds"] = 30
    # Background shell lanes inherit ignored SIGINT; main must restore all its
    # cancellation handlers rather than relying on Python's startup defaults.
    code = "import json,signal,sys; signal.signal(signal.SIGINT,signal.SIG_IGN); sys.path.insert(0,sys.argv[1]); import extension_services as m; item=json.load(sys.stdin); m.configuration=lambda root: ([item],'pending'); raise SystemExit(m.main(['start','--state-dir',sys.argv[2],'--receipt',sys.argv[3]]))"
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(SCRIPTS), str(directory), str(tmp_path / "receipt")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    process.stdin.write(json.dumps(item).encode())
    process.stdin.close()
    process.stdin = None
    try:
        eventually(lambda: manager.status(directory)["state"] == "starting")
        process.send_signal(interruption)
        process.communicate(timeout=10)
        assert process.returncode == 130
        assert not runtime.running(directory)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_cancellation_at_spawn_return_still_rolls_back(directory, tmp_path, monkeypatch):
    spawn = manager._spawn
    def interrupted(*args, **kwargs):
        spawn(*args, **kwargs)
        raise KeyboardInterrupt
    monkeypatch.setattr(manager, "_spawn", interrupted)
    with pytest.raises(KeyboardInterrupt):
        manager.start(directory, [service(tmp_path)], "cancelled", None)
    assert not runtime.running(directory)


@pytest.mark.parametrize("killed_member", ["guard", "witness"])
def test_guard_pair_failure_reaps_real_service_tree_before_releasing_lease(directory, tmp_path, killed_member):
    parent_heartbeat = tmp_path / "parent-heartbeat"
    child_heartbeat = tmp_path / "child-heartbeat"
    heartbeat = "import pathlib,signal,sys,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\np=pathlib.Path(sys.argv[1])\nwhile True:\n p.write_text(str(time.monotonic()))\n time.sleep(.02)"
    parent = "import pathlib,signal,subprocess,sys,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nsubprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[3]])\np=pathlib.Path(sys.argv[2])\nwhile True:\n p.write_text(str(time.monotonic()))\n time.sleep(.02)"
    item = service(tmp_path, command=[sys.executable, "-c", parent, heartbeat, str(parent_heartbeat), str(child_heartbeat)])
    item["shutdown_timeout_seconds"] = 0.7
    manager.start(directory, [item], "guard-failure", None)
    eventually(lambda: parent_heartbeat.exists() and child_heartbeat.exists())
    state = runtime.read_json(directory / "state.json")
    worker_state = directory / (state["run_id"] + "-0.json")
    worker_lease = worker_state.with_suffix(".lock")
    target = state["services"]["demo/main"]["pid"] if killed_member == "guard" else runtime.read_json(worker_state)["witness_pid"]
    # Test-only fault injection into a process created by this test session.
    os.kill(target, signal.SIGKILL)
    eventually(lambda: manager.status(directory)["state"] == "stopping")
    assert runtime.running(directory, worker_lease.name)
    assert runtime.running(directory)
    with pytest.raises(runtime.ServiceError, match="configuration_changed"):
        manager.start(directory, [item], "guard-failure", None)
    eventually(lambda: not runtime.running(directory))
    assert manager.status(directory)["state"] == "failed"
    before = (parent_heartbeat.read_text(), child_heartbeat.read_text())
    time.sleep(0.15)
    assert (parent_heartbeat.read_text(), child_heartbeat.read_text()) == before
    assert manager.start(directory, [service(tmp_path)], "replacement", None)["state"] == "ready"
