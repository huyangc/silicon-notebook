from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.repositories.ports import ChunkWrite
from app.repositories.postgres.search import (
    PAYLOAD_NAME_EXPRESSION,
    TAGS_JSON_EXPRESSION,
    chunk_candidate_rows_for_terms,
    knowledge_candidate_rows_for_terms,
    lexical_candidate_sql,
    like_contains_pattern,
)


NOW = "2026-07-22T00:00:00+00:00"
pytestmark = pytest.mark.postgres_integration


EXPECTED_IDS = tuple(f"ko-search-{index:02d}" for index in range(10))
EXPECTED_CHUNK_IDS = tuple(f"chunk-search-{index:02d}" for index in range(10))
EXPECTED_SOURCE_IDS = {"source-search-a", "source-search-b"}
EXPECTED_ELEMENT_IDS = {"element-search-a", "element-search-b"}


def test_json_search_expressions_match_postgres_gin_indexes_exactly():
    assert PAYLOAD_NAME_EXPRESSION == '(payload ->> \'name\') COLLATE "C"'
    assert TAGS_JSON_EXPRESSION == '(tags_json::text) COLLATE "C"'


def test_lexical_candidate_sql_is_bounded_and_deterministic():
    statement = lexical_candidate_sql(
        table="knowledge_objects",
        id_column="id",
        text_expression=PAYLOAD_NAME_EXPRESSION,
    )
    assert "public.similarity(" in statement
    assert "LIMIT %s" in statement
    assert '"id" COLLATE "C"' in statement
    assert "?" not in statement


def test_multi_term_candidates_use_one_lateral_query_with_exact_quota():
    class _Result:
        @staticmethod
        def fetchall():
            return []

    class _Connection:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((statement, params))
            return _Result()

    connection = _Connection()
    knowledge_candidate_rows_for_terms(
        connection, "nb", ["thermal", "ZXCV9000"], per_term_limit=2
    )

    assert len(connection.calls) == 1
    statement, params = connection.calls[0]
    assert "CROSS JOIN LATERAL" in statement
    assert statement.count("LIMIT %s") == 3
    assert " UNION " in statement
    assert " OPERATOR(public.%%) lexical_terms.term OR " not in statement
    assert params == [
        0,
        "thermal",
        "%thermal%",
        1,
        "ZXCV9000",
        "%ZXCV9000%",
        "nb",
        2,
        "nb",
        2,
        2,
    ]


def test_source_scope_is_inside_each_postgres_candidate_limit():
    class _Result:
        @staticmethod
        def fetchall():
            return []

    class _Connection:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((statement, params))
            return _Result()

    connection = _Connection()
    knowledge_candidate_rows_for_terms(
        connection, "nb", ["target command"], per_term_limit=2,
        allowed_source_ids=["A", "B"],
    )
    statement, params = connection.calls[0]
    lateral = statement.split("CROSS JOIN LATERAL", 1)[1]
    assert "EXISTS (SELECT 1 FROM knowledge_object_sources" in lateral
    assert lateral.index("knowledge_object_sources") < lateral.index("LIMIT %s")
    assert params == [
        0, "target command", "%target command%",
        "nb", ["A", "B"], 2,
        "nb", ["A", "B"], 2,
        2,
    ]

    connection = _Connection()
    knowledge_candidate_rows_for_terms(
        connection, "nb", ["target command"], per_term_limit=2,
        allowed_source_ids=["A", "B"], authoritative_source_filter=True,
    )
    statement, params = connection.calls[0]
    lateral = statement.split("CROSS JOIN LATERAL", 1)[1]
    assert "jsonb_array_elements" in lateral
    assert "knowledge_object_sources" not in lateral
    assert lateral.index("jsonb_array_elements") < lateral.index("LIMIT %s")
    assert params == [
        0, "target command", "%target command%",
        "nb", ["A", "B"], 2,
        "nb", ["A", "B"], 2,
        2,
    ]

    connection = _Connection()
    chunk_candidate_rows_for_terms(
        connection, "nb", ["target command"], per_term_limit=2,
        allowed_source_ids=["A", "B"],
    )
    statement, params = connection.calls[0]
    lateral = statement.split("CROSS JOIN LATERAL", 1)[1]
    assert '"chunks".source_id=ANY(%s)' in lateral
    assert lateral.index("source_id=ANY(%s)") < lateral.index("LIMIT %s")
    assert params == [
        0, "target command", "%target command%", "nb", ["A", "B"], 2
    ]


def test_like_contains_pattern_keeps_metacharacters_literal():
    # The trigram arm keeps the raw term; only the LIKE arm is escaped.
    assert like_contains_pattern("plain") == "%plain%"
    assert like_contains_pattern("set_db") == r"%set\_db%"
    assert like_contains_pattern("100%") == r"%100\%%"
    assert like_contains_pattern("a\\b") == r"%a\\b%"


def test_lateral_probe_escapes_only_the_like_arm():
    class _Result:
        @staticmethod
        def fetchall():
            return []

    class _Connection:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((statement, params))
            return _Result()

    connection = _Connection()
    knowledge_candidate_rows_for_terms(connection, "nb", ["set_db"], per_term_limit=2)

    statement, params = connection.calls[0]
    assert "ILIKE lexical_terms.like_pattern ESCAPE '\\'" in statement
    assert "'%' || lexical_terms.term" not in statement
    assert "OPERATOR(public.%%) lexical_terms.term" in statement
    assert " OPERATOR(public.%%) lexical_terms.term OR " not in statement
    assert params == [
        0, "set_db", r"%set\_db%", "nb", 2, "nb", 2, 2
    ]


def test_knowledge_knn_routes_only_short_non_cjk_terms_and_keeps_ranks(
    monkeypatch,
):
    from app.repositories.postgres import search as search_module

    calls = []

    def fake_knn(_connection, _notebook_id, ranked_terms, _limit, routing_stats):
        calls.append(("knn", ranked_terms))
        routing_stats["knn_short_fallback_term_count"] = 0
        return [{
            "candidate_id": "latin",
            "term_rank": ranked_terms[0][0],
            "candidate_similarity": 0.8,
        }]

    def fake_legacy(_connection, **kwargs):
        calls.append(("legacy", kwargs["ranked_terms"]))
        return [
            {
                "candidate_id": f"legacy-{rank}",
                "term_rank": rank,
                "candidate_similarity": 0.7,
            }
            for rank, _term in kwargs["ranked_terms"]
        ]

    monkeypatch.setattr(search_module, "knn_name_index_available", lambda _c: True)
    monkeypatch.setattr(search_module, "_knowledge_rows_via_knn", fake_knn)
    monkeypatch.setattr(search_module, "_candidate_rows_for_terms", fake_legacy)
    stats = {}

    rows = knowledge_candidate_rows_for_terms(
        object(),
        "nb",
        ["thermal", "热设计", "latin-term-too-long"],
        per_term_limit=2,
        allow_knn=True,
        knn_max_term_chars=8,
        routing_stats=stats,
    )

    assert calls == [
        ("knn", [(0, "thermal")]),
        ("legacy", [(1, "热设计"), (2, "latin-term-too-long")]),
    ]
    assert [row["term_rank"] for row in rows] == [0, 1, 2]
    assert stats["term_count"] == 3
    assert stats["knn_term_count"] == 1
    assert stats["legacy_term_count"] == 2


