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

from app.migration.sync.manifest import spec_for, synced_tables


# The one-row gate and the append-only log, on both backends.
CONTROL_TABLE = "sync_capture_control"
LOG_TABLE = "sync_change_log"
# SQLite has no "AFTER INSERT OR DELETE OR UPDATE", so each table needs one
# trigger per operation there; PostgreSQL needs one trigger in total.
SQLITE_TRIGGER_OPERATIONS = ("insert", "update", "delete")
# The migration that installed the capture objects of every table synced at
# the time (SQLite v84 / PostgreSQL 0064) -- ``TableSyncSpec.since_schema``'s
# default, and the floor no table's capture can predate.
CAPTURE_BASE_SCHEMA = 84
# Every migration that installs capture objects: SQLite schema version -> the
# packaged PostgreSQL migration file that carries the same tables' functions
# and triggers (rendered by scripts/generate_sync_capture_migration.py, held
# to the generator by backend/tests/test_sync_capture.py). Each key is a
# ``since_schema`` some synced table declares, and every declared
# ``since_schema`` must be a key -- a table made SYNCED without a migration
# that installs its capture would silently never be logged on PostgreSQL.
CAPTURE_MIGRATION_FILES: dict[int, str] = {
    84: "0064_sync_change_capture.sql",
    97: "0077_kg_analysis_findings.sql",
}


def capture_tables(at_schema: int | None = None) -> tuple[str, ...]:
    """The synced tables that carry capture triggers at SQLite schema version
    ``at_schema`` (``since_schema <= at_schema``), in ``synced_tables()``
    order. ``None`` means this build's current schema: every synced table.

    A migration renders its triggers with its OWN version, never with
    ``None``: the manifest describes today's schema, and a migration that read
    it unpinned would change meaning the day a later table became SYNCED
    (``_migration_84`` would try to put a trigger on a table a from-scratch
    ladder does not have yet). Each version keeps the set it had when it was
    written, so v84's rendered text stays byte for byte what deployed
    databases already hold.
    """
    tables = synced_tables()
    if at_schema is None:
        return tables
    if at_schema < CAPTURE_BASE_SCHEMA:
        raise ValueError(
            f"capture triggers do not exist before SQLite v{CAPTURE_BASE_SCHEMA}: "
            f"at_schema={at_schema}"
        )
    return tuple(table for table in tables if spec_for(table).since_schema <= at_schema)


def capture_tables_added_at(schema: int) -> tuple[str, ...]:
    """The synced tables whose capture triggers the migration of SQLite schema
    version ``schema`` installs (``since_schema == schema``), in
    ``synced_tables()`` order."""
    return tuple(
        table for table in synced_tables() if spec_for(table).since_schema == schema
    )


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


def expected_sqlite_trigger_names(at_schema: int | None = None) -> frozenset[str]:
    """Trigger names at SQLite schema ``at_schema`` (``None``: current)."""
    return frozenset(
        sqlite_trigger_name(table, operation)
        for table in capture_tables(at_schema)
        for operation in SQLITE_TRIGGER_OPERATIONS
    )


def expected_postgres_triggers(at_schema: int | None = None) -> dict[str, str]:
    """``{trigger_name: table}`` -- with ``at_schema=None`` the exact set
    ``app.migration.shadow.postgres_catalog`` admits on a business table."""
    return {
        postgres_trigger_name(table): table for table in capture_tables(at_schema)
    }


def expected_postgres_trigger_names(at_schema: int | None = None) -> frozenset[str]:
    return frozenset(expected_postgres_triggers(at_schema))


def expected_postgres_functions(at_schema: int | None = None) -> dict[str, str]:
    """``{function_name: table}`` -- with ``at_schema=None`` the exact set of
    capture trigger functions the PostgreSQL schema may carry."""
    return {
        postgres_function_name(table): table for table in capture_tables(at_schema)
    }


def expected_postgres_function_names(at_schema: int | None = None) -> frozenset[str]:
    return frozenset(expected_postgres_functions(at_schema))
