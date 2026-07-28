from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.models.knowledge import KnowledgeUpdate
from app.repositories.postgres.embedding_store import EmbeddingStore as PostgresEmbeddingStore
from app.repositories.postgres.governance_store import GovernanceStore as PostgresGovernanceStore
from app.repositories.postgres.index_projection_store import (
    IndexProjectionStore as PostgresIndexProjectionStore,
)
from app.repositories.postgres.knowledge_store import KnowledgeStore as PostgresKnowledgeStore
from app.repositories.postgres.query_store import QueryStore as PostgresQueryStore
from app.repositories.postgres.unified_kg_store import UnifiedKgStore as PostgresUnifiedKgStore
from app.services.knowledge_contracts import USABLE_STATUSES
from app.services.ask_service import knowledge_record
from app.services.repository_runtime import RepositoryCompatibilitySeams
from app.services.retrieval import RetrievedKnowledge, RetrievedRelation
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.vector_index import encode_vector


NOW = "2026-07-22T00:00:00+00:00"


pytestmark = pytest.mark.postgres_integration


def _seams() -> RepositoryCompatibilitySeams:
    lock = threading.Lock()
    counter: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        with lock:
            counter[prefix] = counter.get(prefix, 0) + 1
            return f"{prefix}-golden-{counter[prefix]:04d}"

    return RepositoryCompatibilitySeams(
        new_id=new_id,
        now=lambda: NOW,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )


def _seed_catalog(database) -> None:
    user_sql = (
        "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
        "username,password_hash,password_salt,password_iterations) "
        "VALUES (%s,%s,%s,%s,'active',%s,%s,%s,'','',0)"
    )
    user_values = (
        "user-golden",
        "golden@example.test",
        "Golden",
        "admin",
        NOW,
        NOW,
        "g00123456",
    )
    notebook_sql = (
        "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
        "created_at,updated_at,tier) VALUES (%s,%s,'','thermal','ready',%s,%s,%s,%s)"
    )
    source_sql = (
        "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
        "file_name,summary,created_at,updated_at) "
        "VALUES (%s,%s,%s,'file','ready','ready',%s,%s,%s,%s)"
    )
    with database.write() as connection:
        connection.execute(user_sql, user_values)
        for notebook_id, tier in (("nb-personal", "personal"), ("nb-base", "base")):
            connection.execute(
                notebook_sql,
                (notebook_id, notebook_id, "user-golden", NOW, NOW, tier),
            )
        connection.execute(
            source_sql,
            (
                "source-golden",
                "nb-personal",
                "Thermal handbook 热设计手册",
                "thermal.md",
                "thermal 热设计 evidence",
                NOW,
                NOW,
            ),
        )


@dataclass
class KnowledgeHarness:
    database: object
    knowledge: object
    governance: object
    embedding: object


@pytest.fixture
def knowledge_harness(postgres_database) -> KnowledgeHarness:
    seams = _seams()
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    yield KnowledgeHarness(
        database=postgres_database,
        knowledge=PostgresKnowledgeStore(postgres_database, seams),
        governance=PostgresGovernanceStore(postgres_database, seams),
        embedding=PostgresEmbeddingStore(write=postgres_database.write),
    )


def _evidence() -> list[dict]:
    return [
        {
            "source_id": "source-golden",
            "source_title": "Thermal handbook 热设计手册",
            "element_id": "element-golden",
            "element_type": "paragraph",
            "location_label": "§1",
            "quoted_span": "thermal 热设计 evidence",
            "confidence": 1.0,
        }
    ]


def test_usable_status_filter_and_insertion_order_match_golden(knowledge_harness):
    rows = []
    statuses = (*USABLE_STATUSES, "deprecated")
    for index, status in enumerate(statuses):
        rows.append(
            (
                f"ko-status-{index}",
                "nb-personal",
                "claim",
                status,
                json.dumps({"name": f"status {index}", "optional": None}),
                json.dumps(_evidence()),
                "source-golden",
                NOW,
                NOW,
            )
        )
    with knowledge_harness.database.write() as connection:
        knowledge_harness.knowledge.insert_object_chunk(connection, rows)

    with knowledge_harness.database.connect() as connection:
        objects = knowledge_harness.knowledge.retrieval_objects(
            connection,
            "nb-personal",
            "claim",
            USABLE_STATUSES,
            None,
        )

    assert [item["id"] for item in objects] == [
        "ko-status-0",
        "ko-status-1",
        "ko-status-2",
        "ko-status-3",
    ]
    assert [item["status"] for item in objects] == list(USABLE_STATUSES)
    assert all(item["payload"]["optional"] is None for item in objects)
    assert {item["evidence"][0].source_id for item in objects} == {"source-golden"}


def test_formula_evidence_enrichment_on_postgres(knowledge_harness):
    formula = r"C _ {l} = 2 \sigma (\tilde {C} _ {l}).\tag{7}"
    values = (
        "element-formula",
        "source-golden",
        "formula",
        "eq. 7",
        formula,
        NOW,
    )
    with knowledge_harness.database.write() as connection:
        connection.execute(
            "INSERT INTO source_elements"
            "(id,source_id,element_type,location_label,text,created_at) "
            f"VALUES ({','.join(['%s'] * len(values))})",
            values,
        )

    stored_evidence = {
        "source_id": "source-golden",
        "source_title": "2606.19348v1.pdf",
        "element_id": "element-formula",
        "element_type": "paragraph",
        "location_label": "old location",
        "quoted_span": formula,
        "confidence": 0.98,
    }
    with knowledge_harness.database.connect() as connection:
        enriched = knowledge_harness.knowledge._enrich_evidence(
            connection,
            [stored_evidence],
        )[0]

    assert enriched == {
        **stored_evidence,
        "element_type": "formula",
        "location_label": "eq. 7",
        "element_text": formula,
    }


def test_merge_candidate_batches_preserve_persisted_ordinal(knowledge_harness):
    with knowledge_harness.database.write() as connection:
        for index in range(4):
            knowledge_harness.governance.insert_merge_candidate(
                connection,
                "nb-personal",
                f"canonical-{index}",
                f"canonical-{index + 1}",
                1.0 - index / 10,
                NOW,
            )
        first = knowledge_harness.governance.pending_merges_batch(
            connection, "nb-personal", 2
        )
        all_pending = knowledge_harness.governance.pending_merges(
            connection, "nb-personal"
        )

    assert [row["canonical_a"] for row in first] == ["canonical-0", "canonical-1"]
    assert [row["canonical_a"] for row in all_pending] == [
        "canonical-0",
        "canonical-1",
        "canonical-2",
        "canonical-3",
    ]


def test_community_graph_excludes_rejected_bridges_on_postgres(
    knowledge_harness,
):
    unified = PostgresUnifiedKgStore(knowledge_harness.database, now=lambda: NOW)
    object_ids = ["ko-community-a", "ko-community-b", "ko-community-c", "ko-community-d"]
    objects = [
        (
            object_id,
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": object_id}),
            "[]",
            "source-golden",
            NOW,
            NOW,
        )
        for object_id in object_ids
    ]
    relations = [
        (
            relation_id,
            "nb-personal",
            "source-golden",
            source,
            target,
            "supports",
            "[]",
            NOW,
        )
        for relation_id, source, target in (
            ("rel-community-left", "ko-community-a", "ko-community-b"),
            ("rel-community-right", "ko-community-c", "ko-community-d"),
            ("rel-community-rejected", "ko-community-b", "ko-community-c"),
        )
    ]
    cluster_rows = [
        (
            f"cluster-row-{index}",
            "nb-personal",
            f"canonical-{letter}",
            object_id,
            f"canonical-{letter}",
            "claim",
            "",
            "",
            NOW,
        )
        for index, (letter, object_id) in enumerate(
            zip(("a", "b", "c", "d"), object_ids), 1
        )
    ]
    with knowledge_harness.database.write() as connection:
        knowledge_harness.knowledge.insert_object_chunk(connection, objects)
        knowledge_harness.knowledge.insert_relation_chunk(connection, relations)
        unified.replace_cluster_rows_streamed(
            connection, "nb-personal", "claim", cluster_rows
        )
        connection.execute(
            "UPDATE knowledge_relations SET review_status='rejected' WHERE id=%s",
            ("rel-community-rejected",),
        )
        names, graph_rows = unified.community_graph_rows(connection, "nb-personal")
        graph_rows = list(graph_rows)
        edges = {(row["s"], row["t"]) for row in graph_rows}
        _objects, community_relations = (
            knowledge_harness.knowledge.community_context_rows(
                connection, "nb-personal", object_ids
            )
        )

    assert set(names) == {
        "canonical-a",
        "canonical-b",
        "canonical-c",
        "canonical-d",
    }
    assert edges == {
        ("canonical-a", "canonical-b"),
        ("canonical-c", "canonical-d"),
    }
    assert [(row["s"], row["t"]) for row in graph_rows] == [
        ("canonical-a", "canonical-b"),
        ("canonical-c", "canonical-d"),
    ]
    assert {
        (row["source_object_id"], row["target_object_id"])
        for row in community_relations
    } == {
        ("ko-community-a", "ko-community-b"),
        ("ko-community-c", "ko-community-d"),
    }

    service = CandidateRetrievalService.__new__(CandidateRetrievalService)
    service._connect = knowledge_harness.database.connect
    service.knowledge = knowledge_harness.knowledge
    service._scale_index = lambda *_args, **_kwargs: None
    service.notebook_copy_stats = lambda _notebook_id: {"copyable": True}
    service._embed_query = lambda _query: None
    service._vector_matrix = lambda *_args, **_kwargs: ([], [])
    service.settings = SimpleNamespace(
        relation_recall=10,
        kg_about_downweight_enabled=False,
    )
    service._IN_CHUNK = 100
    retrieved = service._retrieve_relations_scored("nb-personal", "supports")
    assert {row.relation_id for row in retrieved} == {
        "rel-community-left",
        "rel-community-right",
    }


