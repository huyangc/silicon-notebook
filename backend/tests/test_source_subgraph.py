from __future__ import annotations

import json

import pytest

from app.repositories import source_subgraph_projection
from app.core.config import Settings
from app.models.notebooks import NotebookCreate
from app.services.source_subgraph import SourceSubgraphService
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'subgraph.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def _seed(repo: SQLiteRepository) -> tuple[str, str, str]:
    notebook = repo.create_notebook(NotebookCreate(name="source subgraph"))
    now = repo._runtime.seams.now()
    with repo._write() as db:
        for source_id in ("src-a", "src-b"):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,parse_status,file_name,"
                "file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
                "VALUES (?,?,?,'markdown','ready','parsed',?, ?,1,?,'','',?,?)",
                (
                    source_id,
                    notebook.id,
                    source_id,
                    f"{source_id}.md",
                    f"{source_id}.md",
                    f"hash-{source_id}",
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO extraction_runs "
                "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
                "VALUES (?,?,?,'kg','completed','kg objects=2 relations=1',?,?)",
                (f"run-{source_id}", notebook.id, source_id, now, now),
            )
            db.execute(
                "INSERT INTO knowledge_source_fact_backfills "
                "(source_id,notebook_id,source_generation,projection_version,status,"
                "after_object_id,objects_scanned,facts_written,incomplete_objects,"
                "incomplete_reason,failure_code,created_at,updated_at) "
                "VALUES (?,?,?,1,'complete','',2,2,0,'','',?,?)",
                (source_id, notebook.id, f"run-{source_id}", now, now),
            )

        db.execute(
            "INSERT INTO unified_kg_state "
            "(notebook_id,dirty,kg_mutation_seq,cluster_mutation_seq,"
            "source_index_backfilled,updated_at) VALUES (?,0,7,3,1,?) "
            "ON CONFLICT(notebook_id) DO UPDATE SET kg_mutation_seq=7,"
            "cluster_mutation_seq=3,source_index_backfilled=1,updated_at=excluded.updated_at",
            (notebook.id, now),
        )
        object_rows = (
            ("ko-a1", "src-a", "A one", "concept"),
            ("ko-a2", "src-a", "A two", "concept"),
            ("ko-b1", "src-b", "B one", "concept"),
            ("ko-b2", "src-b", "B two", "concept"),
        )
        for object_id, source_id, name, object_type in object_rows:
            evidence = json.dumps(
                [{"source_id": source_id, "element_id": f"el-{object_id}"}]
            )
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
                "created_at,updated_at) VALUES (?,?,?,'approved','',?,?,?, ?,?)",
                (
                    object_id,
                    notebook.id,
                    object_type,
                    json.dumps({"name": name}),
                    evidence,
                    source_id,
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
                "VALUES (?,?,?)",
                (object_id, source_id, notebook.id),
            )
            fact_id = f"fact-{object_id}"
            db.execute(
                "INSERT INTO knowledge_source_facts "
                "(id,notebook_id,source_id,source_generation,local_object_id,"
                "global_object_id,object_type,payload,evidence,projection_version,"
                "projection_origin,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,'historical',?,?)",
                (
                    fact_id,
                    notebook.id,
                    source_id,
                    f"run-{source_id}",
                    f"local-{object_id}",
                    object_id,
                    object_type,
                    json.dumps({"name": name}),
                    evidence,
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO knowledge_source_fact_elements "
                "(fact_id,notebook_id,source_id,source_generation,element_id,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    fact_id,
                    notebook.id,
                    source_id,
                    f"run-{source_id}",
                    f"el-{object_id}",
                    now,
                ),
            )

        for source_id, object_ids in (
            ("src-a", ("ko-a1", "ko-a2")),
            ("src-b", ("ko-b1", "ko-b2")),
        ):
            element_ids = [f"el-{object_id}" for object_id in object_ids]
            db.execute(
                "INSERT INTO chunks "
                "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    f"chunk-{source_id}",
                    notebook.id,
                    source_id,
                    source_id,
                    source_id,
                    json.dumps(element_ids),
                    now,
                ),
            )
        for relation_id, source_id, left, right in (
            ("rel-a", "src-a", "ko-a1", "ko-a2"),
            ("rel-b", "src-b", "ko-b1", "ko-b2"),
            ("rel-cross", "src-a", "ko-a1", "ko-b1"),
        ):
            db.execute(
                "INSERT INTO knowledge_relations "
                "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,"
                "evidence,review_status,created_at) "
                "VALUES (?,?,?,?,?,'part_of',?,'pending',?)",
                (
                    relation_id,
                    notebook.id,
                    source_id,
                    left,
                    right,
                    json.dumps([{"source_id": source_id}]),
                    now,
                ),
            )
        for canonical_id, member_id in (
            ("canon-a", "ko-a1"),
            ("canon-a", "ko-a2"),
            ("canon-b", "ko-b1"),
        ):
            db.execute(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
                "object_type,created_at) VALUES (?,?,?,?,?,'concept',?)",
                (
                    f"cluster-{canonical_id}-{member_id}",
                    notebook.id,
                    canonical_id,
                    member_id,
                    canonical_id,
                    now,
                ),
            )
    return notebook.id, "src-a", "src-b"


