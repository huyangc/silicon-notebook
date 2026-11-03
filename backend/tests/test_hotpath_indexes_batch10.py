"""G20 anti-drift and collision checks for the owner-bound sample index."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.repositories.postgres.hotpath_indexes import (
    HOTPATH_INDEX_SPECS,
    _matches_shape,
    _normalized_expr,
)
from tests.hotpath_index_batches import EXPECTED_SPEC_TOTAL

NAME = "idx_ask_jobs_memory_suggest_sample"
MIGRATION = Path(__file__).resolve().parents[2] / "backend/app/repositories/postgres/migrations/0087_memory_suggest.sql"


def _spec():
    return next(spec for spec in HOTPATH_INDEX_SPECS if spec.name == NAME)


def _row():
    spec = _spec()
    return {"indisunique":False, "indnkeyatts":4, "indnatts":4,
            "keys":spec.columns, "include_keys":(), "predicate":spec.predicate_shape,
            "access_method":"btree", "opclasses":spec.opclasses,
            "collations":spec.collations}


def test_batch10_registry_and_online_ddl_match_the_migration():
    spec = _spec()
    assert len(HOTPATH_INDEX_SPECS) == EXPECTED_SPEC_TOTAL
    assert spec.table == "ask_jobs" and spec.columns == ("created_by", "notebook_id", "created_at", "id")
    text = MIGRATION.read_text()
    created = re.search(rf"CREATE INDEX IF NOT EXISTS {NAME}\s+ON (\w+)\(([^)]+)\)\s+WHERE ([^;]+);", text)
    assert created is not None
    assert created.group(1) == spec.table
    assert tuple(column.strip() for column in created.group(2).split(",")) == spec.columns
    assert created.group(3) == spec.predicate
    assert "CONCURRENTLY IF NOT EXISTS" in spec.ddl("public", concurrently=True).as_string(None)


def test_preexisting_index_validation_carries_every_shape_dimension():
    spec = _spec()
    text = MIGRATION.read_text()
    for values in (spec.columns, spec.opclasses, spec.collations):
        assert "ARRAY[" + ", ".join("'" + value + "'" for value in values) + "]" in text
    assert "NOT existing.indisvalid OR NOT existing.indisready" in text
    assert "existing.indisunique" in text and "existing.indnatts <> 4" in text
    assert "actual_predicate IS DISTINCT FROM" in text
    assert "lower(existing.predicate)" not in text


def test_online_builder_prerequisites_match_the_sealed_column_migrations():
    migrations = MIGRATION.parent
    introduced_by = {
        "submitted_via": "0054_question_submitted_via.sql",
        "output": "0070_ask_job_output.sql",
        "report_id": "0080_ask_job_report_id.sql",
    }
    prerequisites = _spec().prerequisites
    assert {column for _, column, _ in prerequisites} == set(introduced_by)
    for table, column, definition in prerequisites:
        assert table == "ask_jobs"
        ddl = (migrations / introduced_by[column]).read_text()
        assert f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition};" in ddl


def test_migration_derived_catalog_includes_online_prebuilt_batch9_and_batch10_indexes():
    from app.migration.shadow.postgres_catalog import EXPECTED_OPERATIONAL_INDEXES

    for name in ("idx_source_elements_reading_order", "idx_source_elements_type_reading_order", NAME):
        spec = next(spec for spec in HOTPATH_INDEX_SPECS if spec.name == name)
        expected = EXPECTED_OPERATIONAL_INDEXES[name]
        assert expected.table == spec.table
        assert len(expected.keys) == len(spec.columns)


@pytest.mark.parametrize("value", ["DONE", "done ", " done", "do::textne", "done::text"])
def test_inspector_rejects_a_same_named_index_with_a_different_literal(value):
    row = _row()
    assert _matches_shape(row, _spec())
    row["predicate"] = row["predicate"].replace("'done'", "'" + value + "'")
    assert not _matches_shape(row, _spec())


def test_sql_normalization_preserves_literal_case_spacing_and_cast_text():
    assert _normalized_expr("STATUS = 'done'::text") == _normalized_expr("status = 'done'")
    assert _normalized_expr("value = 'a  b'") != _normalized_expr("value = 'a b'")
    assert _normalized_expr("value = 'a::text'") != _normalized_expr("value = 'a'")
    assert _normalized_expr('"Answer ID"') != _normalized_expr('"answer id"')
    assert _normalized_expr('"a  b"') != _normalized_expr('"a b"')
