from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.models.knowledge import KnowledgeUpdate
from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.embedding_store import EmbeddingStore as PostgresEmbeddingStore
from app.repositories.postgres.governance_store import GovernanceStore as PostgresGovernanceStore
from app.repositories.postgres.index_projection_store import (
    IndexProjectionStore as PostgresIndexProjectionStore,
)
from app.repositories.postgres.knowledge_store import KnowledgeStore as PostgresKnowledgeStore
from app.repositories.postgres.query_store import QueryStore as PostgresQueryStore
from app.repositories.postgres.unified_kg_store import UnifiedKgStore as PostgresUnifiedKgStore
from app.services.kg_analysis_precompute import (
    ARTIFACT_CLUSTER_HISTOGRAM,
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_KINDS,
    ARTIFACT_LARGEST_CLUSTERS,
    ARTIFACT_RELATION_PROVENANCE,
    ARTIFACT_SOURCE_PROFILES,
    BOARD_DEPENDENT_ARTIFACT_KINDS,
    CLUSTER_DEPENDENT_ARTIFACT_KINDS,
    CLUSTER_SEQ_PAYLOAD_KEY,
    MAINSTREAM_COVERAGE,
    stamp_cluster_seq,
)
from app.services.knowledge_contracts import (
    KG_COMMUNITY_EDGES_MAX,
    KG_SOURCE_PAGE_MAX,
    USABLE_STATUSES,
)
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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

    assert PostgresMigrator(postgres_database).migrate() == 14
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


# ---------------------------------------------------------------------------
# KG 质量分析只读聚合(T1)的双后端一致性
#
# 两侧各写各的方言 SQL(占位符、jsonb 算子、COLLATE),`community_overview` 的**查询
# 形态还刻意分了岔**(PG 一条窗口函数,SQLite 逐板块有界 top-K)。载荷形状由
# app.repositories.kg_analysis_payloads 的中性装配函数保证。这里对**同一批夹具数据**
# 在两个后端各跑一遍、断言同一份期望值 —— parity 因此是被证明的,不是被声称的。
#
# 夹具必须让**每一条口径分支都在 PostgreSQL 上真正被执行到**。初版夹具做不到:它只有
# 单 notebook 的简单数据,把 PG 侧四处 notebook 归属约束全删光仍然 53 passed(同样的
# 变异打 SQLite 侧立刻 2 failed)。所以这里补齐:
#   · 跨 notebook 的簇成员对象与跨 notebook 的边端点(归属约束的唯一证人);
#   · 一个成员全被排除的空簇(`empty` 分支);
#   · 一条**既被否决、端点又不可用**的边(`rejected` / `endpoint_unusable` 的
#     CASE 分支顺序);
#   · 落在 `4-5` / `2` / `1` 三档的簇(桶边界);
#   · level > 0 的板块记录(level 归属);
#   · concept 之外的三类 + 两个契约外的自定义类型(D1 的分组与「其他」组);
#   · 一个 canonical_name 为空串的成员(`MIN(NULLIF(...))` 的空名劫持)。
#
# 顺带钉住只有真 PostgreSQL 才暴露的坑:LIKE 里的 `%` 必须写成 `%%`(psycopg 的参数
# 展开)、jsonb 的 `-> 0 ->> 'basis'` 取值、`SUM()` 返回 Decimal 需要 int() 收口、
# `GROUP BY` 必须含 `object_type`(PG 不允许 bare column)、窗口函数 CTE 的 LEFT JOIN。
# ---------------------------------------------------------------------------

ANALYSIS_NB = "nb-personal"
FOREIGN_NB = "nb-base"


def _analysis_unified_store(harness):
    return PostgresUnifiedKgStore(harness.database, now=lambda: NOW)


def _analysis_payloads(*, cluster_seq: int = 0, **overrides) -> dict:
    """一批合法的账本 payload,簇世代照生产路径经 `stamp_cluster_seq` 盖上。

    夹具刻意**不手抄**那个 key —— 分类(哪几份依赖合并结果)一旦改了,夹具跟着改,
    不会出现「守卫按新分类查、夹具还按旧分类拼」这种两侧各自自洽却对不上的局面。
    """
    payloads = {kind: {"level": 0} for kind in ARTIFACT_KINDS}
    payloads[ARTIFACT_COMMUNITY_EDGES] = {"level": 0, "communities": 2}
    payloads.update(overrides)
    return stamp_cluster_seq(payloads, cluster_seq)


def _analysis_object(object_id: str, *, notebook: str = ANALYSIS_NB,
                     status: str = "approved", object_type: str = "concept") -> tuple:
    return (
        object_id,
        notebook,
        object_type,
        status,
        json.dumps({"name": object_id}),
        "[]",
        "source-golden",
        NOW,
        NOW,
    )


def _analysis_cluster(index: int, canonical: str, object_id: str, *,
                      object_type: str = "concept", name: str | None = None) -> tuple:
    return (
        f"cluster-an-{object_type}-{index}",
        ANALYSIS_NB,
        canonical,
        object_id,
        canonical if name is None else name,
        object_type,
        "",
        "",
        NOW,
    )


# 契约外的 object_type。knowhow 投影用**用户的列名**建自定义类型,本机库里真实出现过
# 这类值;它们必须归进「其他」组、只报计数,类型名不得进载荷。
CUSTOM_TYPE_A = "根因分析动作"
CUSTOM_TYPE_B = "周几"


