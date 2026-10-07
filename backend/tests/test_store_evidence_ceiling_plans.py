"""SQLite pins for the E2-4 store reads (PR-E2; ledger B-6, B-9, B-11, N-4).

The contract itself lives in ``store_evidence_cases`` and runs unchanged on
PostgreSQL (``postgres/test_store_evidence_ceiling_explain_pins.py``).  This
file adds what only SQLite has:

* **three planner-statistics states** -- ``no_stats`` is production (the
  repository never runs ``ANALYZE``); ``stats_prod`` installs row counts of a
  large production notebook, so a small fixture cannot make a list-driven plan
  look as cheap as the intended one; ``stats_thin`` is the same notebook with
  one or two rows per source on the source indexes -- the state in which a
  list-driven plan (one seek per listed id) looks cheapest.  A plan pin must
  hold in all three;
* **plan shape** (``EXPLAIN QUERY PLAN`` of the captured statement): a
  ceiling filters and never drives (``+col IN json_each``); the relation read
  binds no list and is driven by its 40 endpoint ids; the owner-library read
  of ``_enrich_evidence`` / ``_element_texts`` goes element → source by
  primary key and never scans a library's sources;
* **one parameter** per ceiling and the deployment variable limit (32,766):
  a 49k-id ceiling runs.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.repository import RepositoryCompatibilitySeams
from app.repositories.sqlite import database as sqlite_database
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.embedding_store import EmbeddingStore
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from tests import store_evidence_cases as cases

DEPLOYMENT_VARIABLE_LIMIT = 32_766
STATS_STATES = ("no_stats", "stats_prod", "stats_thin")
# ``stats_thin``: one or two rows per source id on the source indexes.
THIN_STATS = {
    "idx_knowledge_relations_source": "8350000 1",
    "idx_chunks_source": "98058 2",
}
# 40 admitted objects, the size of a real answer context.
FORTY = ["ko-a", "ko-b", "ko-c"] + [f"ko-none-{n:02d}" for n in range(37)]
PRODUCTION_STATS = [
    ("knowledge_relations", "idx_knowledge_relations_nb_source_target_edge", "8350000 8350000 4 2 1"),
    ("knowledge_relations", "idx_knowledge_relations_nb_source", "8350000 8350000 4"),
    ("knowledge_relations", "idx_knowledge_relations_nb_target", "8350000 8350000 4"),
    ("knowledge_relations", "idx_knowledge_relations_nb_source_id", "8350000 8350000 4 1"),
    ("knowledge_relations", "idx_knowledge_relations_nb_target_id", "8350000 8350000 4 1"),
    ("knowledge_relations", "idx_knowledge_relations_nb_review", "8350000 8350000 2783333"),
    ("knowledge_relations", "idx_knowledge_relations_nb_created", "8350000 8350000 2"),
    ("knowledge_relations", "idx_knowledge_relations_source", "8350000 170"),
    ("relation_embeddings", "idx_relation_embeddings_nb", "8350000 8350000"),
    ("relation_embeddings", "sqlite_autoindex_relation_embeddings_1", "8350000 1"),
    ("knowledge_relations", "sqlite_autoindex_knowledge_relations_1", "8350000 1"),
    ("knowledge_objects", "sqlite_autoindex_knowledge_objects_1", "49008 1"),
    ("source_elements", "idx_source_elements_source", "2000000 40"),
    ("source_elements", "idx_source_elements_source_created", "2000000 40 1 1"),
    ("source_elements", "idx_source_elements_source_type", "2000000 40 10 1 1"),
    ("source_elements", "sqlite_autoindex_source_elements_1", "2000000 1"),
    ("sources", "idx_sources_nb_hidden_type", "49078 24539 16360"),
    ("sources", "idx_sources_visible_identity", "49058 24529 24529 1"),
    ("sources", "idx_sources_notebook_created", "49078 24539 24539"),
    ("sources", "idx_sources_nb_parse_status", "49078 24539 24539"),
    ("sources", "sqlite_autoindex_sources_1", "49078 1"),
    ("chunks", "idx_chunks_nb", "98058 49029"),
    ("chunks", "idx_chunks_nb_created", "98058 49029 49029"),
    ("chunks", "idx_chunks_source", "98058 2"),
    ("chunks", "sqlite_autoindex_chunks_1", "98058 1"),
    ("notebooks", "sqlite_autoindex_notebooks_1", "200 1"),
]
# A wide frozen ceiling: every real source plus ids no row carries, past the
# deployment variable limit.
WIDE = frozenset(cases.SOURCES + ["s-priv"] + [f"pad-{n:06d}" for n in range(49_000)])


def _seams() -> RepositoryCompatibilitySeams:
    return RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-e24", now=lambda: cases.NOW,
        copy_chunk_size=lambda: 100, remap_json_ids=lambda value, _map: value,
        in_chunk_size=lambda: 900,
    )


def _seed(db: sqlite3.Connection) -> None:
    cases.seed(db.execute, "?")
    cases.seed_relation_vectors(db.execute, "?", "AAAA")
    for chunk_id, source, text in cases.exact_chunks():
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,created_at) VALUES (?,?,?,?,?)",
            (chunk_id, cases.NB, source, text, cases.NOW),
        )
        db.execute(
            "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
            (chunk_id, cases.NB, text),
        )
    db.execute("DELETE FROM sync_change_log")


def _install_production_stats(db: sqlite3.Connection, *, thin: bool = False) -> None:
    db.execute("ANALYZE")
    tables = sorted({table for table, _index, _stat in PRODUCTION_STATS})
    marks = ",".join("?" * len(tables))
    db.execute(f"DELETE FROM sqlite_stat1 WHERE tbl IN ({marks})", tables)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat4'").fetchone():
        db.execute(f"DELETE FROM sqlite_stat4 WHERE tbl IN ({marks})", tables)
    db.executemany(
        "INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES (?,?,?)",
        [(table, index, THIN_STATS.get(index, stat) if thin else stat)
         for table, index, stat in PRODUCTION_STATS],
    )


@pytest.fixture(scope="module", params=STATS_STATES)
def database(request, tmp_path_factory, _sqlite_schema_template) -> SqliteDatabase:
    root: Path = tmp_path_factory.mktemp(f"e24-{request.param}")
    shutil.copyfile(_sqlite_schema_template, root / "test.db")
    settings = Settings(database_url=f"sqlite:///{root / 'test.db'}")
    database = SqliteDatabase(settings, root)
    with database.write() as db:
        _seed(db)
        if request.param in ("stats_prod", "stats_thin"):
            _install_production_stats(db, thin=request.param == "stats_thin")
        elif db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone():
            db.execute("DELETE FROM sqlite_stat1")
    database.close_local()
    database = SqliteDatabase(settings, root)
    has_stats = database.connect().execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'"
    ).fetchone()[0] and database.connect().execute(
        "SELECT COUNT(*) FROM sqlite_stat1"
    ).fetchone()[0]
    assert bool(has_stats) == (request.param != "no_stats")
    yield database
    database.close_local()


@pytest.fixture
def conn(database):
    connection = database.connect()
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, DEPLOYMENT_VARIABLE_LIMIT)
    yield connection
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 250_000)


@pytest.fixture
def store(database) -> KnowledgeStore:
    return KnowledgeStore(database, _seams())


@pytest.fixture
def statements(monkeypatch):
    """Every statement the store sent, with its parameters."""
    seen: list[tuple[str, tuple]] = []
    original = sqlite_database._Conn.execute

    def recording(self, sql, parameters=(), /):
        if isinstance(sql, str):
            seen.append((sql, tuple(parameters)))
        return original(self, sql, parameters)

    monkeypatch.setattr(sqlite_database._Conn, "execute", recording)
    return seen


def _plan(db, sql: str, params: tuple) -> str:
    return "\n".join(
        row[3] for row in sqlite3.Connection.execute(db, "EXPLAIN QUERY PLAN " + sql, params)
    )


def _only(statements, marker: str) -> tuple[str, tuple]:
    matching = [(sql, params) for sql, params in statements if marker in sql]
    assert len(matching) == 1, [sql[:120] for sql, _ in statements]
    return matching[0]


# ------------------------------------------------------------------ contract
def test_in_network_relation_rows_count_only_in_ceiling_sources(store, conn):
    cases.check_in_network_relations(
        lambda ids, **kwargs: store.in_network_relation_rows(conn, cases.NB, ids, **kwargs)
    )


def test_chunk_exact_search_filters_below_the_probe_window(store, conn):
    cases.check_chunk_exact(
        lambda needle, k, **kwargs: store.chunk_exact_search(conn, cases.NB, needle, k, **kwargs)
    )


def test_node_context_reads_elements_only_from_the_objects_own_library(store):
    cases.check_node_context_owner(
        lambda object_id: store.node_context(cases.NB, object_id, check_access=False)
    )


def test_element_texts_read_texts_and_ordinals_from_the_owner_library(store, conn):
    cases.check_element_texts_owner(
        lambda ids, **kwargs: store._element_texts(conn, ids, **kwargs)
    )


def test_follow_relation_evidence_titles_come_from_the_relations_own_library(store, conn):
    cases.check_follow_relation_evidence(
        lambda ids, **kwargs: store.follow_relation_evidence_rows(conn, ids, **kwargs)
    )


# ------------------------------------------------------------ byte identity
def test_without_a_ceiling_the_statements_are_the_historical_ones(store, conn, statements):
    """``None`` sends exactly the statement and parameters from before the
    keyword existed (the trace a run without a binding ceiling produces)."""
    ids = ["ko-a", "ko-b", "ko-c"]
    store.in_network_relation_rows(conn, cases.NB, ids)
    store.chunk_exact_search(conn, cases.NB, cases.NEEDLE, 5)
    store._enrich_evidence(conn, [{"element_id": "el-own"}])
    assert statements == [
        (
            "SELECT DISTINCT r.source_object_id, r.target_object_id, r.edge_type, "
            "src.object_type AS source_type, tgt.object_type AS target_type "
            "FROM knowledge_relations AS r "
            "JOIN knowledge_objects AS src ON src.id=r.source_object_id "
            "JOIN knowledge_objects AS tgt ON tgt.id=r.target_object_id "
            "WHERE r.notebook_id=? AND r.review_status!='rejected' "
            "AND r.source_object_id IN (?,?,?) "
            "AND r.target_object_id IN (?,?,?) "
            "ORDER BY r.source_object_id, r.edge_type, r.target_object_id",
            (cases.NB, *ids, *ids),
        ),
        (
            "SELECT chunks_fts.chunk_id AS chunk_id, c.source_id AS source_id, "
            "c.section_path AS section_path, bm25(chunks_fts) AS rank "
            "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.chunk_id "
            "WHERE chunks_fts.notebook_id=? AND chunks_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (cases.NB, f'"{cases.NEEDLE}"', 5),
        ),
        (
            "SELECT id, source_id, element_type, location_label, text "
            "FROM source_elements WHERE id IN (?)",
            ("el-own",),
        ),
    ]


# ------------------------------------------------------------------ plans
def test_sourced_relation_read_binds_no_list_and_the_endpoints_drive(store, conn, statements):
    """The relation read a covered library makes (``with_source_ids``) binds
    nothing but its 40 endpoint ids twice, and those drive -- never the
    ``source_id`` index, in any statistics state."""
    rows = store.in_network_relation_rows(conn, cases.NB, FORTY, with_source_ids=True)
    assert {dict(row)["source_id"] for row in rows} >= {"s-01", "s-02", "s-03"}
    sql, params = _only(statements, "r.source_id FROM knowledge_relations")
    assert "json_each" not in sql
    assert len(params) == 1 + 40 + 40
    plan = _plan(conn, sql, params)
    assert "SEARCH r USING INDEX idx_knowledge_relations_nb_" in plan, plan
    assert "idx_knowledge_relations_source " not in plan, plan
    assert "idx_knowledge_relations_source)" not in plan, plan
    assert "SCAN r" not in plan, plan


def test_exact_probe_ceiling_filters_the_text_match(store, conn, statements):
    store.chunk_exact_search(
        conn, cases.NB, cases.NEEDLE, 5, allowed_source_ids=sorted(WIDE),
    )
    sql, params = _only(statements, "chunks_fts MATCH")
    assert sum(isinstance(p, str) and p.startswith("[") for p in params) == 1
    plan = _plan(conn, sql, params)
    assert "SEARCH c USING INDEX idx_chunks_source" not in plan, plan
    assert "SCAN chunks_fts VIRTUAL TABLE" in plan, plan
    assert "SEARCH c USING INDEX sqlite_autoindex_chunks_1 (id=?)" in plan, plan
    assert "idx_chunks_source" not in plan, plan


def test_owner_library_reads_go_by_primary_keys(store, conn, statements):
    store._enrich_evidence(
        conn, [{"element_id": "el-own"}, {"element_id": "el-priv"}],
        owner_notebook_id=cases.NB,
    )
    store._element_texts(conn, ["el-own", "el-priv"], owner_notebook_id=cases.NB)
    for marker in ("se.element_type", "SELECT se.id, se.text"):
        sql, params = _only(statements, marker)
        plan = _plan(conn, sql, params)
        assert "USING INDEX sqlite_autoindex_source_elements_1 (id=?)" in plan, plan
        assert "SEARCH os USING INDEX sqlite_autoindex_sources_1 (id=?)" in plan, plan
        # Only the one-row owner constant (``o``) may be scanned.
        assert "SCAN se" not in plan and "SCAN os" not in plan, plan
        assert "idx_sources_" not in plan, plan


def test_follow_relation_evidence_goes_by_relation_primary_keys(store, conn, statements):
    store.follow_relation_evidence_rows(conn, ["kr-1", "kr-7"], notebook_id=cases.NB)
    sql, params = _only(statements, "FROM knowledge_relations r LEFT JOIN sources s")
    plan = _plan(conn, sql, params)
    assert "SEARCH r USING INDEX sqlite_autoindex_knowledge_relations_1 (id=?)" in plan, plan
    assert "SEARCH s USING INDEX sqlite_autoindex_sources_1 (id=?)" in plan, plan
    assert "SCAN" not in plan, plan


def test_owner_forms_bind_no_list(store, conn, statements):
    """The owner-library predicate is one scalar, whatever the evidence size."""
    evidence = [{"element_id": f"el-missing-{n}"} for n in range(40)] + [
        {"element_id": "el-own"}
    ]
    enriched = store._enrich_evidence(conn, evidence, owner_notebook_id=cases.NB)
    assert [row["element_text"] for row in enriched][-1] == cases.OWN_TEXT
    sql, params = _only(statements, "se.element_type")
    assert params[0] == cases.NB and len(params) == 42
    assert json.dumps(params)  # scalars only


# --------------------------------------------- relation_delta_rows (E2-2)
def test_relation_delta_rows_map_relations_to_sources_in_one_read(conn):
    cases.check_relation_delta_rows(
        lambda ids, **kwargs: EmbeddingStore.relation_delta_rows(conn, cases.NB, ids, **kwargs)
    )


def test_relation_delta_rows_default_statement_is_unchanged(conn, statements):
    EmbeddingStore.relation_delta_rows(conn, cases.NB, ["s-01", "s-02"])
    assert statements == [(
        "SELECT relation_id AS vid, vector FROM relation_embeddings "
        "WHERE notebook_id=? AND relation_id IN "
        "(SELECT id FROM knowledge_relations WHERE notebook_id=? AND source_id IN (?,?))",
        (cases.NB, cases.NB, "s-01", "s-02"),
    )]


def test_relation_delta_rows_with_sources_probe_by_source_then_primary_key(conn, statements):
    EmbeddingStore.relation_delta_rows(
        conn, cases.NB, [f"s-{n:02d}" for n in range(40)], with_source_id=True,
    )
    sql, params = _only(statements, "kr.source_id FROM knowledge_relations kr")
    plan = _plan(conn, sql, params)
    assert "SEARCH kr USING INDEX idx_knowledge_relations_source (source_id=?)" in plan, plan
    assert "SEARCH re USING INDEX sqlite_autoindex_relation_embeddings_1 (relation_id=?)" in plan, plan
    assert "SCAN" not in plan, plan
