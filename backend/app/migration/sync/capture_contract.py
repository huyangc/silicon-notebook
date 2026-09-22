"""Names of the source-side change-capture objects -- and nothing else.

``app.migration.sync.capture`` renders the actual DDL, and to do that it needs
each table's primary key, which it reads from
``app.migration.shadow.postgres_catalog``'s parse of the packaged PostgreSQL
migrations. That same catalog module has to know which triggers it may find on
a business table, so importing ``capture`` from there would close an import
cycle (the architecture guard rejects one, and rightly: a cycle here means
neither module can be read without the other).

So the *names* live here, where they depend on the sync manifest alone, and
both sides import them:

- ``capture`` renders one statement per name;
- ``postgres_catalog`` admits exactly these names on a business table and
  nothing else.

A name that only one of the two knows about is the failure this split is
meant to make impossible.
"""

from __future__ import annotations

from app.migration.sync.manifest import synced_tables


# The one-row gate and the append-only log, on both backends.
CONTROL_TABLE = "sync_capture_control"
LOG_TABLE = "sync_change_log"
# The single plpgsql function every PostgreSQL capture trigger executes.
POSTGRES_CAPTURE_FUNCTION = "sync_capture_row"
# SQLite has no "AFTER INSERT OR UPDATE OR DELETE", so each table needs one
# trigger per operation there; PostgreSQL needs one trigger in total.
SQLITE_TRIGGER_OPERATIONS = ("insert", "update", "delete")


def sqlite_trigger_name(table: str, operation: str) -> str:
    return f"sync_capture_{table}_{operation}"


def postgres_trigger_name(table: str) -> str:
    return f"sync_capture_{table}"


def expected_sqlite_trigger_names() -> frozenset[str]:
    return frozenset(
        sqlite_trigger_name(table, operation)
        for table in synced_tables()
        for operation in SQLITE_TRIGGER_OPERATIONS
    )


def expected_postgres_triggers() -> dict[str, str]:
    """``{trigger_name: table}`` -- the exact set
    ``app.migration.shadow.postgres_catalog`` admits on a business table."""
    return {postgres_trigger_name(table): table for table in synced_tables()}


def expected_postgres_trigger_names() -> frozenset[str]:
    return frozenset(expected_postgres_triggers())
