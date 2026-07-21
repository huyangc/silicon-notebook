from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from app.core import diagnostics_runtime as diagnostics


def _runtime(tmp_path: Path, **kwargs) -> diagnostics.DiagnosticsRuntime:
    return diagnostics.DiagnosticsRuntime(
        tmp_path,
        readiness_provider=kwargs.pop(
            "readiness_provider", lambda: {"ready": True, "phase": "ready"}
        ),
        concurrency_provider=kwargs.pop(
            "concurrency_provider",
            lambda: {"kg": {"active": 1, "maximum": 2, "waiting": 3}},
        ),
        interval_seconds=kwargs.pop("interval_seconds", 0.02),
        enable_signal=kwargs.pop("enable_signal", False),
        **kwargs,
    )


def _wait_for_json(path: Path, *, after: str | None = None) -> dict:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.01)
            continue
        if after is None or value["heartbeat_at"] > after:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def test_request_phase_sql_job_and_lock_snapshot_contains_metadata_only(tmp_path):
    runtime = _runtime(tmp_path)
    secret = "SENSITIVE-SQL-VALUE"
    runtime.start()
    try:
        with diagnostics.install_runtime(runtime):
            with diagnostics.request_scope(
                "req-test", "DELETE", "/api/notebooks/nb-private?token=secret"
            ):
                with diagnostics.diagnostic_phase("notebook_delete.db"):
                    waiter = diagnostics.begin_write_wait("notebook.delete")
                    diagnostics.write_acquired(waiter, "notebook.delete")
                    sql = f"DELETE FROM notebooks WHERE id='{secret}'"
                    with diagnostics.sql_scope("write", sql, "notebook.delete"):
                        snapshot = runtime.snapshot()
                    diagnostics.write_released()
                with diagnostics.job_scope("follow-up"):
                    job_snapshot = runtime.snapshot()
        encoded = json.dumps([snapshot, job_snapshot])
        assert secret not in encoded
        assert "token" not in encoded
        assert "nb-private" in encoded
        assert "DELETE FROM notebooks" not in encoded
        assert snapshot["write_lock"]["holder"]["operation"] == "notebook.delete"
        assert snapshot["active_sql"][0]["table"] == "notebooks"
        assert snapshot["active_requests"][0]["phase"] == "notebook_delete.db"
        assert snapshot["active_requests"][0]["path"] == "/api/notebooks/{id}"
        assert job_snapshot["active_jobs"][0]["name"] == "follow-up"
    finally:
        runtime.stop()


def test_snapshot_has_exact_contract_and_heartbeat_advances_atomically(tmp_path):
    runtime = _runtime(
        tmp_path,
        readiness_provider=lambda: {
            "ready": False,
            "phase": "warming",
            "error": "PRIVATE PROVIDER ERROR",
        },
        concurrency_provider=lambda: {},
    )
    runtime.start()
    try:
        path = tmp_path / "runtime.json"
        first = _wait_for_json(path)
        second = _wait_for_json(path, after=first["heartbeat_at"])
        assert set(second) == {
            "schema_version",
            "pid",
            "process_started_at",
            "heartbeat_at",
            "last_state_change_at",
            "state_revision",
            "snapshot_failures",
            "readiness",
            "concurrency",
            "active_requests",
            "active_sql",
            "write_lock",
            "active_jobs",
            "recent_jobs",
        }
        assert second["heartbeat_at"] > first["heartbeat_at"]
        assert second["schema_version"] == 1
        assert second["readiness"] == {"ready": False, "phase": "warming"}
        assert not (tmp_path / "runtime.json.tmp").exists()
    finally:
        runtime.stop()


def test_install_runtime_is_process_global_and_rejects_a_different_runtime(tmp_path):
    runtime = _runtime(tmp_path / "one")
    other = _runtime(tmp_path / "two")
    seen: list[object] = []

    assert diagnostics.current_runtime() is None
    with diagnostics.install_runtime(runtime):
        thread = threading.Thread(target=lambda: seen.append(diagnostics.current_runtime()))
        thread.start()
        thread.join(timeout=1)
        assert seen == [runtime]
        with diagnostics.install_runtime(runtime):
            assert diagnostics.current_runtime() is runtime
        with pytest.raises(RuntimeError, match="different diagnostics runtime"):
            with diagnostics.install_runtime(other):
                pass
    assert diagnostics.current_runtime() is None