def test_all_non_knn_terms_skip_the_ordered_knn_availability_probe(monkeypatch):
    from app.repositories.postgres import search as search_module

    def unexpected_probe(_connection):
        pytest.fail("CJK/over-limit terms must not enter ordered KNN")

    seen = []

    def fake_legacy(_connection, **kwargs):
        seen.extend(kwargs["terms"])
        return []

    monkeypatch.setattr(search_module, "knn_name_index_available", unexpected_probe)
    monkeypatch.setattr(search_module, "_candidate_rows_for_terms", fake_legacy)

    knowledge_candidate_rows_for_terms(
        object(),
        "nb",
        ["热设计", "latin-term-too-long"],
        per_term_limit=2,
        allow_knn=True,
        knn_max_term_chars=8,
    )

    assert seen == ["热设计", "latin-term-too-long"]


@dataclass
class SearchHarness:
    database: object
    knowledge: object
    chunks: object
    queries: object


def _seed_catalog(harness: SearchHarness) -> None:
    from psycopg.types.json import Jsonb

    user_sql = (
        "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
        "username,password_hash,password_salt,password_iterations) "
        "VALUES (%s,%s,%s,'admin','active',%s,%s,%s,'','',0)"
    )
    notebook_sql = (
        "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
        "created_at,updated_at,tier) "
        "VALUES (%s,%s,'','semiconductor','ready',%s,%s,%s,'personal')"
    )
    source_sql = (
        "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
        "file_name,summary,created_at,updated_at) "
        "VALUES (%s,%s,%s,'file','ready','ready',%s,%s,%s,%s)"
    )
    element_sql = (
        "INSERT INTO source_elements(id,source_id,element_type,location_label,text,"
        "metadata,created_at) VALUES (%s,%s,'paragraph',%s,%s,%s,%s)"
    )
    metadata = Jsonb({"kind": "golden"})

    with harness.database.write() as connection:
        connection.execute(
            user_sql,
            (
                "user-search",
                "search@example.test",
                "Search",
                NOW,
                NOW,
                "s00123456",
            ),
        )
        connection.execute(
            notebook_sql,
            ("nb-search", "Search Golden", "user-search", NOW, NOW),
        )
        source_rows = (
            (
                "source-search-a",
                "nb-search",
                "Thermal handbook 热设计手册",
                "thermal-a.md",
                "thermal 热设计 source A",
                NOW,
                NOW,
            ),
            (
                "source-search-b",
                "nb-search",
                "Reference B",
                "thermal-b.md",
                "thermal 热设计 source B",
                NOW,
                NOW,
            ),
        )
        for source_row in source_rows:
            connection.execute(source_sql, source_row)
        connection.execute(
            element_sql,
            (
                "element-search-a",
                "source-search-a",
                "§A",
                "thermal 热设计 evidence A",
                metadata,
                NOW,
            ),
        )
        connection.execute(
            element_sql,
            (
                "element-search-b",
                "source-search-b",
                "§B",
                "thermal 热设计 evidence B",
                metadata,
                NOW,
            ),
        )

        object_rows = []
        fts_rows = []
        statuses = ("approved", "reviewed", "project_specific", "conflict")
        for index, object_id in enumerate(EXPECTED_IDS):
            source_suffix = "a" if index % 2 == 0 else "b"
            source_id = f"source-search-{source_suffix}"
            element_id = f"element-search-{source_suffix}"
            payload = {
                "name": f"thermal 热设计 principle {index:02d}",
                "statement": f"golden relevance {index:02d}",
            }
            evidence = [
                {
                    "source_id": source_id,
                    "source_title": f"source {source_suffix}",
                    "element_id": element_id,
                    "element_type": "paragraph",
                    "location_label": f"§{source_suffix.upper()}",
                    "quoted_span": f"thermal 热设计 evidence {source_suffix}",
                    "confidence": 1.0,
                }
            ]
            object_rows.append(
                (
                    object_id,
                    "nb-search",
                    "claim",
                    statuses[index % len(statuses)],
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    source_id,
                    NOW,
                    NOW,
                )
            )
            fts_rows.append((object_id, "nb-search", payload["name"]))

        object_rows.extend(
            (
                (
                    "ko-search-distractor",
                    "nb-search",
                    "claim",
                    "approved",
                    json.dumps({"name": "unrelated power integrity"}),
                    "[]",
                    "source-search-a",
                    NOW,
                    NOW,
                ),
                (
                    "ko-search-deprecated",
                    "nb-search",
                    "claim",
                    "deprecated",
                    json.dumps({"name": "thermal 热设计 obsolete"}, ensure_ascii=False),
                    "[]",
                    "source-search-a",
                    NOW,
                    NOW,
                ),
            )
        )
        harness.knowledge.insert_object_chunk(connection, object_rows)
        harness.knowledge.insert_kg_fts_rows(connection, fts_rows)

    chunks = [
        ChunkWrite(
            id=chunk_id,
            text=f"thermal 热设计 chunk evidence {index:02d}",
            section_path=f"§{index:02d}",
            element_ids=(
                "element-search-a" if index % 2 == 0 else "element-search-b",
            ),
        )
        for index, chunk_id in enumerate(EXPECTED_CHUNK_IDS)
    ]
    chunks.append(
        ChunkWrite(
            id="chunk-search-distractor",
            text="unrelated signal integrity",
            section_path="§X",
            element_ids=("element-search-a",),
        )
    )
    harness.chunks.replace_source_chunks(
        "source-search-a", "nb-search", chunks, created_at=NOW
    )


@pytest.fixture
def search_harness(postgres_database) -> SearchHarness:
    from app.repositories.postgres.chunk_store import ChunkStore as PostgresChunkStore
    from app.repositories.postgres.knowledge_store import KnowledgeStore as PostgresKnowledgeStore
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.query_store import QueryStore as PostgresQueryStore
    from app.services.repository_runtime import RepositoryCompatibilitySeams

    assert PostgresMigrator(postgres_database).migrate() == 46
    seams = RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: NOW,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )
    harness = SearchHarness(
        database=postgres_database,
        knowledge=PostgresKnowledgeStore(postgres_database, seams),
        chunks=PostgresChunkStore(postgres_database),
        queries=PostgresQueryStore(postgres_database),
    )
    _seed_catalog(harness)
    return harness


def _recall(ids: list[str], expected: tuple[str, ...]) -> float:
    return len(set(ids) & set(expected)) / len(expected)


@pytest.mark.postgres_integration
@pytest.mark.parametrize("query", ("thermal", "热设计"))
def test_zh_en_search_quality_and_citation_identity(
    search_harness, query
):
    with search_harness.database.connect() as connection:
        knowledge_ids = [
            row["object_id"]
            for row in search_harness.knowledge.fts_search(
                connection, "nb-search", query, 12
            )
        ]
        chunk_ids = [
            row["chunk_id"]
            for row in search_harness.knowledge.chunk_fts_search(
                connection, "nb-search", query, 12
            )
        ]
        objects = search_harness.knowledge.retrieval_objects(
            connection,
            "nb-search",
            "claim",
            ("approved", "reviewed", "project_specific", "conflict"),
            EXPECTED_IDS,
        )
    assert {item["id"] for item in objects} == set(EXPECTED_IDS)
    assert {item["evidence"][0].source_id for item in objects} == EXPECTED_SOURCE_IDS
    assert {item["evidence"][0].element_id for item in objects} == EXPECTED_ELEMENT_IDS

    notebook_search = search_harness.queries.search_notebook("nb-search", query)
    source_ids = {
        hit.source_id for hit in notebook_search.hits if hit.scope == "Source"
    }
    element_ids = {
        hit.element_id for hit in notebook_search.hits if hit.scope == "Element"
    }
    assert source_ids == EXPECTED_SOURCE_IDS
    assert element_ids == EXPECTED_ELEMENT_IDS
    assert _recall(knowledge_ids, EXPECTED_IDS) == 1.0
    assert _recall(chunk_ids, EXPECTED_CHUNK_IDS) == 1.0