def _seed_analysis_fixture(harness) -> None:
    unified = _analysis_unified_store(harness)
    objects = [
        # concept:4 个可用(canonical-quad → "4-5" 档)
        *[_analysis_object(f"ko-an-q{i}") for i in range(1, 5)],
        # concept:2 个可用(canonical-blank → "2" 档,其中一个 canonical_name 是空串)
        _analysis_object("ko-an-b1"),
        _analysis_object("ko-an-b2"),
        # concept:1 个可用(canonical-solo → "1" 档)
        _analysis_object("ko-an-solo"),
        # concept:deprecated(canonical-dead 的唯一成员 → 整簇落空 → "empty")
        _analysis_object("ko-an-dead", status="deprecated"),
        # **别的 notebook** 的对象:canonical-foreign 的成员 / rel-an-foreign 的端点。
        # 这是「归属约束」的唯一证人 —— 没有它,把 o.notebook_id = c.notebook_id 删掉
        # 也测不出来。
        _analysis_object("ko-an-foreign", notebook=FOREIGN_NB),
        # 非 concept 三类
        _analysis_object("ko-an-l1", object_type="claim"),
        _analysis_object("ko-an-l2", object_type="claim"),
        _analysis_object("ko-an-l3", object_type="claim"),
        _analysis_object("ko-an-f1", object_type="formula"),
        _analysis_object("ko-an-p1", object_type="procedure"),
        # 契约外的两个自定义类型
        _analysis_object("ko-an-x1", object_type=CUSTOM_TYPE_A),
        _analysis_object("ko-an-x2", object_type=CUSTOM_TYPE_B),
    ]
    concept_clusters = [
        *[_analysis_cluster(i, "canonical-quad", f"ko-an-q{i}") for i in range(1, 5)],
        # 空名成员:裸 MIN() 会让空串劫持榜首,MIN(NULLIF(...)) 才拿到 "zeta board"。
        _analysis_cluster(5, "canonical-blank", "ko-an-b1", name=""),
        _analysis_cluster(6, "canonical-blank", "ko-an-b2", name="zeta board"),
        _analysis_cluster(7, "canonical-solo", "ko-an-solo"),
        _analysis_cluster(8, "canonical-dead", "ko-an-dead"),
        # 成员对象属于别的 notebook → 不是本库的可用成员 → 整簇落空。
        _analysis_cluster(9, "canonical-foreign", "ko-an-foreign"),
        # 成员对象根本不存在(损坏数据)→ 同样落空。
        _analysis_cluster(10, "canonical-ghost", "ko-an-nowhere"),
    ]
    claim_clusters = [
        _analysis_cluster(1, "KL-pair", "ko-an-l1", object_type="claim"),
        _analysis_cluster(2, "KL-pair", "ko-an-l2", object_type="claim"),
        _analysis_cluster(3, "KL-solo", "ko-an-l3", object_type="claim"),
    ]
    relations = [
        (relation_id, ANALYSIS_NB, "source-golden", source, target, "about",
         evidence, NOW)
        for relation_id, source, target, evidence in (
            ("rel-an-shared", "ko-an-q1", "ko-an-q2",
             json.dumps([{"basis": "relink:shared-element", "quote": ""}])),
            ("rel-an-name", "ko-an-q2", "ko-an-q3",
             json.dumps([{"basis": "relink:name-match", "quote": ""}])),
            # LIKE 'relink:%%' 分支 —— psycopg 的 `%` 转义只有真 PG 才会炸。
            ("rel-an-other", "ko-an-q3", "ko-an-q4",
             json.dumps([{"basis": "relink:future-rule", "quote": ""}])),
            ("rel-an-tagged", "ko-an-q4", "ko-an-q1",
             json.dumps([{"basis": "delegation", "quote": ""}])),
            # knowhow 投影写的 about 边:evidence='[]' → untagged。
            ("rel-an-untagged", "ko-an-q1", "ko-an-q3", "[]"),
            # evidence[0] 是标量,取 basis 必须安全返回 NULL 而不是报错。
            ("rel-an-scalar", "ko-an-q2", "ko-an-q4", json.dumps([42])),
            # 端点 deprecated → 不在可用节点集里,这条边进不了图。
            ("rel-an-dead", "ko-an-q1", "ko-an-dead", "[]"),
            # 端点不存在(损坏数据)同样排除。
            ("rel-an-ghost", "ko-an-q1", "ko-an-nowhere", "[]"),
            # 端点属于**别的 notebook** —— 边归属约束的唯一证人。
            ("rel-an-foreign", "ko-an-q1", "ko-an-foreign", "[]"),
            # 反向:源端点跨库。
            ("rel-an-foreign-src", "ko-an-foreign", "ko-an-q1", "[]"),
            # 只是被否决(端点都好):计入 rejected。
            ("rel-an-rejected", "ko-an-q1", "ko-an-q2", "[]"),
            # **既被否决、端点又不可用**:CASE 顺序要求它只计一次(rejected)。
            ("rel-an-rejected-ghost", "ko-an-q1", "ko-an-nowhere", "[]"),
        )
    ]
    with harness.database.write() as connection:
        harness.knowledge.insert_object_chunk(connection, objects)
        harness.knowledge.insert_relation_chunk(connection, relations)
        unified.replace_cluster_rows_streamed(
            connection, ANALYSIS_NB, "concept", concept_clusters
        )
        unified.replace_cluster_rows_streamed(
            connection, ANALYSIS_NB, "claim", claim_clusters
        )
        unified.replace_cluster_rows_streamed(
            connection, ANALYSIS_NB, "formula",
            [_analysis_cluster(1, "KF-solo", "ko-an-f1", object_type="formula")],
        )
        unified.replace_cluster_rows_streamed(
            connection, ANALYSIS_NB, "procedure",
            [_analysis_cluster(1, "KP-solo", "ko-an-p1", object_type="procedure")],
        )
        unified.replace_cluster_rows_streamed(
            connection, ANALYSIS_NB, CUSTOM_TYPE_A,
            [_analysis_cluster(1, "KX-a", "ko-an-x1", object_type=CUSTOM_TYPE_A)],
        )
        unified.replace_cluster_rows_streamed(
            connection, ANALYSIS_NB, CUSTOM_TYPE_B,
            [_analysis_cluster(1, "KX-b", "ko-an-x2", object_type=CUSTOM_TYPE_B)],
        )
        unified.replace_communities(
            connection,
            ANALYSIS_NB,
            0,
            [
                ("cm-big", ["canonical-blank", "canonical-quad", "canonical-solo"]),
                ("cm-small", ["canonical-solo"]),
            ],
            {
                "canonical-quad": "Quad board anchor",
                "canonical-solo": "Solo concept",
                "canonical-blank": "Blank-name concept",
            },
            {"canonical-quad": 0.2, "canonical-solo": 0.9, "canonical-blank": 0.5},
            NOW,
        )
        # level > 0:板块查询按 level 归属,没有这条记录那个谓词从未被执行到。
        unified.replace_communities(
            connection,
            ANALYSIS_NB,
            1,
            [("cm-l1", ["canonical-quad"])],
            {"canonical-quad": "Level one board"},
            {"canonical-quad": 1.0},
            NOW,
        )
        # 别的 notebook 也有板块:板块查询的 notebook 归属谓词的证人。
        unified.replace_communities(
            connection,
            FOREIGN_NB,
            0,
            [("cm-foreign", ["canonical-elsewhere"])],
            {"canonical-elsewhere": "Elsewhere board"},
            {"canonical-elsewhere": 1.0},
            NOW,
        )
        placeholder = "%s"
        # 一个**没有任何成员行**的板块(损坏/半写数据)。PG 侧的窗口函数写法用
        # LEFT JOIN 才留得住它;换成 INNER JOIN 它会静默消失 —— 这正是最容易出分歧
        # 的一处,必须有证人。
        connection.execute(
            "INSERT INTO communities (id, notebook_id, level, member_ids, size, created_at) "
            f"VALUES ('cm-orphan', {placeholder}, 0, "
            + "%s::jsonb"
            + f", 2, {placeholder})",
            (ANALYSIS_NB, json.dumps(["k-x", "k-y"]),
             normalize_timestamp(NOW)),
        )
        connection.execute(
            "UPDATE knowledge_relations SET review_status='rejected' WHERE id IN "
            f"({placeholder}, {placeholder})",
            ("rel-an-rejected", "rel-an-rejected-ghost"),
        )
        # 别的 notebook 的边:边查询的 notebook 归属谓词的证人。
        harness.knowledge.insert_relation_chunk(
            connection,
            [("rel-an-elsewhere", FOREIGN_NB, "source-golden",
              "ko-an-foreign", "ko-an-foreign", "about", "[]", NOW)],
        )


