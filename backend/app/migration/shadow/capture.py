"""Transactional SQLite change capture for forward shadow replication.

Only stable replication keys are logged.  Business rows and their log events
therefore share the caller's SQLite transaction without duplicating payloads or
embedding BLOBs in the shadow metadata.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable

from app.migration.shadow.manifest import validate_manifest
from app.migration.shadow.types import Manifest, ReplicationKeyKind, TableSpec


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def _quote(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"unsafe SQLite identifier: {identifier!r}")
    return f'"{identifier}"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _normalize_sql(sql: str) -> str:
    # Whitespace is not structural, but quoted literal bytes are.  In
    # particular, case-folding the whole statement would make a capture table
    # literal such as 'APP_SETTINGS' compare equal to 'app_settings'.
    return " ".join(sql.strip().rstrip(";").split())


def _key_json(spec: TableSpec, row_alias: str) -> str:
    arguments: list[str] = []
    for column in spec.replication_key:
        arguments.extend((_literal(column), f"{row_alias}.{_quote(column)}"))
    return f"json_object({', '.join(arguments)})"


def _key_changed(spec: TableSpec) -> str:
    return " OR ".join(
        f"OLD.{_quote(column)} IS NOT NEW.{_quote(column)}"
        for column in spec.replication_key
    )


def _enabled_predicate(epoch: int) -> str:
    return (
        "EXISTS (SELECT 1 FROM shadow_capture_control "
        f"WHERE singleton = 1 AND enabled = 1 AND schema_epoch = {epoch})"
    )


def _gate_trigger_sql(spec: TableSpec, operation: str, epoch: int) -> tuple[str, str]:
    name = f"shadow_gate_e{epoch}_{spec.name}_{operation}"
    statements = [
        "SELECT CASE WHEN EXISTS ("
        "SELECT 1 FROM shadow_capture_control "
        "WHERE singleton = 1 AND write_frozen = 1 AND apply_active = 0"
        ") THEN RAISE(ABORT, 'shadow writes are frozen') END;"
    ]
    if operation in {"insert", "update"} and spec.sqlite_null_guard_columns:
        nulls = " OR ".join(
            f"NEW.{_quote(column)} IS NULL"
            for column in spec.sqlite_null_guard_columns
        )
        statements.append(
            f"SELECT CASE WHEN {nulls} THEN "
            "RAISE(ABORT, 'shadow replication key must not be NULL') END;"
        )
    sql = (
        f"CREATE TRIGGER {_quote(name)} BEFORE {operation.upper()} ON {_quote(spec.name)} "
        f"BEGIN {' '.join(statements)} END"
    )
    return name, sql


def _capture_trigger_sql(
    spec: TableSpec, operation: str, epoch: int
) -> tuple[str, str]:
    name = f"shadow_capture_e{epoch}_{spec.name}_{operation}"
    enabled = _enabled_predicate(epoch)
    run_id = "(SELECT run_id FROM shadow_capture_control WHERE singleton = 1)"
    schema_epoch = (
        "(SELECT schema_epoch FROM shadow_capture_control WHERE singleton = 1)"
    )
    insert_prefix = (
        "INSERT INTO shadow_change_log "
        "(run_id, table_name, key_json, operation, schema_epoch) SELECT "
    )
    if operation == "insert":
        statements = (
            f"{insert_prefix}{run_id}, {_literal(spec.name)}, "
            f"{_key_json(spec, 'NEW')}, 'upsert', {schema_epoch} "
            f"WHERE {enabled};"
        )
    elif operation == "delete":
        statements = (
            f"{insert_prefix}{run_id}, {_literal(spec.name)}, "
            f"{_key_json(spec, 'OLD')}, 'delete', {schema_epoch} "
            f"WHERE {enabled};"
        )
    else:
        changed = _key_changed(spec)
        statements = " ".join(
            (
                f"{insert_prefix}{run_id}, {_literal(spec.name)}, "
                f"{_key_json(spec, 'OLD')}, 'delete', {schema_epoch} "
                f"WHERE {enabled} AND ({changed});",
                f"{insert_prefix}{run_id}, {_literal(spec.name)}, "
                f"{_key_json(spec, 'NEW')}, 'upsert', {schema_epoch} "
                f"WHERE {enabled};",
            )
        )
    sql = (
        f"CREATE TRIGGER {_quote(name)} AFTER {operation.upper()} ON {_quote(spec.name)} "
        f"BEGIN {statements} END"
    )
    return name, sql


def _expected_triggers(manifest: Manifest) -> dict[str, tuple[str, str]]:
    expected: dict[str, tuple[str, str]] = {}
    epoch = manifest.schema_pair.epoch
    for spec in manifest.replicated:
        for operation in ("insert", "update", "delete"):
            for name, sql in (
                _gate_trigger_sql(spec, operation, epoch),
                _capture_trigger_sql(spec, operation, epoch),
            ):
                expected[name] = (spec.name, sql)
    return expected


def _guard_name(spec: TableSpec) -> str:
    return f"shadow_uq_{spec.name}_replication_key"


def _guard_columns(spec: TableSpec) -> tuple[str, ...]:
    """Return a physical index order without changing the key protocol order.

    ``key_json``/hydration/hash use ``TableSpec.replication_key`` verbatim.  A
    UNIQUE index may permute that tuple because uniqueness is order-insensitive.
    community_members' logical order begins with ``notebook_id`` and otherwise
    competes with its hot ``(notebook_id, canonical_id)`` lookup index; putting
    level first keeps the observation-only guard out of that access path.
    """
    if spec.name == "community_members":
        return ("level", "canonical_id", "notebook_id")
    return spec.replication_key


def _guard_sql(spec: TableSpec) -> str:
    columns = ", ".join(_quote(column) for column in _guard_columns(spec))
    return (
        f"CREATE UNIQUE INDEX {_quote(_guard_name(spec))} "
        f"ON {_quote(spec.name)} ({columns})"
    )


def _scan_replication_keys(conn: sqlite3.Connection, manifest: Manifest) -> None:
    for spec in manifest.replicated:
        columns = [_quote(column) for column in spec.replication_key]
        nulls = " OR ".join(f"{column} IS NULL" for column in columns)
        if conn.execute(
            f"SELECT 1 FROM {_quote(spec.name)} WHERE {nulls} LIMIT 1"
        ).fetchone():
            raise ValueError(f"nullable replication key in {spec.name}")
        if spec.key_kind is ReplicationKeyKind.SHADOW_UNIQUE:
            keys = ", ".join(columns)
            if conn.execute(
                f"SELECT 1 FROM {_quote(spec.name)} GROUP BY {keys} "
                "HAVING COUNT(*) > 1 LIMIT 1"
            ).fetchone():
                raise ValueError(f"duplicate replication key in {spec.name}")


def _execute_install_statement(conn: sqlite3.Connection, sql: str) -> None:
    """Single patch point used by atomic-failure tests."""
    conn.execute(sql)


def _create_if_absent(
    conn: sqlite3.Connection, *, object_type: str, name: str, sql: str
) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?", (object_type, name)
    ).fetchone()
    if row is None:
        _execute_install_statement(conn, sql)


def _catalog_rows(
    conn: sqlite3.Connection, object_type: str, names: Iterable[str]
) -> dict[str, tuple[str, str]]:
    wanted = tuple(names)
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(
        f"SELECT name, tbl_name, sql FROM sqlite_master "
        f"WHERE type = ? AND name IN ({placeholders})",
        (object_type, *wanted),
    ).fetchall()
    return {str(row[0]): (str(row[1]), str(row[2] or "")) for row in rows}


def validate_sqlite_capture(conn: sqlite3.Connection, manifest: Manifest) -> None:
    """Fail closed on missing, extra, modified, or stale-epoch capture DDL."""
    validate_manifest(manifest)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != manifest.schema_pair.sqlite_version:
        raise ValueError("SQLite schema version does not match the shadow manifest")
    control = conn.execute(
        "SELECT run_id, schema_epoch FROM shadow_capture_control WHERE singleton = 1"
    ).fetchone()
    if control is None:
        raise ValueError("shadow capture control row is missing")
    if not str(control[0]).strip():
        raise ValueError("shadow capture control run identity is invalid")
    if int(control[1]) != manifest.schema_pair.epoch:
        raise ValueError("shadow capture control schema epoch is stale")

    expected = _expected_triggers(manifest)
    actual_names = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        if str(row[0]).startswith("shadow_")
    }
    if actual_names != set(expected):
        raise ValueError(
            "shadow capture trigger set mismatch: "
            f"missing={sorted(set(expected) - actual_names)}, "
            f"extra={sorted(actual_names - set(expected))}"
        )
    actual = _catalog_rows(conn, "trigger", expected)
    for name, (table, sql) in expected.items():
        actual_table, actual_sql = actual[name]
        if actual_table != table or _normalize_sql(actual_sql) != _normalize_sql(sql):
            raise ValueError(f"shadow capture trigger definition mismatch: {name}")

    guard_specs = tuple(
        spec
        for spec in manifest.replicated
        if spec.key_kind is ReplicationKeyKind.SHADOW_UNIQUE
    )
    expected_guards = {_guard_name(spec): (spec.name, _guard_sql(spec)) for spec in guard_specs}
    all_shadow_guard_names = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name LIKE 'shadow_uq_%'"
        )
    }
    if all_shadow_guard_names != set(expected_guards):
        raise ValueError(
            "shadow replication-key guard set mismatch: "
            f"missing={sorted(set(expected_guards) - all_shadow_guard_names)}, "
            f"extra={sorted(all_shadow_guard_names - set(expected_guards))}"
        )
    actual_guards = _catalog_rows(conn, "index", expected_guards)
    for name, (table, sql) in expected_guards.items():
        actual_table, actual_sql = actual_guards[name]
        if actual_table != table or _normalize_sql(actual_sql) != _normalize_sql(sql):
            raise ValueError(f"shadow replication-key guard definition mismatch: {name}")


def install_sqlite_capture(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    schema_epoch: int,
    manifest: Manifest,
) -> None:
    """Atomically install or validate capture DDL for one run identity.

    A first install creates disabled, unfrozen, lease-free control state.  A
    same-run crash resume preserves already-valid ``enabled``/``write_frozen``
    state while recreating missing installation objects and validating their
    definitions.  A persisted apply lease is never resumed: it represents an
    unsafe write-freeze bypass and therefore fails closed.
    """
    validate_manifest(manifest)
    if not run_id or run_id != run_id.strip():
        raise ValueError("shadow run_id must be a non-empty canonical string")
    if schema_epoch != manifest.schema_pair.epoch:
        raise ValueError("shadow schema epoch does not match the manifest")
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != manifest.schema_pair.sqlite_version:
        raise ValueError("SQLite schema version does not match the shadow manifest")
    if conn.in_transaction:
        raise ValueError("capture installation requires an idle SQLite connection")

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT enabled, write_frozen, apply_active, run_id, schema_epoch "
            "FROM shadow_capture_control WHERE singleton = 1"
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO shadow_capture_control "
                "(singleton, enabled, write_frozen, apply_active, run_id, schema_epoch) "
                "VALUES (1, 0, 0, 0, ?, ?)",
                (run_id, schema_epoch),
            )
        elif str(existing[3]) != run_id or int(existing[4]) != schema_epoch:
            raise ValueError("shadow capture belongs to a different run identity")
        elif int(existing[2]) != 0:
            raise ValueError(
                "shadow capture installation refuses a persisted apply-active lease"
            )

        _scan_replication_keys(conn, manifest)
        for spec in manifest.replicated:
            if spec.key_kind is ReplicationKeyKind.SHADOW_UNIQUE:
                _create_if_absent(
                    conn,
                    object_type="index",
                    name=_guard_name(spec),
                    sql=_guard_sql(spec),
                )
        for name, (_table, sql) in _expected_triggers(manifest).items():
            _create_if_absent(
                conn, object_type="trigger", name=name, sql=sql
            )
        validate_sqlite_capture(conn, manifest)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def disable_sqlite_capture(conn: sqlite3.Connection, *, run_id: str) -> None:
    """Disable capture for the matching run without reopening frozen writes."""
    if conn.in_transaction:
        raise ValueError("capture disable requires an idle SQLite connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "UPDATE shadow_capture_control SET enabled = 0, apply_active = 0 "
            "WHERE singleton = 1 AND run_id = ?",
            (run_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("shadow capture run identity does not match")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