def test_mount_order_and_equal_score_relation_federation_are_id_stable(
    knowledge_harness,
):
    notebook_sql = (
        "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
        "created_at,updated_at,tier) VALUES (%s,%s,'','thermal','ready',%s,%s,%s,'base')"
    )
    mount_sql = (
        "INSERT INTO notebook_bases(notebook_id,base_notebook_id,created_at,created_by) "
        "VALUES (%s,%s,%s,%s)"
    )
    with knowledge_harness.database.write() as connection:
        for notebook_id in ("nb-duplicate-z", "nb-duplicate-a"):
            connection.execute(
                notebook_sql,
                (notebook_id, "Duplicate name", "user-golden", NOW, NOW),
            )
        for notebook_id in ("nb-duplicate-z", "nb-duplicate-a"):
            connection.execute(
                mount_sql,
                ("nb-personal", notebook_id, NOW, "user-golden"),
            )
        mounted = knowledge_harness.governance.mounted_public_base_ids(
            connection, "nb-personal"
        )
        query_store = PostgresQueryStore(knowledge_harness.database)
        summary_rows = query_store.mounted_bases_row(connection, "nb-personal")

    unified = PostgresUnifiedKgStore(knowledge_harness.database, now=lambda: NOW)
    community_mounted = unified.mounted_base_ids("nb-personal")
    expected_bases = ["nb-duplicate-a", "nb-duplicate-z"]
    assert mounted == expected_bases
    assert [row["id"] for row in summary_rows] == expected_bases
    assert community_mounted == expected_bases

    service = CandidateRetrievalService.__new__(CandidateRetrievalService)
    participant_ids = ["nb-personal", *community_mounted]
    service._connect = knowledge_harness.database.connect
    service.notebooks = SimpleNamespace(
        participant_tiers=lambda _db, _active: (
            participant_ids,
            {notebook_id: "base" for notebook_id in community_mounted}
            | {"nb-personal": "personal"},
        )
    )
    service._retrieve_relations_scored = lambda notebook_id, _query: [
        _relation_hit(f"relation-{notebook_id}", 0.75)
    ]
    hits = service._federated_retrieve_relations_impl("nb-personal", "query")
    assert [hit.notebook_id for hit in hits] == participant_ids


def test_unified_kg_equal_rank_top_k_reads_use_id_tie_breaks(knowledge_harness):
    unified = PostgresUnifiedKgStore(knowledge_harness.database, now=lambda: NOW)
    cluster_rows = [
        (
            f"cluster-tie-{index}",
            "nb-personal",
            canonical_id,
            f"member-tie-{index}",
            canonical_name,
            "concept",
            "",
            "",
            NOW,
        )
        for index, (canonical_id, canonical_name) in enumerate(
            (
                ("focus-z", "Focus"),
                ("focus-a", "Focus"),
                ("peer-z", "Peer Z"),
                ("peer-a", "Peer A"),
            ),
            1,
        )
    ]
    ph = "%s"
    with knowledge_harness.database.write() as connection:
        unified.replace_cluster_rows_streamed(
            connection, "nb-personal", "concept", cluster_rows
        )
        member_sql = (
            "INSERT INTO community_members(canonical_id,notebook_id,level,community_id,"
            "canonical_name,centrality) VALUES ("
            + ",".join([ph] * 6)
            + ")"
        )
        for row in (
            ("focus-a", "nb-personal", 2, "community-z", "Focus", 1.0),
            ("focus-a", "nb-personal", 2, "community-a", "Focus", 1.0),
            ("peer-z", "nb-personal", 1, "community-a", "Peer Z", 0.5),
            ("peer-a", "nb-personal", 1, "community-a", "Peer A", 0.5),
        ):
            connection.execute(member_sql, row)
        comention_sql = (
            "INSERT INTO concept_comentions(notebook_id,canonical_a,canonical_b,bridge_claims) "
            "VALUES (" + ",".join([ph] * 4) + ")"
        )
        for row in (
            ("nb-personal", "focus-a", "peer-z", 3),
            ("nb-personal", "focus-a", "peer-a", 3),
        ):
            connection.execute(comention_sql, row)

    assert unified.resolve_focal("nb-personal", "focus") == "focus-a"
    assert unified.top_community_for("nb-personal", "focus-a") == "community-a"
    assert [row["canonical_name"] for row in unified.community_member_peers(
        "nb-personal", "community-a", "focus-a", 2
    )] == ["Peer A", "Peer Z"]
    assert unified.comention_peers(
        "nb-personal", "focus-a", 1, 2
    ) == [("Peer A", 3), ("Peer Z", 3)]


