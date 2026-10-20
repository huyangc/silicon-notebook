"""Board partition store primitives on PostgreSQL (0078, placeholder number until
the PR-4 merge): the twin of ``tests/test_board_partition_store.py``. Same world,
same checks, same literal expectations (``tests/board_partition_store_cases.py``).

Plus the plan pin of the previous-partition read on a production-shaped corpus
with the planner left alone (no ``enable_seqscan`` games): the statement the
store streams is captured on its way through ``psycopg.Cursor.stream`` and
explained with its real parameters.
"""
from __future__ import annotations

import time
from contextlib import closing
from pathlib import Path

import psycopg
import pytest

from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.unified_kg_store import UnifiedKgStore
from tests import board_partition_store_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def test_board_key_rows_read_one_generation_and_level(postgres_repository):
    cases.check_board_key_rows_read_one_generation_and_level(postgres_repository)


def test_board_keys_are_written_and_copied_forward(postgres_repository):
    cases.check_board_keys_are_written_and_copied_forward(postgres_repository)


def test_partition_columns_round_trip(postgres_repository):
    cases.check_partition_columns_round_trip(postgres_repository)


def test_kg_delete_resets_the_partition_columns(postgres_repository):
    cases.check_kg_delete_resets_the_partition_columns(postgres_repository)


def test_index_publication_resets_the_partition_columns(postgres_repository):
    cases.check_index_publication_resets_the_partition_columns(
        postgres_repository, postgres=True)


def test_board_overview_returns_board_keys(postgres_repository):
    cases.check_board_overview_returns_board_keys(postgres_repository)


# ------------------------------------------------------- slow consumer (#930)

SLOW_NB = "nb-slow-board-keys"
SLOW_ROWS = 12_000
PAD = 700
TIGHT_MS = 500
PAUSE_SECONDS = 1.5
FLOOR_MS = 600_000


def _timeout_ms(db) -> int:
    return int(db.execute(
        "SELECT setting::bigint AS c FROM pg_settings WHERE name='statement_timeout'"
    ).fetchone()["c"])


def test_a_slow_consumer_of_the_previous_partition_survives_a_tight_timeout(
    postgres_database,
):
    """The previous-partition read streams (``cursor().stream()``): the server
    statement lasts until the consumer takes the last row, so it raises this
    transaction's statement timeout to the 600 s maintenance floor first -- the
    contract ``community_graph_rows`` set (#930). A 500 ms session timeout and a
    consumer that stops for 1.5 s at row 100 (rows ~1.4 KB x 12 000, far beyond
    any socket buffer, so the server is held on send) read to the end; inside
    the transaction the timeout is 600 000 ms, after commit the session value
    is back."""
    PostgresMigrator(postgres_database).migrate()
    with postgres_database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,created_at,updated_at) VALUES (%s,%s,%s,%s)",
            (SLOW_NB, SLOW_NB, NOW, NOW))
        db.execute(
            "INSERT INTO communities (id,notebook_id,level,member_ids,size,created_at,"
            "generation,board_key) "
            f"SELECT 'cm-slow-' || b, %s, 0, '[]'::jsonb, 100, %s, 1, "
            f"repeat('k', {PAD}) || b FROM generate_series(1, {SLOW_ROWS // 100}) b",
            (SLOW_NB, NOW))
        db.execute(
            "INSERT INTO community_members (canonical_id,notebook_id,level,community_id,"
            "canonical_name,centrality,generation) "
            f"SELECT repeat('c', {PAD}) || g, %s, 0, 'cm-slow-' || (1 + (g - 1) / 100), "
            f"'', 1.0, 1 FROM generate_series(1, {SLOW_ROWS}) g",
            (SLOW_NB,))
    store = UnifiedKgStore(postgres_database)
    with postgres_database.connect() as db:
        db.execute(f"SET statement_timeout = '{TIGHT_MS}ms'")
        assert _timeout_ms(db) == TIGHT_MS
        count = 0
        with closing(store.community_board_key_rows(db, SLOW_NB, 0, 1)) as rows:
            for _row in rows:
                count += 1
                if count == 100:
                    time.sleep(PAUSE_SECONDS)
        assert count == SLOW_ROWS
        assert _timeout_ms(db) == FLOOR_MS
        db.commit()
        assert _timeout_ms(db) == TIGHT_MS


# ------------------------------------------------------------------ plan pin