@pytest.mark.postgres_integration
def test_like_metacharacters_do_not_widen_the_candidate_probe(search_harness):
    """`set_db` must not admit `setXdb`/`set db` through the ILIKE arm.

    All three rows sit below the trigram similarity threshold for this term, so
    the LIKE arm alone decides membership and the escaping is what is measured.
    """
    probe_chunks = [
        ChunkWrite(
            id="chunk-like-literal",
            text="netlist set_db max_transition guidance",
            section_path="§L",
            element_ids=("element-search-b",),
        ),
        ChunkWrite(
            id="chunk-like-wildcard",
            text="netlist setXdb max_transition guidance",
            section_path="§W",
            element_ids=("element-search-b",),
        ),
        ChunkWrite(
            id="chunk-like-spaced",
            text="netlist set db max_transition guidance",
            section_path="§S",
            element_ids=("element-search-b",),
        ),
    ]
    search_harness.chunks.replace_source_chunks(
        "source-search-b", "nb-search", probe_chunks, created_at=NOW
    )

    with search_harness.database.connect() as connection:
        rows = chunk_candidate_rows_for_terms(
            connection, "nb-search", ["set_db"], per_term_limit=10
        )
        widened = connection.execute(
            "SELECT id FROM chunks WHERE notebook_id=%s AND text ILIKE %s",
            ("nb-search", "%set_db%"),
        ).fetchall()

    # Without escaping the same pattern is a single-character wildcard, so the
    # probe would have pulled all three rows in.
    assert {row["id"] for row in widened} == {
        "chunk-like-literal",
        "chunk-like-wildcard",
        "chunk-like-spaced",
    }
    candidate_ids = {row["candidate_id"] for row in rows}
    assert "chunk-like-literal" in candidate_ids
    assert "chunk-like-wildcard" not in candidate_ids
    assert "chunk-like-spaced" not in candidate_ids


@pytest.mark.postgres_integration
def test_search_expression_indexes_match_catalog_and_are_planner_usable(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 46
    with postgres_database.connect() as connection:
        rows = connection.execute(
            "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema() "
            "AND indexname=ANY(%s)",
            (
                [
                    "idx_chunks_text_trgm",
                    "idx_knowledge_objects_name_trgm",
                    "idx_memory_items_tags_trgm",
                ],
            ),
        ).fetchall()
        definitions = {row["indexname"]: row["indexdef"] for row in rows}
        assert set(definitions) == {
            "idx_chunks_text_trgm",
            "idx_knowledge_objects_name_trgm",
            "idx_memory_items_tags_trgm",
        }
        assert "gin_trgm_ops" in definitions["idx_chunks_text_trgm"]
        assert "payload ->> 'name'::text" in definitions["idx_knowledge_objects_name_trgm"]
        assert "COLLATE \"C\"" in definitions["idx_knowledge_objects_name_trgm"]
        assert "tags_json)::text" in definitions["idx_memory_items_tags_trgm"]
        assert "COLLATE \"C\"" in definitions["idx_memory_items_tags_trgm"]

        connection.execute("SET LOCAL enable_seqscan=off")
        plan_rows = connection.execute(
            "EXPLAIN SELECT id FROM knowledge_objects WHERE "
            + PAYLOAD_NAME_EXPRESSION
            + " OPERATOR(public.%%) %s",
            ("thermal",),
        ).fetchall()
    plan = "\n".join(str(next(iter(row.values()))) for row in plan_rows)
    assert "idx_knowledge_objects_name_trgm" in plan


@pytest.mark.xdist_group(name="postgres_retrieval_indexes")
def test_notebook_aware_trigram_indexes_preserve_candidates_and_prune_in_index(
    search_harness,
):
    from app.repositories.postgres.retrieval_indexes import (
        install_retrieval_indexes,
        ready_index_names,
    )

    def candidates():
        with search_harness.database.connect() as connection:
            objects = knowledge_candidate_rows_for_terms(
                connection, "nb-search", ["thermal"], per_term_limit=20
            )
            chunks = chunk_candidate_rows_for_terms(
                connection, "nb-search", ["thermal"], per_term_limit=20
            )
        return (
            [(row["candidate_id"], row["term_rank"], row["candidate_similarity"])
             for row in objects],
            [(row["candidate_id"], row["term_rank"], row["candidate_similarity"])
             for row in chunks],
        )

    before = candidates()
    with search_harness.database.connect() as connection:
        schema = connection.execute("SELECT current_schema() AS name").fetchone()["name"]
    state = install_retrieval_indexes(
        search_harness.database.settings.database_url,
        schema=schema,
    )
    repeated_state = install_retrieval_indexes(
        search_harness.database.settings.database_url,
        schema=schema,
    )
    after = candidates()

    assert after == before
    assert ready_index_names(state) == {
        "idx_knowledge_objects_nb_name_trgm",
        "idx_chunks_nb_text_trgm",
    }
    assert ready_index_names(repeated_state) == ready_index_names(state)

    # Planner usability is a separate assertion from result equivalence. Remove
    # competing legacy/notebook-only indexes inside this disposable schema so a
    # future opclass/expression drift cannot pass merely because another index was
    # usable. Production planner choice is data-distribution dependent.
    with search_harness.database.write() as connection:
        for index_name in (
            "idx_knowledge_objects_name_trgm",
            "idx_knowledge_objects_nb_status",
            "idx_knowledge_objects_nb_type_created",
            "idx_knowledge_objects_nb_type_status",
            "idx_knowledge_objects_nb_updated",
            "idx_chunks_text_trgm",
            "idx_chunks_nb",
            "idx_chunks_nb_created",
        ):
            connection.execute(f'DROP INDEX IF EXISTS "{index_name}"')

    with search_harness.database.connect() as connection:
        connection.execute("SET LOCAL enable_seqscan=off")
        object_plan = connection.execute(
            "EXPLAIN SELECT id FROM knowledge_objects WHERE notebook_id=%s "
            "AND status!='deprecated' AND (" + PAYLOAD_NAME_EXPRESSION
            + " OPERATOR(public.%%) %s OR " + PAYLOAD_NAME_EXPRESSION
            + " ILIKE %s)",
            ("nb-search", "thermal", "%thermal%"),
        ).fetchall()
        chunk_plan = connection.execute(
            "EXPLAIN SELECT id FROM chunks WHERE notebook_id=%s "
            "AND (text OPERATOR(public.%%) %s OR text ILIKE %s)",
            ("nb-search", "thermal", "%thermal%"),
        ).fetchall()
    assert "idx_knowledge_objects_nb_name_trgm" in "\n".join(
        str(next(iter(row.values()))) for row in object_plan
    )
    assert "idx_chunks_nb_text_trgm" in "\n".join(
        str(next(iter(row.values()))) for row in chunk_plan
    )


@pytest.mark.parametrize(
    "unexpected_ddl",
    [
        "CREATE INDEX idx_knowledge_objects_nb_name_trgm "
        "ON knowledge_objects USING gin (notebook_id public.text_ops, "
        "(((payload->>'name') COLLATE \"POSIX\")) public.gin_trgm_ops) "
        "WHERE status!='deprecated'",
        "CREATE INDEX idx_knowledge_objects_nb_name_trgm "
        "ON knowledge_objects USING gin (notebook_id public.text_ops, "
        "(((payload->>'name') COLLATE \"C\")) public.gin_trgm_ops) "
        "WHERE status!='deprecated' AND object_type='concept'",
    ],
)
@pytest.mark.xdist_group(name="postgres_retrieval_indexes")
def test_retrieval_index_installer_rejects_semantically_different_owned_name(
    search_harness, unexpected_ddl,
):
    from app.repositories.postgres.retrieval_indexes import (
        RetrievalIndexError,
        inspect_retrieval_indexes,
        install_retrieval_indexes,
    )

    with search_harness.database.connect() as connection:
        schema = connection.execute("SELECT current_schema() AS name").fetchone()["name"]
    install_retrieval_indexes(
        search_harness.database.settings.database_url, schema=schema
    )
    with search_harness.database.write() as connection:
        connection.execute("DROP INDEX idx_knowledge_objects_nb_name_trgm")
        connection.execute(unexpected_ddl)

    state = inspect_retrieval_indexes(
        search_harness.database.settings.database_url, schema=schema
    )
    row = next(
        item for item in state["indexes"]
        if item["name"] == "idx_knowledge_objects_nb_name_trgm"
    )
    assert row["state"] == "unexpected"
    with pytest.raises(
        RetrievalIndexError,
        match="unexpected_index_definition:idx_knowledge_objects_nb_name_trgm",
    ):
        install_retrieval_indexes(
            search_harness.database.settings.database_url, schema=schema
        )
    # A same-named DBA definition is fail-closed, never repaired/dropped as if
    # it were this tool's interrupted artifact.
    with search_harness.database.connect() as connection:
        assert connection.execute(
            "SELECT to_regclass(%s) AS name",
            (f'{schema}.idx_knowledge_objects_nb_name_trgm',),
        ).fetchone()["name"] is not None


@pytest.mark.xdist_group(name="postgres_retrieval_indexes")
def test_drop_legacy_refuses_custom_definition_before_dropping_anything(
    search_harness,
):
    from app.repositories.postgres.retrieval_indexes import (
        RetrievalIndexError,
        install_retrieval_indexes,
    )

    with search_harness.database.connect() as connection:
        schema = connection.execute("SELECT current_schema() AS name").fetchone()["name"]
    install_retrieval_indexes(
        search_harness.database.settings.database_url, schema=schema
    )
    with search_harness.database.write() as connection:
        connection.execute("DROP INDEX idx_chunks_text_trgm")
        connection.execute(
            "CREATE INDEX idx_chunks_text_trgm ON knowledge_objects USING gin "
            "((((payload->>'name') COLLATE \"C\")) public.gin_trgm_ops)"
        )

    with pytest.raises(
        RetrievalIndexError,
        match="unexpected_legacy_index_definition:idx_chunks_text_trgm",
    ):
        install_retrieval_indexes(
            search_harness.database.settings.database_url,
            schema=schema,
            drop_legacy=True,
        )

    with search_harness.database.connect() as connection:
        names = {
            row["indexname"]
            for row in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname=%s "
                "AND indexname=ANY(%s)",
                (
                    schema,
                    ["idx_knowledge_objects_name_trgm", "idx_chunks_text_trgm"],
                ),
            ).fetchall()
        }
    assert names == {"idx_knowledge_objects_name_trgm", "idx_chunks_text_trgm"}


