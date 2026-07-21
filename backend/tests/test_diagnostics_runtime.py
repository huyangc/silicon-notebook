from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
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
    assert request["path"] == "/ordinary"
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
