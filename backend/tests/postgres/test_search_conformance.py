from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.core.config import Settings
from app.repositories.ports import ChunkWrite
from app.repositories.postgres.search import (
    PAYLOAD_NAME_EXPRESSION,
    TAGS_JSON_EXPRESSION,
    lexical_candidate_sql,
)


NOW = "2026-07-22T00:00:00+00:00"
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


@dataclass
class SearchHarness:
    backend: str
    database: object
    knowledge: object
    chunks: object
    queries: object


def _seed_catalog(harness: SearchHarness) -> None:
    if harness.backend == "postgres":
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
    else:
        user_sql = (
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES (?,?,?,'admin','active',?,?,?,'','',0)"
        )
        notebook_sql = (
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            "VALUES (?,?,'','semiconductor','ready',?,?,?,'personal')"
        )
        source_sql = (
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,summary,created_at,updated_at) "
            "VALUES (?,?,?,'file','ready','ready',?,?,?,?)"
        )
        element_sql = (
            "INSERT INTO source_elements(id,source_id,element_type,location_label,text,"
            "metadata,created_at) VALUES (?,?,'paragraph',?,?,?,?)"
        )
        metadata = json.dumps({"kind": "golden"})

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
def search_harnesses(tmp_path, postgres_database) -> tuple[SearchHarness, SearchHarness]:
    from app.repositories.postgres.chunk_store import ChunkStore as PostgresChunkStore
    from app.repositories.postgres.knowledge_store import KnowledgeStore as PostgresKnowledgeStore
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.query_store import QueryStore as PostgresQueryStore
    from app.repositories.sqlite.chunk_store import ChunkStore as SqliteChunkStore
    from app.repositories.sqlite.database import SqliteDatabase
    from app.repositories.sqlite.knowledge_store import KnowledgeStore as SqliteKnowledgeStore
    from app.repositories.sqlite.migrations import SqliteMigrator
    from app.repositories.sqlite.query_store import QueryStore as SqliteQueryStore
    from app.services.repository_runtime import RepositoryCompatibilitySeams

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'search-golden.db'}")
    sqlite_database = SqliteDatabase(settings, tmp_path)
    SqliteMigrator(sqlite_database, settings).initialize()
    assert PostgresMigrator(postgres_database).migrate() == 9
    seams = RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: NOW,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )
    sqlite = SearchHarness(
        backend="sqlite",
        database=sqlite_database,
        knowledge=SqliteKnowledgeStore(sqlite_database, seams),
        chunks=SqliteChunkStore(sqlite_database),
        queries=SqliteQueryStore(sqlite_database),
    )
    postgres = SearchHarness(
        backend="postgres",
        database=postgres_database,
        knowledge=PostgresKnowledgeStore(postgres_database, seams),
        chunks=PostgresChunkStore(postgres_database),
        queries=PostgresQueryStore(postgres_database),
    )
    _seed_catalog(sqlite)
    _seed_catalog(postgres)
    try:
        yield sqlite, postgres
    finally:
        sqlite_database.close_local()


def _recall(ids: list[str], expected: tuple[str, ...]) -> float:
    return len(set(ids) & set(expected)) / len(expected)


def _top_overlap(left: list[str], right: list[str], k: int) -> float:
    return len(set(left[:k]) & set(right[:k])) / k


@pytest.mark.postgres_integration
@pytest.mark.parametrize("query", ("thermal", "热设计"))
def test_zh_en_search_quality_and_citation_identity_golden(search_harnesses, query):
    sqlite, postgres = search_harnesses
    knowledge_ids: dict[str, list[str]] = {}
    chunk_ids: dict[str, list[str]] = {}
    for harness in (sqlite, postgres):
        with harness.database.connect() as connection:
            knowledge_ids[harness.backend] = [
                row["object_id"]
                for row in harness.knowledge.fts_search(
                    connection, "nb-search", query, 12
                )
            ]
            chunk_ids[harness.backend] = [
                row["chunk_id"]
                for row in harness.knowledge.chunk_fts_search(
                    connection, "nb-search", query, 12
                )
            ]
            objects = harness.knowledge.retrieval_objects(
                connection,
                "nb-search",
                "claim",
                ("approved", "reviewed", "project_specific", "conflict"),
                EXPECTED_IDS,
            )
        assert {item["id"] for item in objects} == set(EXPECTED_IDS)
        assert {item["evidence"][0].source_id for item in objects} == EXPECTED_SOURCE_IDS
        assert {item["evidence"][0].element_id for item in objects} == EXPECTED_ELEMENT_IDS

        notebook_search = harness.queries.search_notebook("nb-search", query)
        source_ids = {hit.source_id for hit in notebook_search.hits if hit.scope == "Source"}
        element_ids = {
            hit.element_id for hit in notebook_search.hits if hit.scope == "Element"
        }
        assert source_ids == EXPECTED_SOURCE_IDS
        assert element_ids == EXPECTED_ELEMENT_IDS

    sqlite_recall = _recall(knowledge_ids["sqlite"], EXPECTED_IDS)
    postgres_recall = _recall(knowledge_ids["postgres"], EXPECTED_IDS)
    knowledge_overlap = _top_overlap(
        knowledge_ids["sqlite"], knowledge_ids["postgres"], 10
    )
    sqlite_chunk_recall = _recall(chunk_ids["sqlite"], EXPECTED_CHUNK_IDS)
    postgres_chunk_recall = _recall(chunk_ids["postgres"], EXPECTED_CHUNK_IDS)
    chunk_overlap = _top_overlap(chunk_ids["sqlite"], chunk_ids["postgres"], 10)
    print(
        "SEARCH_METRICS "
        f"query={query!r} "
        f"knowledge_recall12_sqlite={sqlite_recall:.3f} "
        f"knowledge_recall12_postgres={postgres_recall:.3f} "
        f"knowledge_top10_overlap={knowledge_overlap:.3f} "
        f"chunk_recall12_sqlite={sqlite_chunk_recall:.3f} "
        f"chunk_recall12_postgres={postgres_chunk_recall:.3f} "
        f"chunk_top10_overlap={chunk_overlap:.3f}"
    )
    assert postgres_recall >= sqlite_recall - 0.01
    assert knowledge_overlap >= 0.90
    assert postgres_chunk_recall >= sqlite_chunk_recall - 0.01
    assert chunk_overlap >= 0.90


@pytest.mark.postgres_integration
def test_search_expression_indexes_match_catalog_and_are_planner_usable(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 9
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
