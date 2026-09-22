"""Names of the source-side change-capture objects -- and nothing else.

``app.migration.sync.capture`` renders the actual DDL, and to do that it needs
each table's primary key, which it reads from
``app.migration.shadow.postgres_catalog``'s parse of the packaged PostgreSQL
migrations. That same catalog module has to know which triggers and functions
it may find on a business table, so importing ``capture`` from there would
close an import cycle (the architecture guard rejects one, and rightly: a
cycle here means neither module can be read without the other).

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
# SQLite has no "AFTER INSERT OR DELETE OR UPDATE", so each table needs one
# trigger per operation there; PostgreSQL needs one trigger in total.
SQLITE_TRIGGER_OPERATIONS = ("insert", "update", "delete")


def sqlite_trigger_name(table: str, operation: str) -> str:
    return f"sync_capture_{table}_{operation}"


def postgres_trigger_name(table: str) -> str:
    return f"sync_capture_{table}"


def postgres_function_name(table: str) -> str:
    """Each synced table gets its OWN trigger function, named after it.

    One shared function taking the key columns through ``TG_ARGV`` would have
    to read an arbitrary column name out of an arbitrary row, and the only way
    to do that without dynamic SQL is ``to_jsonb(NEW)`` -- which serializes the
    WHOLE row. On ``chunk_embeddings``/``knowledge_embeddings`` that is the
    bytea vector, on ``chunks`` the full text, on every write. A per-table
    function names the key columns directly and touches nothing else. The
    trigger and its function deliberately share a name: they exist only as a
    pair, and the catalog guard checks them as one.
    """
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


def expected_postgres_functions() -> dict[str, str]:
    """``{function_name: table}`` -- the exact set of capture trigger
    functions the PostgreSQL schema may carry."""
    return {postgres_function_name(table): table for table in synced_tables()}


def expected_postgres_function_names() -> frozenset[str]:
    return frozenset(expected_postgres_functions())