def _group_of(histogram, object_type: str) -> dict:
    return next(
        row for row in histogram["by_object_type"]
        if row["object_type"] == object_type
    )


def test_cluster_convergence_aggregates_match_on_postgres(knowledge_harness):
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)

    histogram = unified.cluster_size_histogram(ANALYSIS_NB)

    # 全类型合计:concept 4+2+1=7、claim 2+1=3、formula 1、procedure 1、自定义 1+1=2
    assert histogram["member_rows"] == 14
    assert histogram["clusters"] == 9
    # concept 侧被排除的成员行:deprecated 1 + 跨 notebook 1 + 不存在 1
    assert histogram["excluded_member_rows"] == 3
    assert histogram["empty_clusters"] == 3
    assert histogram["empty_cluster_member_rows"] == 3
    assert [(row["bucket"], row["clusters"], row["members"])
            for row in histogram["buckets"]] == [
        ("1", 6, 6), ("2", 2, 4), ("3", 0, 0),
        ("4-5", 1, 4), ("6-10", 0, 0), ("11-50", 0, 0), ("51+", 0, 0),
    ]

    concept = _group_of(histogram, "concept")
    assert concept["member_rows"] == 7
    assert concept["clusters"] == 3
    assert concept["excluded_member_rows"] == 3
    assert concept["empty_clusters"] == 3
    assert concept["object_types"] == 1
    assert [(row["bucket"], row["clusters"], row["members"])
            for row in concept["buckets"]] == [
        ("1", 1, 1), ("2", 1, 2), ("3", 0, 0),
        ("4-5", 1, 4), ("6-10", 0, 0), ("11-50", 0, 0), ("51+", 0, 0),
    ]

    claim = _group_of(histogram, "claim")
    assert (claim["member_rows"], claim["clusters"]) == (3, 2)
    assert _group_of(histogram, "formula")["clusters"] == 1
    assert _group_of(histogram, "procedure")["clusters"] == 1

    other = _group_of(histogram, "other")
    assert (other["member_rows"], other["clusters"], other["object_types"]) == (2, 2, 2)
    # 自定义类型名是用户内容,绝不能出现在这个查询层的返回里。
    serialized = json.dumps(histogram, ensure_ascii=False)
    assert CUSTOM_TYPE_A not in serialized
    assert CUSTOM_TYPE_B not in serialized


def test_largest_clusters_match_on_postgres(knowledge_harness):
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)

    assert unified.largest_clusters(ANALYSIS_NB, 10) == {
        "object_type": "concept",
        "limit": 10,
        "truncated": False,
        "clusters": [
            {"canonical_id": "canonical-quad",
             "canonical_name": "canonical-quad", "members": 4},
            # 空名成员不得劫持榜首:MIN(NULLIF(canonical_name,'')) 拿到 "zeta board"。
            {"canonical_id": "canonical-blank",
             "canonical_name": "zeta board", "members": 2},
            {"canonical_id": "canonical-solo",
             "canonical_name": "canonical-solo", "members": 1},
        ],
    }
    # 榜单只有 concept:claim 的 KL-pair(2 个成员)绝不能挤进来。
    assert "KL-pair" not in [
        row["canonical_id"]
        for row in unified.largest_clusters(ANALYSIS_NB, 10)["clusters"]
    ]

    assert unified.largest_clusters(ANALYSIS_NB, 1)["truncated"] is True
    # 边界档:恰好等于上限时**没有**被截断(`>` 写成 `>=` 只有这一档抓得住)。
    exactly = unified.largest_clusters(ANALYSIS_NB, 3)
    assert len(exactly["clusters"]) == 3
    assert exactly["truncated"] is False


def test_community_overview_matches_on_postgres(knowledge_harness):
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)

    assert unified.community_overview(ANALYSIS_NB, limit=10, top_k=2) == {
        "level": 0,
        "total": 3,
        "returned": 3,
        "truncated": False,
        "limit": 10,
        "top_members_limit": 2,
        "communities": [
            {
                "id": "cm-big",
                "size": 3,
                # centrality 降序:solo(0.9) > blank(0.5) > quad(0.2)
                "top_members": ["Solo concept", "Blank-name concept"],
                "top_members_truncated": True,
            },
            {
                # 成员行缺失的板块照样出现(PG 侧靠窗口函数外的 LEFT JOIN),
                # top_members 为空 + truncated 说明「还有 size 个没给出来」。
                "id": "cm-orphan",
                "size": 2,
                "top_members": [],
                "top_members_truncated": True,
            },
            {
                "id": "cm-small",
                "size": 1,
                "top_members": ["Solo concept"],
                "top_members_truncated": False,
            },
        ],
    }

    capped = unified.community_overview(ANALYSIS_NB, limit=1, top_k=5)
    assert capped["total"] == 3
    assert capped["returned"] == 1
    assert capped["truncated"] is True

    # level > 0 是独立的一层,不与 level 0 混。
    assert unified.community_overview(ANALYSIS_NB, level=1, limit=10, top_k=5) == {
        "level": 1,
        "total": 1,
        "returned": 1,
        "truncated": False,
        "limit": 10,
        "top_members_limit": 5,
        "communities": [
            {"id": "cm-l1", "size": 1,
             "top_members": ["Level one board"], "top_members_truncated": False},
        ],
    }

    # 别的 notebook 的板块不得漏进来(反过来也一样)。
    foreign = unified.community_overview(FOREIGN_NB, limit=10, top_k=5)
    assert [row["id"] for row in foreign["communities"]] == ["cm-foreign"]


def test_relation_provenance_counts_match_on_postgres(knowledge_harness):
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)

    assert unified.relation_provenance_counts(ANALYSIS_NB) == {
        "counted": 6,
        "relink": 3,
        "buckets": {
            "relink:shared-element": 1,
            "relink:name-match": 1,
            "relink:other": 1,
            "tagged:other": 1,
            "untagged": 2,          # evidence='[]' 与 evidence=[42] 都归未标注
        },
        # 端点不可用 4 条:deprecated / 不存在 / 跨库(正反各一)。
        # rejected 2 条,其中一条端点也不可用 —— CASE 顺序保证它只计一次。
        "excluded": {"rejected": 2, "endpoint_unusable": 4},
        "total_rows": 12,
    }

    # 别的 notebook 的边不得漏进来。
    assert unified.relation_provenance_counts(FOREIGN_NB)["total_rows"] == 1