NB = "nb-ppin"
USER = "u-ppin"
NOW = "2026-10-21T00:00:00+00:00"
BOARDS = 4_000             # boards per generation of the pinned notebook
MEMBERS = 25               # members per board
OTHER_NOTEBOOKS = 10
OTHER_BOARDS = 500


@pytest.fixture
def pin_database(postgres_settings):
    database = PostgresDatabase(
        postgres_settings.model_copy(update={
            "postgres_pool_min_size": 1,
            "postgres_pool_max_size": 2,
            "postgres_pool_acquire_timeout_seconds": 10,
            "postgres_statement_timeout_seconds": 60,
            "postgres_lock_timeout_seconds": 10,
        }),
        Path(__file__).resolve().parents[3],
    )
    try:
        assert PostgresMigrator(database).migrate() == 78
        _seed(database)
        yield database
    finally:
        database.close()


def _seed(database: PostgresDatabase) -> None:
    """The pinned notebook holds two generations of 4 000 boards x 25 members
    (the published one and a retired one awaiting the reaper) at level 0;
    10 other notebooks hold two generations of 500 boards x 25 members."""
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES (%s,'ppin@example.test','Pin','admin',%s,%s)", (USER, NOW, NOW))
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
            "SELECT id, id, %s, %s, %s FROM (SELECT %s AS id UNION ALL "
            "SELECT 'nb-pother-'||lpad(g::text,2,'0') FROM generate_series(1, %s) g) n",
            (USER, NOW, NOW, NB, OTHER_NOTEBOOKS))
        # boards: (notebook, generation, board index)
        db.execute(
            "INSERT INTO communities(id,notebook_id,level,member_ids,size,created_at,"
            "generation,board_key) "
            "SELECT 'cm-'||n.id||'-'||gen||'-'||b, n.id, 0, '[]'::jsonb, %s, %s, gen, "
            "'bk-'||n.id||'-'||b "
            "FROM (SELECT %s AS id, %s AS boards UNION ALL "
            "      SELECT 'nb-pother-'||lpad(o::text,2,'0'), %s "
            "      FROM generate_series(1, %s) o) n, "
            "generate_series(2, 3) gen, LATERAL generate_series(0, n.boards - 1) b",
            (MEMBERS, NOW, NB, BOARDS, OTHER_BOARDS, OTHER_NOTEBOOKS))
        db.execute(
            "INSERT INTO community_members(canonical_id,notebook_id,level,community_id,"
            "canonical_name,centrality,generation) "
            "SELECT 'K-'||c.id||'-'||k, c.notebook_id, 0, c.id, 'name', 1.0, c.generation "
            "FROM communities c, generate_series(0, %s) k "
            "WHERE c.id LIKE 'cm-%%'", (MEMBERS - 1,))
    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM (ANALYZE) communities, community_members")


@pytest.mark.xdist_group(name="postgres_board_partition_plan_pins")
def test_previous_partition_read_is_driven_by_the_published_boards(pin_database):
    """This notebook's published generation (100 000 member rows among 450 000
    across generations and notebooks) is reached through its boards
    (``communities(notebook_id, level)``) and each board's members through an
    index on ``community_id`` -- neither table is scanned."""
    store = UnifiedKgStore(pin_database)
    captured: list[tuple[str, object]] = []
    original = psycopg.Cursor.stream

    def recording(self, query, params=None, **options):
        if isinstance(query, str) and "c.board_key AS board_key" in query:
            captured.append((query, params))
        return original(self, query, params, **options)

    psycopg.Cursor.stream = recording
    try:
        with pin_database.connect() as db:
            with closing(store.community_board_key_rows(db, NB, 0, 3)) as rows:
                count = sum(1 for _row in rows)
    finally:
        psycopg.Cursor.stream = original
    assert count == BOARDS * MEMBERS
    [(sql, params)] = captured
    with pin_database.connect() as db:
        plan = "\n".join(
            str(row["QUERY PLAN"])
            for row in db.execute(f"EXPLAIN {sql}", params, prepare=False).fetchall())
    for table in ("communities", "community_members"):
        assert f"Seq Scan on {table} " not in plan + " ", plan
    assert "idx_communities_nb_level" in plan, plan
    assert "idx_commmem_nb_comm" in plan or "pk_community_members" in plan, plan


# ------------------------------------------------- the rebuild on PostgreSQL