def test_selected_source_snapshot_is_induced_before_limit(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    snapshot = repo._runtime.source_subgraphs.snapshot(notebook_id, [source_a])

    assert snapshot.allowed_source_ids == (source_a,)
    assert {node.object_id for node in snapshot.nodes} == {"ko-a1", "ko-a2"}
    assert {node.name for node in snapshot.nodes} == {"A one", "A two"}
    assert {relation.relation_id for relation in snapshot.relations} == {"rel-a"}
    assert {chunk.chunk_id for chunk in snapshot.chunks} == {"chunk-src-a"}
    assert {fact.source_id for fact in snapshot.facts} == {source_a}
    assert snapshot.memberships == (
        ("ko-a1", "chunk-src-a"),
        ("ko-a2", "chunk-src-a"),
    )
    assert snapshot.cluster_memberships == (
        ("canon-a", "ko-a1"),
        ("canon-a", "ko-a2"),
    )
    assert snapshot.capability("neighbors").enabled
    assert snapshot.capability("ppr").enabled


def test_empty_scope_is_zero_io_and_unbackfilled_scope_fails_closed(repo, monkeypatch):
    notebook_id, source_a, _source_b = _seed(repo)
    isolated = SourceSubgraphService(
        projections=repo._runtime.index_projections,
        settings=repo.settings,
    )
    monkeypatch.setattr(
        repo._runtime.index_projections,
        "source_subgraph_signature",
        lambda *_args, **_kwargs: pytest.fail("empty scope performed I/O"),
    )
    assert isolated.snapshot(notebook_id, []).degraded_reasons == (
        "empty_source_scope",
    )
    assert isolated.snapshot(
        notebook_id, [f"src-{index:02d}" for index in range(33)]
    ).degraded_reasons == ("source_limit_exceeded",)

    monkeypatch.undo()
    with repo._write() as db:
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=0 WHERE notebook_id=?",
            (notebook_id,),
        )
    snapshot = SourceSubgraphService(
        projections=repo._runtime.index_projections,
        settings=repo.settings,
    ).snapshot(notebook_id, [source_a])
    assert snapshot.nodes == ()
    assert snapshot.degraded_reasons == ("source_index_unavailable",)
    assert not snapshot.capability("neighbors").enabled
    assert not snapshot.capability("exact_lookup").enabled
    assert not snapshot.capability("enumeration").enabled


def test_signature_cache_invalidates_on_mutation_seq_and_generation_drift(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    service = repo._runtime.source_subgraphs
    first = service.snapshot(notebook_id, [source_a])
    assert service.snapshot(notebook_id, [source_a]) is first

    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_source_facts SET updated_at=? WHERE id='fact-ko-a1'",
            ("2099-01-01T00:00:00+00:00",),
        )
        db.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq=kg_mutation_seq+1 "
            "WHERE notebook_id=?",
            (notebook_id,),
        )
    refreshed = service.snapshot(notebook_id, [source_a])
    assert refreshed is not first

    with repo._write() as db:
        db.execute(
            "DELETE FROM knowledge_source_fact_elements "
            "WHERE fact_id='fact-ko-a2' AND element_id='el-ko-a2'"
        )
        db.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq=kg_mutation_seq+1 "
            "WHERE notebook_id=?",
            (notebook_id,),
        )
    rebound = service.snapshot(notebook_id, [source_a])
    assert rebound is not refreshed
    assert ("ko-a2", "chunk-src-a") not in rebound.memberships

    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_source_fact_backfills SET source_generation='stale-run',"
            "updated_at=? WHERE source_id=?",
            ("2099-01-02T00:00:00+00:00", source_a),
        )
    drifted = service.snapshot(notebook_id, [source_a])
    assert drifted.nodes == ()
    assert "source_projection_unavailable" in drifted.degraded_reasons


