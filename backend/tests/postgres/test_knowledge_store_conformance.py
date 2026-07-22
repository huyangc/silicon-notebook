from __future__ import annotations

import inspect
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.config import Settings
from app.repositories.postgres.embedding_store import EmbeddingStore as PostgresEmbeddingStore
from app.repositories.postgres.governance_store import GovernanceStore as PostgresGovernanceStore
from app.repositories.postgres.index_projection_store import (
    IndexProjectionStore as PostgresIndexProjectionStore,
)
from app.repositories.postgres.knowledge_store import KnowledgeStore as PostgresKnowledgeStore
from app.repositories.postgres.query_store import QueryStore as PostgresQueryStore
from app.repositories.postgres.unified_kg_store import UnifiedKgStore as PostgresUnifiedKgStore
from app.repositories.sqlite.embedding_store import EmbeddingStore as SqliteEmbeddingStore
from app.repositories.sqlite.governance_store import GovernanceStore as SqliteGovernanceStore
from app.repositories.sqlite.index_projection_store import (
    IndexProjectionStore as SqliteIndexProjectionStore,
)
from app.repositories.sqlite.knowledge_store import KnowledgeStore as SqliteKnowledgeStore
from app.repositories.sqlite.query_store import QueryStore as SqliteQueryStore
from app.repositories.sqlite.unified_kg_store import UnifiedKgStore as SqliteUnifiedKgStore
from app.services.knowledge_contracts import USABLE_STATUSES
from app.services.repository_runtime import RepositoryCompatibilitySeams
from app.services.retrieval import RetrievedKnowledge, RetrievedRelation
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.vector_index import encode_vector


NOW = "2026-07-22T00:00:00+00:00"


def _public_callables(cls: type) -> dict[str, object]:
    return {
        name: inspect.getattr_static(cls, name)
        for name in cls.__dict__
        if not name.startswith("_") and callable(getattr(cls, name))
    }


def _signature_shape(method) -> tuple:
    signature = inspect.signature(method)
    return tuple(
        (parameter.name, parameter.kind, parameter.default)
        for parameter in signature.parameters.values()
    )


@pytest.mark.parametrize(
    ("sqlite_cls", "postgres_cls"),
    (
        (SqliteEmbeddingStore, PostgresEmbeddingStore),
        (SqliteKnowledgeStore, PostgresKnowledgeStore),
        (SqliteGovernanceStore, PostgresGovernanceStore),
        (SqliteIndexProjectionStore, PostgresIndexProjectionStore),
        (SqliteQueryStore, PostgresQueryStore),
        (SqliteUnifiedKgStore, PostgresUnifiedKgStore),
    ),
)
def test_postgres_knowledge_store_surfaces_cover_sqlite(sqlite_cls, postgres_cls):
    sqlite_methods = _public_callables(sqlite_cls)
    postgres_methods = _public_callables(postgres_cls)
    assert sqlite_methods.keys() <= postgres_methods.keys()
    for name in sqlite_methods.keys() & postgres_methods.keys():
        assert type(sqlite_methods[name]) is type(postgres_methods[name])
        assert _signature_shape(getattr(sqlite_cls, name)) == _signature_shape(
            getattr(postgres_cls, name)
        )


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


def _seed_catalog(database, backend: str) -> None:
    if backend == "postgres":
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
        values = lambda *items: items
    else:
        user_sql = (
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES (?,?,?,?, 'active',?,?,?,?,?,?)"
        )
        notebook_sql = (
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES (?,?,'','thermal','ready',?,?,?,?)"
        )
        source_sql = (
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,summary,created_at,updated_at) "
            "VALUES (?,?,?,'file','ready','ready',?,?,?,?)"
        )
        user_values = (
            "user-golden",
            "golden@example.test",
            "Golden",
            "admin",
            NOW,
            NOW,
            "g00123456",
            "",
            "",
            0,
        )
        values = lambda *items: items

    with database.write() as connection:
        connection.execute(
            user_sql,
            values(*user_values),
        )
        for notebook_id, tier in (("nb-personal", "personal"), ("nb-base", "base")):
            connection.execute(
                notebook_sql,
                values(notebook_id, notebook_id, "user-golden", NOW, NOW, tier),
            )
        connection.execute(
            source_sql,
            values(
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
    backend: str
    database: object
    knowledge: object
    governance: object
    embedding: object


@pytest.fixture(
    params=("sqlite", pytest.param("postgres", marks=pytest.mark.postgres_integration))
)
def knowledge_harness(request, tmp_path) -> KnowledgeHarness:
    seams = _seams()
    if request.param == "sqlite":
        from app.repositories.sqlite.database import SqliteDatabase
        from app.repositories.sqlite.migrations import SqliteMigrator

        settings = Settings(database_url=f"sqlite:///{tmp_path / 'knowledge-golden.db'}")
        database = SqliteDatabase(settings, tmp_path)
        SqliteMigrator(database, settings).initialize()
        _seed_catalog(database, "sqlite")
        harness = KnowledgeHarness(
            backend="sqlite",
            database=database,
            knowledge=SqliteKnowledgeStore(database, seams),
            governance=SqliteGovernanceStore(database, seams),
            embedding=SqliteEmbeddingStore(write=database.write),
        )
        try:
            yield harness
        finally:
            database.close_local()
        return

    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 6
    _seed_catalog(database, "postgres")
    yield KnowledgeHarness(
        backend="postgres",
        database=database,
        knowledge=PostgresKnowledgeStore(database, seams),
        governance=PostgresGovernanceStore(database, seams),
        embedding=PostgresEmbeddingStore(write=database.write),
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


def test_merge_candidate_batches_preserve_sqlite_rowid_ordinal(knowledge_harness):
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


@pytest.mark.postgres_integration
def test_postgres_embedding_bytea_roundtrip_and_fail_closed_validation(
    postgres_database,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 6
    _seed_catalog(postgres_database, "postgres")
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

    assert PostgresMigrator(postgres_database).migrate() == 6
    _seed_catalog(postgres_database, "postgres")
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
def test_postgres_raw_graph_rows_keep_sqlite_json_text_contract(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 6
    _seed_catalog(postgres_database, "postgres")
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
def test_postgres_query_store_multi_notebook_count_placeholders(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 6
    _seed_catalog(postgres_database, "postgres")
    rows = PostgresQueryStore(postgres_database).list_user_notebooks("user-golden")
    assert {row["id"] for row in rows} == {"nb-base", "nb-personal"}
    assert {row["id"]: row["sources"] for row in rows} == {
        "nb-base": 0,
        "nb-personal": 1,
    }


@pytest.mark.postgres_integration
def test_postgres_unified_kg_temp_search_and_checkpoint_json(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 6
    _seed_catalog(postgres_database, "postgres")
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

    assert PostgresMigrator(postgres_database).migrate() == 6
    _seed_catalog(postgres_database, "postgres")
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