def test_analysis_payload_shapes_are_identical_across_backends(knowledge_harness):
    """形状 parity:两侧返回同一套 key、同一个定长定序的分组/分桶轴。

    值的一致由上面四条(同一批夹具、同一份期望值、两个后端各跑一遍)证明;这条钉的是
    「即使某侧将来改了 SQL 形态(community_overview 已经分岔了),对上层暴露的坐标轴
    也不许换」。
    """
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)

    histogram = unified.cluster_size_histogram(ANALYSIS_NB)
    assert set(histogram) == {
        "member_rows", "clusters", "excluded_member_rows", "empty_clusters",
        "empty_cluster_member_rows", "buckets", "by_object_type",
    }
    assert [row["object_type"] for row in histogram["by_object_type"]] == [
        "concept", "claim", "formula", "procedure", "other",
    ]
    for row in histogram["by_object_type"]:
        assert [bucket["bucket"] for bucket in row["buckets"]] == [
            "1", "2", "3", "4-5", "6-10", "11-50", "51+",
        ]

    assert set(unified.largest_clusters(ANALYSIS_NB, 5)) == {
        "object_type", "limit", "truncated", "clusters",
    }
    assert set(unified.community_overview(ANALYSIS_NB)) == {
        "level", "total", "returned", "truncated", "limit",
        "top_members_limit", "communities",
    }
    assert set(unified.relation_provenance_counts(ANALYSIS_NB)) == {
        "counted", "relink", "buckets", "excluded", "total_rows",
    }


def test_analysis_reads_never_write_on_either_backend(knowledge_harness):
    """只读:四个查询跑完,相关表的行数与内容指纹一个字节都不许变。

    PostgreSQL 侧还有第二道:`_read_snapshot` 让 psycopg 发 `BEGIN … READ ONLY`,
    引擎会直接拒绝写(那道由 test_analysis_reads_run_read_only_on_postgres 钉)。
    """
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    tables = (
        "knowledge_objects", "knowledge_relations",
        "concept_clusters", "communities", "community_members",
    )

    def snapshot() -> dict:
        out = {}
        with knowledge_harness.database.connect() as connection:
            for table in tables:
                row = connection.execute(
                    f"SELECT COUNT(*) AS n, "
                    f"COALESCE(MIN(id), '') AS lo, COALESCE(MAX(id), '') AS hi "
                    f"FROM {table}"
                    if table != "community_members" else
                    f"SELECT COUNT(*) AS n, "
                    f"COALESCE(MIN(canonical_id), '') AS lo, "
                    f"COALESCE(MAX(canonical_id), '') AS hi FROM {table}"
                ).fetchone()
                out[table] = (int(row["n"]), row["lo"], row["hi"])
        return out

    before = snapshot()
    unified.cluster_size_histogram(ANALYSIS_NB)
    unified.largest_clusters(ANALYSIS_NB, 5)
    unified.community_overview(ANALYSIS_NB)
    unified.relation_provenance_counts(ANALYSIS_NB)
    assert snapshot() == before


@pytest.mark.postgres_integration
def test_analysis_reads_run_read_only_on_postgres(postgres_database):
    """PG 侧的只读契约由**引擎**强制:`_read_snapshot` 发 `BEGIN … READ ONLY`,
    快照版还叠 REPEATABLE READ。这里直接问事务自己的 GUC,而不是相信注释。"""
    from app.repositories.postgres.migrator import PostgresMigrator

    PostgresMigrator(postgres_database).migrate()
    store = PostgresUnifiedKgStore(postgres_database, now=lambda: NOW)

    with store._read_snapshot() as connection:
        row = connection.execute(
            "SELECT current_setting('transaction_read_only') AS ro, "
            "current_setting('transaction_isolation') AS iso"
        ).fetchone()
        assert row["ro"] == "on"
        with pytest.raises(Exception):
            connection.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
                "created_by,created_at,updated_at,tier) "
                "VALUES ('nb-should-not-exist','x','','t','ready',NULL,%s,%s,'personal')",
                (NOW, NOW),
            )

    with store._read_snapshot(repeatable=True) as connection:
        row = connection.execute(
            "SELECT current_setting('transaction_read_only') AS ro, "
            "current_setting('transaction_isolation') AS iso"
        ).fetchone()
        assert row["ro"] == "on"
        assert row["iso"] == "repeatable read"


@pytest.mark.postgres_integration
def test_community_overview_reads_one_repeatable_read_snapshot(postgres_database):
    """并发 rebuild_communities 在报告生成中途提交,载荷不得自相矛盾。

    PostgreSQL 的 READ COMMITTED 是**每条语句**取一次快照,所以「先数到 N 个板块、
    再一个都取不到」在 PG 上是真实可复现的:这里在第一条语句(COUNT)之后,用**另一条
    连接**提交一次整表删除(rebuild 的 DELETE 阶段),断言载荷仍然自洽。
    """
    from app.repositories.postgres.migrator import PostgresMigrator

    PostgresMigrator(postgres_database).migrate()
    _seed_catalog(postgres_database)
    store = PostgresUnifiedKgStore(postgres_database, now=lambda: NOW)
    with postgres_database.write() as connection:
        store.replace_communities(
            connection, ANALYSIS_NB, 0,
            [("cm-a", ["k-a", "k-b"]), ("cm-b", ["k-c"])],
            {"k-a": "Alpha", "k-b": "Beta", "k-c": "Gamma"},
            {"k-a": 0.9, "k-b": 0.5, "k-c": 0.7},
            NOW,
        )

    fired: list[str] = []
    original_connect = postgres_database.connect

    @contextmanager
    def spying_connect():
        with original_connect() as connection:
            original_execute = connection.execute

            def spying_execute(sql, params=None, **kwargs):
                cursor = original_execute(sql, params, **kwargs)
                if "FROM communities" in str(sql) and not fired:
                    fired.append(str(sql))
                    worker = threading.Thread(target=_wipe_communities,
                                              args=(postgres_database,))
                    worker.start()
                    worker.join(timeout=30)
                    assert not worker.is_alive(), "并发写者被读事务卡死了"
                return cursor

            connection.execute = spying_execute
            try:
                yield connection
            finally:
                del connection.execute

    postgres_database.connect = spying_connect
    try:
        payload = store.community_overview(ANALYSIS_NB, limit=10, top_k=5)
    finally:
        postgres_database.connect = original_connect

    assert fired, "探针没打中:第一条语句不是对 communities 的读"
    assert payload["total"] == 2
    assert payload["returned"] == 2, (
        "板块明细落在了删除之后的快照上 —— total/returned 自相矛盾"
    )
    assert [row["id"] for row in payload["communities"]] == ["cm-a", "cm-b"]
    assert payload["communities"][0]["top_members"] == ["Alpha", "Beta"]


def _wipe_communities(database) -> None:
    with database.write() as connection:
        connection.execute("DELETE FROM community_members")
        connection.execute("DELETE FROM communities")


