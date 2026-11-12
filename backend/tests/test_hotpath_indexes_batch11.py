"""Operator-built recount covering index; no live PostgreSQL required."""
import re
from pathlib import Path

from app.repositories.postgres import unified_kg_store
from app.repositories.postgres.hotpath_indexes import HOTPATH_INDEX_SPECS


def test_recount_covering_index_shape_and_member_columns(monkeypatch):
    spec = next(s for s in HOTPATH_INDEX_SPECS
                if s.name == "idx_knowledge_objects_rank_recount")
    assert spec.table == "knowledge_objects"
    assert spec.columns == ("id",)
    assert spec.include == ("notebook_id", "object_type", "status", "source_id")
    assert spec.predicate == spec.predicate_shape == ""
    assert spec.ddl("public", concurrently=True).as_string(None) == (
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "idx_knowledge_objects_rank_recount" '
        'ON "public"."knowledge_objects"(id) '
        'INCLUDE (notebook_id, object_type, status, source_id)'
    )

    statements = []

    class EmptyRows:
        def fetchall(self):
            return []

    def capture(_db, statement, _params):
        statements.append(statement)
        return EmptyRows()

    monkeypatch.setattr(unified_kg_store, "execute_ids", capture)
    unified_kg_store.UnifiedKgStore.concept_recount_rows(
        object(), "nb", ["canonical"], ceiling=None,
    )
    member_leg = statements[0].split("UNION ALL", 1)[0]
    assert set(re.findall(r"\bo\.([a-z_]+)", member_leg)) == {
        *spec.columns, *spec.include,
    }


def test_batch11_is_operator_built_without_a_migration():
    migrations = Path(unified_kg_store.__file__).parent / "migrations"
    for path in migrations.glob("*.sql"):
        assert "idx_knowledge_objects_rank_recount" not in path.read_text(), path.name
