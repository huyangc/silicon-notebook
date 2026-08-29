"""Shared composition boundary for direct maintenance command-line tools."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Literal

from app.core.config import Settings
from app.core.database_url import database_identity
from app.bootstrap import create_application_repository
from app.repositories.ports import NotebookRepository, OfflineMaintenanceBusyError


create_repository = create_application_repository


class MaintenanceCliError(RuntimeError):
    """Expected operator-facing failure from a direct maintenance command."""


class MaintenanceCliUsageError(MaintenanceCliError):
    """A direct-maintenance command was invoked without a required guard."""


class MaintenanceCliBusyError(MaintenanceCliError):
    """Another process already owns the database-wide maintenance lock."""


@contextmanager
def open_cli_repository(settings: Settings) -> Iterator[NotebookRepository]:
    """Open the active formal backend without claiming server-startup recovery.

    Both formal repository facades own runtime resources and expose ``close``;
    closing is unconditional on success, failure, and interruption.

    The extension admission snapshot needs no priming call here: ``create_repository``
    above is ``app.bootstrap.create_application_repository``, which primes it as
    part of composition. A CLI gets that startup snapshot and no refresher —
    the polling thread belongs to long-lived serving processes, not to a
    command that runs against a database the operator believes is quiesced.
    """

    repository = create_repository(settings)
    try:
        yield repository
    finally:
        repository.close()


@contextmanager
def open_maintenance_cli_repository(
    settings: Settings,
    *,
    confirm_service_stopped: bool,
    capability: Literal["portable", "sqlite-vector"] = "portable",
) -> Iterator[NotebookRepository]:
    """Open a write-capable maintenance repository under its operator lock.

    The confirmation check intentionally precedes repository composition.  A
    PostgreSQL repository initializes a connection pool, so opening it before
    rejecting an unsafe command would violate the stopped-service boundary and
    make a missing confirmation observable as database activity.
    """

    backend = database_identity(settings.database_url).scheme
    if backend == "postgresql" and capability == "sqlite-vector":
        raise MaintenanceCliUsageError(
            "vectors-to-blob is a SQLite legacy-vector repair; "
            "PostgreSQL already stores vectors as bytea"
        )
    if backend == "postgresql" and not confirm_service_stopped:
        raise MaintenanceCliUsageError(
            "PostgreSQL maintenance requires --confirm-service-stopped"
        )
    with open_cli_repository(settings) as repository:
        try:
            with repository.maintenance.offline_maintenance_lock():
                yield repository
        except OfflineMaintenanceBusyError as exc:
            raise MaintenanceCliBusyError(str(exc)) from None