# The two cliques and the triangle of tests/test_board_partition_wiring.py (the
# SQLite contract suite); here the rebuild path itself runs on PostgreSQL: the
# previous partition is streamed inside the rebuild, the version and mode are
# written by the publishing transaction.
_CLIQUES = (["K-a1", "K-a2", "K-a3", "K-a4"], ["K-b1", "K-b2", "K-b3", "K-b4"],
            ["K-c1", "K-c2", "K-c3"])


def _seed_graph(repository) -> str:
    import itertools
    import json

    from app.models.schemas import NotebookCreate

    notebook_id = repository.create_notebook(NotebookCreate(name="pg-boards")).id
    runtime = repository._runtime
    nodes = sorted(node for clique in _CLIQUES for node in clique)
    edges = [pair for clique in _CLIQUES for pair in itertools.combinations(clique, 2)]
    edges.append(("K-a1", "K-b1"))
    source_id = f"src-{notebook_id}"
    objects = [(f"o-{notebook_id}-{n}", notebook_id, "concept", "approved",
                json.dumps({"name": n}), "[]", source_id, NOW, NOW) for n in nodes]
    clusters = [(f"cl-{notebook_id}-{n}", notebook_id, n, f"o-{notebook_id}-{n}", n,
                 "concept", "", "", NOW) for n in nodes]
    relations = [(f"r-{notebook_id}-{i}", notebook_id, source_id, f"o-{notebook_id}-{a}",
                  f"o-{notebook_id}-{b}", "related_to", "[]", NOW)
                 for i, (a, b) in enumerate(edges)]
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,created_at,updated_at,"
            "doc_type) VALUES (%s,%s,%s,'markdown','extracted','parsed','','',0,%s,'',"
            "%s,%s,'')",
            (source_id, notebook_id, source_id, source_id, NOW, NOW))
        runtime.knowledge.insert_object_chunk(db, objects)
        runtime.knowledge.insert_relation_chunk(db, relations)
        runtime.unified_kg.replace_cluster_rows_streamed(db, notebook_id, "concept", clusters)
    return notebook_id


def _published(repository, notebook_id: str) -> list:
    with repository._runtime.database.connect() as db:
        rows = db.execute(
            "SELECT c.board_key AS board_key, m.canonical_id AS canonical_id "
            "FROM communities c JOIN community_members m ON m.community_id = c.id "
            "AND m.generation = c.generation WHERE c.notebook_id=%s AND c.level=0 "
            "AND c.generation = (SELECT community_generation FROM unified_kg_state "
            "WHERE notebook_id=%s)", (notebook_id, notebook_id)).fetchall()
    boards: dict = {}
    for row in rows:
        boards.setdefault(row["board_key"], []).append(row["canonical_id"])
    return sorted((key, tuple(sorted(members))) for key, members in boards.items())


def test_the_rebuild_starts_cold_then_warm_and_carries_keys_on_postgres(
    postgres_repository, monkeypatch,
):
    events: list = []
    monkeypatch.setattr(postgres_repository._runtime.event_log, "emit", events.append)
    rebuild = postgres_repository._runtime.knowledge_lifecycle.rebuild_communities
    notebook_id = _seed_graph(postgres_repository)

    assert rebuild(notebook_id) == 3
    first = _published(postgres_repository, notebook_id)
    assert sorted(members for _key, members in first) == sorted(tuple(c) for c in _CLIQUES)
    assert cases.partition_state(postgres_repository._runtime, notebook_id) == (1, "cold")

    events.clear()
    assert rebuild(notebook_id, force=True) == 3
    assert _published(postgres_repository, notebook_id) == first
    [rebuilt] = [e for e in events if e.get("kind") == "communities_rebuilt"]
    assert (rebuilt["partition_mode"], rebuilt["board_keys_carried"]) == ("warm", 3)
    assert cases.partition_state(postgres_repository._runtime, notebook_id) == (1, "warm")

    postgres_repository.request_cold_community_partition(notebook_id)
    assert cases.partition_state(postgres_repository._runtime, notebook_id) == (0, "warm")
    events.clear()
    rebuild(notebook_id, force=True)
    [rebuilt] = [e for e in events if e.get("kind") == "communities_rebuilt"]
    assert (rebuilt["partition_mode"], rebuilt["partition_cold_reason"],
            rebuilt["board_keys_carried"]) == ("cold", "partition_version_changed", 3)
    assert _published(postgres_repository, notebook_id) == first
