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
    assert statement.count("LIMIT %s") == 1
    assert params == [
        0,
        "thermal",
        "%thermal%",
        1,
        "ZXCV9000",
        "%ZXCV9000%",
        "nb",
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
        0, "target command", "%target command%", "nb", ["A", "B"], 2
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
    assert params == [0, "set_db", r"%set\_db%", "nb", 2]


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

    assert PostgresMigrator(postgres_database).migrate() == 17
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

    assert PostgresMigrator(postgres_database).migrate() == 17
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

    assert PostgresMigrator(postgres_database).migrate() == 17
    with postgres_database.connect() as connection:
        connection.execute("SET LOCAL enable_seqscan=off")
        plan_rows = connection.execute(
            "EXPLAIN SELECT id FROM chunks WHERE text ILIKE %s", ("%set\\_db%",)
        ).fetchall()
    plan = "\n".join(str(next(iter(row.values()))) for row in plan_rows)
    assert "idx_chunks_text_trgm" in plan