# ------------------------------- exact-identifier fast path (PostgreSQL half)
# The SQLite half of these lives in tests/test_exact_lookup.py. Both adapters
# must agree, and only these can catch a PostgreSQL-only regression: the whole
# risk here is a *silent* precision loss (LIKE reading `_` as a wildcard), which
# raises nothing and merely returns extra rows.
MANUAL_CHUNKS = (
    ("chunk-manual-main", "Manual > Commands > set_db",
     "[Commands > set_db] set_db configures a database property."),
    ("chunk-manual-args", "Manual > Commands > set_db > Arguments",
     "[set_db > Arguments] -name property name. -value property value."),
    ("chunk-manual-examples", "Manual > Commands > set_db > Examples",
     "[set_db > Examples] see the script snippet below."),
    ("chunk-manual-other", "Manual > Commands > report_timing",
     "[Commands > report_timing] report_timing prints a timing report."),
    ("chunk-manual-decoy", "Manual > Notes",
     "[Manual > Notes] setXdb is a similarly spelled historical alias."),
    ("chunk-manual-split", "Manual > Notes",
     "[Manual > Notes] set the db value by hand."),
)


def _seed_manual(harness, rows=MANUAL_CHUNKS, source_id="source-search-b"):
    harness.chunks.replace_source_chunks(
        source_id,
        "nb-search",
        [
            ChunkWrite(id=chunk_id, text=text, section_path=section_path,
                       element_ids=("element-search-b",))
            for chunk_id, section_path, text in rows
        ],
        created_at=NOW,
    )


@pytest.mark.postgres_integration
def test_chunk_exact_search_treats_underscore_literally(search_harness):
    """`_` is LIKE's single-character wildcard and every command name has one.

    `setXdb` is the whole point of this test: without escaping it matches, and
    nothing anywhere would report an error.
    """
    _seed_manual(search_harness)
    with search_harness.database.connect() as connection:
        hits = search_harness.knowledge.chunk_exact_search(
            connection, "nb-search", "set_db", 50)

    ids = {hit["chunk_id"] for hit in hits}
    assert {"chunk-manual-main", "chunk-manual-args", "chunk-manual-examples"} <= ids
    assert "chunk-manual-decoy" not in ids, "unescaped `_` matched `setXdb`"
    assert "chunk-manual-other" not in ids
    by_id = {hit["chunk_id"]: hit for hit in hits}
    assert by_id["chunk-manual-args"]["source_id"] == "source-search-b"
    assert (by_id["chunk-manual-args"]["section_path"]
            == "Manual > Commands > set_db > Arguments")


@pytest.mark.postgres_integration
def test_chunk_exact_search_does_not_decompose_the_needle(search_harness):
    """`chunk_fts_search` ORs `set`/`db` apart; the exact probe must not."""
    _seed_manual(search_harness)
    with search_harness.database.connect() as connection:
        union = search_harness.knowledge.chunk_fts_search(
            connection, "nb-search", "set_db", 50)
        exact = search_harness.knowledge.chunk_exact_search(
            connection, "nb-search", "set_db", 50)
    assert "chunk-manual-split" in {hit["chunk_id"] for hit in union}
    assert "chunk-manual-split" not in {hit["chunk_id"] for hit in exact}


@pytest.mark.postgres_integration
def test_chunk_exact_search_is_notebook_scoped_and_bounded(search_harness):
    _seed_manual(search_harness)
    with search_harness.database.connect() as connection:
        assert search_harness.knowledge.chunk_exact_search(
            connection, "nb-absent", "set_db", 50) == []
        assert len(search_harness.knowledge.chunk_exact_search(
            connection, "nb-search", "set_db", 2)) == 2
        assert search_harness.knowledge.chunk_exact_search(
            connection, "nb-search", "set_db", 0) == []