# ---------------------------------------------------------------------------
# KG 质量分析预计算产物(T2)的双后端一致性
#
# 写路径两侧**没有形态分岔**(单条 hash-aggregate 分组 + 整批重写),所以这里要证明的
# 不是「分岔仍然等价」,而是「同一批夹具在两个后端上产出逐字相同的产物行」。
#
# 只有真 PostgreSQL 才暴露的坑,这里都踩过:`COUNT(*)` 回 int 而 `SUM` 回 Decimal、
# jsonb 参数必须过 `jsonb()`、`COALESCE(c.canonical_id, o.id)` 在 LEFT JOIN 条件里的
# NULL 语义、以及 `GROUP BY` 里的 NULL 组(没进任何板块的对象)在两个后端上的分组行为。
# ---------------------------------------------------------------------------


def _analysis_source_counts(harness, level: int = 0) -> dict:
    unified = _analysis_unified_store(harness)
    with harness.database.connect() as connection:
        return {
            (row["source_id"], row["community_id"]): int(row["n"])
            for row in unified.source_community_counts(connection, ANALYSIS_NB, level)
        }


def test_source_community_counts_match_on_postgres(knowledge_harness):
    _seed_analysis_fixture(knowledge_harness)

    # 夹具里 ANALYSIS_NB 的 14 个可用对象都挂 source-golden。
    #   cm-big   ← canonical-quad(q1..q4)+ canonical-blank(b1,b2)+ canonical-solo = 7
    #   cm-small ← canonical-solo = 1
    #   NULL 组  ← claim×3 / formula / procedure / 两个自定义类型 = 7(它们的 canonical
    #             不在任何板块里,只抬 n_objects、不进分母 —— 这一组必须存在,
    #             INNER JOIN 会把它整片吞掉)
    # 合计 15 > 14:`canonical-solo` 在这份**合成**夹具里同时挂在两个板块下,所以
    # ko-an-solo 被两条 community_members 行各计一次。生产不会出现 —— Louvain 的
    # membership 是一个划分,`replace_communities` 照它整表重写。这里刻意保留这个形态,
    # 因为它同时证明了两个后端在「同一对象命中多条成员行」上的行为逐字一致。
    counts = _analysis_source_counts(knowledge_harness)
    assert counts == {
        ("source-golden", None): 7,
        ("source-golden", "cm-big"): 7,
        ("source-golden", "cm-small"): 1,
    }
    # 别的 notebook 的对象绝不能混进来(归属谓词的证人)。
    assert all(source == "source-golden" for source, _community in counts)


def test_source_counts_drop_hidden_sources_but_keep_orphans_on_postgres(
    knowledge_harness,
):
    """来源画像的可见性口径,逐字比对返回值。

      · `source_type IN ('memory','knowhow')` 的合成来源 → **排除**(它们的 `title`
        是用户内容:knowhow 投影的是表名、Memory 的是那条记忆的抬头,而产品其余各处
        一律不显示它们)。
      · **孤儿来源**(`source_id` 在 `sources` 里没有对应行)→ **保留**,读侧靠
        `source_missing` 报出来。这是本视图有意的诊断能力。

    ⚠ 两半必须一起断:同一个谓词决定这两件事,而把 `NOT EXISTS` 写成 `JOIN sources`
    或 `LEFT JOIN … WHERE s.source_type NOT IN (…)`(孤儿的 `s.source_type` 是 NULL,
    `NULL NOT IN (…)` 为 NULL → 整行被滤掉)对「排除合成
    来源」这一半完全正确、却会把孤儿一起吞掉。

    ⚠ 孤儿的证人**必须自己造**:`source-golden` 看着像孤儿,其实 `_seed_catalog` 给它
    建了一行 `source_type='file'` 的真来源 —— 拿它当孤儿,移动变异(改成 `JOIN sources`)
    照样全绿,这一半就是个空守卫。移动变异实测过这一条:第一版正是这么写的、真的没报红。
    所以下面显式塞一个 `source-deleted`(只有对象、没有 `sources` 行)。
    """
    _seed_analysis_fixture(knowledge_harness)
    placeholder = "%s"
    stamp = normalize_timestamp(NOW)
    with knowledge_harness.database.write() as connection:
        for source_id, title, source_type in (
            ("source-visible", "公开的 PDF", "pdf"),
            ("source-memory", "老板不喜欢周一开会", "memory"),
            ("source-knowhow", "季度奖金核算口径", "knowhow"),
        ):
            connection.execute(
                "INSERT INTO sources (id, notebook_id, title, source_type, "
                " created_at, updated_at) "
                f"VALUES ({placeholder}, {placeholder}, {placeholder}, "
                f"        {placeholder}, {placeholder}, {placeholder})",
                (source_id, ANALYSIS_NB, title, source_type, stamp, stamp),
            )
        # 四个对象形态完全相同(可用、挂来源、不在任何板块里),唯一的差别是来源那一侧
        # —— 所以留下/排除只可能来自来源可见性谓词本身。`source-deleted` 刻意**没有**
        # `sources` 行:它就是那个孤儿证人。
        knowledge_harness.knowledge.insert_object_chunk(
            connection,
            [
                (f"ko-an-vis-{index}", ANALYSIS_NB, "claim", "approved",
                 json.dumps({"name": object_id}), "[]", object_id, NOW, NOW)
                for index, object_id in enumerate(
                    ("source-visible", "source-memory", "source-knowhow",
                     "source-deleted")
                )
            ],
        )

    counts = _analysis_source_counts(knowledge_harness)

    assert ("source-memory", None) not in counts, "Memory 的合成来源进了来源画像"
    assert ("source-knowhow", None) not in counts, "knowhow 投影的来源进了来源画像"
    assert counts[("source-visible", None)] == 1
    assert counts[("source-deleted", None)] == 1, (
        "孤儿来源被一起排除了 —— 一个有意的诊断信号变成了静默丢弃"
    )
    assert counts == {
        ("source-golden", None): 7,
        ("source-golden", "cm-big"): 7,
        ("source-golden", "cm-small"): 1,
        ("source-visible", None): 1,
        ("source-deleted", None): 1,
    }


