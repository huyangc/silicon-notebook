"""Source-side change capture DDL for cross-environment notebook sync
(docs/incremental-sync-design.md §7).

Pure functions: every statement this module returns is derived from static
data -- the sync manifest (``app.migration.sync.manifest``) for the table set
and each table's notebook scope, and the PostgreSQL migration DDL's parsed
primary keys (``app.migration.shadow.postgres_catalog.EXPECTED_CONSTRAINTS``)
for each table's row key. Nothing here opens a database, so the SQLite
migration, the PostgreSQL migration file, the snapshot verifier and the
catalog drift guard can all render the same text and compare it byte for
byte.

Shape (both backends):

- ``sync_capture_control`` is a one-row gate. The row is NOT seeded by the
  migration; capture stays off until an operator turns it on (PR-3c's
  ``sync capture enable``). Every trigger re-reads the gate per row, so
  turning it off costs one extra index probe per business write and nothing
  else.
- ``sync_change_log`` is an append-only, environment-local log of row
  identities (never row payloads): ``(table_name, key_json, operation,
  parent_key, notebook_id, txid, changed_at)`` ordered by ``seq``.

Per-row attribution follows ``TableSyncSpec.scope``:

- ``NOTEBOOK``: ``notebook_id`` is read straight off the row's own scope
  column (``notebooks`` itself uses ``id``), ``parent_key`` stays NULL.
- ``PARENT``: the row has no notebook column, so ``parent_key`` carries the
  parent row's id and ``notebook_id`` stays NULL -- the exporter resolves the
  notebook by walking ``scope_chain`` at export time, not at capture time
  (the parent row may not even exist any more by then, and resolving it
  inside a per-row trigger would put a join on every business write).
- ``GLOBAL``: both stay NULL.

``operation`` is ``upsert`` or ``delete``, plus one extra event kind:
``kg_epoch`` on ``unified_kg_state`` whenever ``kg_reset_epoch`` moves (or is
already non-zero at insert). That marks the point where the source's KG was
reset, which lets the incremental exporter compact everything logged for that
notebook before it (PR-3b); it is an optimization, never a correctness
premise, because the selective row-level deletes are logged in full anyway.

An UPDATE that moves a row's key is logged as the old key's ``delete``
followed by the new key's ``upsert`` -- the same rule the shadow capture
triggers use (``app.migration.shadow.capture._capture_trigger_sql``).

Two tables carry no primary key of their own and register an explicit
``TableSyncSpec.key`` instead; SQLite v84 / PostgreSQL 0064 give both a real
unique surface (a UNIQUE INDEX on SQLite, which cannot add a primary key to
an existing table, and a PRIMARY KEY on PostgreSQL). ``key_columns`` below
cross-checks a registered key against the PostgreSQL catalog so the two can
never drift apart silently.
"""

from __future__ import annotations

import re

from app.migration.shadow.postgres_catalog import EXPECTED_CONSTRAINTS
from app.migration.sync.capture_contract import (
    CONTROL_TABLE,
    LOG_TABLE,
    POSTGRES_CAPTURE_FUNCTION,
    SQLITE_TRIGGER_OPERATIONS,
    expected_postgres_trigger_names,
    expected_postgres_triggers,
    expected_sqlite_trigger_names,
    postgres_trigger_name,
    sqlite_trigger_name,
)
from app.migration.sync.manifest import ScopeKind, spec_for, synced_tables


# Re-exported so a caller that renders capture DDL does not need both modules;
# the names themselves live in capture_contract, which the PostgreSQL catalog
# validator also imports (see that module's docstring for why the split).
__all__ = [
    "CONTROL_TABLE",
    "LOG_TABLE",
    "POSTGRES_CAPTURE_FUNCTION",
    "expected_postgres_trigger_names",
    "expected_postgres_triggers",
    "expected_sqlite_trigger_names",
    "key_columns",
    "postgres_function_sql",
    "postgres_trigger_sql",
    "sqlite_trigger_sql",
]

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")

# The one table whose reset counter produces the extra ``kg_epoch`` event.
_KG_EPOCH_TABLE = "unified_kg_state"
_KG_EPOCH_COLUMN = "kg_reset_epoch"

_SQLITE_LOG_COLUMNS = (
    "table_name, key_json, operation, parent_key, notebook_id, changed_at"
)
_POSTGRES_LOG_COLUMNS = (
    "table_name, key_json, operation, parent_key, notebook_id, txid, changed_at"
)
# SQLite has no transaction id to record; ``txid`` stays NULL there and the
# incremental exporter's in-flight-transaction compensation (PR-3b) is a
# PostgreSQL-only concern.
_SQLITE_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
_SQLITE_GATE = (
    f"EXISTS (SELECT 1 FROM {CONTROL_TABLE} WHERE singleton = 1 AND enabled = 1)"
)

