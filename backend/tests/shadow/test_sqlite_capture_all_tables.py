from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.shadow.capture import install_sqlite_capture
from app.migration.shadow.manifest import MANIFEST
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.sqlite.unified_kg_store import UnifiedKgStore


REPO_ROOT = Path(__file__).resolve().parents[3]
CASCADE_CONTRACT_PATH = (
    REPO_ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "shadow_sqlite_cascade_contract.json"
)
EXPECTED_CASCADE_IDENTITIES = tuple(
    json.loads(CASCADE_CONTRACT_PATH.read_text(encoding="utf-8"))
)


@dataclass(frozen=True)
class CascadeConstraint:
    child_table: str
    constraint_id: int
    parent_table: str
    child_columns: tuple[str, ...]
    parent_columns: tuple[str, ...]

    @property
    def identity(self) -> str:
        child = ",".join(self.child_columns)
        parent = ",".join(self.parent_columns)
        return (
            f"{self.child_table}#{self.constraint_id}({child})->"
            f"{self.parent_table}({parent})"
        )


@pytest.fixture
def installed(tmp_path: Path):
    path = tmp_path / "all-tables.db"
    settings = Settings(database_url=f"sqlite:///{path}", _env_file=None)
    database = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(database, settings).migrate()
    try:
        with database.connect() as conn:
            install_sqlite_capture(
                conn, run_id="all-tables-run", schema_epoch=1, manifest=MANIFEST
            )
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "UPDATE shadow_capture_control SET enabled=1 WHERE singleton=1"
            )
            conn.commit()
            yield conn
    finally:
        database.close_local()


_TEXT_OVERRIDES = {
    ("system_model_service_status", "status"): "ok",
    ("system_model_service_status", "trigger"): "manual_test",
    ("model_service_status", "status"): "ok",
    ("model_service_status", "trigger"): "manual_test",
    ("memory_items", "origin"): "ask_answer",
    ("memory_items", "status"): "confirmed",
    ("memory_provenance", "origin"): "ask_answer",
    ("memory_revisions", "status"): "confirmed",
    ("memory_revisions", "promotion_state"): "none",
}


def _minimal_value(table: str, column: str, declared_type: str, ordinal: int):
    override = _TEXT_OVERRIDES.get((table, column))
    if override is not None:
        return override
    upper = declared_type.upper()
    if "INT" in upper:
        return ordinal
    if any(token in upper for token in ("REAL", "FLOA", "DOUB")):
        return float(ordinal)
    if "BLOB" in upper:
        return b"\0\0\0\0"
    if column == "role":
        return "user"
    if column == "email":
        return f"shadow-{ordinal}@example.test"
    if column == "username":
        return f"z00{ordinal:06d}"[-9:]
    if column.endswith("_at"):
        return "2026-01-01T00:00:00+00:00"
    if any(
        token in column
        for token in (
            "json",
            "payload",
            "evidence",
            "context",
            "metadata",
            "scope",
            "fields",
            "findings",
            "member_ids",
            "domain_focus",
            "tags",
            "outline",
            "content",
        )
    ):
        return "{}"
    return f"shadow-{table}-{column}-{ordinal}"


def _minimal_row(conn, spec, ordinal: int) -> dict[str, object]:
    values: dict[str, object] = {}
    for column in conn.execute(f'PRAGMA table_info("{spec.name}")'):
        name = str(column[1])
        required_without_default = bool(column[3]) and column[4] is None
        if name in spec.replication_key or required_without_default:
            values[name] = _minimal_value(
                spec.name, name, str(column[2] or ""), ordinal
            )
    return values


def _where_key(spec, key: dict[str, object]) -> tuple[str, list[object]]:
    return (
        " AND ".join(f'"{column}" IS ?' for column in spec.replication_key),
        [key[column] for column in spec.replication_key],
    )


def _cascade_constraints(conn) -> dict[str, CascadeConstraint]:
    replicated = set(MANIFEST.replicated_names)
    constraints: list[CascadeConstraint] = []
    for child_table in MANIFEST.replicated_names:
        grouped: dict[tuple[int, str], list[tuple[int, str, str]]] = {}
        for row in conn.execute(f'PRAGMA foreign_key_list("{child_table}")'):
            parent_table = str(row[2])
            if (
                parent_table not in replicated
                or str(row[6]).upper() != "CASCADE"
            ):
                continue
            grouped.setdefault((int(row[0]), parent_table), []).append(
                (int(row[1]), str(row[3]), str(row[4]))
            )
        for (constraint_id, parent_table), columns in grouped.items():
            ordered = sorted(columns)
            constraints.append(
                CascadeConstraint(
                    child_table=child_table,
                    constraint_id=constraint_id,
                    parent_table=parent_table,
                    child_columns=tuple(column[1] for column in ordered),
                    parent_columns=tuple(column[2] for column in ordered),
                )
            )
    return {constraint.identity: constraint for constraint in constraints}


def _insert_row(conn, table: str, values: dict[str, object]) -> None:
    columns = tuple(values)
    quoted = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
        tuple(values[column] for column in columns),
    )


