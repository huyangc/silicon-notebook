"""Foreground-only lifecycle for the forward shadow replicator."""

from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType
from typing import Any, Iterator

from app.migration.shadow.replicator import (
    Direction,
    ShadowReplicator,
    WorkerLeaseUnavailable,
)
from app.migration.shadow.retention import retain_change_log


@dataclass(frozen=True)
class LeaseObservation:
    acquired: bool
    lease_seconds: int


def acquire_worker_lease(
    target: Any,
    *,
    run_id: str,
    direction: Direction,
    worker_id: str,
    lease_seconds: int,
) -> LeaseObservation:
    """Acquire only an absent, same-owner, or database-expired lease."""

    with target.write() as conn:
        row = conn.execute(
            "INSERT INTO silicon_shadow.workers "
            "(run_id,direction,worker_id,lease_expires_at,heartbeat_at) "
            "VALUES (%s,%s,%s,clock_timestamp()+(%s * interval '1 second'),"
            "clock_timestamp()) ON CONFLICT (run_id,direction) DO UPDATE SET "
            "worker_id=EXCLUDED.worker_id,lease_expires_at=EXCLUDED.lease_expires_at,"
            "heartbeat_at=clock_timestamp() "
            "WHERE silicon_shadow.workers.worker_id=EXCLUDED.worker_id "
            "OR silicon_shadow.workers.lease_expires_at<=clock_timestamp() "
            "RETURNING worker_id",
            (run_id, direction.value, worker_id, lease_seconds),
        ).fetchone()
    if row is None or str(row["worker_id"]) != worker_id:
        raise WorkerLeaseUnavailable("shadow forward worker lease is already held")
    return LeaseObservation(True, lease_seconds)


def heartbeat_worker_lease(
    target: Any,
    *,
    run_id: str,
    direction: Direction,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    """Renew exactly the live worker identity using PostgreSQL's clock."""

    with target.write() as conn:
        row = conn.execute(
            "UPDATE silicon_shadow.workers SET "
            "lease_expires_at=clock_timestamp()+(%s * interval '1 second'),"
            "heartbeat_at=clock_timestamp() WHERE run_id=%s AND direction=%s "
            "AND worker_id=%s AND lease_expires_at>clock_timestamp() "
            "RETURNING worker_id",
            (lease_seconds, run_id, direction.value, worker_id),
        ).fetchone()
    return row is not None and str(row["worker_id"]) == worker_id


def release_worker_lease(
    target: Any,
    *,
    run_id: str,
    direction: Direction,
    worker_id: str,
) -> bool:
    """Release only this process identity; never delete a successor's lease."""

    with target.write() as conn:
        row = conn.execute(
            "DELETE FROM silicon_shadow.workers WHERE run_id=%s AND direction=%s "
            "AND worker_id=%s RETURNING worker_id",
            (run_id, direction.value, worker_id),
        ).fetchone()
    return row is not None


@contextmanager
def termination_event() -> Iterator[threading.Event]:
    """Translate TERM/INT into a cooperative stop after the current batch."""

    stop = threading.Event()
    installed: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            installed[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
    try:
        yield stop
    finally:
        for signum, previous in installed.items():
            signal.signal(signum, previous)


class ForwardWorker:
    """Run replication, heartbeat, and best-effort retention in foreground."""

    def __init__(
        self,
        source: Any,
        target: Any,
        *,
        run_id: str,
        replicator: ShadowReplicator | None = None,
        retention_interval_seconds: float = 60.0,
    ) -> None:
        if not run_id or run_id.strip() != run_id:
            raise ValueError("shadow worker run id is invalid")
        if retention_interval_seconds <= 0:
            raise ValueError("shadow retention interval must be positive")
        self.source = source
        self.target = target
        self.run_id = run_id
        self.replicator = replicator or ShadowReplicator(source, target)
        self.retention_interval_seconds = retention_interval_seconds
        self._lease_lost = threading.Event()
        self._maintenance_error: BaseException | None = None

    def _maintenance(self, stop: threading.Event) -> None:
        heartbeat_interval = max(0.25, self.replicator.lease_seconds / 3)
        next_retention = 0.0
        elapsed = 0.0
        try:
            while not stop.wait(heartbeat_interval):
                try:
                    owned = heartbeat_worker_lease(
                        self.target,
                        run_id=self.run_id,
                        direction=Direction.SQLITE_TO_POSTGRES,
                        worker_id=self.replicator.worker_id,
                        lease_seconds=self.replicator.lease_seconds,
                    )
                except Exception:
                    # An unavailable target cannot permit unsafe target writes:
                    # every apply transaction independently reclaims this exact
                    # lease.  Keep trying until ownership is disproved.
                    owned = True
                if not owned:
                    self._lease_lost.set()
                    stop.set()
                    return
                elapsed += heartbeat_interval
                if elapsed < next_retention:
                    continue
                try:
                    retain_change_log(self.source, self.target, run_id=self.run_id)
                except Exception as exc:
                    # Retention is conservative maintenance. Failure retains
                    # more data and must not interrupt safe replication.
                    self._maintenance_error = exc
                next_retention = elapsed + self.retention_interval_seconds
        finally:
            close_local = getattr(self.source, "close_local", None)
            if close_local is not None:
                close_local()

    def run(self, stop: threading.Event) -> None:
        if not isinstance(stop, threading.Event):
            raise TypeError("shadow worker stop must be a threading.Event")
        direction = Direction.SQLITE_TO_POSTGRES
        acquire_worker_lease(
            self.target,
            run_id=self.run_id,
            direction=direction,
            worker_id=self.replicator.worker_id,
            lease_seconds=self.replicator.lease_seconds,
        )
        maintenance = threading.Thread(
            target=self._maintenance,
            args=(stop,),
            name="shadow-forward-maintenance",
            daemon=True,
        )
        maintenance.start()
        try:
            self.replicator.run_forever(
                run_id=self.run_id,
                direction=direction,
                stop=stop,
            )
            if self._lease_lost.is_set():
                raise WorkerLeaseUnavailable("shadow forward worker lease was lost")
        finally:
            stop.set()
            maintenance.join(timeout=max(1.0, self.replicator.lease_seconds))
            try:
                release_worker_lease(
                    self.target,
                    run_id=self.run_id,
                    direction=direction,
                    worker_id=self.replicator.worker_id,
                )
            except Exception:
                # A target outage leaves a short database-clock lease which a
                # successor may take only after expiry.
                pass