def test_overlapping_same_runtime_install_scopes_keep_runtime_until_both_exit(tmp_path):
    runtime = _runtime(tmp_path)
    both_installed = threading.Barrier(2)
    release_worker = threading.Event()
    worker_done = threading.Event()
    worker_seen: list[object] = []

    def worker() -> None:
        with diagnostics.install_runtime(runtime):
            both_installed.wait(timeout=1)
            release_worker.wait(timeout=1)
            worker_seen.append(diagnostics.current_runtime())
        worker_done.set()

    with diagnostics.install_runtime(runtime):
        thread = threading.Thread(target=worker)
        thread.start()
        both_installed.wait(timeout=1)
    try:
        assert diagnostics.current_runtime() is runtime
    finally:
        release_worker.set()
        worker_done.wait(timeout=1)
        thread.join(timeout=1)
    assert worker_seen == [runtime]
    assert diagnostics.current_runtime() is None


def test_rejected_activation_has_no_files_writer_or_signal_side_effects(tmp_path):
    active = _runtime(tmp_path / "active")
    rejected_path = tmp_path / "rejected"
    writers_before = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name == "diagnostics-snapshot"
    }
    with diagnostics.install_runtime(active):
        with pytest.raises(RuntimeError, match="different diagnostics runtime"):
            with diagnostics.activate_runtime(
                rejected_path,
                readiness_provider=lambda: {},
                concurrency_provider=lambda: {},
                interval_seconds=0.02,
                enable_signal=True,
            ):
                pass
        assert diagnostics.current_runtime() is active
    writers_after = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name == "diagnostics-snapshot"
    }
    assert writers_after == writers_before
    assert not rejected_path.exists()


def test_module_wrappers_are_noops_without_an_installed_runtime():
    assert diagnostics.current_runtime() is None
    with diagnostics.request_scope("req", "GET", "/api/notebooks/private"):
        with diagnostics.diagnostic_phase("private-phase"):
            with diagnostics.sql_scope("read", "SELECT 'private'", "private-op"):
                with diagnostics.job_scope("private-job"):
                    waiter = diagnostics.begin_write_wait("private-op")
                    diagnostics.write_acquired(waiter, "private-op")
                    diagnostics.write_wait_cancelled(waiter)
                    diagnostics.write_released()


@pytest.mark.parametrize(
    "path,secrets,expected",
    [
        (
            "/api/shared/Bearer-secret-share-token",
            ("Bearer-secret-share-token",),
            "/api/shared/{id}",
        ),
        (
            "/api/sources/src-private456/elements/el-private789",
            ("src-private456", "el-private789"),
            "/api/sources/{id}/elements/{id}",
        ),
        (
            "/api/memory/mem-private456",
            ("mem-private456",),
            "/api/memory/{id}",
        ),
        (
            "/api/agent/knowhow/tables/table-private/rows/row-private",
            ("table-private", "row-private"),
            "/api/agent/knowhow/tables/{id}/rows/{id}",
        ),
        (
            "/api/conversations/conv-private456",
            ("conv-private456",),
            "/api/conversations/{id}",
        ),
        (
            "/api/files/customer-secret-design.pdf",
            ("customer-secret-design.pdf",),
            "/api/files/{id}",
        ),
        (
            "/api/search/customer-secret-query?token=Bearer-secret",
            ("customer-secret-query", "Bearer-secret"),
            "/api/search/{id}",
        ),
    ],
)
def test_request_paths_redact_all_arbitrary_segments(path, secrets, expected, tmp_path):
    runtime = _runtime(tmp_path)
    with diagnostics.install_runtime(runtime):
        with diagnostics.request_scope("req-safe", "GET", path):
            request = runtime.snapshot()["active_requests"][0]
    assert request["path"] == expected
    assert request["notebook_id"] is None
    encoded = json.dumps(request)
    for secret in secrets:
        assert secret not in encoded


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/files/status", "/api/files/{id}"),
        ("/api/search/memory", "/api/search/{id}"),
        ("/api/shared/status", "/api/shared/{id}"),
        (
            "/api/sources/memory/elements/status",
            "/api/sources/{id}/elements/{id}",
        ),
        ("/api/memory/status", "/api/memory/{id}"),
        (
            "/api/agent/knowhow/tables/status/rows/memory",
            "/api/agent/knowhow/tables/{id}/rows/{id}",
        ),
        (
            "/api/kg/concept-whitelist/memory",
            "/api/kg/concept-whitelist/{id}",
        ),
    ],
)
def test_request_path_dynamic_positions_redact_values_that_look_static(
    path, expected, tmp_path
):
    runtime = _runtime(tmp_path)
    with diagnostics.install_runtime(runtime):
        with diagnostics.request_scope("req-collision", "GET", path):
            request = runtime.snapshot()["active_requests"][0]
    assert request["path"] == expected


