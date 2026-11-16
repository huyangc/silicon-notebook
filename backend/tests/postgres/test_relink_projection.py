"""Real driver parity and repeated-execution planning for relink reads."""
import pytest

from app.repositories.postgres.knowledge_store import KnowledgeStore
from tests.relink_projection_cases import assert_projection, projection_rows

pytestmark = pytest.mark.postgres_integration


def test_compact_projection_and_repeated_calls_do_not_cache_generic_plan(postgres_database):
    with postgres_database.write() as db:
        db.execute("CREATE TEMP TABLE knowledge_objects (id text, notebook_id text, "
                   "source_id text, object_type text, status text, payload jsonb, "
                   "evidence jsonb, ordinal bigint GENERATED ALWAYS AS IDENTITY)")
        for row in projection_rows():
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,source_id,object_type,status,payload,evidence) "
                       "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)", row)
        # Exercise beyond both automatic preparation and generic-plan thresholds.
        for _ in range(16):
            assert_projection(KnowledgeStore, db)
            legacy = KnowledgeStore.relink_object_rows_for_source(db, "nb", "s")
            assert [row["id"] for row in legacy] == ["z-first", "a-second"]
        prepared = db.execute("SELECT statement FROM pg_prepared_statements").fetchall()
        assert not any("FROM knowledge_objects" in row["statement"] for row in prepared)
