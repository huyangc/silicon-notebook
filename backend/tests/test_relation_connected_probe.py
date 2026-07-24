"""Regression guards for candidate-bounded isolated-node relation probes."""

import sqlite3

from app.repositories.sqlite.knowledge_store import KnowledgeStore


def test_high_degree_relation_probe_short_circuits_per_candidate():
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.executescript(
        """
        CREATE TABLE knowledge_relations (
            notebook_id TEXT NOT NULL,
            source_object_id TEXT NOT NULL,
            target_object_id TEXT NOT NULL
        );
        CREATE INDEX idx_rel_source
            ON knowledge_relations(notebook_id, source_object_id);
        CREATE INDEX idx_rel_target
            ON knowledge_relations(notebook_id, target_object_id);
        """
    )
    fanout = 10_000
    database.executemany(
        "INSERT INTO knowledge_relations VALUES (?,?,?)",
        (("nb", "source-hub", f"leaf-{index}") for index in range(fanout)),
    )
    database.executemany(
        "INSERT INTO knowledge_relations VALUES (?,?,?)",
        (("nb", f"other-{index}", "target-hub") for index in range(fanout)),
    )

    progress_calls = 0

    def count_progress():
        nonlocal progress_calls
        progress_calls += 1
        return 0

    database.set_progress_handler(count_progress, 100)
    rows = KnowledgeStore.relation_connected_object_ids(
        database, "nb", ["source-hub", "target-hub", "isolated"]
    )
    database.set_progress_handler(None, 0)

    assert {row["object_id"] for row in rows} == {"source-hub", "target-hub"}
    assert len(rows) == 2
    # A materializing endpoint query executes work proportional to all 20k
    # incident edges.  Indexed EXISTS should finish in only a few VM batches.
    assert progress_calls < 20