def test_sql_metadata_normalization_is_stable_and_never_exposes_literals():
    first = diagnostics.normalize_sql_metadata(
        " UPDATE [notebooks] SET title = 'private' WHERE id = 123.5 "
    )
    second = diagnostics.normalize_sql_metadata(
        "UPDATE [notebooks] SET title = 'other' WHERE id = 999"
    )
    assert first == diagnostics.SqlMetadata(
        verb="UPDATE", table="notebooks", fingerprint=second.fingerprint
    )
    assert len(first.fingerprint) == 12
    assert diagnostics.normalize_sql_metadata(" -- no verb").verb == "UNKNOWN"


def test_write_lock_tracks_reentry_and_only_matching_waiter_is_cancelled(tmp_path):
    runtime = _runtime(tmp_path)
    with diagnostics.install_runtime(runtime):
        waiter = diagnostics.begin_write_wait("outer")
        diagnostics.write_acquired(waiter, "outer")
        assert diagnostics.begin_write_wait("nested") is None
        diagnostics.write_acquired(None, "nested")
        nested = runtime.snapshot()["write_lock"]
        assert nested["holder"]["operation"] == "outer"
        assert nested["holder"]["depth"] == 2
        assert nested["waiters"] == []

        holder_ready = threading.Event()
        allow_cancel = threading.Event()

        def wait_then_cancel() -> None:
            token = diagnostics.begin_write_wait("blocked")
            holder_ready.set()
            allow_cancel.wait(timeout=1)
            diagnostics.write_wait_cancelled(token)

        thread = threading.Thread(target=wait_then_cancel)
        thread.start()
        assert holder_ready.wait(timeout=1)
        waiting = runtime.snapshot()["write_lock"]["waiters"]
        assert [entry["operation"] for entry in waiting] == ["blocked"]
        diagnostics.write_wait_cancelled(object())
        assert len(runtime.snapshot()["write_lock"]["waiters"]) == 1
        allow_cancel.set()
        thread.join(timeout=1)
        assert runtime.snapshot()["write_lock"]["waiters"] == []

        diagnostics.write_released()
        assert runtime.snapshot()["write_lock"]["holder"]["depth"] == 1
        diagnostics.write_released()
        assert runtime.snapshot()["write_lock"]["holder"] is None


def test_phase_exit_does_not_restore_a_replacement_write_holder(tmp_path):
    runtime = _runtime(tmp_path)
    replacement_acquired = threading.Event()
    release_replacement = threading.Event()

    def replacement_holder() -> None:
        with diagnostics.diagnostic_phase("replacement-phase"):
            diagnostics.write_acquired(None, "replacement")
            replacement_acquired.set()
            release_replacement.wait(timeout=1)
            diagnostics.write_released()

    with diagnostics.install_runtime(runtime):
        diagnostics.write_acquired(None, "original")
        with diagnostics.diagnostic_phase("original-phase"):
            diagnostics.write_released()
            thread = threading.Thread(target=replacement_holder)
            thread.start()
            assert replacement_acquired.wait(timeout=1)
        try:
            holder = runtime.snapshot()["write_lock"]["holder"]
            assert holder["operation"] == "replacement"
            assert holder["phase"] == "replacement-phase"
        finally:
            release_replacement.set()
            thread.join(timeout=1)


