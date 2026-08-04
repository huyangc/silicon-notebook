from __future__ import annotations

import json

import pytest

import app.services.source_graph_primitives as primitives_module
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


def test_neighbors_relation_search_and_two_hop_chain_stay_inside_scope(repo):
    notebook_id, source_a, source_b = _add_chain_and_manual(repo)
    primitives = repo._runtime.source_graph_primitives
    snapshot = primitives.snapshot(notebook_id, [source_a])

    neighbors = primitives.neighbors(snapshot, "ko-a1", direction="out")
    assert [node.object_id for node in neighbors.nodes] == ["ko-a2"]
    assert {relation.source_id for relation in neighbors.relations} == {source_a}
    assert all(source_b not in node.source_ids for node in neighbors.nodes)

    relations = primitives.retrieve_relations(
        snapshot, "A two", endpoint_object_ids=["ko-a2"]
    )
    assert {hit.relation.relation_id for hit in relations.hits} == {"rel-a", "rel-a2"}
    assert all(hit.relation.source_id == source_a for hit in relations.hits)

    chain = primitives.follow_chain(
        snapshot, "ko-a1", edge_type="part_of", direction="out"
    )
    assert len(chain.inferences) == 1
    assert chain.inferences[0].via_id == "ko-a2"
    assert chain.inferences[0].target_id == "ko-a3"
    assert {hop.relation_id for hop in chain.inferences[0].hops} == {
        "rel-a",
        "rel-a2",
    }


def test_high_degree_and_expand_graph_obey_result_and_node_bounds(repo, monkeypatch):
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
    snapshot = repo._runtime.source_graph_primitives.snapshot(notebook_id, [source_a])
    neighbors = repo._runtime.source_graph_primitives.neighbors(
        snapshot, "ko-a1", direction="out", max_results=3
    )
    assert len(neighbors.relations) == 3
    assert neighbors.truncated

    expansion = repo._runtime.source_graph_primitives.expand_graph(
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

    one_node = repo._runtime.source_graph_primitives.expand_graph(
        snapshot,
        ["ko-a1"],
        max_depth=3,
        max_fan_out=16,
        max_nodes=1,
    )
    assert [node.object_id for node in one_node.nodes] == ["ko-a1"]
    assert one_node.relations == ()

    captured_relation_counts = []
    original_compose = primitives_module.compose_two_hop_paths

    def capture_compose(nodes, relations, *args, **kwargs):
        captured_relation_counts.append(len(relations))
        return original_compose(nodes, relations, *args, **kwargs)

    monkeypatch.setattr(primitives_module, "compose_two_hop_paths", capture_compose)
    repo._runtime.source_graph_primitives.follow_chain(
        snapshot,
        "ko-a1",
        edge_type="part_of",
        max_fan_out=3,
        max_results=16,
    )
    # At most one bounded first frontier and one bounded second frontier per
    # middle node reach the quadratic pure composer; the 20 parallel edges do
    # not expand it to the complete snapshot relation set.
    assert captured_relation_counts == [4]

    # Non-transitive edges are filtered before frontier truncation. They may
    # never consume the bounded seats and starve a valid transitive chain when
    # the caller leaves edge_type unspecified.
    default_chain = repo._runtime.source_graph_primitives.follow_chain(
        snapshot,
        "ko-a1",
        max_fan_out=3,
        max_results=16,
    )
    assert len(default_chain.inferences) == 1
    assert captured_relation_counts == [4, 4]
    unsupported = repo._runtime.source_graph_primitives.follow_chain(
        snapshot, "ko-a1", edge_type="contrasts_with"
    )
    assert unsupported.capability.reason == "unsupported_chain_edge_type"


def test_exact_subtree_and_cursor_enumeration_return_only_selected_source(repo):
    notebook_id, source_a, source_b = _add_chain_and_manual(repo)
    primitives = repo._runtime.source_graph_primitives
    snapshot = primitives.snapshot(notebook_id, [source_a])

    exact = primitives.exact_lookup(snapshot, "set_db 如何使用")
    assert {chunk.chunk_id for chunk in exact.chunks} == {
        "manual-main",
        "manual-args",
        "manual-example",
    }
    assert {chunk.source_id for chunk in exact.chunks} == {source_a}
    assert all(chunk.source_id != source_b for chunk in exact.chunks)

    first = primitives.enumerate(snapshot, "chunks", limit=2)
    assert first.total == len(snapshot.chunks)
    assert len(first.items) == 2
    assert first.next_cursor
    assert not first.complete
    second = primitives.enumerate(
        snapshot, "chunks", after=first.next_cursor, limit=100
    )
    assert second.complete
    assert not {item.chunk_id for item in first.items} & {
        item.chunk_id for item in second.items
    }
    assert all(item.source_id == source_a for item in (*first.items, *second.items))


def test_channel_specific_capability_reasons_are_preserved(repo):
    notebook_id, source_a, _source_b = _add_chain_and_manual(repo)
    with repo._write() as db:
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=0 WHERE notebook_id=?",
            (notebook_id,),
        )
    primitives = repo._runtime.source_graph_primitives
    snapshot = primitives.snapshot(notebook_id, [source_a])
    assert primitives.neighbors(snapshot, "ko-a1").capability.reason == (
        "source_index_unavailable"
    )
    assert primitives.exact_lookup(snapshot, "set_db").capability.reason == (
        "source_index_unavailable"
    )
    assert primitives.enumerate(snapshot, "chunks").capability.reason == (
        "source_index_unavailable"
    )


def test_shadow_primitives_are_not_public_retrieval_consumers(repo, monkeypatch):
    notebook_id, _source_a, _source_b = _add_chain_and_manual(repo)

    def unexpected_snapshot(*_args, **_kwargs):
        pytest.fail("public retrieval consumed shadow source-graph primitives")

    monkeypatch.setattr(
        repo._runtime.source_graph_primitives, "snapshot", unexpected_snapshot
    )
    assert repo.retrieval.exact_lookup_chunks(notebook_id, "set_db") == []
