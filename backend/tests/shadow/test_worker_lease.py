from __future__ import annotations

import threading
from types import SimpleNamespace

from app.migration.shadow import worker as worker_module
from app.migration.shadow.worker import ForwardWorker


class _Source:
    def __init__(self) -> None:
        self.closed = 0

    def close_local(self) -> None:
        self.closed += 1


class _Replicator:
    worker_id = "worker-a"
    lease_seconds = 1

    def __init__(self) -> None:
        self.batch_started = threading.Event()
        self.finish_batch = threading.Event()
        self.batch_finished = threading.Event()

    def run_forever(self, *, run_id, direction, stop) -> None:
        assert run_id == "run-a"
        self.batch_started.set()
        self.finish_batch.wait(2)
        self.batch_finished.set()


def test_stop_waits_for_current_batch_then_releases_exact_lease(monkeypatch):
    source = _Source()
    replicator = _Replicator()
    calls: list[str] = []
    monkeypatch.setattr(
        worker_module,
        "acquire_worker_lease",
        lambda *args, **kwargs: calls.append("acquire"),
    )
    monkeypatch.setattr(
        worker_module,
        "heartbeat_worker_lease",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        worker_module,
        "release_worker_lease",
        lambda *args, **kwargs: calls.append(kwargs["worker_id"]) or True,
    )
    monkeypatch.setattr(
        worker_module,
        "retain_change_log",
        lambda *args, **kwargs: SimpleNamespace(deleted_rows=0),
    )
    worker = ForwardWorker(
        source,
        object(),
        run_id="run-a",
        replicator=replicator,
        retention_interval_seconds=10,
    )
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,))
    thread.start()
    assert replicator.batch_started.wait(1)

    stop.set()
    thread.join(0.05)
    assert thread.is_alive()
    assert not replicator.batch_finished.is_set()
    replicator.finish_batch.set()
    thread.join(2)

    assert not thread.is_alive()
    assert replicator.batch_finished.is_set()
    assert calls == ["acquire", "worker-a"]
    assert source.closed == 1