def test_active_items_have_context_thread_and_monotonic_duration(tmp_path):
    runtime = _runtime(tmp_path)
    with diagnostics.install_runtime(runtime):
        with diagnostics.request_scope("req-correlation", "POST", "/ordinary?secret=x"):
            with diagnostics.job_scope("job-correlation"):
                with diagnostics.diagnostic_phase("working"):
                    with diagnostics.sql_scope("read", "SELECT 1 FROM notebooks", "lookup"):
                        snapshot = runtime.snapshot()

    request = snapshot["active_requests"][0]
    job = snapshot["active_jobs"][0]
    sql = snapshot["active_sql"][0]
    for entry in (request, job, sql):
        assert isinstance(entry["thread_id"], int)
        assert entry["started_at"].endswith("+00:00")
        assert entry["duration_ms"] >= 0
    assert request["path"] == "/{id}"
    assert job["request_id"] == "req-correlation"
    assert sql["request_id"] == "req-correlation"
    assert sql["job_id"] == job["job_id"]
    assert sql["phase"] == "working"


def test_job_completion_is_bounded_and_records_error_without_exception_text(tmp_path):
    runtime = _runtime(tmp_path)
    with diagnostics.install_runtime(runtime):
        for index in range(105):
            with diagnostics.job_scope(f"job-{index}"):
                pass
        with pytest.raises(RuntimeError, match="PRIVATE FAILURE"):
            with diagnostics.job_scope("failed-job"):
                raise RuntimeError("PRIVATE FAILURE")
    recent = runtime.snapshot()["recent_jobs"]
    assert len(recent) == 100
    assert recent[-1]["name"] == "failed-job"
    assert recent[-1]["status"] == "error"
    assert "PRIVATE FAILURE" not in json.dumps(recent)


def test_provider_failures_are_best_effort_and_counted(tmp_path):
    def broken() -> dict:
        raise RuntimeError("PRIVATE PROVIDER FAILURE")

    runtime = _runtime(
        tmp_path,
        readiness_provider=broken,
        concurrency_provider=broken,
    )
    runtime.start()
    try:
        snapshot = _wait_for_json(tmp_path / "runtime.json")
        assert snapshot["readiness"] == {}
        assert snapshot["concurrency"] == {}
        assert snapshot["snapshot_failures"] >= 2
        assert "PRIVATE PROVIDER FAILURE" not in json.dumps(snapshot)
    finally:
        runtime.stop()


def test_snapshot_bounds_identifiers_concurrency_width_and_non_finite_values(tmp_path):
    oversized = "private-" + "x" * 10_000
    concurrency = {
        **{f"private-group-{index}": {"active": index} for index in range(100)},
        "kg": {
            "window_active": 1,
            "window_max": 2,
            "window_waiting": 3,
            "unknown_private_metric": 4,
            **{f"private-metric-{index}": index for index in range(100)},
        },
        "llm": {"active": 1, "maximum": float("inf"), "waiting": float("nan")},
        "embedding": {"active": 1, "maximum": 2, "waiting": 0},
    }
    runtime = _runtime(tmp_path, concurrency_provider=lambda: concurrency)
    with diagnostics.install_runtime(runtime):
        with diagnostics.request_scope(
            oversized, "GET", f"/api/notebooks/{oversized}/sources/{oversized}"
        ):
            snapshot = runtime.snapshot()
    request = snapshot["active_requests"][0]
    assert len(request["request_id"]) <= 200
    assert request["notebook_id"] is None
    assert request["path"] == "/api/notebooks/{id}"
    assert snapshot["concurrency"] == {
        "kg": {"window_active": 1, "window_max": 2, "window_waiting": 3},
        "llm": {"active": 1},
        "embedding": {"active": 1, "maximum": 2, "waiting": 0},
    }
    encoded = json.dumps(snapshot, allow_nan=False)
    assert oversized not in encoded
    assert len(encoded.encode("utf-8")) < 100_000


def test_snapshot_bounds_readiness_counts_and_active_sql_names(tmp_path):
    huge_number = 10**10_000
    huge_verb = "A" * 10_000
    huge_table = "t" * 10_000
    runtime = _runtime(
        tmp_path,
        readiness_provider=lambda: {
            "ready": False,
            "phase": "w" * 10_000,
            "warmed_notebooks": huge_number,
            "total_notebooks": huge_number,
        },
        concurrency_provider=lambda: {},
    )
    with diagnostics.install_runtime(runtime):
        with diagnostics.sql_scope(
            "read", f"{huge_verb} FROM {huge_table}", "bounded-sql"
        ):
            snapshot = runtime.snapshot()
    active_sql = snapshot["active_sql"][0]
    assert len(snapshot["readiness"]["phase"]) <= 80
    assert "warmed_notebooks" not in snapshot["readiness"]
    assert "total_notebooks" not in snapshot["readiness"]
    assert len(active_sql["verb"]) <= 16
    assert len(active_sql["table"]) <= 128
    encoded = json.dumps(snapshot, allow_nan=False)
    assert huge_verb not in encoded
    assert huge_table not in encoded