_PRIMARY_KEY_COLUMNS: dict[str, tuple[str, ...]] = {
    contract.table: contract.columns
    for contract in EXPECTED_CONSTRAINTS.values()
    if contract.kind == "p"
}


def _quote(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"unsafe SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def key_columns(table: str) -> tuple[str, ...]:
    """The columns that identify one row of ``table`` in the change log.

    The PostgreSQL primary key parsed out of the packaged migration DDL is
    the answer for every table that has one. ``TableSyncSpec.key`` is the
    registered answer for a table that has none, and when both exist they
    must agree -- a registered key that no longer matches the catalog would
    make the capture triggers log one identity while the exporter/importer
    matched rows by another.
    """
    declared = spec_for(table).key
    catalog = _PRIMARY_KEY_COLUMNS.get(table, ())
    if declared and catalog and declared != catalog:
        raise ValueError(
            f"{table}: TableSyncSpec.key {declared} disagrees with the "
            f"PostgreSQL primary key {catalog}"
        )
    columns = declared or catalog
    if not columns:
        raise ValueError(
            f"{table}: no PostgreSQL primary key and no TableSyncSpec.key; "
            "register one before the table can be change-captured"
        )
    return columns


def _scope_expressions(table: str, row_alias: str) -> tuple[str, str]:
    """``(parent_key, notebook_id)`` SQL expressions for one row alias."""
    scope = spec_for(table).scope
    if scope is None:
        raise ValueError(f"{table}: a synced table must declare a TableScope")
    if scope.kind is ScopeKind.NOTEBOOK:
        return "NULL", f"{row_alias}.{_quote(scope.column)}"
    if scope.kind is ScopeKind.PARENT:
        return f"{row_alias}.{_quote(scope.column)}", "NULL"
    return "NULL", "NULL"


def _sqlite_key_json(table: str, row_alias: str) -> str:
    arguments: list[str] = []
    for column in key_columns(table):
        arguments.extend((_literal(column), f"{row_alias}.{_quote(column)}"))
    return f"json_object({', '.join(arguments)})"


def _sqlite_log_statement(
    table: str,
    row_alias: str,
    operation: str,
    *,
    predicate: str | None = None,
) -> str:
    parent, notebook = _scope_expressions(table, row_alias)
    gate = _SQLITE_GATE if predicate is None else f"{_SQLITE_GATE} AND ({predicate})"
    return (
        f"INSERT INTO {LOG_TABLE} ({_SQLITE_LOG_COLUMNS}) SELECT "
        f"{_literal(table)}, {_sqlite_key_json(table, row_alias)}, "
        f"{_literal(operation)}, {parent}, {notebook}, {_SQLITE_NOW} "
        f"WHERE {gate};"
    )


def _sqlite_key_changed(table: str) -> str:
    return " OR ".join(
        f"OLD.{_quote(column)} IS NOT NEW.{_quote(column)}"
        for column in key_columns(table)
    )


def _sqlite_trigger_statements(table: str, operation: str) -> list[str]:
    statements: list[str] = []
    if operation == "delete":
        statements.append(_sqlite_log_statement(table, "OLD", "delete"))
        return statements
    if operation == "update":
        statements.append(
            _sqlite_log_statement(
                table, "OLD", "delete", predicate=_sqlite_key_changed(table)
            )
        )
    statements.append(_sqlite_log_statement(table, "NEW", "upsert"))
    if table == _KG_EPOCH_TABLE:
        epoch = _quote(_KG_EPOCH_COLUMN)
        predicate = (
            f"NEW.{epoch} > 0"
            if operation == "insert"
            else f"OLD.{epoch} IS NOT NEW.{epoch}"
        )
        statements.append(
            _sqlite_log_statement(table, "NEW", "kg_epoch", predicate=predicate)
        )
    return statements


def sqlite_trigger_sql() -> dict[str, tuple[str, str]]:
    """``{trigger_name: (table, CREATE TRIGGER sql)}`` for all synced tables.

    Three AFTER FOR EACH ROW triggers per table. The statements deliberately
    carry no ``IF NOT EXISTS``: the migration drops each name first and then
    creates it, so the text SQLite stores in ``sqlite_master`` is byte-equal
    to what this function returns and ``scripts/verify_repository_snapshot.py``
    can compare them literally.
    """
    triggers: dict[str, tuple[str, str]] = {}
    for table in synced_tables():
        for operation in SQLITE_TRIGGER_OPERATIONS:
            name = sqlite_trigger_name(table, operation)
            body = " ".join(_sqlite_trigger_statements(table, operation))
            sql = (
                f"CREATE TRIGGER {_quote(name)} AFTER {operation.upper()} "
                f"ON {_quote(table)} BEGIN {body} END"
            )
            triggers[name] = (table, sql)
    return triggers


_POSTGRES_FUNCTION_SQL = f"""\
CREATE FUNCTION {POSTGRES_CAPTURE_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $sync_capture_row$
DECLARE
  key_columns text[] := string_to_array(TG_ARGV[0], ',');
  scope_kind text := TG_ARGV[1];
  scope_column text := TG_ARGV[2];
  old_row jsonb;
  new_row jsonb;
  old_key jsonb;
  new_key jsonb;
  xact bigint;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM {CONTROL_TABLE} WHERE singleton = 1 AND enabled
  ) THEN
    RETURN NULL;
  END IF;
  xact := pg_current_xact_id()::text::bigint;
  IF TG_OP <> 'INSERT' THEN
    old_row := to_jsonb(OLD);
    SELECT jsonb_object_agg(key_column, old_row -> key_column) INTO old_key
      FROM unnest(key_columns) AS key_column;
  END IF;
  IF TG_OP <> 'DELETE' THEN
    new_row := to_jsonb(NEW);
    SELECT jsonb_object_agg(key_column, new_row -> key_column) INTO new_key
      FROM unnest(key_columns) AS key_column;
  END IF;
  IF TG_OP = 'DELETE'
     OR (TG_OP = 'UPDATE' AND old_key IS DISTINCT FROM new_key) THEN
    INSERT INTO {LOG_TABLE} ({_POSTGRES_LOG_COLUMNS})
    VALUES (TG_TABLE_NAME, old_key, 'delete',
            CASE WHEN scope_kind = 'parent' THEN old_row ->> scope_column END,
            CASE WHEN scope_kind = 'notebook' THEN old_row ->> scope_column END,
            xact, now());
  END IF;
  IF TG_OP <> 'DELETE' THEN
    INSERT INTO {LOG_TABLE} ({_POSTGRES_LOG_COLUMNS})
    VALUES (TG_TABLE_NAME, new_key, 'upsert',
            CASE WHEN scope_kind = 'parent' THEN new_row ->> scope_column END,
            CASE WHEN scope_kind = 'notebook' THEN new_row ->> scope_column END,
            xact, now());
    IF TG_TABLE_NAME = '{_KG_EPOCH_TABLE}' AND (
         (TG_OP = 'INSERT' AND (new_row ->> '{_KG_EPOCH_COLUMN}')::bigint > 0)
         OR (TG_OP = 'UPDATE'
             AND (old_row -> '{_KG_EPOCH_COLUMN}')
                 IS DISTINCT FROM (new_row -> '{_KG_EPOCH_COLUMN}'))
       ) THEN
      INSERT INTO {LOG_TABLE} ({_POSTGRES_LOG_COLUMNS})
      VALUES (TG_TABLE_NAME, new_key, 'kg_epoch',
              CASE WHEN scope_kind = 'parent' THEN new_row ->> scope_column END,
              CASE WHEN scope_kind = 'notebook' THEN new_row ->> scope_column END,
              xact, now());
    END IF;
  END IF;
  RETURN NULL;
END;
$sync_capture_row$"""


def postgres_function_sql() -> str:
    """The one plpgsql function every PostgreSQL capture trigger executes.

    One function rather than 46 generated ones: the per-table differences are
    exactly the three trigger arguments (key columns, scope kind, scope
    column), which ``postgres_trigger_sql`` passes through ``TG_ARGV``.

    Cost note: with the gate open the function materializes ``to_jsonb(row)``
    once per changed row, which on a wide table (embeddings, payloads) is not
    free. That is the price of reading an arbitrary key column without
    dynamic SQL; with the gate closed the function returns after a single
    one-row index probe and never touches the row at all.
    """
    return _POSTGRES_FUNCTION_SQL


def postgres_trigger_sql() -> dict[str, tuple[str, str]]:
    """``{trigger_name: (table, CREATE TRIGGER sql)}`` for all synced tables.

    One AFTER INSERT OR UPDATE OR DELETE row trigger per table, all executing
    ``sync_capture_row`` with that table's key columns and notebook scope.
    """
    triggers: dict[str, tuple[str, str]] = {}
    for table in synced_tables():
        scope = spec_for(table).scope
        assert scope is not None  # synced_tables() never yields a LOCAL table
        name = postgres_trigger_name(table)
        arguments = ", ".join(
            (
                _literal(",".join(key_columns(table))),
                _literal(scope.kind.value),
                _literal(scope.column),
            )
        )
        triggers[name] = (
            table,
            f"CREATE TRIGGER {name} AFTER INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_CAPTURE_FUNCTION}({arguments})",
        )
    return triggers