@pytest.mark.postgres_integration
def test_chunks_by_section_takes_the_subtree_in_document_order(search_harness):
    _seed_manual(search_harness)
    with search_harness.database.connect() as connection:
        rows = search_harness.chunks.chunks_by_section(
            connection, "nb-search", "source-search-b",
            "Manual > Commands > set_db", 12)
        capped = search_harness.chunks.chunks_by_section(
            connection, "nb-search", "source-search-b",
            "Manual > Commands > set_db", 2)
        wrong_notebook = search_harness.chunks.chunks_by_section(
            connection, "nb-absent", "source-search-b",
            "Manual > Commands > set_db", 12)
        empty_path = search_harness.chunks.chunks_by_section(
            connection, "nb-search", "source-search-b", "", 12)

    assert [row["id"] for row in rows] == [
        "chunk-manual-main", "chunk-manual-args", "chunk-manual-examples"]
    assert rows[0]["source_title"] == "Reference B"
    assert json.loads(rows[0]["element_ids"]) == ["element-search-b"]
    assert [row["id"] for row in capped] == ["chunk-manual-main", "chunk-manual-args"]
    assert wrong_notebook == []
    assert empty_path == []


@pytest.mark.postgres_integration
def test_chunks_by_section_escapes_like_wildcards(search_harness):
    """A section literally named `a_b` must not also drag in `axb`."""
    _seed_manual(search_harness, rows=(
        ("chunk-under", "a_b", "real section"),
        ("chunk-under-child", "a_b > Leaf", "real child"),
        ("chunk-wild", "axb > Leaf", "unrelated section"),
    ))
    with search_harness.database.connect() as connection:
        rows = search_harness.chunks.chunks_by_section(
            connection, "nb-search", "source-search-b", "a_b", 12)
    assert [row["id"] for row in rows] == ["chunk-under", "chunk-under-child"]


_EXACT_ROW_FIELDS = ("id", "source_id", "text", "section_path",
                     "element_ids", "source_title")


@pytest.mark.postgres_integration
def test_hydrate_rows_matches_the_section_row_shape_for_the_exact_channel(
        search_harness):
    """精确通道的两条取数分支必须给出同一种行。

    有面包屑的库按小节整节取齐;没有面包屑的库(MinerU 解析的 PDF/DOCX)按命中
    id 直接取行——两条分支的结果落进同一个 `_build_chunks`,行形状不一致会在
    生产上静默少列。SQLite 侧的对等断言在 tests/test_exact_lookup.py。
    """
    _seed_manual(search_harness)
    with search_harness.database.connect() as connection:
        section = search_harness.chunks.chunks_by_section(
            connection, "nb-search", "source-search-b",
            "Manual > Commands > set_db", 12)
        hydrated = search_harness.chunks.hydrate_rows(
            connection, ["chunk-manual-args", "chunk-manual-main"])

    by_section = {row["id"]: dict(row) for row in section}
    by_id = {row["id"]: dict(row) for row in hydrated}
    assert set(by_id) == {"chunk-manual-args", "chunk-manual-main"}
    for chunk_id, row in by_id.items():
        assert set(row) >= set(_EXACT_ROW_FIELDS), row
        assert ({field: row[field] for field in _EXACT_ROW_FIELDS}
                == {field: by_section[chunk_id][field]
                    for field in _EXACT_ROW_FIELDS})
    # element_ids 两条分支都归一成 JSON 字符串形态(`_build_chunks` 依赖这一点)。
    assert json.loads(by_id["chunk-manual-args"]["element_ids"]) == [
        "element-search-b"]


@pytest.mark.postgres_integration
def test_exact_chunk_probe_is_planner_usable_on_the_trigram_index(postgres_database):
    """Bounded is not enough — the probe must ride `idx_chunks_text_trgm`."""
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 46
    with postgres_database.connect() as connection:
        connection.execute("SET LOCAL enable_seqscan=off")
        plan_rows = connection.execute(
            "EXPLAIN SELECT id FROM chunks WHERE text ILIKE %s", ("%set\\_db%",)
        ).fetchall()
    plan = "\n".join(str(next(iter(row.values()))) for row in plan_rows)
    assert "idx_chunks_text_trgm" in plan


# ── corpus-language gate on the lexical candidate probe ───────────────

_LONG_CHINESE_QUERY = (
    "请围绕这份资料回答：关于时序收敛与布局布线优化的整体方法论是什么？"
    "必须覆盖以下主题：时钟树综合的关键步骤、静态时序分析的约束建模、"
    "拥塞驱动的布局策略、以及物理验证阶段的常见失败模式。"
    "请给出结构化的对比与结论，并说明每个结论的依据来源。"
    "重点关注 set_db 与 place_opt_design 在 timing closure 流程中的作用。"
)


class _ProbeCountingConnection:
    """Delegates to a real connection while counting LATERAL probe terms.

    The probe count is read off the SQL that PostgreSQL actually executed, so
    it measures the adapter, not the helper the assertions also call.
    """

    def __init__(self, connection):
        self._connection = connection
        self.lateral_probes = 0

    def execute(self, statement, params=None):
        if "CROSS JOIN LATERAL" in statement:
            self.lateral_probes = statement.count("(%s,%s,%s)")
            assert self.lateral_probes, "expected an inline VALUES term list"
        return (self._connection.execute(statement, params)
                if params is not None else self._connection.execute(statement))


def _seed_latin_only_notebook(harness: SearchHarness) -> None:
    """A second notebook holding no CJK character at all."""
    with harness.database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
            "created_by,created_at,updated_at,tier) "
            "VALUES (%s,%s,'','semiconductor','ready',%s,%s,%s,'personal')",
            ("nb-latin", "Latin only", "user-search", NOW, NOW),
        )
        connection.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,"
            "parse_status,file_name,summary,created_at,updated_at) "
            "VALUES (%s,%s,%s,'file','ready','ready',%s,%s,%s,%s)",
            ("source-latin", "nb-latin", "Manual", "manual.md", "", NOW, NOW),
        )
    harness.chunks.replace_source_chunks(
        "source-latin", "nb-latin",
        [
            ChunkWrite(
                id="chunk-latin-set-db",
                text="set_db configures the engine before place_opt_design runs",
                section_path="Manual > Commands",
                element_ids=(),
            ),
            ChunkWrite(
                id="chunk-latin-timing",
                text="timing closure needs placement optimization and CTS",
                section_path="Manual > Flow",
                element_ids=(),
            ),
            ChunkWrite(
                id="chunk-latin-distractor",
                text="unrelated power integrity discussion",
                section_path="Manual > Misc",
                element_ids=(),
            ),
        ],
        created_at=NOW,
    )


@pytest.mark.postgres_integration
def test_all_cjk_terms_return_nothing_from_a_latin_corpus(search_harness):
    """The proof the gate rests on, executed against a real PostgreSQL.

    Every trigram of an all-CJK term still contains a CJK character, so against
    a corpus holding none the intersection is empty: `%` cannot fire and ILIKE
    cannot substring-match. These probes are pure cost.
    """
    from app.repositories.lexical_query import lexical_recall_terms

    _seed_latin_only_notebook(search_harness)
    cjk_terms = [
        term for term in lexical_recall_terms(_LONG_CHINESE_QUERY)
        if term and all("㐀" <= char <= "鿿" for char in term)
    ]
    assert len(cjk_terms) >= 20, "expected the CJK fragment crowd"
    with search_harness.database.connect() as connection:
        rows = chunk_candidate_rows_for_terms(
            connection, "nb-latin", cjk_terms, per_term_limit=13
        )
    assert rows == []


