"""Staged replacement must requeue surviving partitions before losing adjacency."""
import json

from app.domain.kg.relink import RELINK_ALGORITHM_VERSION
from app.models.schemas import NotebookCreate


def assert_staged_cleanup_receipts(repo, *, postgres: bool, rollback: bool, staged: bool):
    nb = repo.create_notebook(NotebookCreate(name="staged relink")).id
    other = repo.create_notebook(NotebookCreate(name="unrelated relink")).id
    work = repo._runtime.knowledge.relink_work
    if postgres:
        from app.repositories.postgres.kg_build_job_store import KgBuildJobStore
    else:
        from app.repositories.sqlite.kg_build_job_store import KgBuildJobStore

    def execute(db, sql, params=()):
        return db.execute(sql.replace("?", "%s") if postgres else sql, params)

    now = "2026-01-01T00:00:00+00:00"
    with repo._write() as db:
        for sid, owner, kind in (("replaced", nb, "file"), ("fused", nb, "file"),
                                 ("hidden", nb, "memory"), ("edge-only", nb, "file"),
                                 ("other", other, "file")):
            execute(db, "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                    "parse_status,file_name,summary,created_at,updated_at) "
                    "VALUES (?,?,?,?,'ready','ready','','',?,?)",
                    (sid, owner, sid, kind, now, now))
        for oid, sid, kind, name, evidence in (
            ("owned", "replaced", "concept", "old source", []),
            ("fused-object", "fused", "concept", "old fusion", [{"source_id": "replaced"}]),
            ("blank-concept", "", "concept", "Engram", []),
            ("blank-claim", "", "claim", "Engram improves memory", []),
            ("hidden-concept", "hidden", "concept", "Engram", []),
            ("hidden-claim", "hidden", "claim", "Engram improves memory", []),
            ("edge-only-concept", "edge-only", "concept", "Engram", []),
            ("edge-only-claim", "edge-only", "claim", "Engram improves memory", []),
        ):
            execute(db, "INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,"
                    "status,payload,evidence,created_at,updated_at) "
                    "VALUES (?,?,?,?,'approved',?,?,?,?)",
                    (oid, nb, sid, kind, json.dumps({"name": name}), json.dumps(evidence), now, now))
            repo._runtime.knowledge.replace_object_sources(db, oid, nb, json.dumps(evidence))
        for index, target in enumerate(("blank-concept", "blank-claim", "hidden-concept", "hidden-claim")):
            execute(db, "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
                    "source_object_id,target_object_id,edge_type,evidence,created_at) "
                    "VALUES (?,?,?,'owned',?,'about','[]',?)",
                    (f"old-edge-{index}", nb, "replaced" if index % 2 or not staged else None, target, now))
        # Relation provenance can name the replaced source even when neither
        # endpoint is deleted (for example, previously fused knowledge).
        execute(db, "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
                "source_object_id,target_object_id,edge_type,evidence,created_at) "
                "VALUES ('edge-only',?,'replaced','edge-only-claim','edge-only-concept','about','[]',?)",
                (nb, now))
        for notebook in (nb, other):
            work.prepare(db, notebook, RELINK_ALGORITHM_VERSION)
            for row in work.page(db, notebook, None, 10):
                assert work.finish(db, notebook, row["source_id"], row["revision"], 0, 0, 0)
            assert work.finish_full(db, notebook)

    class AbortPublication(Exception):
        pass

    try:
        with repo._write() as db:
            if staged:
                KgBuildJobStore._delete_source_kg(db, nb, "replaced")
            else:
                repo._runtime.knowledge.clear_source_graph_state(db, "replaced", nb)
            if rollback:
                raise AbortPublication()
    except AbortPublication:
        assert rollback

    with repo._connect() as db:
        pending = {row["source_id"] for row in work.page(db, nb, None, 10)}
        assert work.count_pending(db, other) == 0
        live = {row["id"] for row in execute(db, "SELECT id FROM knowledge_objects WHERE notebook_id=?", (nb,))}
        if rollback:
            assert pending == set()
            assert {"owned", "fused-object"} <= live
        else:
            assert pending == {"", "replaced", "fused", "hidden", "edge-only"}
            assert live == {"blank-concept", "blank-claim", "hidden-concept", "hidden-claim",
                            "edge-only-concept", "edge-only-claim"}
    if not rollback:
        # Formerly connected partitions now need deterministic completion.
        assert repo._runtime.knowledge_lifecycle.relink_notebook_kg(nb, incremental=True)["edges_added"] == 3
