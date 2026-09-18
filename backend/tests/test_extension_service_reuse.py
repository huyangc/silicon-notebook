"""Reusing a service session requires fresh readiness without taking ownership."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
from threading import Event

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import extension_services as manager
import extension_service_runtime as runtime


@pytest.fixture
def session(tmp_path):
    directory = tmp_path / "runtime"
    runtime.ensure_state(directory)
    available = tmp_path / "available"
    available.touch()
    service = {
        "key": "demo/external", "mode": "external", "command": [],
        "cwd": str(tmp_path), "env": dict(os.environ), "healthcheck_url": "",
        "healthcheck_command": [sys.executable, "-c",
            "import pathlib,sys; sys.exit(not pathlib.Path(sys.argv[1]).exists())",
            str(available)],
        "startup_timeout_seconds": 5.0, "shutdown_timeout_seconds": 0.1,
        "healthcheck_timeout_seconds": 2.0,
    }
    manager.start(directory, [service], "same", None)
    yield directory, service, available
    manager.stop(directory, None)


def test_unavailable_external_service_blocks_reuse_without_stopping_original(session, tmp_path):
    directory, service, available = session
    original = runtime.read_json(directory / "state.json")["run_id"]
    available.unlink()
    service["startup_timeout_seconds"] = 0.3
    receipt = tmp_path / "receipt"
    with pytest.raises(runtime.ServiceError, match="demo/external:readiness_timeout"):
        manager.start(directory, [service], "same", receipt)
    assert runtime.read_json(receipt) == {"run_id": original, "owned": False}
    assert manager.stop(directory, receipt)["state"] == "not_owned"
    assert runtime.running(directory)
    assert runtime.read_json(directory / "state.json")["run_id"] == original


def test_reuse_rechecks_every_service_and_retries_transient_unreadiness(session, monkeypatch):
    directory, service, _ = session
    calls = []
    def probe(item, timeout):
        assert 0 < timeout <= item["healthcheck_timeout_seconds"]
        calls.append(item["key"])
        return len(calls) > 1
    monkeypatch.setattr(manager, "healthy", probe)
    second = {**service, "key": "demo/managed", "mode": "managed"}
    assert manager.start(directory, [service, second], "same", None) == {
        "state": "ready", "reused": True,
    }
    assert calls == ["demo/external", "demo/external", "demo/managed"]


@pytest.mark.parametrize("replace_session", [False, True])
def test_stop_remains_available_during_recheck_and_changed_run_is_rejected(
    session, monkeypatch, replace_session,
):
    directory, service, _ = session
    entered, release = Event(), Event()
    def probe(item, timeout):
        entered.set()
        assert release.wait(5)
        return True
    monkeypatch.setattr(manager, "healthy", probe)
    with ThreadPoolExecutor() as executor:
        attempt = executor.submit(manager.start, directory, [service], "same", None)
        try:
            assert entered.wait(5)
            stopped = executor.submit(manager.stop, directory, None)
            assert stopped.result(timeout=3)["state"] == "stopped"
            if replace_session:
                manager.start(directory, [service], "same", None)
        finally:
            release.set()
        with pytest.raises(runtime.ServiceError, match="reused_session_changed"):
            attempt.result(timeout=3)
    assert runtime.running(directory) is replace_session


def test_cancelled_recheck_does_not_take_ownership_or_stop_existing_session(session, monkeypatch, tmp_path):
    directory, service, _ = session
    def cancelled(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(manager, "healthy", cancelled)
    receipt = tmp_path / "receipt"
    with pytest.raises(KeyboardInterrupt):
        manager.start(directory, [service], "same", receipt)
    assert runtime.read_json(receipt)["owned"] is False
    assert runtime.running(directory)