def test_precomputed_artifacts_round_trip_identically_on_postgres(
    knowledge_harness,
):
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    edges = [("cm-big", "cm-small", 7)]
    profiles = [("source-golden", 12, 9, "cm-big", 0.75, 2, 0.5)]
    payloads = stamp_cluster_seq({
        ARTIFACT_COMMUNITY_EDGES: {"level": 0, "edges": 1, "edges_total": 1,
                                   "truncated": False, "edge_limit": 200000,
                                   "cross_weight": 7, "intra_weight": 3,
                                   "communities": 2},
        ARTIFACT_SOURCE_PROFILES: {"level": 0, "sources": 1,
                                   "mainstream_coverage": MAINSTREAM_COVERAGE,
                                   "head_communities": 1, "head_members": 3,
                                   "total_members": 4},
        ARTIFACT_CLUSTER_HISTOGRAM: unified.cluster_size_histogram(ANALYSIS_NB),
        ARTIFACT_LARGEST_CLUSTERS: unified.largest_clusters(ANALYSIS_NB, 20),
        ARTIFACT_RELATION_PROVENANCE: unified.relation_provenance_counts(ANALYSIS_NB),
    }, 19)
    with knowledge_harness.database.write() as connection:
        unified.replace_kg_analysis_artifacts(
            connection, ANALYSIS_NB, 41, edges, profiles, payloads, NOW
        )

    placeholder = "%s"
    with knowledge_harness.database.connect() as connection:
        stored_edges = [
            (row["src_community_id"], row["dst_community_id"], int(row["weight"]))
            for row in connection.execute(
                "SELECT src_community_id, dst_community_id, weight "
                f"FROM kg_community_edges WHERE notebook_id={placeholder} "
                "ORDER BY src_community_id, dst_community_id", (ANALYSIS_NB,)
            ).fetchall()
        ]
        stored_profiles = [
            (row["source_id"], int(row["n_objects"]), int(row["n_graph_objects"]),
             row["top_community_id"], float(row["top_share"]),
             int(row["community_spread"]), float(row["mainstream_share"]))
            for row in connection.execute(
                "SELECT source_id, n_objects, n_graph_objects, top_community_id, "
                "       top_share, community_spread, mainstream_share "
                f"FROM kg_source_profiles WHERE notebook_id={placeholder} "
                "ORDER BY source_id", (ANALYSIS_NB,)
            ).fetchall()
        ]
        ledger_rows = connection.execute(
            "SELECT kind, kg_mutation_seq, payload FROM kg_analysis_artifacts "
            f"WHERE notebook_id={placeholder} ORDER BY kind", (ANALYSIS_NB,)
        ).fetchall()

    assert stored_edges == edges
    assert stored_profiles == profiles
    ledger = {
        row["kind"]: (
            int(row["kg_mutation_seq"]),
            row["payload"] if isinstance(row["payload"], dict)
            else json.loads(row["payload"]),
        )
        for row in ledger_rows
    }
    assert set(ledger) == set(ARTIFACT_KINDS)
    assert {seq for seq, _payload in ledger.values()} == {41}
    for kind, expected in payloads.items():
        assert ledger[kind][1] == json.loads(json.dumps(expected)), kind


def test_discarding_board_dependent_artifacts_on_postgres(
    knowledge_harness,
):
    """板块重铸时作废依赖板块的两份产物 —— 必须删掉**恰好那一批**行。

    PostgreSQL 侧用 `kind = ANY(%s)` 而不是逐值展开,所以「删对了哪些行」不能靠
    读代码断言,只能靠这条对同一批夹具比对
    删除前后的行。少删一份 = 报告继续画已经不存在的板块;多删一份 = 平白丢掉一份
    与板块无关、仍然可读的陈旧统计快照。
    """
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    payloads = _analysis_payloads()
    with knowledge_harness.database.write() as connection:
        unified.replace_kg_analysis_artifacts(
            connection, ANALYSIS_NB, 41,
            [("cm-big", "cm-small", 7)],
            [("source-golden", 12, 9, "cm-big", 0.75, 2, 0.5)],
            payloads, NOW,
        )

    placeholder = "%s"

    def counts() -> dict:
        with knowledge_harness.database.connect() as connection:
            return {
                "edges": int(connection.execute(
                    "SELECT COUNT(*) AS n FROM kg_community_edges "
                    f"WHERE notebook_id={placeholder}", (ANALYSIS_NB,)
                ).fetchone()["n"]),
                "profiles": int(connection.execute(
                    "SELECT COUNT(*) AS n FROM kg_source_profiles "
                    f"WHERE notebook_id={placeholder}", (ANALYSIS_NB,)
                ).fetchone()["n"]),
                "ledger": sorted(
                    row["kind"] for row in connection.execute(
                        "SELECT kind FROM kg_analysis_artifacts "
                        f"WHERE notebook_id={placeholder}", (ANALYSIS_NB,)
                    ).fetchall()
                ),
            }

    assert counts() == {"edges": 1, "profiles": 1, "ledger": sorted(ARTIFACT_KINDS)}

    with knowledge_harness.database.write() as connection:
        unified.discard_board_dependent_kg_analysis_artifacts(connection, ANALYSIS_NB)

    assert counts() == {
        "edges": 0,
        "profiles": 0,
        "ledger": sorted(set(ARTIFACT_KINDS) - set(BOARD_DEPENDENT_ARTIFACT_KINDS)),
    }


def test_replace_kg_analysis_artifacts_rejects_unknown_kinds_on_postgres(
    knowledge_harness,
):
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    with pytest.raises(ValueError, match="契约外的 kind"):
        with knowledge_harness.database.write() as connection:
            unified.replace_kg_analysis_artifacts(
                connection, ANALYSIS_NB, 1, [], [], {"board_edges": {}}, NOW
            )


def test_replace_kg_analysis_artifacts_rejects_a_missing_kind_on_postgres(
    knowledge_harness,
):
    """账本整批重写:少写一行 = 下游读成「从来没算过」,两侧都必须硬失败。"""
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    payloads = _analysis_payloads()
    payloads.pop(ARTIFACT_CLUSTER_HISTOGRAM)
    with pytest.raises(ValueError, match="缺少必需的 kind"):
        with knowledge_harness.database.write() as connection:
            unified.replace_kg_analysis_artifacts(
                connection, ANALYSIS_NB, 1, [], [], payloads, NOW
            )


def test_analysis_snapshots_refuse_a_write_transaction_on_postgres(
    knowledge_harness,
):
    """全表级只读聚合在写事务里必须当场炸 —— 两个后端都要有这条守卫(§3.35)。

    PostgreSQL 上的危害是从池里另借的连接读到提交前的库(静默过时);SQLite 上还额外
    把进程级写锁按住一整趟全表扫。形状守卫抓不到这类违规:这几条查询一张产物表都不碰。
    """
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    with knowledge_harness.database.write():
        for call in (
            lambda: unified.cluster_size_histogram(ANALYSIS_NB),
            lambda: unified.largest_clusters(ANALYSIS_NB, 20),
            lambda: unified.community_overview(ANALYSIS_NB),
            lambda: unified.relation_provenance_counts(ANALYSIS_NB),
        ):
            with pytest.raises(RuntimeError, match="写事务"):
                call()