@pytest.mark.postgres_integration
def test_corpus_gate_keeps_every_latin_hit_while_cutting_the_probe_count(
        search_harness):
    """Same rows, far fewer LATERAL probes — measured 64 -> 3 on 7,026 chunks."""
    from app.repositories.lexical_query import (
        corpus_gated_recall_terms,
        lexical_recall_terms,
    )

    _seed_latin_only_notebook(search_harness)
    ungated_terms = lexical_recall_terms(_LONG_CHINESE_QUERY)
    gated_terms = corpus_gated_recall_terms(_LONG_CHINESE_QUERY, ["en"])
    assert len(gated_terms) * 4 < len(ungated_terms)

    with search_harness.database.connect() as connection:
        ungated_probe = _ProbeCountingConnection(connection)
        ungated = search_harness.knowledge.chunk_fts_search(
            ungated_probe, "nb-latin", _LONG_CHINESE_QUERY, 12
        )
        gated_probe = _ProbeCountingConnection(connection)
        gated = search_harness.knowledge.chunk_fts_search(
            gated_probe, "nb-latin", _LONG_CHINESE_QUERY, 12,
            corpus_langs=["en"],
        )
    assert [row["chunk_id"] for row in gated] == [
        row["chunk_id"] for row in ungated
    ]
    assert "chunk-latin-set-db" in {row["chunk_id"] for row in gated}
    # The SQL that actually reached PostgreSQL has to carry the smaller term
    # crowd. Equal rows alone would still pass if the adapter accepted
    # `corpus_langs` and then quietly ignored it.
    assert ungated_probe.lateral_probes == len(ungated_terms)
    assert gated_probe.lateral_probes == len(gated_terms)


@pytest.mark.postgres_integration
def test_corpus_gate_leaves_a_bilingual_notebook_untouched(search_harness):
    """`nb-search` holds 热设计, so the gate must never fire for it."""
    with search_harness.database.connect() as connection:
        ungated = search_harness.knowledge.chunk_fts_search(
            connection, "nb-search", "热设计", 12
        )
        gated = search_harness.knowledge.chunk_fts_search(
            connection, "nb-search", "热设计", 12, corpus_langs=["zh", "en"]
        )
        objects_gated = search_harness.knowledge.fts_search(
            connection, "nb-search", "热设计", 12, corpus_langs=["zh", "en"]
        )
    assert [row["chunk_id"] for row in gated] == [
        row["chunk_id"] for row in ungated
    ]
    assert _recall([row["chunk_id"] for row in gated], EXPECTED_CHUNK_IDS) == 1.0
    assert _recall([row["object_id"] for row in objects_gated], EXPECTED_IDS) == 1.0


# ---------------------------------------------------------------------------
# GiST KNN early-stop (POSTGRES_LEXICAL_KNN_ENABLED) — adapter-level contract.
#
# The seed names form a similarity STAIRCASE against the probe term "thermal":
# each name is strictly longer, so every similarity is distinct and there are
# no equal-similarity ties.  That is what lets these tests demand bit-for-bit
# equality between the KNN and legacy paths — the registered tie-class trade
# (285 rows named "DAC" in production) is precisely the case these rows avoid,
# so any inequality here is a real defect, never tie noise.
# ---------------------------------------------------------------------------

KNN_NOTEBOOK = "nb-knn"
KNN_STAIRCASE = (
    ("ko-knn-00", "thermal"),
    ("ko-knn-01", "thermal pad"),
    ("ko-knn-02", "thermal pad grid"),
    ("ko-knn-03", "thermal pad grid matrix"),
    # Long enough that similarity('thermal', name) drops below the `%`
    # threshold: reachable through the ILIKE arm only — the row the KNN
    # phase cannot see and the top-up phase exists to recover.
    ("ko-knn-04", "thermal pad grid matrix layout extension appendix rev nine"),
)


def _seed_knn_staircase(harness) -> None:
    with harness.database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
            "created_by,created_at,updated_at,tier) "
            "VALUES (%s,'KNN Golden','','semiconductor','ready','user-search',"
            "%s,%s,'personal')",
            (KNN_NOTEBOOK, NOW, NOW),
        )
        rows = [
            (
                object_id,
                KNN_NOTEBOOK,
                "claim",
                "approved",
                json.dumps({"name": name}),
                "[]",
                "source-search-a",
                NOW,
                NOW,
            )
            for object_id, name in KNN_STAIRCASE
        ]
        # The deprecated row must be provably EXCLUDED, which means its
        # similarity has to place it INSIDE the narrow top-k if the status
        # predicate were dropped: "thermals" scores ≈0.7 against "thermal" —
        # rank #2, ahead of "thermal pad" — so a KNN statement missing
        # `status!='deprecated'` shifts the full-page top-3 and the equality
        # with legacy goes red.  (Its first spelling, "thermal deprecated
        # twin", ranked #5 and a mutation run proved it invisible.)
        rows.append((
            "ko-knn-deprecated",
            KNN_NOTEBOOK,
            "claim",
            "deprecated",
            json.dumps({"name": "thermals"}),
            "[]",
            "source-search-a",
            NOW,
            NOW,
        ))
        harness.knowledge.insert_object_chunk(connection, rows)


def _create_knn_index(harness) -> None:
    from app.repositories.postgres.search import reset_knn_index_cache

    with harness.database.write() as connection:
        connection.execute(
            "CREATE INDEX idx_knn_conformance_gist ON knowledge_objects "
            "USING gist ((((payload ->> 'name') COLLATE \"C\")) "
            "public.gist_trgm_ops(siglen=128)) WHERE status != 'deprecated'"
        )
    reset_knn_index_cache()


def _candidate_tuples(rows):
    return [
        (row["candidate_id"], int(row["term_rank"]), row["candidate_similarity"])
        for row in rows
    ]


@pytest.mark.postgres_integration
def test_split_legacy_arms_preserve_c_collated_ties_and_term_ranks(
    search_harness,
):
    """Equal-score rows retain the legacy id tie-break for every term."""
    terms = ["thermal", "热设计"]
    with search_harness.database.connect() as connection:
        pages = []
        for per_term_limit in (1, 4, 12):
            rows = knowledge_candidate_rows_for_terms(
                connection, "nb-search", terms, per_term_limit
            )
            pages.append([
                [row["candidate_id"] for row in rows if row["term_rank"] == rank]
                for rank in range(len(terms))
            ])

    assert pages == [
        [list(EXPECTED_IDS[:1]), list(EXPECTED_IDS[:1])],
        [list(EXPECTED_IDS[:4]), list(EXPECTED_IDS[:4])],
        [list(EXPECTED_IDS), list(EXPECTED_IDS)],
    ]


