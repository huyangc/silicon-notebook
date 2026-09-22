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
  ``sync capture enable``). Every trigger re-reads the gate per row.
- ``sync_change_log`` is an append-only, environment-local log of row
  identities (never row payloads): ``(table_name, key_json, operation,
  parent_key, notebook_id, txid, changed_at)`` ordered by ``seq``.

Every capture statement names the row's key columns literally -- SQLite
``json_object('id', NEW."id")``, PostgreSQL ``jsonb_build_object('id',
NEW."id")`` -- and reads the scope column straight off ``NEW``/``OLD``. No
statement here ever materializes a whole row (``to_jsonb(NEW)`` and friends),
because a synced row can be a bytea embedding vector or a full chunk text and
capture must not pay for reading it.

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

Cost, with the gate CLOSED (the state every deployment is in until an
operator turns capture on): on PostgreSQL every modified row still enters the
AFTER-trigger event queue, still costs one plpgsql function invocation, and
still runs the one-row ``EXISTS`` probe on the gate before returning; the
shadow migration's bulk COPY pays that per copied row too. On SQLite each
statement's ``WHERE EXISTS`` gate is evaluated per affected row. That is
small but it is not nothing, and it is not zero-cost.
"""

from __future__ import annotations

import re

from app.migration.shadow.postgres_catalog import EXPECTED_CONSTRAINTS
from app.migration.sync.capture_contract import (
    CONTROL_TABLE,
    LOG_TABLE,
    SQLITE_TRIGGER_OPERATIONS,
    expected_postgres_function_names,
    expected_postgres_functions,
    expected_postgres_trigger_names,
    expected_postgres_triggers,
    expected_sqlite_trigger_names,
    postgres_function_name,
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
    "expected_postgres_function_names",
    "expected_postgres_functions",
    "expected_postgres_trigger_names",
    "expected_postgres_triggers",
    "expected_sqlite_trigger_names",
    "key_columns",
    "postgres_function_body",
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
#
# ``%f`` is seconds-with-milliseconds, so three zeros pad it out to the
# microsecond precision every other timestamp in a SQLite database carries
# (``app.repositories.sqlite.migrations._now`` ->
# ``datetime.now(timezone.utc).isoformat()``). Same shape, same offset
# spelling, so a change-log timestamp sorts and compares against a business
# row's ``updated_at`` as plain text without a reformat step.
_SQLITE_NOW = "strftime('%Y-%m-%dT%H:%M:%f','now') || '000+00:00'"
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

    ORDER IS PART OF THE KEY, and the comparison below is between ordered
    tuples on purpose. ``key_json`` is built by naming the columns in this
    order, so two spellings of the same column set produce two different
    JSON objects for the same row -- a log written under one order and read
    back under the other matches nothing. That makes a same-set,
    different-order change a real break, not a cosmetic one, and this
    function refuses it rather than silently picking a side.

    The PostgreSQL primary key parsed out of the packaged migration DDL is
    the answer for every table that has one. ``TableSyncSpec.key`` is the
    registered answer for a table that has none, and when both exist they
    must agree -- a registered key that no longer matches the catalog would
    make the capture triggers log one identity while the exporter/importer
    matched rows by another.

    The SQLite side of that parity is not checked here (this module never
    opens a database); ``tests/test_sync_manifest.py`` holds each table's
    SQLite primary key to this same ordered tuple, so a migration that
    changes one backend's key without the other fails there.
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


def _key_json(table: str, row_alias: str, builder: str) -> str:
    arguments: list[str] = []
    for column in key_columns(table):
        arguments.extend((_literal(column), f"{row_alias}.{_quote(column)}"))
    return f"{builder}({', '.join(arguments)})"


# ------------------------------------------------------------------ SQLite


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
        f"{_literal(table)}, {_key_json(table, row_alias, 'json_object')}, "
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


# -------------------------------------------------------------- PostgreSQL


def _postgres_log_statement(
    table: str, row_alias: str, operation: str, indent: str
) -> str:
    parent, notebook = _scope_expressions(table, row_alias)
    return (
        f"{indent}INSERT INTO {LOG_TABLE} ({_POSTGRES_LOG_COLUMNS})\n"
        f"{indent}VALUES ({_literal(table)}, "
        f"{_key_json(table, row_alias, 'jsonb_build_object')}, "
        f"{_literal(operation)},\n"
        f"{indent}        {parent}, {notebook}, xact, now());"
    )


def _postgres_key_changed(table: str) -> str:
    return "\n       OR ".join(
        f"OLD.{_quote(column)} IS DISTINCT FROM NEW.{_quote(column)}"
        for column in key_columns(table)
    )


def postgres_function_body(table: str) -> str:
    """The plpgsql body of ``sync_capture_<table>``, exactly as
    ``pg_proc.prosrc`` stores it (the text between the dollar-quote tags).

    ``OLD`` is only ever referenced inside a branch that already established
    ``TG_OP`` is UPDATE or DELETE. plpgsql evaluates an ``IF`` condition as one
    SQL expression with no short-circuit, so a flat
    ``TG_OP = 'UPDATE' AND OLD.x ...`` would touch ``OLD`` on an INSERT too;
    the nesting below is what keeps that from happening.
    """
    epoch = _quote(_KG_EPOCH_COLUMN)
    lines = [
        "",
        "DECLARE",
        "  xact bigint;",
        "BEGIN",
        "  IF NOT EXISTS (",
        f"    SELECT 1 FROM {CONTROL_TABLE} WHERE singleton = 1 AND enabled",
        "  ) THEN",
        "    RETURN NULL;",
        "  END IF;",
        "  xact := pg_current_xact_id()::text::bigint;",
        "  IF TG_OP = 'DELETE' THEN",
        _postgres_log_statement(table, "OLD", "delete", "    "),
        "    RETURN NULL;",
        "  END IF;",
        "  IF TG_OP = 'UPDATE' THEN",
        f"    IF {_postgres_key_changed(table)} THEN",
        _postgres_log_statement(table, "OLD", "delete", "      "),
        "    END IF;",
        "  END IF;",
        _postgres_log_statement(table, "NEW", "upsert", "  "),
    ]
    if table == _KG_EPOCH_TABLE:
        lines.extend(
            [
                "  IF TG_OP = 'INSERT' THEN",
                f"    IF NEW.{epoch} > 0 THEN",
                _postgres_log_statement(table, "NEW", "kg_epoch", "      "),
                "    END IF;",
                "  ELSE",
                f"    IF OLD.{epoch} IS DISTINCT FROM NEW.{epoch} THEN",
                _postgres_log_statement(table, "NEW", "kg_epoch", "      "),
                "    END IF;",
                "  END IF;",
            ]
        )
    lines.extend(["  RETURN NULL;", "END;", ""])
    return "\n".join(lines)


def postgres_function_sql() -> dict[str, tuple[str, str]]:
    """``{function_name: (table, CREATE FUNCTION sql)}`` -- one trigger
    function per synced table.

    Per-table rather than one shared ``TG_ARGV``-driven function: see
    ``capture_contract.postgres_function_name`` for why (a generic function
    cannot name a key column without ``to_jsonb`` of the entire row).

    No ``SET search_path``, deliberately. These are SECURITY INVOKER
    functions, so they already run with the writer's own privileges, and the
    two unqualified names in the body (``sync_capture_control``,
    ``sync_change_log``) are meant to resolve the way every other statement in
    that session resolves them: to the schema the write is happening in. That
    is what makes the scheme work per schema -- the shadow migration's target
    schema and each test lane's disposable schema carry their own gate row and
    their own log, and a write into one is never captured into another's log.
    Pinning ``search_path`` to one schema would send every schema's captures
    into that one log, which is the opposite of what a per-environment control
    plane needs.
    """
    functions: dict[str, tuple[str, str]] = {}
    for table in synced_tables():
        name = postgres_function_name(table)
        tag = f"${name}$"
        functions[name] = (
            table,
            f"CREATE FUNCTION {name}() RETURNS trigger\n"
            f"LANGUAGE plpgsql AS {tag}{postgres_function_body(table)}{tag}",
        )
    return functions


def postgres_trigger_sql() -> dict[str, tuple[str, str]]:
    """``{trigger_name: (table, CREATE TRIGGER sql)}`` for all synced tables.

    The event list is spelled ``INSERT OR DELETE OR UPDATE`` -- PostgreSQL's
    own canonical order, the one ``pg_get_triggerdef()`` prints. Writing it
    that way means the catalog guard can compare the deparsed definition
    against this very string after nothing more than dropping the schema
    qualifier and collapsing whitespace.
    """
    triggers: dict[str, tuple[str, str]] = {}
    for table in synced_tables():
        name = postgres_trigger_name(table)
        triggers[name] = (
            table,
            f"CREATE TRIGGER {name} AFTER INSERT OR DELETE OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {postgres_function_name(table)}()",
        )
    return triggers