def test_replace_kg_analysis_artifacts_polices_the_cluster_stamp_on_postgres(
    knowledge_harness,
):
    """簇世代的戳:依赖合并结果的四份必须有、不依赖的那份必须没有 —— 硬失败。

    这道守卫读的正是 `payload` 列(PostgreSQL 上是 jsonb)。一次参数适配的差错
    就会让守卫在生产后端上静默失效 —— 而它失效的表现是「这个库的预计算永远重跑」,
    没有任何报错。
    """
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)

    for kind in sorted(CLUSTER_DEPENDENT_ARTIFACT_KINDS):
        payloads = _analysis_payloads()
        payloads[kind] = {
            key: value for key, value in payloads[kind].items()
            if key != CLUSTER_SEQ_PAYLOAD_KEY
        }
        with pytest.raises(ValueError, match="必须带整数"):
            with knowledge_harness.database.write() as connection:
                unified.replace_kg_analysis_artifacts(
                    connection, ANALYSIS_NB, 1, [], [], payloads, NOW
                )

    extra = _analysis_payloads()
    extra[ARTIFACT_RELATION_PROVENANCE] = dict(
        extra[ARTIFACT_RELATION_PROVENANCE], **{CLUSTER_SEQ_PAYLOAD_KEY: 3}
    )
    with pytest.raises(ValueError, match="不依赖合并结果"):
        with knowledge_harness.database.write() as connection:
            unified.replace_kg_analysis_artifacts(
                connection, ANALYSIS_NB, 1, [], [], extra, NOW
            )


def test_the_cluster_stamp_round_trips_identically_on_postgres(knowledge_harness):
    """簇世代写进 payload、再读回来必须是 **int**。

    这是「刻意不加列」那个决定的证人:世代存在 JSON 里(PostgreSQL 上是 jsonb)。
    读回字符串 `"77"` 而不是 `77`,新鲜度判据就会恒判「不新鲜」——预计算永远重跑,
    而且不报错。
    """
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    with knowledge_harness.database.write() as connection:
        unified.replace_kg_analysis_artifacts(
            connection, ANALYSIS_NB, 77, [], [],
            _analysis_payloads(cluster_seq=19), NOW,
        )

    with knowledge_harness.database.connect() as connection:
        rows = unified.kg_analysis_artifact_rows(connection, ANALYSIS_NB)

    assert set(rows) == set(ARTIFACT_KINDS)
    for kind, entry in rows.items():
        assert entry["kg_mutation_seq"] == 77, kind
        stamp = entry["payload"].get(CLUSTER_SEQ_PAYLOAD_KEY)
        if kind in CLUSTER_DEPENDENT_ARTIFACT_KINDS:
            assert stamp == 19 and isinstance(stamp, int), kind
        else:
            assert stamp is None, kind


# ------------------------------------------------- KG 分析产物的读路径(T3)
# 这三条是**唯一**可以挂在在线请求上的 KG 分析读(只碰预计算产物表,代价按行数硬
# 有界)。它们在两个后端上必须给出逐字相同的行、相同的排序、相同的 clamp —— 尤其是
# 并列消歧:没有次级键,同一份数据在 SQLite 与 PostgreSQL 上会给出不同的顺序,而两边
# 都「没错」,分页会以最难查的方式碎掉。


def _seed_analysis_ledger(harness, *, seq: int = 55) -> dict:
    unified = _analysis_unified_store(harness)
    payloads = _analysis_payloads(**{ARTIFACT_COMMUNITY_EDGES: {
        "level": 0, "edges": 3, "edges_total": 9, "truncated": True,
        "edge_limit": 3, "cross_weight": 40, "intra_weight": 5, "communities": 4,
    }})
    with harness.database.write() as connection:
        unified.replace_kg_analysis_artifacts(
            connection, ANALYSIS_NB, seq, [], [], payloads, NOW
        )
    return payloads


def test_kg_analysis_artifact_rows_match_on_postgres(knowledge_harness):
    """账本全文(payload + 建库时刻)在两侧读回同一形状。

    ⚠ `payload` 列两侧类型不同:SQLite 是 JSON **文本**,PostgreSQL 是 jsonb(psycopg
    直接给 dict)。中性的 `artifact_ledger_rows` 负责把两者收成同一个 dict —— 这条就是
    那次收口的证人。`created_at` 两侧表示法不同(timestamptz → ISO 文本 vs 原样字符串),
    所以只断言它非空,不逐字比。
    """
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    with knowledge_harness.database.connect() as connection:
        assert unified.kg_analysis_artifact_rows(connection, ANALYSIS_NB) == {}

    payloads = _seed_analysis_ledger(knowledge_harness, seq=55)
    with knowledge_harness.database.connect() as connection:
        rows = unified.kg_analysis_artifact_rows(connection, ANALYSIS_NB)

    assert set(rows) == set(ARTIFACT_KINDS)
    for kind, entry in rows.items():
        assert entry["kg_mutation_seq"] == 55, kind
        assert entry["payload"] == json.loads(json.dumps(payloads[kind])), kind
        assert entry["created_at"], kind


def test_kg_analysis_artifact_rows_reject_a_malformed_payload_on_postgres(
    knowledge_harness,
):
    """行在、内容读不出来:既不是「缺失」也不是「为空」,两侧都必须硬失败。"""
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    placeholder = "%s"
    payload_expr = "%s::jsonb"
    with knowledge_harness.database.write() as connection:
        connection.execute(
            "INSERT INTO kg_analysis_artifacts "
            "(notebook_id, kind, kg_mutation_seq, payload, created_at) "
            f"VALUES ({placeholder}, {placeholder}, 1, {payload_expr}, {placeholder})",
            (ANALYSIS_NB, ARTIFACT_CLUSTER_HISTOGRAM, json.dumps([1, 2, 3]),
             normalize_timestamp(NOW)),
        )
    with knowledge_harness.database.connect() as connection:
        with pytest.raises(ValueError, match="payload 不是对象"):
            unified.kg_analysis_artifact_rows(connection, ANALYSIS_NB)


def test_kg_community_edges_top_matches_on_postgres(knowledge_harness):
    """跨板块边 top-N:weight 降序,**并列按 (src, dst) 升序**。

    并列消歧不是洁癖:板块 id 是随机铸的,没有次级键时两个后端的行到达顺序不同,
    俯瞰图会在同一份数据上画出两张不同的图。
    """
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    edges = [
        ("cm-a", "cm-b", 9),
        ("cm-a", "cm-c", 4),      # 与下面一条 weight 并列 → 靠 (src,dst) 定序
        ("cm-b", "cm-c", 4),
        ("cm-c", "cm-d", 1),
    ]
    payloads = _analysis_payloads(**{
        ARTIFACT_COMMUNITY_EDGES: {"level": 0, "communities": 4}})
    with knowledge_harness.database.write() as connection:
        unified.replace_kg_analysis_artifacts(
            connection, ANALYSIS_NB, 12, edges, [], payloads, NOW
        )

    with knowledge_harness.database.connect() as connection:
        top3 = unified.kg_community_edges_top(connection, ANALYSIS_NB, 3)
        clamped = unified.kg_community_edges_top(connection, ANALYSIS_NB, 10**9)

    assert top3 == [("cm-a", "cm-b", 9), ("cm-a", "cm-c", 4), ("cm-b", "cm-c", 4)]
    assert clamped == sorted(edges, key=lambda e: (-e[2], e[0], e[1]))