def test_snapshot_writer_recovers_after_one_replace_failure(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    original_replace = diagnostics.os.replace
    attempts = 0

    def fail_once(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(diagnostics.os, "replace", fail_once)
    runtime.start()
    try:
        snapshot = _wait_for_json(tmp_path / "runtime.json")
        assert attempts >= 2
        assert snapshot["snapshot_failures"] >= 1
    finally:
        runtime.stop()


def test_state_change_bursts_are_coalesced_to_minimum_interval(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, interval_seconds=0.02)
    original_replace = diagnostics.os.replace
    replacements: list[float] = []

    def record_replace(source, destination):
        replacements.append(time.monotonic())
        return original_replace(source, destination)

    monkeypatch.setattr(diagnostics.os, "replace", record_replace)
    runtime.start()
    try:
        _wait_for_json(tmp_path / "runtime.json")
        with diagnostics.install_runtime(runtime):
            for index in range(100):
                with diagnostics.request_scope(
                    f"req-{index}", "GET", "/api/health"
                ):
                    pass
        deadline = time.monotonic() + 0.2
        while len(replacements) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        gaps = [later - earlier for earlier, later in zip(replacements, replacements[1:])]
        assert gaps
        assert min(gaps) >= 0.015
    finally:
        runtime.stop()


def test_active_request_registry_is_bounded(tmp_path):
    runtime = _runtime(tmp_path)
    with diagnostics.install_runtime(runtime):
        with ExitStack() as stack:
            for index in range(1_100):
                stack.enter_context(
                    diagnostics.request_scope(f"req-{index}", "GET", "/api/health")
                )
            active = runtime.snapshot()["active_requests"]
    assert len(active) <= 1_000


def test_atomic_snapshot_survives_repeated_concurrent_reads(tmp_path):
    runtime = _runtime(tmp_path)
    errors: list[BaseException] = []
    stop_reader = threading.Event()
    runtime.start()
    try:
        path = tmp_path / "runtime.json"
        _wait_for_json(path)

        def read_repeatedly() -> None:
            while not stop_reader.is_set():
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except BaseException as exc:  # pragma: no cover - assertion path
                    errors.append(exc)
                    return

        reader = threading.Thread(target=read_repeatedly)
        reader.start()
        with diagnostics.install_runtime(runtime):
            for index in range(50):
                with diagnostics.request_scope(
                    f"req-{index}", "GET", "/api/health"
                ):
                    time.sleep(0.002)
        stop_reader.set()
        reader.join(timeout=1)
        assert errors == []
    finally:
        stop_reader.set()
        runtime.stop()


def test_concurrent_start_creates_one_writer_and_stop_terminates_it(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    original_mkdir = Path.mkdir

    def delayed_mkdir(path, *args, **kwargs):
        if path == tmp_path:
            time.sleep(0.03)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", delayed_mkdir)
    start_barrier = threading.Barrier(8)
    starters = [
        threading.Thread(target=lambda: (start_barrier.wait(timeout=1), runtime.start()))
        for _ in range(8)
    ]
    writers_before = set(threading.enumerate())
    for thread in starters:
        thread.start()
    for thread in starters:
        thread.join(timeout=1)
    writers = [
        thread
        for thread in threading.enumerate()
        if thread not in writers_before and thread.name == "diagnostics-snapshot"
    ]
    try:
        assert len(writers) == 1
    finally:
        runtime.stop()
        runtime.stop()
        for writer in writers:
            writer.join(timeout=1)
        assert all(not writer.is_alive() for writer in writers)


def test_timed_out_stop_retains_stopping_writer_until_safe_later_cleanup(tmp_path):
    provider_entered = threading.Event()
    release_provider = threading.Event()

    def blocked_provider() -> dict:
        provider_entered.set()
        release_provider.wait(timeout=1)
        return {}

    runtime = _runtime(
        tmp_path,
        readiness_provider=blocked_provider,
        concurrency_provider=lambda: {},
    )
    runtime.start()
    assert provider_entered.wait(timeout=1)
    writer = runtime._thread
    assert writer is not None
    try:
        runtime.stop()
        assert writer.is_alive()
        assert runtime._thread is writer

        runtime.start()
        assert runtime._thread is writer
    finally:
        release_provider.set()
        runtime.stop()
        writer.join(timeout=1)
    assert not writer.is_alive()
    runtime.stop()
    assert runtime._thread is None


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(signal, "SIGUSR1"),
    reason="SIGUSR1 requires POSIX",
)
def test_sigusr1_owner_is_process_global_and_second_runtime_cannot_replace_it(tmp_path):
    code = """
import os, signal, sys, time
from pathlib import Path
from app.core.diagnostics_runtime import DiagnosticsRuntime
root = Path(sys.argv[1])
first = DiagnosticsRuntime(root / 'first', lambda: {}, lambda: {}, interval_seconds=0.02, enable_signal=True)
second = DiagnosticsRuntime(root / 'second', lambda: {}, lambda: {}, interval_seconds=0.02, enable_signal=True)
first.start()
second.start()
assert first.signal_capture_available is True
assert second.signal_capture_available is False
os.kill(os.getpid(), signal.SIGUSR1)
first_dump = root / 'first' / 'thread-dumps.log'
deadline = time.monotonic() + 2
while time.monotonic() < deadline and first_dump.stat().st_size == 0:
    time.sleep(0.02)
size = first_dump.stat().st_size
second.stop()
os.kill(os.getpid(), signal.SIGUSR1)
deadline = time.monotonic() + 2
while time.monotonic() < deadline and first_dump.stat().st_size <= size:
    time.sleep(0.02)
assert first_dump.stat().st_size > size
assert (root / 'second' / 'thread-dumps.log').stat().st_size == 0
first.stop()
print('OK', flush=True)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "OK"


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(signal, "SIGUSR1"),
    reason="SIGUSR1 requires POSIX",
)
def test_sigusr1_appends_all_threads_without_terminating_child(tmp_path):
    code = """
import sys, threading, time
from pathlib import Path
from app.core.diagnostics_runtime import DiagnosticsRuntime
root = Path(sys.argv[1])
runtime = DiagnosticsRuntime(root, lambda: {}, lambda: {}, interval_seconds=0.02, enable_signal=True)
runtime.start()
def _wait_forever():
    private_local = 'PRIVATE-SIGNAL-LOCAL'
    while True:
        assert private_local
        time.sleep(0.02)
threading.Thread(target=lambda: _wait_forever(), name='diag-child-worker', daemon=True).start()
print('READY', flush=True)
while True:
    time.sleep(0.02)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        os.kill(child.pid, signal.SIGUSR1)
        dump = tmp_path / "thread-dumps.log"
        deadline = time.monotonic() + 2.0
        text = ""
        while time.monotonic() < deadline:
            if dump.exists():
                text = dump.read_text(errors="replace")
                thread_count = text.count("Thread 0x") + text.count(
                    "Current thread 0x"
                )
                if thread_count >= 2:
                    break
            time.sleep(0.02)
        assert child.poll() is None
        assert text.count("Thread 0x") + text.count("Current thread 0x") >= 2
        assert "<lambda>" in text
        assert "PRIVATE" not in text
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_signal_registration_is_unavailable_off_main_thread_without_failing(tmp_path):
    runtime = _runtime(tmp_path, enable_signal=True)
    failures: list[BaseException] = []

    def start_and_stop() -> None:
        try:
            runtime.start()
            runtime.stop()
        except BaseException as exc:  # pragma: no cover - failure assertion path
            failures.append(exc)

    thread = threading.Thread(target=start_and_stop)
    thread.start()
    thread.join(timeout=1)
    assert failures == []
    assert runtime.signal_capture_available is False


def test_activate_runtime_installs_starts_and_always_cleans_up(tmp_path):
    with pytest.raises(RuntimeError, match="application failure"):
        with diagnostics.activate_runtime(
            tmp_path,
            readiness_provider=lambda: {},
            concurrency_provider=lambda: {},
            interval_seconds=0.02,
            enable_signal=False,
        ) as runtime:
            assert diagnostics.current_runtime() is runtime
            assert _wait_for_json(tmp_path / "runtime.json")["pid"] == os.getpid()
            raise RuntimeError("application failure")
    assert diagnostics.current_runtime() is None


def test_fastapi_lifespan_exposes_blocked_request_before_completion():
    from fastapi.testclient import TestClient

    from app.core import readiness
    from app.main import create_app

    entered = threading.Event()
    release = threading.Event()
    result: dict[str, object] = {}
    test_app = create_app()

    @test_app.get("/_diagnostics-test/block")
    def blocked_route() -> dict[str, bool]:
        entered.set()
        assert release.wait(timeout=5)
        return {"ok": True}

    readiness.mark_ready()
    try:
        with TestClient(test_app) as client:
            runtime = diagnostics.current_runtime()
            assert runtime is not None

            def request() -> None:
                result["response"] = client.get("/_diagnostics-test/block")

            thread = threading.Thread(target=request)
            thread.start()
            assert entered.wait(timeout=5)
            snapshot = runtime.snapshot()
            active = snapshot["active_requests"]
            assert len(active) == 1
            assert active[0]["method"] == "GET"
            assert active[0]["path"] == "/_diagnostics-test/block"
            assert active[0]["request_id"].startswith("req-")
            assert active[0]["phase"] == "http.dispatch"
            assert snapshot["readiness"]["ready"] is True

            release.set()
            thread.join(timeout=5)
            assert not thread.is_alive()
            response = result["response"]
            assert response.status_code == 200
            assert response.headers["X-Request-Id"] == active[0]["request_id"]
            assert runtime.snapshot()["active_requests"] == []
    finally:
        release.set()
    assert diagnostics.current_runtime() is None


def test_notebook_delete_reports_database_then_filesystem_phases(tmp_path, monkeypatch):
    from app.services import notebook_catalog

    observed: list[tuple[str, str | None]] = []
    runtime = _runtime(tmp_path)

    class Store:
        def delete_row_and_orphan_embeddings(self, notebook_id: str) -> list[str]:
            phase = runtime.snapshot()["active_requests"][0]["phase"]
            observed.append(("db", phase))
            return ["stored-source"]

    service = object.__new__(notebook_catalog.NotebookCatalogService)
    service._store = Store()
    service._storage_dir = lambda: tmp_path
    service.get_notebook = lambda _notebook_id: object()

    def delete_source(_path: str) -> None:
        phase = runtime.snapshot()["active_requests"][0]["phase"]
        observed.append(("source", phase))

    def delete_assets(_storage_dir: Path, _notebook_id: str) -> None:
        phase = runtime.snapshot()["active_requests"][0]["phase"]
        observed.append(("assets", phase))

    monkeypatch.setattr(notebook_catalog, "_delete_source_file", delete_source)
    monkeypatch.setattr(notebook_catalog, "_delete_notebook_asset_dir", delete_assets)

    with diagnostics.install_runtime(runtime):
        with diagnostics.request_scope(
            "req-delete", "DELETE", "/api/notebooks/nb-delete"
        ):
            service.delete_notebook("nb-delete")

    assert observed == [
        ("db", "notebook_delete.db"),
        ("source", "notebook_delete.files"),
        ("assets", "notebook_delete.files"),
    ]


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(signal, "SIGUSR1"),
    reason="SIGUSR1 requires POSIX",
)
def test_activate_runtime_late_writer_exit_finalizes_without_second_stop(tmp_path):
    provider_entered = threading.Event()
    release_provider = threading.Event()

    def blocked_provider() -> dict:
        provider_entered.set()
        release_provider.wait(timeout=1)
        return {}

    runtime = None
    with diagnostics.activate_runtime(
        tmp_path / "first",
        readiness_provider=blocked_provider,
        concurrency_provider=lambda: {},
        interval_seconds=0.02,
        enable_signal=True,
    ) as active:
        runtime = active
        assert active.signal_capture_available is True
        assert provider_entered.wait(timeout=1)

    assert runtime is not None
    release_provider.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and runtime._lifecycle_state != "stopped":
        time.sleep(0.02)
    cleaned = (
        runtime._lifecycle_state == "stopped"
        and runtime._thread is None
        and runtime._dump_handle is None
        and runtime._owns_signal is False
        and runtime.signal_capture_available is False
    )
    assert cleaned

    with diagnostics.activate_runtime(
        tmp_path / "second",
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        interval_seconds=0.02,
        enable_signal=True,
    ) as second:
        assert second.signal_capture_available is True