def test_signature_is_constant_cost_and_projection_queries_are_source_first(
    repo, monkeypatch
):
    notebook_id, source_a, _source_b = _seed(repo)
    statements = []
    original_rows = source_subgraph_projection._rows

    def capture_rows(connection, sql, params):
        statements.append((sql, tuple(params)))
        return original_rows(connection, sql, params)

    monkeypatch.setattr(source_subgraph_projection, "_rows", capture_rows)
    with repo._runtime.database.connect() as db:
        signature = source_subgraph_projection.source_subgraph_signature_on(
            db,
            notebook_id,
            [source_a],
            placeholder="?",
            postgres=False,
        )
        assert signature
        assert all("COUNT(" not in sql and "MAX(" not in sql for sql, _ in statements)

        statements.clear()
        source_subgraph_projection.source_subgraph_rows_on(
            db,
            notebook_id,
            [source_a],
            repo._runtime.source_subgraphs._limits.as_mapping(),
            placeholder="?",
            postgres=False,
        )
        plans = {}
        for sql, params in statements:
            if "SELECT kos.source_id,ko.id AS object_id" in sql:
                plans["objects"] = db.execute(
                    "EXPLAIN QUERY PLAN " + sql, params
                ).fetchall()
            elif "SELECT id AS chunk_id" in sql:
                plans["chunks"] = db.execute(
                    "EXPLAIN QUERY PLAN " + sql, params
                ).fetchall()
            elif "SELECT DISTINCT cc.canonical_id" in sql:
                plans["clusters"] = db.execute(
                    "EXPLAIN QUERY PLAN " + sql, params
                ).fetchall()

    plan_text = {
        name: "\n".join(str(row["detail"]) for row in rows)
        for name, rows in plans.items()
    }
    assert "idx_kos_source_object" in plan_text["objects"]
    assert "idx_chunks_source" in plan_text["chunks"]
    assert "idx_kos_source_object" in plan_text["clusters"]
    assert "idx_clusters_member" in plan_text["clusters"]
    assert "idx_kos_notebook" not in plan_text["objects"]
    assert "idx_chunks_nb" not in plan_text["chunks"]
    assert "idx_clusters_nb" not in plan_text["clusters"]


def test_unknown_fact_projection_version_fails_closed(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_source_facts SET projection_version=2 WHERE source_id=?",
            (source_a,),
        )
        db.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq=kg_mutation_seq+1 "
            "WHERE notebook_id=?",
            (notebook_id,),
        )
    snapshot = repo._runtime.source_subgraphs.snapshot(notebook_id, [source_a])
    assert "fact_projection_version_unsupported" in snapshot.degraded_reasons
    assert not snapshot.capability("neighbors").enabled
    assert not snapshot.capability("source_facts").enabled
    assert not snapshot.capability("ppr").enabled


def test_cached_payload_and_evidence_are_recursively_immutable(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_source_facts SET payload=?,evidence=? WHERE id='fact-ko-a1'",
            (
                json.dumps({"name": "A one", "nested": {"items": ["x"]}}),
                json.dumps(
                    [
                        {
                            "source_id": source_a,
                            "element_id": "el-ko-a1",
                            "nested": {"items": ["x"]},
                        }
                    ]
                ),
            ),
        )
        db.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq=kg_mutation_seq+1 "
            "WHERE notebook_id=?",
            (notebook_id,),
        )
    fact = repo._runtime.source_subgraphs.snapshot(notebook_id, [source_a]).facts[0]
    with pytest.raises(TypeError):
        fact.payload["nested"]["items"][0] = "poison"
    with pytest.raises(TypeError):
        fact.evidence[0]["nested"]["items"][0] = "poison"


