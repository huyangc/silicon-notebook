"""Shared composition boundary for direct maintenance command-line tools.

Every command that opens a repository here is a **schema guest**
(``app.repositories.schema_currency``): it never migrates and never seeds. It
checks, on a bare connection and before composing anything, that the database
is at exactly this build's schema, and refuses with
``MaintenanceCliSchemaError`` (exit status 3, the shared reason-plus-remedy
wording) otherwise. Migrating belongs to the serving process's startup and to
the explicit ``database migrate`` command (:func:`run_schema_migration`);
seeding belongs to the serving process alone, because its ``UPDATE users``
re-hashes ``settings.admin_password`` unconditionally and would reset the
production admin credential from any operator shell that lacks it.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Literal

from app.core.config import Settings
from app.core.database_url import database_identity
from app.bootstrap import create_application_repository
from app.repositories.ports import NotebookRepository, OfflineMaintenanceBusyError
from app.repositories.schema_currency import (
    EXIT_SCHEMA_NOT_CURRENT,
    SchemaMigration,
    SchemaNotMigratableError,
    inspect_schema_currency,
    migrate_schema,
    schema_refusal_message,
)


class MaintenanceCliError(RuntimeError):
    """Expected operator-facing failure from a direct maintenance command."""

    #: The process exit status a command returns for this failure.
    exit_code = 2


class MaintenanceCliUsageError(MaintenanceCliError):
    """A direct-maintenance command was invoked without a required guard."""


class MaintenanceCliBusyError(MaintenanceCliError):
    """Another process already owns the database-wide maintenance lock."""


class MaintenanceCliSchemaError(MaintenanceCliError):
    """The database is not at this build's schema; this command will not fix it."""

    exit_code = EXIT_SCHEMA_NOT_CURRENT


def require_current_schema(settings: Settings) -> None:
    """The one schema-version gate for every schema-guest command."""
    state = inspect_schema_currency(settings)
    if not state.is_current:
        raise MaintenanceCliSchemaError(schema_refusal_message(state))


def create_schema_guest_repository(settings: Settings) -> NotebookRepository:
    """Compose the application repository as a schema guest.

    The version gate runs first, so a refusal composes nothing (no pool, no
    SQLite file). Composition is then the server's own -- the same extension
    seats, admission prime and auth wiring -- with ``migrate=False,
    seed=False``.
    """
    require_current_schema(settings)
    return create_application_repository(settings, owns_schema=False)


def create_bare_schema_guest_repository(
    settings: Settings, *, sqlite_only: bool = False
) -> NotebookRepository:
    """A schema guest WITHOUT extension seats, for the eval/replay/diagnostic
    tools that always composed a bare repository: giving them the plugin seats
    now would change what they measure. ``sqlite_only`` keeps the refusal a
    tool had when it named ``SQLiteRepository`` directly."""
    if sqlite_only and database_identity(settings.database_url).scheme != "sqlite":
        raise MaintenanceCliUsageError("this tool supports a SQLite DATABASE_URL only")
    require_current_schema(settings)
    from app.repositories.factory import create_repository as compose

    return compose(settings, migrate=False, seed=False)


#: The composition seam ``open_cli_repository`` goes through (tests replace it).
create_repository = create_schema_guest_repository


@contextmanager
def open_cli_repository(settings: Settings) -> Iterator[NotebookRepository]:
    """Open the active formal backend as a schema guest, without claiming
    server-startup recovery.

    Both formal repository facades own runtime resources and expose ``close``;
    closing is unconditional on success, failure, and interruption.

    The extension admission snapshot needs no priming call here: composition
    goes through ``app.bootstrap.create_application_repository``, which primes
    it. A CLI gets that startup snapshot and no refresher -- the polling thread
    belongs to long-lived serving processes, not to a command that runs against
    a database the operator believes is quiesced.
    """

    repository = create_repository(settings)
    try:
        yield repository
    finally:
        repository.close()


def _require_stopped_service_confirmation(
    settings: Settings, *, confirm_service_stopped: bool
) -> None:
    if (
        database_identity(settings.database_url).scheme == "postgresql"
        and not confirm_service_stopped
    ):
        raise MaintenanceCliUsageError(
            "PostgreSQL maintenance requires --confirm-service-stopped"
        )


@contextmanager
def open_maintenance_cli_repository(
    settings: Settings,
    *,
    confirm_service_stopped: bool,
    capability: Literal["portable", "sqlite-vector"] = "portable",
) -> Iterator[NotebookRepository]:
    """Open a write-capable maintenance repository under its operator lock.

    The confirmation check intentionally precedes repository composition (and
    the schema-version gate inside it).  A PostgreSQL repository initializes a
    connection pool, so opening it before rejecting an unsafe command would
    violate the stopped-service boundary and make a missing confirmation
    observable as database activity.
    """

    backend = database_identity(settings.database_url).scheme
    if backend == "postgresql" and capability == "sqlite-vector":
        raise MaintenanceCliUsageError(
            "vectors-to-blob is a SQLite legacy-vector repair; "
            "PostgreSQL already stores vectors as bytea"
        )
    _require_stopped_service_confirmation(
        settings, confirm_service_stopped=confirm_service_stopped
    )
    with open_cli_repository(settings) as repository:
        try:
            with repository.maintenance.offline_maintenance_lock():
                yield repository
        except OfflineMaintenanceBusyError as exc:
            raise MaintenanceCliBusyError(str(exc)) from None


def run_schema_migration(
    settings: Settings, *, confirm_service_stopped: bool
) -> SchemaMigration:
    """The explicit ``database migrate`` command: migrate, never seed.

    Unlike the other maintenance commands it requires the stopped-service
    confirmation on BOTH backends: migration DDL under a running server is
    exactly what this boundary exists to prevent, whatever the backend.
    """
    if not confirm_service_stopped:
        raise MaintenanceCliUsageError(
            "database migrate requires --confirm-service-stopped "
            "(stop the API and every background writer first)"
        )
    try:
        return migrate_schema(settings)
    except SchemaNotMigratableError as exc:
        raise MaintenanceCliSchemaError(str(exc)) from None
    except OfflineMaintenanceBusyError as exc:
        raise MaintenanceCliBusyError(str(exc)) from None
