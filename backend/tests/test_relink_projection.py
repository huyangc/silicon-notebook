"""Hermetic compact-reader semantics and PostgreSQL planning boundary."""
import sqlite3

from app.repositories.postgres.knowledge_store import KnowledgeStore as PgStore
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from tests.relink_projection_cases import assert_projection, projection_rows


def test_sqlite_compact_projection_preserves_relink_inputs():
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE knowledge_objects (id, notebook_id, source_id, "
                   "object_type, status, payload, evidence)")
        db.executemany("INSERT INTO knowledge_objects VALUES (?, ?, ?, ?, ?, ?, ?)",
                       projection_rows())
        assert_projection(KnowledgeStore, db)


def test_postgres_relink_reads_request_parameter_specific_plans():
    class Connection:
        def __init__(self):
            self.planning_modes = []

        def execute(self, statement, params, *, prepare=None):
            self.planning_modes.append(prepare)
            return self

        def fetchall(self):
            return []

    db = Connection()
    PgStore.relink_object_rows_for_source(db, "nb", "s")
    PgStore.relink_node_rows_for_source(db, "nb", "s")
    assert db.planning_modes == [False, False]