def test_kg_source_profile_page_matches_on_postgres(knowledge_harness):
    """来源画像分页:mainstream_share 升/降序 + source_id 并列消歧 + 孤儿来源标记。

    夹具刻意让三条并列在 `mainstream_share = 0.0` 上 —— 混杂库里那一片就是这个形态,
    也正是没有次级键时跨页重复/漏行的触发条件。
    """
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    # ⚠ **倒序插入**(d → a):按 src-a..src-d 升序插入时堆顺序恰好等于目标顺序,
    # 并列消歧一点功都不做 —— 删掉 SQL 里的 `source_id COLLATE "C" ASC` 照样全绿
    # (评审实测)。倒序让堆顺序与目标顺序相反,次级键这才变成载重件。
    profiles = [
        ("src-d", 40, 32, "cm-big", 0.8, 4, 0.9),
        ("src-c", 30, 24, "cm-small", 0.7, 1, 0.0),
        ("src-b", 20, 16, "cm-big", 0.6, 3, 0.0),
        ("src-a", 10, 8, "cm-big", 0.5, 2, 0.0),
    ]
    payloads = _analysis_payloads()
    placeholder = "%s"
    with knowledge_harness.database.write() as connection:
        unified.replace_kg_analysis_artifacts(
            connection, ANALYSIS_NB, 13, [], profiles, payloads, NOW
        )
        # 只给 src-b 建来源行:其余三条是孤儿引用(source_id 没有外键,历史清理会留下
        # 这种行)。标题为空到底是「没标题」还是「已删除」,必须分得开。
        connection.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, "
            " created_at, updated_at) "
            f"VALUES ({placeholder}, {placeholder}, {placeholder}, 'file', "
            f"        {placeholder}, {placeholder})",
            ("src-b", ANALYSIS_NB, "Golden source",
             normalize_timestamp(NOW),
             normalize_timestamp(NOW)),
        )

    with knowledge_harness.database.connect() as connection:
        total, page1 = unified.kg_source_profile_page(
            connection, ANALYSIS_NB, limit=2, offset=0
        )
        _total2, page2 = unified.kg_source_profile_page(
            connection, ANALYSIS_NB, limit=2, offset=2
        )
        _total3, descending = unified.kg_source_profile_page(
            connection, ANALYSIS_NB, limit=4, offset=0, ascending=False
        )

    assert total == 4
    assert [row["source_id"] for row in page1] == ["src-a", "src-b"]
    assert [row["source_id"] for row in page2] == ["src-c", "src-d"]
    assert [row["source_id"] for row in descending][0] == "src-d"
    assert page1[0] == {
        "source_id": "src-a", "title": "", "source_missing": True,
        "n_objects": 10, "n_graph_objects": 8, "top_community_id": "cm-big",
        "top_share": 0.5, "community_spread": 2, "mainstream_share": 0.0,
    }
    assert page1[1]["title"] == "Golden source"
    assert page1[1]["source_missing"] is False


def test_kg_community_edges_top_is_clamped_on_postgres(knowledge_harness):
    """store 侧的 clamp **本身**是载重件,不是注释。

    ⚠ 上面那条只塞 4 条边,`_clamp(limit, KG_COMMUNITY_EDGES_MAX)` 在 2000 上永远不
    生效 —— 删掉它照样全绿(评审实测)。设计把 store clamp 定为纵深防御的一层
    (「任何调用方都不可能拿到无界返回」),批 2–4 里任何直接调 store 的新调用方
    传一个 `limit=10**9` 就会兑现那个上限。所以这里塞**超过上限**的边并断返回条数。
    """
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    over = KG_COMMUNITY_EDGES_MAX + 5
    # weight 全部互不相同,排序结果与并列消歧无关 —— 这条只测「有界」这一件事。
    edges = [("cm-src", f"cm-{i:06d}", over - i) for i in range(over)]
    payloads = _analysis_payloads()
    with knowledge_harness.database.write() as connection:
        unified.replace_kg_analysis_artifacts(
            connection, ANALYSIS_NB, 14, edges, [], payloads, NOW
        )

    with knowledge_harness.database.connect() as connection:
        clamped = unified.kg_community_edges_top(connection, ANALYSIS_NB, 10**9)
        asked = unified.kg_community_edges_top(connection, ANALYSIS_NB, 7)

    assert len(clamped) == KG_COMMUNITY_EDGES_MAX
    assert [weight for _s, _d, weight in clamped] == sorted(
        (weight for _s, _d, weight in clamped), reverse=True)
    # 上限之下照常按调用方给的数返回(clamp 不是「一律返回上限」)。
    assert len(asked) == 7


def test_kg_source_profile_page_is_clamped_and_stable_on_ties_on_postgres(
    knowledge_harness,
):
    """来源画像分页的两条不变式一起钉:**有界** + **并列组内跨页不重不漏**。

    ⚠ 与上面那条同理,4 行的夹具让 `_clamp(limit, KG_SOURCE_PAGE_MAX)` 永不生效。
    并列消歧同样如此:这里让全部行并列在 `mainstream_share = 0.0`(混杂库里那一片
    就是这个形态)、并按 **source_id 倒序插入**,堆顺序与目标顺序相反,
    `source_id COLLATE "C" ASC` 这才是载重件。
    """
    _seed_analysis_fixture(knowledge_harness)
    unified = _analysis_unified_store(knowledge_harness)
    over = KG_SOURCE_PAGE_MAX + 3
    ids = [f"src-{i:05d}" for i in range(over)]
    profiles = [
        (source_id, 10, 8, "cm-big", 0.5, 2, 0.0) for source_id in reversed(ids)
    ]
    payloads = _analysis_payloads()
    with knowledge_harness.database.write() as connection:
        unified.replace_kg_analysis_artifacts(
            connection, ANALYSIS_NB, 15, [], profiles, payloads, NOW
        )

    with knowledge_harness.database.connect() as connection:
        total, clamped = unified.kg_source_profile_page(
            connection, ANALYSIS_NB, limit=10**9, offset=0
        )
        pages = [
            unified.kg_source_profile_page(
                connection, ANALYSIS_NB, limit=50, offset=offset
            )[1]
            for offset in range(0, over, 50)
        ]

    assert total == over
    assert len(clamped) == KG_SOURCE_PAGE_MAX
    # 有界之外还得是**头部**那一页,不是随便 200 行。
    assert [row["source_id"] for row in clamped] == ids[:KG_SOURCE_PAGE_MAX]

    seen = [row["source_id"] for page in pages for row in page]
    assert seen == ids, "并列组内翻页必须既不重复也不漏行,且顺序两个后端一致"