def _column_type(conn, table: str, column: str) -> str:
    return next(
        str(row[2] or "")
        for row in conn.execute(f'PRAGMA table_info("{table}")')
        if str(row[1]) == column
    )


def _canonical_key_json(key: dict[str, object]) -> str:
    return json.dumps(key, ensure_ascii=False, separators=(",", ":"))


@pytest.mark.parametrize(
    ("ordinal", "spec"), enumerate(MANIFEST.replicated, start=1)
)
def test_every_replicated_table_exercises_all_capture_mutations(
    installed, ordinal, spec
):
    table = spec.name
    epoch = MANIFEST.schema_pair.epoch
    expected = {
        f"shadow_{kind}_e{epoch}_{table}_{operation}"
        for kind in ("gate", "capture")
        for operation in ("insert", "update", "delete")
    }
    rows = installed.execute(
        "SELECT name,tbl_name,sql FROM sqlite_master "
        "WHERE type='trigger' AND tbl_name=? AND name LIKE 'shadow_%'",
        (table,),
    ).fetchall()
    assert {row[0] for row in rows} == expected
    assert all(row[1] == table and table in row[2] for row in rows)

    values = _minimal_row(installed, spec, ordinal)
    columns = tuple(values)
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    installed.execute(
        f'INSERT INTO "{table}" ({quoted_columns}) '
        f'VALUES ({", ".join("?" for _ in columns)})',
        tuple(values[column] for column in columns),
    )
    installed.commit()

    key = {column: values[column] for column in spec.replication_key}
    non_keys = [
        str(column[1])
        for column in installed.execute(f'PRAGMA table_info("{table}")')
        if str(column[1]) not in spec.replication_key
    ]
    where_sql, where_values = _where_key(spec, key)
    if non_keys:
        non_key = non_keys[0]
        installed.execute(
            f'UPDATE "{table}" SET "{non_key}"="{non_key}" WHERE {where_sql}',
            where_values,
        )

    changed_key = dict(key)
    first_key = spec.replication_key[0]
    old_value = changed_key[first_key]
    changed_key[first_key] = (
        old_value + 1000 if isinstance(old_value, int) else f"{old_value}-changed"
    )
    installed.execute(
        f'UPDATE "{table}" SET "{first_key}"=? WHERE {where_sql}',
        (changed_key[first_key], *where_values),
    )
    changed_where, changed_values = _where_key(spec, changed_key)
    installed.execute(f'DELETE FROM "{table}" WHERE {changed_where}', changed_values)
    installed.commit()

    events = installed.execute(
        "SELECT key_json,operation,run_id,schema_epoch FROM shadow_change_log "
        "WHERE table_name=? ORDER BY seq",
        (table,),
    ).fetchall()
    expected_events = [(key, "upsert")]
    if non_keys:
        expected_events.append((key, "upsert"))
    expected_events.extend(
        [
            (key, "delete"),
            (changed_key, "upsert"),
            (changed_key, "delete"),
        ]
    )
    assert [(json.loads(row[0]), row[1]) for row in events] == expected_events
    assert all(row[2:] == ("all-tables-run", 1) for row in events)


def test_cascade_contract_pins_every_replicated_on_delete_cascade(installed):
    actual = _cascade_constraints(installed)
    # The reviewed prompt's estimate was 58; the executable v31 schema has 59
    # replicated CASCADE constraints. Pin the real current-schema set so a new
    # constraint cannot silently escape the behavior matrix.
    assert len(EXPECTED_CASCADE_IDENTITIES) == 59
    assert set(actual) == set(EXPECTED_CASCADE_IDENTITIES)


@pytest.mark.parametrize(
    ("ordinal", "constraint_identity"),
    enumerate(EXPECTED_CASCADE_IDENTITIES, start=1),
)
def test_every_replicated_fk_cascade_captures_child_before_parent(
    installed, ordinal, constraint_identity
):
    constraints = _cascade_constraints(installed)
    assert set(constraints) == set(EXPECTED_CASCADE_IDENTITIES)
    constraint = constraints[constraint_identity]
    specs = {spec.name: spec for spec in MANIFEST.replicated}
    parent_spec = specs[constraint.parent_table]
    child_spec = specs[constraint.child_table]

    parent_values = _minimal_row(installed, parent_spec, 1000 + ordinal * 2)
    child_values = _minimal_row(installed, child_spec, 1001 + ordinal * 2)
    for parent_column in constraint.parent_columns:
        parent_values.setdefault(
            parent_column,
            _minimal_value(
                constraint.parent_table,
                parent_column,
                _column_type(installed, constraint.parent_table, parent_column),
                1000 + ordinal * 2,
            ),
        )
    for child_column, parent_column in zip(
        constraint.child_columns, constraint.parent_columns, strict=True
    ):
        child_values[child_column] = parent_values[parent_column]

    # Seed only the two rows while FK checks are off: unrelated required FKs on
    # either row intentionally need no third-party fixture. Re-enable FK before
    # the observed parent DELETE so the selected schema constraint itself is
    # exercised by SQLite, not simulated in test code.
    assert installed.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    _insert_row(installed, constraint.parent_table, parent_values)
    _insert_row(installed, constraint.child_table, child_values)
    installed.commit()
    installed.execute("DELETE FROM shadow_change_log")
    installed.commit()
    installed.execute("PRAGMA foreign_keys=ON")
    assert installed.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    parent_key = {
        column: parent_values[column] for column in parent_spec.replication_key
    }
    child_key = {
        column: child_values[column] for column in child_spec.replication_key
    }
    parent_where, parent_parameters = _where_key(parent_spec, parent_key)
    installed.execute(
        f'DELETE FROM "{constraint.parent_table}" WHERE {parent_where}',
        parent_parameters,
    )
    installed.commit()

    events = installed.execute(
        "SELECT table_name,key_json,operation,run_id,schema_epoch "
        "FROM shadow_change_log ORDER BY seq"
    ).fetchall()
    assert [tuple(row) for row in events] == [
        (
            constraint.child_table,
            _canonical_key_json(child_key),
            "delete",
            "all-tables-run",
            1,
        ),
        (
            constraint.parent_table,
            _canonical_key_json(parent_key),
            "delete",
            "all-tables-run",
            1,
        ),
    ]