class _KnnCountingConnection:
    """Delegates everything while counting executed KNN-shaped statements.

    Equality with legacy is this feature's CONTRACT, which cuts both ways: a
    gate mutation that never takes the KNN branch also satisfies every
    equality.  Counting the `<->` statements PostgreSQL actually executed is
    what separates "equivalent through the new path" from "never left the old
    one".  `__getattr__` forwards `.info` etc. for the availability probe.
    """

    def __init__(self, connection):
        self._connection = connection
        self.knn_statements = 0

    def execute(self, statement, params=None):
        if "OPERATOR(public.<->)" in statement:
            self.knn_statements += 1
        return (self._connection.execute(statement, params)
                if params is not None else self._connection.execute(statement))

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.mark.postgres_integration
def test_knn_full_page_and_top_up_both_equal_the_legacy_statement(search_harness):
    """Two-phase KNN ≡ legacy, proven on both phases and on the rank remap.

    per_term_limit=3 with four `%`-reachable rows → the KNN page is FULL and
    phase 2 never runs for "thermal"; "grid" has fewer matches than the limit →
    its page is SHORT and phase 2 replaces it wholesale.  Probing both terms in
    one call also exercises the term_rank remap of the top-up rows.
    """
    from app.repositories.postgres.search import (
        knn_name_index_available,
        reset_knn_index_cache,
    )

    _seed_knn_staircase(search_harness)
    _create_knn_index(search_harness)
    terms = ["thermal", "grid"]
    try:
        with search_harness.database.connect() as connection:
            # Precondition, not decoration: if detection fails here the KNN
            # branch silently never runs and every equality below passes
            # vacuously — the exact false-green a mutation run exposed.
            assert knn_name_index_available(connection) is True
            counting = _KnnCountingConnection(connection)
            legacy = knowledge_candidate_rows_for_terms(
                counting, KNN_NOTEBOOK, terms, per_term_limit=3
            )
            assert counting.knn_statements == 0, "legacy 调用不得发出 KNN SQL"
            routing_stats = {}
            knn = knowledge_candidate_rows_for_terms(
                counting, KNN_NOTEBOOK, terms, per_term_limit=3, allow_knn=True,
                routing_stats=routing_stats,
            )
            assert counting.knn_statements == 1, (
                "hinted 调用必须真的经 KNN 语句取数——否则上面的等价断言全是空转"
            )
            # The ILIKE-only staircase row is invisible to `%`: a wide limit
            # forces the short-page top-up, whose output must again be exactly
            # the legacy rows — including ko-knn-04.
            legacy_wide = knowledge_candidate_rows_for_terms(
                connection, KNN_NOTEBOOK, terms, per_term_limit=10
            )
            knn_wide = knowledge_candidate_rows_for_terms(
                connection, KNN_NOTEBOOK, terms, per_term_limit=10, allow_knn=True
            )
            # Reversed permutation: SHORT page at rank 0, FULL page at rank 1.
            # The cross-phase `merged.sort` is what restores rank-ascending
            # output when the top-up rows belong to the EARLIER rank; a
            # mutation run proved the one-sided permutation cannot see its
            # removal.
            reversed_terms = ["grid", "thermal"]
            legacy_reversed = knowledge_candidate_rows_for_terms(
                connection, KNN_NOTEBOOK, reversed_terms, per_term_limit=3
            )
            knn_reversed = knowledge_candidate_rows_for_terms(
                connection, KNN_NOTEBOOK, reversed_terms, per_term_limit=3,
                allow_knn=True,
            )
    finally:
        reset_knn_index_cache()

    assert _candidate_tuples(knn) == _candidate_tuples(legacy)
    assert routing_stats["knn_term_count"] == 2
    assert routing_stats["legacy_term_count"] == 0
    assert routing_stats["knn_short_fallback_term_count"] == 1
    assert routing_stats["knn_seconds"] >= 0
    assert routing_stats["knn_short_fallback_seconds"] >= 0
    assert _candidate_tuples(knn_wide) == _candidate_tuples(legacy_wide)
    assert _candidate_tuples(knn_reversed) == _candidate_tuples(legacy_reversed)
    assert [
        row["candidate_id"] for row in legacy_wide
        if int(row["term_rank"]) == 0
    ] == [object_id for object_id, _name in KNN_STAIRCASE]
    assert "ko-knn-04" in {row["candidate_id"] for row in knn_wide}
    # The deprecated high-similarity twin must be OUT of every page: it ranks
    # #2 by similarity, so a KNN statement missing `status!='deprecated'`
    # cannot pass the narrow equality above.
    assert "ko-knn-deprecated" not in {
        row["candidate_id"] for rows in (knn, knn_wide, knn_reversed)
        for row in rows
    }
    # The staircase really is tie-free, or the equalities above prove nothing.
    similarities = [row["candidate_similarity"] for row in legacy_wide
                    if int(row["term_rank"]) == 0]
    assert len(similarities) == len(set(similarities))


@pytest.mark.postgres_integration
def test_knn_hint_travels_from_fts_search_to_the_adapter(search_harness):
    """钉住跨层接线:`fts_search(allow_knn=True)` 必须一路到达 KNN 语句。

    服务层有测试(hint 被算出并发出)、SQL 层有测试(适配器收到 hint 会走 KNN),
    但 `fts_search → _lexical_candidate_union → candidate_rows_for_terms` 这根
    中间线曾经一个断言都没有——把透传掐断,全部测试照样全绿,开关静默失效
    (规格评审 mutE 实测)。这条测试就是那根线。
    """
    from app.repositories.postgres.search import reset_knn_index_cache

    _seed_knn_staircase(search_harness)
    _create_knn_index(search_harness)
    try:
        with search_harness.database.connect() as connection:
            counting = _KnnCountingConnection(connection)
            baseline = search_harness.knowledge.fts_search(
                counting, KNN_NOTEBOOK, "thermal", 12
            )
            assert counting.knn_statements == 0, "缺省调用不得发 KNN SQL"
            hinted = search_harness.knowledge.fts_search(
                counting, KNN_NOTEBOOK, "thermal", 12, allow_knn=True
            )
            assert counting.knn_statements >= 1, (
                "fts_search(allow_knn=True) 没有到达 KNN 语句——透传断线"
            )
            # Scoped runs must stay on legacy even when hinted: the adapter
            # gate is the second defence behind the service verdict, and its
            # docstring claims it — so a statement-level assertion backs it.
            counting.knn_statements = 0
            scoped = search_harness.knowledge.fts_search(
                counting, KNN_NOTEBOOK, "thermal", 12,
                allowed_source_ids=["source-search-a"], allow_knn=True,
            )
            assert counting.knn_statements == 0, (
                "scoped 调用带着 hint 也不得发 KNN SQL"
            )
    finally:
        reset_knn_index_cache()
    assert [hit["object_id"] for hit in hinted] == [
        hit["object_id"] for hit in baseline
    ]
    assert isinstance(scoped, list)


