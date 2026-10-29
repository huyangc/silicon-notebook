"""Unit tests (fake-connection only, no live PG -- G1 tier, same placement
rule as the batch-1..7 siblings) for batch 9 of ``HOTPATH_INDEX_SPECS``
(migration ``0086_source_element_reading_order.sql``): the two reading-order
successors of the ``(created_at, id)`` element indexes.

Anti-drift, same rationale as the earlier batches: the migration's DDL, its
DO-block validation values and the spec tuple are hand-authored copies of one
index shape, and the expression in all of them must be the one the readers
order by (``app.repositories.element_reading_order.INDEX_DIGITS_SQL``).
The live half (catalog echo, builder-first accept, reject paths) is
``tests/postgres/test_hotpath_indexes_batch9_live.py``.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.repositories.element_reading_order import INDEX_DIGITS_SQL
from app.repositories.postgres.hotpath_indexes import HOTPATH_INDEX_SPECS
from tests.hotpath_index_batches import EXPECTED_SPEC_TOTAL, total_mismatch_message

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (
    _REPO_ROOT / "backend" / "app" / "repositories" / "postgres"
    / "migrations" / "0086_source_element_reading_order.sql"
)
_NAMES = ("idx_source_elements_reading_order", "idx_source_elements_type_reading_order")
_RETIRED = ("idx_source_elements_source_created", "idx_source_elements_source_type")


def _ddl_only() -> str:
    return "\n".join(
        line for line in _MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )


def _specs():
    found = {spec.name: spec for spec in HOTPATH_INDEX_SPECS if spec.name in _NAMES}
    assert set(found) == set(_NAMES), sorted(found)
    return [found[name] for name in _NAMES]


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def test_registry_total_counts_batch_8():
    names = [spec.name for spec in HOTPATH_INDEX_SPECS]
    assert len(names) == EXPECTED_SPEC_TOTAL, total_mismatch_message(names)
    assert not set(_RETIRED) & set(names)


def test_specs_carry_the_readers_expression():
    for spec in _specs():
        assert spec.table == "source_elements"
        assert spec.unique is False and spec.include == () and spec.predicate == ""
        assert spec.ddl_columns.endswith(f", {INDEX_DIGITS_SQL}, id"), spec.name
        assert len(spec.columns) == len(spec.opclasses) == len(spec.collations)
        text = spec.ddl("public", concurrently=True).as_string(None)
        assert text.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS"), text
        assert INDEX_DIGITS_SQL in text, text


def test_migration_creates_the_successors_and_drops_the_retired():
    ddl = _ddl_only()
    created = re.findall(
        r"CREATE INDEX IF NOT EXISTS (\w+)\s+ON source_elements\((.*)\);", ddl)
    assert [name for name, _ in created] == list(_NAMES)
    for (name, columns), spec in zip(created, _specs()):
        assert columns == spec.ddl_columns, name
    dropped = re.findall(r"^DROP INDEX IF EXISTS (\w+);$", ddl, re.MULTILINE)
    assert dropped == list(_RETIRED)
    # Drops after the creates: a crash between them leaves both generations,
    # never neither.
    assert ddl.index("DROP INDEX IF EXISTS") > ddl.rindex("CREATE INDEX IF NOT EXISTS")


def test_do_block_expected_values_reconcile_with_the_specs():
    text = _MIGRATION.read_text(encoding="utf-8")
    for spec in _specs():
        row = (
            f"      ('{spec.name}',\n"
            f"       '{spec.table}', 'btree', false,\n"
            f"       ARRAY[{', '.join(_literal(c) for c in spec.columns)}],\n"
            "       ARRAY[]::text[],\n"
            f"       ARRAY[{', '.join(_literal(c) for c in spec.opclasses)}],\n"
            f"       ARRAY[{', '.join(_literal(c) for c in spec.collations)}])"
        )
        assert row in text, spec.name
