from __future__ import annotations

import dataclasses
import json

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.test_source_subgraph import _seed


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'primitives.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def _add_chain_and_manual(repo):
    notebook_id, source_a, source_b = _seed(repo)
    now = repo._runtime.seams.now()
    with repo._write() as db:
        db.execute(
            "UPDATE extraction_runs SET error_message='kg objects=3 relations=2' "
            "WHERE source_id=?",
            (source_a,),
        )
        db.execute(
            "UPDATE knowledge_source_fact_backfills SET objects_scanned=3,"
            "facts_written=3,updated_at=? WHERE source_id=?",
            (now, source_a),
        )
        db.execute(
            "UPDATE knowledge_relations SET evidence=? WHERE id='rel-a'",
            (
                json.dumps(
                    [
                        {
                            "source_id": source_a,
                            "element_id": "el-ko-a1",
                            "quote": "A one contains A two",
                            "confidence": 1.0,
                        }
                    ]
                ),
            ),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
            "created_at,updated_at) VALUES "
            "('ko-a3',?,'concept','approved','',?,?,'src-a',?,?)",
            (
                notebook_id,
                json.dumps({"name": "A three"}),
                json.dumps([{"source_id": source_a, "element_id": "el-ko-a3"}]),
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "VALUES ('ko-a3',?,?)",
            (source_a, notebook_id),
        )
        db.execute(
            "INSERT INTO knowledge_source_facts "
            "(id,notebook_id,source_id,source_generation,local_object_id,"
            "global_object_id,object_type,payload,evidence,projection_version,"
            "projection_origin,created_at,updated_at) "
            "VALUES ('fact-ko-a3',?,?,?,'local-ko-a3','ko-a3','concept',?,?,1,"
            "'historical',?,?)",
            (
                notebook_id,
                source_a,
                f"run-{source_a}",
                json.dumps({"name": "A three"}),
                json.dumps([{"source_id": source_a, "element_id": "el-ko-a3"}]),
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO knowledge_source_fact_elements "
            "(fact_id,notebook_id,source_id,source_generation,element_id,created_at) "
            "VALUES ('fact-ko-a3',?,?,?,'el-ko-a3',?)",
            (notebook_id, source_a, f"run-{source_a}", now),
        )
        db.execute(
            "INSERT INTO knowledge_relations "
            "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,"
            "evidence,review_status,created_at) "
            "VALUES ('rel-a2',?,?,'ko-a2','ko-a3','part_of',?,'verified',?)",
            (
                notebook_id,
                source_a,
                json.dumps(
                    [
                        {
                            "source_id": source_a,
                            "element_id": "el-ko-a3",
                            "quote": "A two contains A three",
                            "confidence": 1.0,
                        }
                    ]
                ),
                now,
            ),
        )
        for index, (chunk_id, section_path, text) in enumerate(
            (
                (
                    "manual-main",
                    "Manual > Commands > set_db",
                    "[Commands > set_db] set_db command overview",
                ),
                (
                    "manual-args",
                    "Manual > Commands > set_db > Arguments",
                    "[Arguments] name and value",
                ),
                (
                    "manual-example",
                    "Manual > Commands > set_db > Examples",
                    "[Examples] an invocation",
                ),
            )
        ):
            db.execute(
                "INSERT INTO chunks "
                "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    chunk_id,
                    notebook_id,
                    source_a,
                    text,
                    section_path,
                    json.dumps([f"manual-el-{index}"]),
                    f"{now}-{index}",
                ),
            )
        db.execute(
            "INSERT INTO chunks "
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES ('manual-b',?,?, 'set_db excluded source',"
            "'Manual > set_db','[]',?)",
            (notebook_id, source_b, now),
        )
        db.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq=kg_mutation_seq+1 "
            "WHERE notebook_id=?",
            (notebook_id,),
        )
    return notebook_id, source_a, source_b


def _primitives(repo):
    """The primitives the selected-source graph activation expands with."""
    return repo._runtime.selected_source_graph._primitives


def test_expand_graph_stays_inside_the_selected_source(repo):
    notebook_id, source_a, source_b = _add_chain_and_manual(repo)
    snapshot = repo._runtime.source_subgraphs.snapshot(notebook_id, [source_a])
    # Defense in depth: even a snapshot carrying an edge that only the other
    # source asserted, between two selected-source nodes, never expands it.
    template = next(r for r in snapshot.relations if r.relation_id == "rel-a")
    foreign = dataclasses.replace(
        template, relation_id="rel-foreign", source_id=source_b,
        target_object_id="ko-a3",
    )
    snapshot = dataclasses.replace(snapshot, relations=(*snapshot.relations, foreign))
    expansion = _primitives(repo).expand_graph(snapshot, ["ko-a1"], max_depth=3)
    assert {node.object_id for node in expansion.nodes} >= {"ko-a1", "ko-a2"}
    assert "rel-foreign" not in {relation.relation_id for relation in expansion.relations}
    assert {relation.source_id for relation in expansion.relations} == {source_a}
    assert all(source_b not in node.source_ids for node in expansion.nodes)
    assert all(set(node.source_ids) <= {source_a} for node in expansion.nodes)


def test_high_degree_expand_graph_obeys_result_and_node_bounds(repo):
    notebook_id, source_a, _source_b = _add_chain_and_manual(repo)
    now = repo._runtime.seams.now()
    with repo._write() as db:
        for index in range(20):
            db.execute(
                "INSERT INTO knowledge_relations "
                "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,"
                "evidence,review_status,created_at) VALUES "
                "(?,?,?,'ko-a1','ko-a2','part_of',?,'pending',?)",
                (
                    f"rel-fan-{index:02d}",
                    notebook_id,
                    source_a,
                    json.dumps(
                        [
                            {
                                "source_id": source_a,
                                "quote": f"bounded edge {index}",
                            }
                        ]
                    ),
                    now,
                ),
            )
            db.execute(
                "INSERT INTO knowledge_relations "
                "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,"
                "evidence,review_status,created_at) VALUES "
                "(?,?,?,'ko-a1','ko-a2','contrasts_with',?,'pending',?)",
                (
                    f"rel-non-transitive-{index:02d}",
                    notebook_id,
                    source_a,
                    json.dumps(
                        [
                            {
                                "source_id": source_a,
                                "quote": f"non-transitive edge {index}",
                            }
                        ]
                    ),
                    now,
                ),
            )
        db.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq=kg_mutation_seq+1 "
            "WHERE notebook_id=?",
            (notebook_id,),
        )
    primitives = _primitives(repo)
    snapshot = repo._runtime.source_subgraphs.snapshot(notebook_id, [source_a])
    expansion = primitives.expand_graph(
        snapshot,
        ["ko-a1"],
        max_depth=99,
        max_fan_out=2,
        max_nodes=2,
    )
    assert len(expansion.nodes) <= 2
    assert len(expansion.relations) <= 4
    assert expansion.truncated
    assert {
        endpoint
        for relation in expansion.relations
        for endpoint in (relation.source_object_id, relation.target_object_id)
    } <= {node.object_id for node in expansion.nodes}

    one_node = primitives.expand_graph(
        snapshot,
        ["ko-a1"],
        max_depth=3,
        max_fan_out=16,
        max_nodes=1,
    )
    assert [node.object_id for node in one_node.nodes] == ["ko-a1"]
    assert one_node.relations == ()


def test_expand_graph_reports_the_snapshot_capability_reason(repo):
    notebook_id, source_a, _source_b = _add_chain_and_manual(repo)
    with repo._write() as db:
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=0 WHERE notebook_id=?",
            (notebook_id,),
        )
    snapshot = repo._runtime.source_subgraphs.snapshot(notebook_id, [source_a])
    expansion = _primitives(repo).expand_graph(snapshot, ["ko-a1"])
    assert expansion.capability.reason == "source_index_unavailable"
    assert expansion.nodes == () and expansion.relations == ()