def test_object_overflow_disables_topology_without_leaking_other_sources(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    settings = repo.settings.model_copy(update={"source_subgraph_max_objects": 1})
    snapshot = SourceSubgraphService(
        projections=repo._runtime.index_projections,
        settings=settings,
    ).snapshot(notebook_id, [source_a])
    assert "object_limit_exceeded" in snapshot.degraded_reasons
    assert not snapshot.capability("neighbors").enabled
    assert all(source_a in node.source_ids for node in snapshot.nodes)
    assert all("src-b" not in node.source_ids for node in snapshot.nodes)


def test_unselected_library_rows_cannot_consume_selected_source_rails(repo):
    notebook_id, source_a, source_b = _seed(repo)
    now = repo._runtime.seams.now()
    with repo._write() as db:
        for index in range(30):
            object_id = f"ko-000-b-{index:02d}"
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
                "created_at,updated_at) VALUES (?,?,'concept','approved','','{}','[]',?,?,?)",
                (object_id, notebook_id, source_b, now, now),
            )
            db.execute(
                "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
                "VALUES (?,?,?)",
                (object_id, source_b, notebook_id),
            )
            db.execute(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
                "object_type,created_at) VALUES (?,?,?,?,?,'concept',?)",
                (
                    f"cluster-000-b-{index:02d}",
                    notebook_id,
                    f"canonical-000-b-{index:02d}",
                    object_id,
                    object_id,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO knowledge_relations "
                "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,"
                "evidence,review_status,created_at) VALUES (?,?,?,?,?,'part_of','[]',"
                "'pending',?)",
                (
                    f"relation-000-b-{index:02d}",
                    notebook_id,
                    source_b,
                    "ko-b1",
                    object_id,
                    now,
                ),
            )

    settings = repo.settings.model_copy(
        update={
            "source_subgraph_max_objects": 2,
            "source_subgraph_max_relations": 1,
            "source_subgraph_max_cluster_memberships": 2,
        }
    )
    snapshot = SourceSubgraphService(
        projections=repo._runtime.index_projections,
        settings=settings,
    ).snapshot(notebook_id, [source_a])

    assert snapshot.complete
    assert snapshot.degraded_reasons == ()
    assert {node.object_id for node in snapshot.nodes} == {"ko-a1", "ko-a2"}
    assert {relation.relation_id for relation in snapshot.relations} == {"rel-a"}
    assert snapshot.cluster_memberships == (
        ("canon-a", "ko-a1"),
        ("canon-a", "ko-a2"),
    )


def test_incomplete_projection_exposes_only_source_local_labels_and_disables_fact_consumers(
    repo,
):
    notebook_id, source_a, _source_b = _seed(repo)
    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_objects SET payload=? WHERE id='ko-a1'",
            (json.dumps({"name": "fused payload must stay hidden"}),),
        )
        db.execute(
            "UPDATE knowledge_source_fact_backfills SET status='incomplete',"
            "incomplete_objects=1,incomplete_reason='ambiguous_payload' "
            "WHERE source_id=?",
            (source_a,),
        )

    snapshot = repo._runtime.source_subgraphs.snapshot(notebook_id, [source_a])

    assert {node.name for node in snapshot.nodes} == {"A one", "A two"}
    assert all(
        item["source_id"] == source_a
        for fact in snapshot.facts
        for item in fact.evidence
    )
    assert "source_projection_incomplete" in snapshot.degraded_reasons
    assert snapshot.capability("neighbors").enabled
    assert not snapshot.capability("source_facts").enabled
    assert not snapshot.capability("ppr").enabled
    assert not snapshot.complete


def test_live_projection_derives_completeness_from_current_generation_fact_count(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    with repo._write() as db:
        db.execute(
            "DELETE FROM knowledge_source_fact_backfills WHERE source_id=?",
            (source_a,),
        )
        db.execute(
            "UPDATE knowledge_source_facts SET projection_origin='live' "
            "WHERE id='fact-ko-a1'",
        )
        db.execute("DELETE FROM knowledge_source_facts WHERE id='fact-ko-a2'")

    snapshot = repo._runtime.source_subgraphs.snapshot(notebook_id, [source_a])

    assert snapshot.source_generations[0].projection_status == "incomplete"
    assert {node.object_id for node in snapshot.nodes} == {"ko-a1", "ko-a2"}
    assert {node.name for node in snapshot.nodes} == {"A one", ""}
    assert not snapshot.capability("neighbors").enabled
    assert not snapshot.capability("source_facts").enabled
    assert not snapshot.capability("ppr").enabled


def test_non_positive_snapshot_rails_fail_settings_validation(monkeypatch):
    monkeypatch.setenv("SOURCE_SUBGRAPH_MAX_OBJECTS", "0")
    with pytest.raises(ValueError, match="SOURCE_SUBGRAPH_MAX_OBJECTS"):
        Settings(_env_file=None)