@pytest.mark.postgres_integration
def test_knn_hint_is_inert_without_a_conforming_index(search_harness):
    """allow_knn=True degrades to the legacy statement when no index matches.

    Covers the inert-detection shapes: no GiST index at all, a GiST index on
    the wrong expression, and one missing `COLLATE "C"`.  The scoped-run
    exclusion is asserted statement-level in
    `test_knn_hint_travels_from_fts_search_to_the_adapter`.
    """
    from app.repositories.postgres.search import (
        knn_name_index_available,
        reset_knn_index_cache,
    )

    _seed_knn_staircase(search_harness)
    reset_knn_index_cache()
    try:
        with search_harness.database.connect() as connection:
            assert knn_name_index_available(connection) is False
            baseline = knowledge_candidate_rows_for_terms(
                connection, KNN_NOTEBOOK, ["thermal"], per_term_limit=3
            )
            hinted = knowledge_candidate_rows_for_terms(
                connection, KNN_NOTEBOOK, ["thermal"], per_term_limit=3,
                allow_knn=True,
            )
        assert _candidate_tuples(hinted) == _candidate_tuples(baseline)

        with search_harness.database.write() as connection:
            connection.execute(
                "CREATE INDEX idx_knn_wrong_expr ON knowledge_objects "
                "USING gist ((((payload ->> 'statement') COLLATE \"C\")) "
                "public.gist_trgm_ops)"
            )
        reset_knn_index_cache()
        with search_harness.database.connect() as connection:
            assert knn_name_index_available(connection) is False

        # The textbook spelling WITHOUT `COLLATE "C"` is the dangerous false
        # positive: the planner refuses it against the C-collated query
        # expression, so "available" would mean an unindexed distance sort per
        # term — a silent perf regression, not an error.  Detection must say no.
        with search_harness.database.write() as connection:
            connection.execute(
                "CREATE INDEX idx_knn_no_collate ON knowledge_objects "
                "USING gist (((payload ->> 'name')) public.gist_trgm_ops)"
            )
        reset_knn_index_cache()
        with search_harness.database.connect() as connection:
            assert knn_name_index_available(connection) is False

        # A wrong-CASE json key is the false positive a case-folding
        # normalizer would admit: `payload ->> 'Name'` reads a different key,
        # so the planner can never use it for the query expression.  The
        # comparison must preserve quoted literals exactly (codex #463 R1 P2).
        with search_harness.database.write() as connection:
            connection.execute(
                "CREATE INDEX idx_knn_wrong_case ON knowledge_objects "
                "USING gist ((((payload ->> 'Name') COLLATE \"C\")) "
                "public.gist_trgm_ops) WHERE status != 'deprecated'"
            )
        reset_knn_index_cache()
        with search_harness.database.connect() as connection:
            assert knn_name_index_available(connection) is False
    finally:
        reset_knn_index_cache()


@pytest.mark.postgres_integration
def test_knn_probe_failure_leaves_the_transaction_usable(search_harness):
    """探针失败必须回答「不可用」且**不毒化事务**。

    连接池是 autocommit=False:失败语句会把调用方的隐式事务置为 aborted,单纯
    return False 兑现不了「回落 legacy」的承诺——下一条 legacy 语句直接抛
    InFailedSqlTransaction,词法臂坍缩成 [](codex #463 R1 P2)。SAVEPOINT
    括号是修法;这条测试用一个真实毒化探针证明 legacy 语句事后仍能跑。
    """
    from app.repositories.postgres import search as search_module
    from app.repositories.postgres.search import (
        knn_name_index_available,
        reset_knn_index_cache,
    )

    _seed_knn_staircase(search_harness)
    reset_knn_index_cache()

    def poisoned_index_row(connection, table_oid):
        # 真实地毒化事务:非法 SQL 让 PostgreSQL 把事务置 aborted,
        # 而不是抛一个从未触库的 Python 异常。
        return connection.execute("SELECT no_such_column FROM pg_index").fetchone()

    real = search_module._knn_index_row
    search_module._knn_index_row = poisoned_index_row
    try:
        with search_harness.database.connect() as connection:
            assert knn_name_index_available(connection) is False
            # 同一连接、同一事务:legacy 语句必须照常工作。
            rows = knowledge_candidate_rows_for_terms(
                connection, KNN_NOTEBOOK, ["thermal"], per_term_limit=3
            )
        assert [row["candidate_id"] for row in rows] == [
            "ko-knn-00", "ko-knn-01", "ko-knn-02",
        ]
    finally:
        search_module._knn_index_row = real
        reset_knn_index_cache()


def test_chunk_fts_private_timeout_restores_transaction_and_pool_state(
    search_harness, monkeypatch
):
    """A real QueryCanceled stays inside the chunk-FTS savepoint boundary."""
    from app.repositories.ports import ChunkLexicalSearchTimeout
    from app.repositories.postgres import knowledge_store as knowledge_module

    search_harness.database._pool.resize(1, 1)
    monkeypatch.setattr(
        search_harness.database.settings,
        "postgres_chunk_fts_timeout_seconds",
        0.05,
    )

    def _slow_candidates(connection, *_args, **_kwargs):
        connection.execute("SELECT pg_sleep(0.2)")
        return []

    monkeypatch.setattr(
        knowledge_module,
        "chunk_candidate_rows_for_terms",
        _slow_candidates,
    )

    with search_harness.database.connect() as connection:
        before = connection.execute(
            "SELECT current_setting('statement_timeout') AS value"
        ).fetchone()["value"]
        with pytest.raises(ChunkLexicalSearchTimeout):
            search_harness.knowledge.chunk_fts_search(
                connection,
                "nb-timeout",
                "thermal control",
                k=5,
            )
        assert connection.execute("SELECT 1 AS value").fetchone()["value"] == 1
        assert connection.execute(
            "SELECT current_setting('statement_timeout') AS value"
        ).fetchone()["value"] == before

    # Pool size one makes this the same physical connection after check-in.
    with search_harness.database.connect() as connection:
        assert connection.execute(
            "SELECT current_setting('statement_timeout') AS value"
        ).fetchone()["value"] == before


@pytest.mark.postgres_integration
def test_knn_lateral_statement_is_planner_usable(search_harness):
    """The parameterized LATERAL can run as an index-ordered KNN scan.

    Mirrors the notebook-aware index test's precedent: shape usability is
    asserted with competing plans disabled, because on conformance-sized data
    the planner legitimately prefers a bitmap.  Production choice was measured
    directly (9.1M-row base: planner picked the KNN scan unforced, 60×).
    """
    from app.repositories.postgres.search import (
        _knn_candidate_rows_for_terms,
        reset_knn_index_cache,
    )

    _seed_knn_staircase(search_harness)
    _create_knn_index(search_harness)
    try:
        with search_harness.database.connect() as connection:
            connection.execute("SET enable_bitmapscan = off")
            connection.execute("SET enable_sort = off")
            rows = connection.execute(
                "EXPLAIN WITH lexical_terms(term_rank,term) AS (VALUES (%s,%s)) "
                "SELECT candidate.candidate_id FROM lexical_terms "
                "CROSS JOIN LATERAL ("
                "SELECT id AS candidate_id FROM knowledge_objects "
                "WHERE notebook_id=%s AND status!='deprecated' "
                "AND ((payload ->> 'name') COLLATE \"C\") "
                "OPERATOR(public.%%) lexical_terms.term "
                "ORDER BY ((payload ->> 'name') COLLATE \"C\") "
                "OPERATOR(public.<->) lexical_terms.term LIMIT %s"
                ") AS candidate",
                (0, "thermal", KNN_NOTEBOOK, 3),
            ).fetchall()
            plan = "\n".join(str(list(row.values())[0]) for row in rows)
            connection.execute("RESET enable_bitmapscan")
            connection.execute("RESET enable_sort")
        assert "idx_knn_conformance_gist" in plan
        assert "Order By:" in plan

        # And the real two-phase helper returns rows through that shape.
        with search_harness.database.connect() as connection:
            rows = _knn_candidate_rows_for_terms(
                connection, KNN_NOTEBOOK, ["thermal"], 3
            )
        assert [row["candidate_id"] for row in rows] == [
            "ko-knn-00", "ko-knn-01", "ko-knn-02",
        ]
    finally:
        reset_knn_index_cache()