@pytest.mark.postgres_integration
def test_postgres_embedding_bytea_roundtrip_and_fail_closed_validation(
    postgres_database,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresEmbeddingStore(write=postgres_database.write)
    expected = np.asarray([0.125, -1.5, 3.25, 0.0], dtype=np.float32)
    store.replace_knowledge_vectors(
        "nb-personal",
        (("ko-vector-array", expected), ("ko-vector-bytes", encode_vector(expected))),
        created_at=NOW,
    )
    with postgres_database.connect() as connection:
        rows = store.vector_rows(
            connection, "nb-personal", "knowledge_embeddings", "object_id"
        )
        version = store.version_row(
            connection, "nb-personal", "knowledge_embeddings"
        )
    assert {row["vid"] for row in rows} == {"ko-vector-array", "ko-vector-bytes"}
    for row in rows:
        assert isinstance(row["vector"], bytes)
        assert len(row["vector"]) == expected.size * np.dtype(np.float32).itemsize
        np.testing.assert_array_equal(np.frombuffer(row["vector"], dtype=np.float32), expected)
    assert version == {"c": 2, "ts": NOW}

    with pytest.raises(ValueError, match="byte length"):
        store.replace_knowledge_vectors(
            "nb-personal", (("bad-byte-length", b"abc"),), created_at=NOW
        )
    with pytest.raises(ValueError, match="inconsistent dimensions"):
        store.replace_knowledge_vectors(
            "nb-personal",
            (("dim-2", [1.0, 2.0]), ("dim-3", [1.0, 2.0, 3.0])),
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="finite"):
        store.replace_knowledge_vectors(
            "nb-personal", (("nan", [1.0, float("nan")]),), created_at=NOW
        )


@pytest.mark.postgres_integration
def test_postgres_jsonb_preserves_nested_null_and_rejects_top_level_null_or_nan(
    postgres_database,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresKnowledgeStore(postgres_database, _seams())
    valid = (
        "ko-json-null",
        "nb-personal",
        "claim",
        "approved",
        json.dumps({"name": "valid", "optional": None}),
        json.dumps(_evidence()),
        "source-golden",
        NOW,
        NOW,
    )
    with postgres_database.write() as connection:
        store.insert_object_chunk(connection, [valid])
    with postgres_database.connect() as connection:
        row = store.retrieval_objects(
            connection, "nb-personal", "claim", USABLE_STATUSES, ["ko-json-null"]
        )[0]
    assert row["payload"] == {"name": "valid", "optional": None}

    for object_id, payload in (
        ("ko-top-null", "null"),
        ("ko-nan", '{"name":"invalid","score":NaN}'),
    ):
        bad = (object_id, "nb-personal", "claim", "approved", payload, "[]", "", NOW, NOW)
        with pytest.raises(ValueError):
            with postgres_database.write() as connection:
                store.insert_object_chunk(connection, [bad])
    with postgres_database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE id=ANY(%s)",
            (["ko-top-null", "ko-nan"],),
        ).fetchone()["c"]
    assert count == 0


@pytest.mark.postgres_integration
def test_postgres_raw_graph_rows_keep_repository_json_text_contract(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresKnowledgeStore(postgres_database, _seams())
    rows = [
        (
            object_id,
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": object_id}),
            json.dumps(_evidence()),
            "source-golden",
            NOW,
            NOW,
        )
        for object_id in ("ko-json-a", "ko-json-b")
    ]
    relation = (
        "rel-json",
        "nb-personal",
        "source-golden",
        "ko-json-a",
        "ko-json-b",
        "derived_from",
        json.dumps(_evidence()),
        NOW,
    )
    with postgres_database.write() as connection:
        store.insert_object_chunk(connection, rows)
        store.insert_relation_chunk(connection, [relation])
    with postgres_database.connect() as connection:
        graph_nodes = store.graph_node_rows(connection, "nb-personal")
        context_rows = store.relation_context_rows(
            connection, "nb-personal", ["rel-json"]
        )
        start = store.follow_start_row(
            connection, "ko-json-a", "nb-personal", USABLE_STATUSES
        )
        relation_rows = store.follow_relation_evidence_rows(connection, ["rel-json"])
        object_rows = store.follow_object_rows(
            connection,
            "nb-personal",
            ["ko-json-a", "ko-json-b"],
            USABLE_STATUSES,
        )

    assert {json.loads(row["payload"])["name"] for row in graph_nodes} == {
        "ko-json-a",
        "ko-json-b",
    }
    assert json.loads(context_rows[0]["ev"])[0]["element_id"] == "element-golden"
    assert json.loads(context_rows[0]["sp"])["name"] == "ko-json-a"
    assert json.loads(context_rows[0]["tpl"])["name"] == "ko-json-b"
    assert json.loads(start["payload"])["name"] == "ko-json-a"
    assert json.loads(start["evidence"])[0]["source_id"] == "source-golden"
    assert start["created_at"] == NOW
    assert json.loads(relation_rows[0]["evidence"])[0]["element_id"] == "element-golden"
    assert {json.loads(row["payload"])["name"] for row in object_rows} == {
        "ko-json-a",
        "ko-json-b",
    }


@pytest.mark.postgres_integration
def test_postgres_retrieve_neighbors_consumes_repository_rows(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresKnowledgeStore(postgres_database, _seams())
    objects = [
        (
            object_id,
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": name}),
            json.dumps(_evidence()),
            "source-golden",
            NOW,
            NOW,
        )
        for object_id, name in (
            ("ko-neighbor-source", "source node"),
            ("ko-neighbor-target", "target node"),
        )
    ]
    relation = (
        "rel-neighbor",
        "nb-personal",
        "source-golden",
        "ko-neighbor-source",
        "ko-neighbor-target",
        "derived_from",
        json.dumps(_evidence()),
        NOW,
    )
    with postgres_database.write() as connection:
        store.insert_object_chunk(connection, objects)
        store.insert_relation_chunk(connection, [relation])

    service = CandidateRetrievalService.__new__(CandidateRetrievalService)
    service._connect = postgres_database.connect
    service.knowledge = store
    hits = service._retrieve_neighbors(
        "nb-personal", "ko-neighbor-source", edge_type="derived_from"
    )

    assert [hit.object_id for hit in hits] == ["ko-neighbor-target"]
    assert hits[0].payload == {"name": "target node"}
    assert hits[0].evidence[0].source_id == "source-golden"
    assert hits[0].last_reviewed == ""


def test_retrieve_neighbors_excludes_rejected_relations_on_postgres(
    knowledge_harness,
):
    objects = [
        (
            object_id,
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": object_id}),
            json.dumps(_evidence()),
            "source-golden",
            NOW,
            NOW,
        )
        for object_id in ("ko-rejected-neighbor-source", "ko-rejected-neighbor-target")
    ]
    relation = (
        "rel-rejected-neighbor",
        "nb-personal",
        "source-golden",
        "ko-rejected-neighbor-source",
        "ko-rejected-neighbor-target",
        "derived_from",
        json.dumps(_evidence()),
        NOW,
    )
    with knowledge_harness.database.write() as connection:
        knowledge_harness.knowledge.insert_object_chunk(connection, objects)
        knowledge_harness.knowledge.insert_relation_chunk(connection, [relation])
        knowledge_harness.governance.update_edge_review(
            connection,
            "nb-personal",
            "rel-rejected-neighbor",
            "rejected",
        )

    service = CandidateRetrievalService.__new__(CandidateRetrievalService)
    service._connect = knowledge_harness.database.connect
    service.knowledge = knowledge_harness.knowledge

    assert service._retrieve_neighbors(
        "nb-personal",
        "ko-rejected-neighbor-source",
        edge_type="derived_from",
    ) == []


def test_relation_connected_probe_returns_only_candidate_ids_on_postgres(
    knowledge_harness,
):
    object_ids = ["ko-probe-hub", "ko-probe-isolated"] + [
        f"ko-probe-leaf-{index:03d}" for index in range(128)
    ]
    objects = [
        (
            object_id,
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": object_id}),
            "[]",
            "source-golden",
            NOW,
            NOW,
        )
        for object_id in object_ids
    ]
    relations = [
        (
            f"rel-probe-{index:03d}",
            "nb-personal",
            "source-golden",
            "ko-probe-hub",
            f"ko-probe-leaf-{index:03d}",
            "supports",
            "[]",
            NOW,
        )
        for index in range(128)
    ]
    with knowledge_harness.database.write() as connection:
        knowledge_harness.knowledge.insert_object_chunk(connection, objects)
        knowledge_harness.knowledge.insert_relation_chunk(connection, relations)

    with knowledge_harness.database.connect() as connection:
        rows = knowledge_harness.knowledge.relation_connected_object_ids(
            connection,
            "nb-personal",
            ["ko-probe-hub", "ko-probe-leaf-000", "ko-probe-isolated"],
        )

    assert {row["object_id"] for row in rows} == {
        "ko-probe-hub",
        "ko-probe-leaf-000",
    }
    assert len(rows) == 2


@pytest.mark.postgres_integration
def test_postgres_knowledge_list_and_retrieval_normalize_review_timestamps(
    postgres_database,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresKnowledgeStore(postgres_database, _seams())
    rows = [
        (
            object_id,
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": object_id}),
            json.dumps(_evidence()),
            "source-golden",
            NOW,
            NOW,
        )
        for object_id in ("ko-review-empty", "ko-review-aware")
    ]
    aware = datetime(
        2026, 7, 22, 8, 30, 45, tzinfo=timezone(timedelta(hours=8))
    )
    with postgres_database.write() as connection:
        store.insert_object_chunk(connection, rows)
        connection.execute(
            "UPDATE knowledge_objects SET last_reviewed=%s WHERE id=%s",
            (aware, "ko-review-aware"),
        )
    with postgres_database.connect() as connection:
        retrieved = store.retrieval_objects(
            connection,
            "nb-personal",
            "claim",
            USABLE_STATUSES,
            ["ko-review-empty", "ko-review-aware"],
        )
        total, listed = store.list_knowledge_page(
            connection, "nb-personal", "claim", "approved", 0, 10
        )

    expected = {
        "ko-review-empty": "",
        "ko-review-aware": "2026-07-22T00:30:45+00:00",
    }
    assert {row["id"]: row["last_reviewed"] for row in retrieved} == expected
    assert {row["id"]: row["last_reviewed"] for row in listed} == expected
    assert total == 2
    for row in (*retrieved, *listed):
        assert knowledge_record("claim", row, None).last_reviewed == expected[row["id"]]


@pytest.mark.postgres_integration
def test_postgres_fts_candidate_window_cannot_be_crowded_out_by_deprecated_rows(
    postgres_database,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresKnowledgeStore(postgres_database, _seams())
    query = "crowdout exact thermal phrase"
    rows = [
        (
            f"ko-crowdout-deprecated-{index:02d}",
            "nb-personal",
            "claim",
            "deprecated",
            json.dumps({"name": query}),
            "[]",
            "source-golden",
            NOW,
            NOW,
        )
        for index in range(49)
    ]
    rows.append(
        (
            "ko-crowdout-live-z",
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": query}),
            "[]",
            "source-golden",
            NOW,
            NOW,
        )
    )
    with postgres_database.write() as connection:
        store.insert_object_chunk(connection, rows)

    with postgres_database.connect() as connection:
        hits = store.fts_search(connection, "nb-personal", query, k=12)

    assert [hit["object_id"] for hit in hits] == ["ko-crowdout-live-z"]


@pytest.mark.postgres_integration
def test_postgres_fts_recalls_independent_term_from_long_query(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresKnowledgeStore(postgres_database, _seams())
    with postgres_database.write() as connection:
        store.insert_object_chunk(connection, [(
            "ko-zxcv-controller",
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": "ZXCV9000 timing controller"}),
            "[]",
            "source-golden",
            NOW,
            NOW,
        )])

    query = (
        "please compare thermal behavior around the ZXCV9000 controller "
        "during transient startup"
    )
    with postgres_database.connect() as connection:
        hits = store.fts_search(connection, "nb-personal", query, k=12)

    assert "ko-zxcv-controller" in {hit["object_id"] for hit in hits}


@pytest.mark.postgres_integration
def test_postgres_merge_review_job_start_is_single_flight(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresGovernanceStore(postgres_database, _seams())
    with postgres_database.write() as connection:
        store.insert_merge_candidate(
            connection,
            "nb-personal",
            "canonical-a",
            "canonical-b",
            0.9,
            NOW,
        )

    callers_ready = threading.Barrier(2)
    old_status_reads = threading.Barrier(2)

    class GateOldStatusRead:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, params=None):
            cursor = self.connection.execute(query, params)
            if "SELECT status FROM merge_review_jobs" in str(query):
                old_status_reads.wait(timeout=2)
            return cursor

    def begin():
        with postgres_database.write() as connection:
            callers_ready.wait(timeout=2)
            return store.begin_merge_review_job(
                GateOldStatusRead(connection), "nb-personal", NOW
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=3) for future in (
            executor.submit(begin),
            executor.submit(begin),
        )]

    assert results.count(None) == 1
    assert [result for result in results if result is not None] == [1]
    with postgres_database.connect() as connection:
        row = store.merge_review_job_row(connection, "nb-personal")
    assert dict(row) == {"status": "running", "total": 1, "done": 0, "error": ""}


def test_cluster_append_dedupes_repeated_member_within_one_input(
    knowledge_harness,
):
    with knowledge_harness.database.write() as connection:
        added = knowledge_harness.governance.insert_clusters(
            connection,
            "nb-personal",
            "concept",
            [
                {
                    "canonical_id": "canonical-first-input",
                    "member_object_id": "ko-repeated-input-member",
                    "canonical_name": "first",
                },
                {
                    "canonical_id": "canonical-second-input",
                    "member_object_id": "ko-repeated-input-member",
                    "canonical_name": "second",
                },
            ],
            NOW,
        )

    with knowledge_harness.database.connect() as connection:
        rows = connection.execute(
            "SELECT canonical_id,member_object_id FROM concept_clusters "
            "WHERE notebook_id=%s AND object_type=%s",
            ("nb-personal", "concept"),
        ).fetchall()

    assert added == 1
    assert [dict(row) for row in rows] == [
        {
            "canonical_id": "canonical-first-input",
            "member_object_id": "ko-repeated-input-member",
        }
    ]


@pytest.mark.postgres_integration
def test_postgres_concurrent_cluster_appends_are_member_idempotent(
    postgres_database,
):
    """Two real transactions must not both pass the append membership check."""
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresGovernanceStore(postgres_database, _seams())
    first_reached = threading.Event()
    second_reached = threading.Event()
    release_first = threading.Event()

    class GateClusterAppend:
        def __init__(self, connection, role):
            self.connection = connection
            self.role = role
            self.lock_seen = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query, params=None):
            sql = " ".join(str(query).split())
            if "pg_advisory_xact_lock" in sql:
                if self.role == "second":
                    second_reached.set()
                cursor = self.connection.execute(query, params)
                self.lock_seen = True
                if self.role == "first":
                    first_reached.set()
                    assert release_first.wait(timeout=2)
                return cursor
            cursor = self.connection.execute(query, params)
            if (
                sql.startswith("SELECT member_object_id FROM concept_clusters")
                and not self.lock_seen
            ):
                if self.role == "first":
                    first_reached.set()
                    assert second_reached.wait(timeout=2)
                else:
                    second_reached.set()
            return cursor

    def append(role, canonical_id):
        with postgres_database.write() as connection:
            return store.insert_clusters(
                GateClusterAppend(connection, role),
                "nb-personal",
                "concept",
                [
                    {
                        "canonical_id": canonical_id,
                        "member_object_id": "ko-concurrent-cluster-member",
                        "canonical_name": canonical_id,
                    }
                ],
                NOW,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(append, "first", "canonical-first")
        assert first_reached.wait(timeout=2)
        second = executor.submit(append, "second", "canonical-second")
        assert second_reached.wait(timeout=2)
        release_first.set()
        added = [first.result(timeout=3), second.result(timeout=3)]

    with postgres_database.connect() as connection:
        rows = connection.execute(
            "SELECT canonical_id,member_object_id FROM concept_clusters "
            "WHERE notebook_id=%s AND object_type=%s ORDER BY canonical_id",
            ("nb-personal", "concept"),
        ).fetchall()

    assert added == [1, 0]
    assert rows == [
        {
            "canonical_id": "canonical-first",
            "member_object_id": "ko-concurrent-cluster-member",
        }
    ]


@pytest.mark.postgres_integration
def test_postgres_concurrent_cluster_replacements_publish_one_complete_final_set(
    postgres_database,
):
    """A later replacement must erase, not mix with, the prior complete set."""
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresUnifiedKgStore(postgres_database, now=lambda: NOW)
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
            "canonical_description,canonical_desc_sig,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'','',%s)",
            (
                "cc-replace-initial",
                "nb-personal",
                "canonical-initial",
                "ko-replace-initial",
                "initial",
                "concept",
                NOW,
            ),
        )

    first_deleted = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()

    class GateClusterReplace:
        def __init__(self, connection, role):
            self.connection = connection
            self.role = role

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query, params=None):
            sql = " ".join(str(query).split())
            if self.role == "second" and "pg_advisory_xact_lock" in sql:
                second_started.set()
            if self.role == "second" and sql.startswith(
                "DELETE FROM concept_clusters"
            ):
                second_started.set()
            cursor = self.connection.execute(query, params)
            if self.role == "first" and sql.startswith(
                "DELETE FROM concept_clusters"
            ):
                first_deleted.set()
                assert release_first.wait(timeout=2)
            return cursor

    def replacement_rows(label):
        return [
            (
                f"cc-replace-{label}-{index}",
                "nb-personal",
                f"canonical-{label}-{index}",
                f"ko-replace-member-{index}",
                f"{label}-{index}",
                "concept",
                "",
                "",
                NOW,
            )
            for index in range(2)
        ]

    def replace(role, label):
        with postgres_database.write() as connection:
            store.replace_cluster_rows_streamed(
                GateClusterReplace(connection, role),
                "nb-personal",
                "concept",
                iter(replacement_rows(label)),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(replace, "first", "first")
        assert first_deleted.wait(timeout=2)
        second = executor.submit(replace, "second", "second")
        assert second_started.wait(timeout=2)
        release_first.set()
        first.result(timeout=3)
        second.result(timeout=3)

    with postgres_database.connect() as connection:
        rows = connection.execute(
            "SELECT canonical_id,member_object_id FROM concept_clusters "
            "WHERE notebook_id=%s AND object_type=%s "
            "ORDER BY member_object_id,canonical_id",
            ("nb-personal", "concept"),
        ).fetchall()

    assert rows == [
        {
            "canonical_id": "canonical-second-0",
            "member_object_id": "ko-replace-member-0",
        },
        {
            "canonical_id": "canonical-second-1",
            "member_object_id": "ko-replace-member-1",
        },
    ]
    assert len({row["member_object_id"] for row in rows}) == len(rows)


def _race_evidence(element_id: str) -> list[dict]:
    evidence = dict(_evidence()[0])
    evidence["element_id"] = element_id
    evidence["quoted_span"] = f"evidence {element_id}"
    return [evidence]


@pytest.mark.postgres_integration
@pytest.mark.parametrize("promotion_kind", ("knowledge", "memory"))
def test_postgres_promotion_dedup_does_not_overwrite_concurrent_merge_evidence(
    postgres_database,
    promotion_kind,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresGovernanceStore(postgres_database, _seams())
    knowledge = PostgresKnowledgeStore(postgres_database, _seams())
    promoted_element = f"promotion-{promotion_kind}"
    objects = [
        (
            "ko-promotion-race-target",
            "nb-base",
            "claim",
            "approved",
            json.dumps({"name": "promotion evidence race"}),
            json.dumps(_race_evidence("base-existing")),
            "",
            NOW,
            NOW,
        ),
        (
            "ko-promotion-race-merge-source",
            "nb-base",
            "claim",
            "approved",
            json.dumps({"name": "merge-only source"}),
            json.dumps(_race_evidence("concurrent-merge")),
            "",
            NOW,
            NOW,
        ),
    ]
    if promotion_kind == "knowledge":
        objects.append(
            (
                "ko-promotion-race-personal",
                "nb-personal",
                "claim",
                "approved",
                json.dumps({"name": "promotion evidence race"}),
                json.dumps(_race_evidence(promoted_element)),
                "source-golden",
                NOW,
                NOW,
            )
        )
    candidate_id = f"promotion-evidence-race-{promotion_kind}"
    candidate_object_id = (
        "ko-promotion-race-personal"
        if promotion_kind == "knowledge"
        else "memory-promotion-race"
    )
    with postgres_database.write() as connection:
        knowledge.insert_object_chunk(connection, objects)
        store.insert_promotion_candidate(
            connection,
            candidate_id,
            "nb-personal",
            candidate_object_id,
            "claim",
            NOW,
            "nb-base",
        )

    base_rows_read = threading.Event()
    merge_lock_attempted = threading.Event()
    merge_committed = threading.Event()
    release_promotion = threading.Event()

    class GateFetchall:
        def __init__(self, cursor, locks_rows):
            self.cursor = cursor
            self.locks_rows = locks_rows

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def fetchall(self):
            rows = self.cursor.fetchall()
            base_rows_read.set()
            if self.locks_rows:
                assert release_promotion.wait(timeout=2)
            else:
                assert merge_committed.wait(timeout=2)
            return rows

    class GatePromotionBaseRead:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query, params=None):
            sql = " ".join(str(query).split())
            cursor = self.connection.execute(query, params)
            if (
                "FROM knowledge_objects WHERE notebook_id=%s" in sql
                and "object_type=%s" in sql
                and "status IN" in sql
                and "evidence" in sql
            ):
                return GateFetchall(cursor, "FOR UPDATE" in sql)
            return cursor

    class ObserveMergeLock:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query, params=None):
            sql = " ".join(str(query).split())
            if (
                sql.startswith("SELECT * FROM knowledge_objects WHERE notebook_id = %s")
                and "FOR UPDATE" in sql
            ):
                merge_lock_attempted.set()
            return self.connection.execute(query, params)

    def approve():
        with postgres_database.write() as connection:
            gated = GatePromotionBaseRead(connection)
            if promotion_kind == "memory":
                return store.approve_memory_promotion_in_transaction(
                    gated,
                    candidate_id,
                    [
                        {
                            "object_type": "claim",
                            "payload": {"name": "promotion evidence race"},
                        }
                    ],
                    _race_evidence(promoted_element),
                    "curator",
                    NOW,
                )
            return store.approve_promotion_in_transaction(
                gated,
                candidate_id,
                NOW,
                "curator",
            )

    def merge():
        with postgres_database.write() as connection:
            result = store.merge_objects_in_transaction(
                ObserveMergeLock(connection),
                "nb-base",
                "ko-promotion-race-merge-source",
                "ko-promotion-race-target",
                NOW,
            )
        merge_committed.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        promotion_future = executor.submit(approve)
        assert base_rows_read.wait(timeout=2)
        merge_future = executor.submit(merge)
        assert merge_lock_attempted.wait(timeout=2)
        release_promotion.set()
        promotion_future.result(timeout=3)
        merge_future.result(timeout=3)

    with postgres_database.connect() as connection:
        evidence = connection.execute(
            "SELECT evidence FROM knowledge_objects WHERE id=%s",
            ("ko-promotion-race-target",),
        ).fetchone()["evidence"]
    assert {item["element_id"] for item in evidence} == {
        "base-existing",
        promoted_element,
        "concurrent-merge",
    }


@pytest.mark.postgres_integration
def test_postgres_concurrent_merges_preserve_all_target_evidence(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresGovernanceStore(postgres_database, _seams())
    knowledge = PostgresKnowledgeStore(postgres_database, _seams())
    rows = [
        (
            object_id,
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": object_id}),
            json.dumps(_race_evidence(element_id)),
            "source-golden",
            NOW,
            NOW,
        )
        for object_id, element_id in (
            ("ko-merge-target", "element-target"),
            ("ko-merge-source-a", "element-source-a"),
            ("ko-merge-source-b", "element-source-b"),
        )
    ]
    with postgres_database.write() as connection:
        knowledge.insert_object_chunk(connection, rows)

    callers_ready = threading.Barrier(2)
    old_target_reads = threading.Barrier(2)

    class GateOldTargetRead:
        def __init__(self, connection):
            self.connection = connection
            self.gated = False
            self.locking_read_seen = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query, params=None):
            sql = " ".join(str(query).split())
            if "FROM knowledge_objects" in sql and "FOR UPDATE" in sql:
                self.locking_read_seen = True
            cursor = self.connection.execute(query, params)
            if (
                sql.startswith("SELECT * FROM knowledge_objects WHERE id = %s")
                and params
                and params[0] == "ko-merge-target"
                and "FOR UPDATE" not in sql
                and not self.gated
                and not self.locking_read_seen
            ):
                self.gated = True
                old_target_reads.wait(timeout=2)
            return cursor

    def merge(source_id: str):
        with postgres_database.write() as connection:
            callers_ready.wait(timeout=2)
            return store.merge_objects_in_transaction(
                GateOldTargetRead(connection),
                "nb-personal",
                source_id,
                "ko-merge-target",
                NOW,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=3) for future in (
            executor.submit(merge, "ko-merge-source-a"),
            executor.submit(merge, "ko-merge-source-b"),
        )]

    assert {row["id"] for row in results} == {"ko-merge-target"}
    with postgres_database.connect() as connection:
        target = connection.execute(
            "SELECT evidence FROM knowledge_objects WHERE id=%s",
            ("ko-merge-target",),
        ).fetchone()
        sources = connection.execute(
            "SELECT id,status FROM knowledge_objects WHERE id=ANY(%s) ORDER BY id",
            (["ko-merge-source-a", "ko-merge-source-b"],),
        ).fetchall()
    assert {item["element_id"] for item in target["evidence"]} == {
        "element-target",
        "element-source-a",
        "element-source-b",
    }
    assert [row["status"] for row in sources] == ["deprecated", "deprecated"]


@pytest.mark.postgres_integration
def test_postgres_concurrent_partial_updates_do_not_lose_fields(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresGovernanceStore(postgres_database, _seams())
    knowledge = PostgresKnowledgeStore(postgres_database, _seams())
    row = (
        "ko-update-race",
        "nb-personal",
        "claim",
        "approved",
        json.dumps({"name": "before"}),
        json.dumps(_evidence()),
        "source-golden",
        NOW,
        NOW,
    )
    with postgres_database.write() as connection:
        knowledge.insert_object_chunk(connection, [row])

    callers_ready = threading.Barrier(2)
    old_reads = threading.Barrier(2)

    class GateOldUnlockedRead:
        def __init__(self, connection):
            self.connection = connection
            self.gated = False
            self.locking_read_seen = False

        def execute(self, query, params=None):
            sql = " ".join(str(query).split())
            if "FROM knowledge_objects" in sql and "FOR UPDATE" in sql:
                self.locking_read_seen = True
            cursor = self.connection.execute(query, params)
            if (
                sql.startswith("SELECT * FROM knowledge_objects WHERE id = %s")
                and params
                and params[0] == "ko-update-race"
                and "FOR UPDATE" not in sql
                and not self.gated
                and not self.locking_read_seen
            ):
                self.gated = True
                old_reads.wait(timeout=2)
            return cursor

    def update(payload: KnowledgeUpdate):
        with postgres_database.write() as connection:
            callers_ready.wait(timeout=2)
            return store.update_object_in_transaction(
                GateOldUnlockedRead(connection),
                "nb-personal",
                "ko-update-race",
                payload,
                NOW,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        [future.result(timeout=3) for future in (
            executor.submit(update, KnowledgeUpdate(payload={"name": "after"})),
            executor.submit(update, KnowledgeUpdate(owner="owner-after")),
        )]

    with postgres_database.connect() as connection:
        final = connection.execute(
            "SELECT payload,owner FROM knowledge_objects WHERE id=%s",
            ("ko-update-race",),
        ).fetchone()
    assert final["payload"] == {"name": "after"}
    assert final["owner"] == "owner-after"


@pytest.mark.postgres_integration
def test_postgres_concurrent_promotion_proposals_are_idempotent(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresGovernanceStore(postgres_database, _seams())
    knowledge = PostgresKnowledgeStore(postgres_database, _seams())
    row = (
        "ko-proposal-race",
        "nb-personal",
        "claim",
        "approved",
        json.dumps({"name": "proposal race"}),
        json.dumps(_evidence()),
        "source-golden",
        NOW,
        NOW,
    )
    with postgres_database.write() as connection:
        knowledge.insert_object_chunk(connection, [row])

    callers_ready = threading.Barrier(2)
    old_active_reads = threading.Barrier(2)

    class GateOldActiveRead:
        def __init__(self, connection):
            self.connection = connection
            self.advisory_lock_seen = False

        def execute(self, query, params=None):
            sql = " ".join(str(query).split())
            if "pg_advisory_xact_lock" in sql:
                self.advisory_lock_seen = True
            cursor = self.connection.execute(query, params)
            if (
                "FROM promotion_candidates WHERE object_id=%s" in sql
                and not self.advisory_lock_seen
            ):
                old_active_reads.wait(timeout=2)
            return cursor

    def propose(suffix: str):
        with postgres_database.write() as connection:
            callers_ready.wait(timeout=2)
            gated = GateOldActiveRead(connection)
            obj = store.promotion_object_type_row(
                gated, "nb-personal", "ko-proposal-race"
            )
            assert obj is not None
            existing = store.active_promotion_for_object(gated, "ko-proposal-race")
            if existing is not None:
                return existing["id"]
            candidate_id = f"promotion-proposal-{suffix}"
            store.insert_promotion_candidate(
                gated,
                candidate_id,
                "nb-personal",
                "ko-proposal-race",
                obj["object_type"],
                NOW,
                "nb-base",
            )
            return candidate_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=3) for future in (
            executor.submit(propose, "a"),
            executor.submit(propose, "b"),
        )]

    assert results[0] == results[1]
    with postgres_database.connect() as connection:
        rows = connection.execute(
            "SELECT id FROM promotion_candidates WHERE object_id=%s",
            ("ko-proposal-race",),
        ).fetchall()
    assert [row["id"] for row in rows] == [results[0]]


@pytest.mark.postgres_integration
def test_postgres_reject_waiting_behind_approve_cannot_overwrite(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresGovernanceStore(postgres_database, _seams())
    knowledge = PostgresKnowledgeStore(postgres_database, _seams())
    source = (
        "ko-reject-race",
        "nb-personal",
        "claim",
        "approved",
        json.dumps({"name": "reject race"}),
        json.dumps(_evidence()),
        "source-golden",
        NOW,
        NOW,
    )
    with postgres_database.write() as connection:
        knowledge.insert_object_chunk(connection, [source])
        store.insert_promotion_candidate(
            connection,
            "promotion-reject-race",
            "nb-personal",
            "ko-reject-race",
            "claim",
            NOW,
            "nb-base",
        )

    approval_locked = threading.Event()
    release_approval = threading.Event()
    reject_query_started = threading.Event()
    reject_unlocked_read = threading.Event()

    class GateApprovalAfterCandidateLock:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query, params=None):
            cursor = self.connection.execute(query, params)
            sql = " ".join(str(query).split())
            if (
                sql.startswith("SELECT * FROM promotion_candidates WHERE id=%s FOR UPDATE")
                and params
                and params[0] == "promotion-reject-race"
            ):
                approval_locked.set()
                assert release_approval.wait(timeout=2)
            return cursor

    class ObserveRejectCandidateRead:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, params=None):
            sql = " ".join(str(query).split())
            if sql.startswith("SELECT * FROM promotion_candidates WHERE id=%s"):
                reject_query_started.set()
            cursor = self.connection.execute(query, params)
            if (
                sql.startswith("SELECT * FROM promotion_candidates WHERE id=%s")
                and "FOR UPDATE" not in sql
            ):
                reject_unlocked_read.set()
            return cursor

    def approve():
        with postgres_database.write() as connection:
            return store.approve_promotion_in_transaction(
                GateApprovalAfterCandidateLock(connection),
                "promotion-reject-race",
                NOW,
                "curator-approve",
            )

    def reject():
        with postgres_database.write() as connection:
            observed = ObserveRejectCandidateRead(connection)
            candidate = store.promotion_candidate_row(
                observed, "promotion-reject-race"
            )
            if candidate["status"] == "approved":
                raise ValueError("cannot reject an approved promotion candidate")
            store.set_promotion_rejected(
                observed,
                "promotion-reject-race",
                "reject",
                NOW,
                "curator-reject",
            )
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        approval_future = executor.submit(approve)
        assert approval_locked.wait(timeout=2)
        reject_future = executor.submit(reject)
        assert reject_query_started.wait(timeout=2)
        reject_unlocked_read.wait(timeout=0.2)
        release_approval.set()
        approval = approval_future.result(timeout=3)
        with pytest.raises(ValueError, match="cannot reject an approved"):
            reject_future.result(timeout=3)

    assert approval.created_new_object is True
    with postgres_database.connect() as connection:
        candidate = connection.execute(
            "SELECT status,reviewed_by FROM promotion_candidates WHERE id=%s",
            ("promotion-reject-race",),
        ).fetchone()
        base_rows = connection.execute(
            "SELECT id FROM knowledge_objects WHERE source_candidate_id=%s",
            ("promotion-reject-race",),
        ).fetchall()
    assert candidate == {"status": "approved", "reviewed_by": "curator-approve"}
    assert [row["id"] for row in base_rows] == [approval.base_object_id]


@pytest.mark.postgres_integration
def test_postgres_graph_build_order_and_equal_confidence_fanout_are_physical_order_independent(
    postgres_database,
    postgres_settings,
):
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    knowledge = PostgresKnowledgeStore(postgres_database, _seams())
    unified = PostgresUnifiedKgStore(postgres_database, now=lambda: NOW)
    target_ids = [f"ko-fanout-target-{index:02d}" for index in reversed(range(10))]
    object_ids = ["ko-fanout-source", *target_ids]
    objects = [
        (
            object_id,
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": object_id}),
            "[]",
            "source-golden",
            NOW,
            NOW,
        )
        for object_id in object_ids
    ]
    relations = [
        (
            f"rel-fanout-{index:02d}",
            "nb-personal",
            "source-golden",
            "ko-fanout-source",
            f"ko-fanout-target-{index:02d}",
            "derived_from",
            "[]",
            NOW,
        )
        for index in reversed(range(10))
    ]
    cluster_rows = [
        (
            "cc-order-z",
            "nb-personal",
            "canonical-b",
            "ko-fanout-target-00",
            "b",
            "concept",
            "",
            "",
            NOW,
        ),
        (
            "cc-order-a2",
            "nb-personal",
            "canonical-a",
            "ko-fanout-target-02",
            "a",
            "concept",
            "",
            "",
            NOW,
        ),
        (
            "cc-order-a1",
            "nb-personal",
            "canonical-a",
            "ko-fanout-target-01",
            "a",
            "concept",
            "",
            "",
            NOW,
        ),
    ]
    with postgres_database.write() as connection:
        knowledge.insert_object_chunk(connection, objects)
        knowledge.insert_relation_chunk(connection, relations)
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
                "canonical_description,canonical_desc_sig,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                cluster_rows,
            )

    projection = PostgresIndexProjectionStore(
        postgres_settings,
        connect=postgres_database.connect,
        in_batches=lambda values: [list(values)],
        ent_chunk_map=lambda _notebook_id: {},
        mention_extra_edges=lambda _notebook_id: [],
        vector_matrix=lambda *_args, **_kwargs: ([], []),
    )

    def snapshot():
        with postgres_database.connect() as connection:
            object_rows = knowledge.graph_object_rows(
                connection, "nb-personal", USABLE_STATUSES
            )
            relation_rows = knowledge.graph_relation_rows(
                connection, "nb-personal"
            )
            members = unified.cluster_member_rows(connection, "nb-personal")
        nodes = {
            row["id"]: {
                "type": row["object_type"],
                "name": json.loads(row["payload"])["name"],
            }
            for row in object_rows
        }
        graph, idx_to_oid, oid_to_idx = build_rx_graph(nodes, relation_rows)
        traversal = multihop_subgraph(
            graph,
            oid_to_idx,
            idx_to_oid,
            ["ko-fanout-source"],
            edge_types=frozenset({"derived_from"}),
            max_depth=1,
            max_fan_out=3,
        )
        artifact = projection.graph_rows("nb-personal", None, synonym_edges=[])
        return {
            "object_ids": [row["id"] for row in object_rows],
            "relation_ids": [row["id"] for row in relation_rows],
            "members": [dict(row) for row in members],
            "fanout": [node["object_id"] for node, edge, _source in traversal if edge],
            "artifact_kg_ids": artifact.kg_node_ids,
            "artifact_hubs": [node for node in artifact.node_ids if node.startswith("cluster:")],
        }

    before = snapshot()
    with postgres_database.write() as connection:
        connection.execute("CLUSTER knowledge_objects USING pk_knowledge_objects")
        connection.execute("CLUSTER knowledge_relations USING pk_knowledge_relations")
        connection.execute("CLUSTER concept_clusters USING pk_concept_clusters")
    after = snapshot()

    expected = {
        "object_ids": object_ids,
        "relation_ids": [f"rel-fanout-{index:02d}" for index in range(10)],
        "members": [
            {
                "canonical_id": "canonical-a",
                "member_object_id": "ko-fanout-target-01",
            },
            {
                "canonical_id": "canonical-a",
                "member_object_id": "ko-fanout-target-02",
            },
            {
                "canonical_id": "canonical-b",
                "member_object_id": "ko-fanout-target-00",
            },
        ],
        "fanout": [f"ko-fanout-target-{index:02d}" for index in range(3)],
        "artifact_kg_ids": object_ids,
        "artifact_hubs": ["cluster:canonical-a", "cluster:canonical-b"],
    }
    assert before == expected
    assert after == expected


@pytest.mark.postgres_integration
def test_postgres_graph_rows_follow_persisted_ordinals_for_degree_ties(
    postgres_database,
    postgres_settings,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    knowledge = PostgresKnowledgeStore(postgres_database, _seams())
    object_ids = ["ko-order-z", "ko-order-a", "ko-order-m"]
    rows = [
        (
            object_id,
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": object_id}),
            "[]",
            "source-golden",
            NOW,
            NOW,
        )
        for object_id in object_ids
    ]
    chunk_ids = ["chunk-order-z", "chunk-order-a"]
    with postgres_database.write() as connection:
        knowledge.insert_object_chunk(connection, rows)
        for chunk_id in chunk_ids:
            connection.execute(
                "INSERT INTO chunks(id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (%s,%s,%s,%s,'','[]'::jsonb,%s)",
                (chunk_id, "nb-personal", "source-golden", chunk_id, NOW),
            )
        # Make heap order deliberately disagree with insertion ordinals. All KG
        # nodes have degree zero, so a stable degree tie must retain ordinal order.
        connection.execute("CLUSTER knowledge_objects USING pk_knowledge_objects")
        connection.execute("CLUSTER chunks USING pk_chunks")

    projection = PostgresIndexProjectionStore(
        postgres_settings,
        connect=postgres_database.connect,
        in_batches=lambda values: [list(values)],
        ent_chunk_map=lambda _notebook_id: {},
        mention_extra_edges=lambda _notebook_id: [],
        vector_matrix=lambda *_args, **_kwargs: ([], []),
    )
    with postgres_database.connect() as connection:
        unified = knowledge.unified_graph_rows(connection, "nb-personal")
        active = projection.active_object_graph_rows(connection, "nb-personal")
    graph = projection.graph_rows("nb-personal", None, synonym_edges=[])

    assert [row["id"] for row in unified] == object_ids
    assert [row["id"] for row in active] == object_ids
    assert graph.kg_node_ids == object_ids
    assert graph.chunk_ids == chunk_ids
    assert graph.node_ids == object_ids + chunk_ids


class _OrderedMembershipSet(set):
    """A real set with a controlled iterator for hash/insertion-order tests."""

    def __init__(self, values):
        super().__init__(values)
        self._iteration_order = tuple(values)

    def __iter__(self):
        return iter(self._iteration_order)


class _ProjectionCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _ProjectionConnection:
    def execute(self, statement, _params):
        statement = str(statement)
        if "FROM knowledge_objects" in statement:
            return _ProjectionCursor(
                [
                    {
                        "id": "object-a",
                        "object_type": "concept",
                        "payload": json.dumps({"name": "A"}),
                    },
                    {
                        "id": "object-b",
                        "object_type": "concept",
                        "payload": json.dumps({"name": "B"}),
                    },
                ]
            )
        if "FROM knowledge_relations" in statement:
            return _ProjectionCursor([])
        if "FROM chunks" in statement:
            return _ProjectionCursor([{"id": "chunk-a"}, {"id": "chunk-z"}])
        if "FROM concept_clusters" in statement:
            return _ProjectionCursor([])
        raise AssertionError(statement)


def test_projection_membership_artifact_order_ignores_map_and_set_iteration():
    connection = _ProjectionConnection()

    def build(ent_chunk_map):
        projection = PostgresIndexProjectionStore(
            SimpleNamespace(ppr_variant_edge_weight=0.35),
            connect=lambda: nullcontext(connection),
            in_batches=lambda values: [list(values)],
            ent_chunk_map=lambda _notebook_id: ent_chunk_map,
            mention_extra_edges=lambda _notebook_id: [],
            vector_matrix=lambda *_args, **_kwargs: ([], []),
        )
        strings = projection.graph_rows(
            "nb-personal", None, synonym_edges=[]
        )
        arrays = projection.graph_rows(
            "nb-personal", None, synonym_edges=[], as_arrays=True
        )
        return strings, arrays

    reverse, forward = (
        {
            "object-b": _OrderedMembershipSet(("chunk-z", "chunk-a")),
            "object-a": _OrderedMembershipSet(("chunk-z", "chunk-a")),
        },
        {
            "object-a": _OrderedMembershipSet(("chunk-a", "chunk-z")),
            "object-b": _OrderedMembershipSet(("chunk-a", "chunk-z")),
        },
    )
    reverse_strings, reverse_arrays = build(reverse)
    forward_strings, forward_arrays = build(forward)

    expected_edges = [
        ("object-a", "chunk-a", 1.0),
        ("chunk-a", "object-a", 1.0),
        ("object-a", "chunk-z", 1.0),
        ("chunk-z", "object-a", 1.0),
        ("object-b", "chunk-a", 1.0),
        ("chunk-a", "object-b", 1.0),
        ("object-b", "chunk-z", 1.0),
        ("chunk-z", "object-b", 1.0),
    ]
    assert reverse_strings.edges == forward_strings.edges == expected_edges
    assert reverse_strings.node_ids == forward_strings.node_ids == [
        "object-a",
        "object-b",
        "chunk-a",
        "chunk-z",
    ]
    assert reverse_strings.chunk_ids == forward_strings.chunk_ids == [
        "chunk-a",
        "chunk-z",
    ]
    assert reverse_strings.kg_node_ids == forward_strings.kg_node_ids == [
        "object-a",
        "object-b",
    ]
    assert reverse_strings.membership_counts == forward_strings.membership_counts
    assert list(reverse_strings.membership_counts) == ["object-a", "object-b"]

    reverse_src, reverse_tgt, reverse_weight = reverse_arrays.edges
    forward_src, forward_tgt, forward_weight = forward_arrays.edges
    np.testing.assert_array_equal(reverse_src, forward_src)
    np.testing.assert_array_equal(reverse_tgt, forward_tgt)
    np.testing.assert_array_equal(reverse_weight, forward_weight)
    assert reverse_arrays.node_ids == forward_arrays.node_ids
    assert reverse_arrays.chunk_ids == forward_arrays.chunk_ids
    assert reverse_arrays.kg_node_ids == forward_arrays.kg_node_ids
    assert reverse_arrays.membership_counts == forward_arrays.membership_counts
    assert reverse_src.tolist() == [0, 0, 1, 1, 2, 3, 2, 3]
    assert reverse_tgt.tolist() == [2, 3, 2, 3, 0, 0, 1, 1]


@pytest.mark.postgres_integration
def test_postgres_follow_endpoint_limit_is_stable_and_prioritizes_live_edges(
    postgres_database,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    knowledge = PostgresKnowledgeStore(postgres_database, _seams())
    object_ids = ["ko-follow-start", "ko-follow-a", "ko-follow-m", "ko-follow-z"]
    objects = [
        (
            object_id,
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": object_id}),
            "[]",
            "source-golden",
            NOW,
            NOW,
        )
        for object_id in object_ids
    ]
    relations = [
        (
            relation_id,
            "nb-personal",
            "source-golden",
            "ko-follow-start",
            target_id,
            "derived_from",
            "[]",
            NOW,
        )
        for relation_id, target_id in (
            ("rel-a-rejected", "ko-follow-a"),
            ("rel-z-live", "ko-follow-z"),
            ("rel-m-live", "ko-follow-m"),
        )
    ]
    with postgres_database.write() as connection:
        knowledge.insert_object_chunk(connection, objects)
        knowledge.insert_relation_chunk(connection, relations)
        connection.execute(
            "UPDATE knowledge_relations SET review_status='rejected' WHERE id=%s",
            ("rel-a-rejected",),
        )
        connection.execute(
            "UPDATE knowledge_relations SET review_status='verified' "
            "WHERE id=ANY(%s)",
            (["rel-z-live", "rel-m-live"],),
        )

    calls = []
    with postgres_database.connect() as connection:
        for _ in range(3):
            calls.append([
                row["id"]
                for row in knowledge.follow_endpoint_rows(
                    connection,
                    "nb-personal",
                    "ko-follow-start",
                    "source_object_id",
                    2,
                )
            ])
    assert calls == [["rel-m-live", "rel-z-live"]] * 3


@pytest.mark.postgres_integration
def test_postgres_query_store_multi_notebook_count_placeholders(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    rows = PostgresQueryStore(postgres_database).list_user_notebooks("user-golden")
    assert {row["id"] for row in rows} == {"nb-base", "nb-personal"}
    assert {row["id"]: row["sources"] for row in rows} == {
        "nb-base": 0,
        "nb-personal": 1,
    }


@pytest.mark.postgres_integration
def test_postgres_notebook_analytics_dedupes_low_rated_questions_by_latest_feedback(
    postgres_database,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    with postgres_database.write() as connection:
        for answer_id, question, created_at in (
            ("answer-repeat-old", "repeat question", "2026-07-20T00:00:00+00:00"),
            ("answer-other", "other question", "2026-07-21T00:00:00+00:00"),
            ("answer-repeat-new", "repeat question", "2026-07-22T00:00:00+00:00"),
        ):
            connection.execute(
                "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
                "VALUES (%s,%s,%s,'{}'::jsonb,%s)",
                (answer_id, "nb-personal", question, created_at),
            )
        for feedback_id, answer_id, created_at in (
            ("feedback-repeat-old", "answer-repeat-old", "2026-07-20T01:00:00+00:00"),
            ("feedback-other", "answer-other", "2026-07-21T01:00:00+00:00"),
            ("feedback-repeat-new", "answer-repeat-new", "2026-07-22T01:00:00+00:00"),
        ):
            connection.execute(
                "INSERT INTO feedback(id,answer_id,notebook_id,rating,comment,created_at) "
                "VALUES (%s,%s,%s,'not_useful','',%s)",
                (feedback_id, answer_id, "nb-personal", created_at),
            )

    analytics = PostgresQueryStore(postgres_database).notebook_analytics("nb-personal")

    assert analytics.answers_total == 3
    assert analytics.feedback_not_useful == 3
    assert analytics.low_rated_questions == ["repeat question", "other question"]


@pytest.mark.postgres_integration
def test_postgres_unified_kg_temp_search_and_checkpoint_json(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresUnifiedKgStore(postgres_database, now=lambda: NOW)
    claims = (
        ("claim-en", "thermal design method"),
        ("claim-zh", "热设计 方法"),
        ("claim-other", "signal integrity"),
    )
    with store.mention_alias_candidate_batches(
        claims, ("thermal", "热设计")
    ) as batches:
        matches = {alias: list(rows) for alias, rows in batches}
    assert matches == {
        "thermal": [("claim-en", "thermal design method")],
        "热设计": [("claim-zh", "热设计 方法")],
    }
    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT to_regclass('pg_temp.mention_scan_claims') AS relation"
        ).fetchone()["relation"] is None

    store.checkpoint_put(
        "nb-personal",
        "input-v1",
        "cluster",
        [("item-1", {"optional": None, "names": ["A", "B"]})],
        NOW,
    )
    assert store.checkpoint_load("nb-personal", "input-v1", "cluster") == {
        "item-1": {"optional": None, "names": ["A", "B"]}
    }


@pytest.mark.postgres_integration
def test_concurrent_equivalent_promotions_serialize_base_dedup(
    postgres_database,
    monkeypatch,
):
    from app.repositories.postgres import governance_store as governance_module
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 13
    _seed_catalog(postgres_database)
    store = PostgresGovernanceStore(postgres_database, _seams())
    knowledge = PostgresKnowledgeStore(postgres_database, _seams())
    source_objects = [
        (
            f"ko-promote-source-{suffix}",
            "nb-personal",
            "claim",
            "approved",
            json.dumps({"name": "promotion atomicity"}),
            json.dumps(_evidence()),
            "source-golden",
            NOW,
            NOW,
        )
        for suffix in ("a", "b")
    ]
    with postgres_database.write() as connection:
        knowledge.insert_object_chunk(connection, source_objects)
        for suffix in ("a", "b"):
            store.insert_promotion_candidate(
                connection,
                f"promotion-atomic-{suffix}",
                "nb-personal",
                f"ko-promote-source-{suffix}",
                "claim",
                NOW,
                "nb-base",
            )

    original_require = governance_module.require_live_promotion_target
    first_has_lock = threading.Event()
    release_first = threading.Event()
    gate_lock = threading.Lock()
    first = True

    def gated_require(connection, notebook_id):
        nonlocal first
        with gate_lock:
            should_wait = first
            first = False
        result = original_require(connection, notebook_id)
        if should_wait:
            first_has_lock.set()
            assert release_first.wait(timeout=2)
        return result

    monkeypatch.setattr(governance_module, "require_live_promotion_target", gated_require)
    second_started = threading.Event()

    def approve(candidate_id: str, second: bool):
        with postgres_database.write() as connection:
            if second:
                second_started.set()
            return store.approve_promotion_in_transaction(
                connection, candidate_id, NOW, "curator"
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(approve, "promotion-atomic-a", False)
        assert first_has_lock.wait(timeout=2)
        future_b = executor.submit(approve, "promotion-atomic-b", True)
        assert second_started.wait(timeout=2)
        release_first.set()
        results = (future_a.result(timeout=3), future_b.result(timeout=3))

    assert results[0].base_object_id == results[1].base_object_id
    assert sorted(result.created_new_object for result in results) == [False, True]
    with postgres_database.connect() as connection:
        candidates = connection.execute(
            "SELECT id,status FROM promotion_candidates WHERE id=ANY(%s) ORDER BY id",
            (["promotion-atomic-a", "promotion-atomic-b"],),
        ).fetchall()
        rows = connection.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=%s AND "
            "(payload ->> 'name') COLLATE \"C\"=%s",
            ("nb-base", "promotion atomicity"),
        ).fetchall()
    assert [row["status"] for row in candidates] == ["approved", "approved"]
    assert [row["id"] for row in rows] == [results[0].base_object_id]


def _knowledge_hit(object_id: str, score: float) -> RetrievedKnowledge:
    return RetrievedKnowledge(object_id=object_id, object_type="claim", payload={}, score=score)


def _relation_hit(relation_id: str, score: float) -> RetrievedRelation:
    return RetrievedRelation(
        relation_id=relation_id,
        source_object_id="a",
        target_object_id="b",
        edge_type="about",
        score=score,
    )


def test_federation_base_tie_break_is_knowledge_only(monkeypatch):
    service = CandidateRetrievalService.__new__(CandidateRetrievalService)
    service._connect = lambda: nullcontext(SimpleNamespace())
    service.notebooks = SimpleNamespace(
        participant_tiers=lambda _db, _active: (
            ["nb-personal", "nb-base"],
            {"nb-personal": "personal", "nb-base": "base"},
        )
    )
    monkeypatch.setattr(
        service,
        "_retrieve_scored",
        lambda notebook_id, *_args, **_kwargs: [
            _knowledge_hit(f"knowledge-{notebook_id}", 0.75)
        ],
    )
    knowledge_hits = service._federated_retrieve_impl("nb-personal", "thermal")
    assert [hit.tier for hit in knowledge_hits] == ["base", "personal"]

    monkeypatch.setattr(
        service,
        "_retrieve_relations_scored",
        lambda notebook_id, *_args, **_kwargs: [
            _relation_hit(f"relation-{notebook_id}", 0.75)
        ],
    )
    relation_hits = service._federated_retrieve_relations_impl(
        "nb-personal", "thermal"
    )
    assert [hit.tier for hit in relation_hits] == ["personal", "base"]