def _plan(conn, sql: str, params: tuple[str, ...]) -> str:
    return " | ".join(
        str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
    )


def test_shadow_unique_guards_are_named_and_do_not_steal_operational_query_plans(
    installed,
):
    expected = {
        "shadow_uq_community_members_replication_key",
        "shadow_uq_kg_canonical_scratch_replication_key",
        "shadow_uq_kg_cluster_scratch_replication_key",
        "shadow_uq_knowledge_object_sources_replication_key",
    }
    actual = {
        row[0]
        for row in installed.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'shadow_uq_%'"
        )
    }
    assert actual == expected
    assert [
        row[2]
        for row in installed.execute(
            "PRAGMA index_info(shadow_uq_community_members_replication_key)"
        )
    ] == ["level", "canonical_id", "notebook_id"]

    assert "idx_kg_cluster_scratch_nb_run" in _plan(
        installed,
        "SELECT object_id,seed FROM kg_cluster_scratch "
        "WHERE notebook_id=? AND run_id=? ORDER BY rowid",
        ("nb", "run"),
    )
    assert "idx_kg_canonical_scratch_nb_run_seed" in _plan(
        installed,
        "SELECT seed,canonical_id FROM kg_canonical_scratch "
        "WHERE notebook_id=? AND run_id=?",
        ("nb", "run"),
    )
    community_plan = _plan(
        installed,
        "SELECT community_id FROM community_members "
        "WHERE notebook_id=? AND canonical_id=? ORDER BY level DESC,community_id",
        ("nb", "canonical"),
    )
    assert "idx_commmem_nb_can" in community_plan
    kos_plan = _plan(
        installed,
        "SELECT DISTINCT object_id FROM knowledge_object_sources "
        "WHERE source_id=? AND notebook_id=?",
        ("source", "nb"),
    )
    assert "shadow_uq_knowledge_object_sources" not in kos_plan
    assert "idx_kos_" in kos_plan


def test_production_scratch_vector_stream_keeps_rowid_order_after_analyze(installed):
    installed.executemany(
        "INSERT INTO kg_cluster_scratch(notebook_id,run_id,object_id,seed) "
        "VALUES ('nb-order','run-order',?,?)",
        [("object-z", "seed-z"), ("object-a", "seed-a"), ("object-m", "seed-m")],
    )
    # Deliberately use a different embedding insertion order so only the
    # scratch rowid contract can determine the production join stream.
    installed.executemany(
        "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
        "VALUES (?,'nb-order',?,'t')",
        [
            ("object-a", "vector-a"),
            ("object-m", "vector-m"),
            ("object-z", "vector-z"),
        ],
    )
    installed.commit()

    def production_rows():
        return [
            (row["seed"], row["vector"])
            for row in UnifiedKgStore.scratch_vector_rows(
                installed, "nb-order", "run-order"
            )
        ]

    expected = [
        ("seed-z", "vector-z"),
        ("seed-a", "vector-a"),
        ("seed-m", "vector-m"),
    ]
    assert production_rows() == expected
    installed.execute("ANALYZE")
    assert production_rows() == expected

    production_sql = (
        "SELECT s.seed AS seed, e.vector AS vector "
        "FROM knowledge_embeddings e "
        "JOIN kg_cluster_scratch s ON s.object_id=e.object_id "
        "AND s.notebook_id=e.notebook_id AND s.run_id=? "
        "WHERE e.notebook_id=? ORDER BY s.rowid"
    )
    plan = _plan(installed, production_sql, ("run-order", "nb-order"))
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan
    assert "shadow_uq_kg_cluster_scratch_replication_key" not in plan


def test_rebuilt_and_shadow_internal_tables_have_no_capture_triggers(installed):
    excluded = set(MANIFEST.rebuilt_names) | {
        spec.name for spec in MANIFEST.tables if spec.table_class.value == "shadow-internal"
    }
    for table in excluded:
        assert installed.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name=? AND name LIKE 'shadow_%'",
            (table,),
        ).fetchone() is None
