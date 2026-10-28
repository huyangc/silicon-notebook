"""Board partition store primitives on PostgreSQL (0083): the twin of ``tests/test_board_partition_store.py``. Same world,
same checks, same literal expectations (``tests/board_partition_store_cases.py``).

Plus the plan pin of the previous-partition read on two corpus shapes (a
minority share and the production shape where one notebook owns the table) with
the planner left alone (no ``enable_seqscan`` games): the statement the store
streams is captured on its way through ``psycopg.Cursor.stream`` and explained
with its real parameters. What is pinned is that the read returns exactly the
generation, streamed, and never scans a table once per row -- not a plan shape
that holds in one corpus only. Also the rebuild path itself on PostgreSQL.
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
    from app.services.postgres_repository import PostgresRepository

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


def test_partition_version_cas(postgres_repository):
    cases.check_partition_version_cas(postgres_repository)


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

# Two corpus shapes. ``minority``: the pinned notebook's published generation is
# a small share of the member table (many notebooks, a retired generation each)
# -- the planner reaches the notebook's rows through its indexes. ``dominant``:
# one notebook owns nearly the whole table with three generations, the
# production shape (9 万 boards x 3 generations, 543 万 member rows, 181 万 in
# the published one) -- the planner reads it with a (parallel) sequential scan
# and a hash join. Both are right for a read that returns the whole generation.
SHAPES = {
    #            (pinned notebook's generations, other notebooks, their boards, their generations)
    "minority": ((2, 3), 10, 500, (2, 3)),
    "dominant": ((1, 2, 3), 2, 100, (3,)),
}


@pytest.fixture(params=sorted(SHAPES))
def pin_database(request, postgres_settings):
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
        assert PostgresMigrator(database).migrate() == 85
        _seed(database, *SHAPES[request.param])
        yield database
    finally:
        database.close()


def _seed(database: PostgresDatabase, nb_generations, others: int, other_boards: int,
          other_generations) -> None:
    """The pinned notebook holds ``BOARDS`` x ``MEMBERS`` at level 0 in each of
    ``nb_generations`` (generation 3 is the published one); ``others`` notebooks
    hold ``other_boards`` x ``MEMBERS`` in each of ``other_generations``."""
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES (%s,'ppin@example.test','Pin','admin',%s,%s)", (USER, NOW, NOW))
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
            "SELECT id, id, %s, %s, %s FROM (SELECT %s AS id UNION ALL "
            "SELECT 'nb-pother-'||lpad(g::text,2,'0') FROM generate_series(1, %s) g) n",
            (USER, NOW, NOW, NB, others))
        # boards: (notebook, generation, board index)
        db.execute(
            "INSERT INTO communities(id,notebook_id,level,member_ids,size,created_at,"
            "generation,board_key) "
            "SELECT 'cm-'||n.id||'-'||gen||'-'||b, n.id, 0, '[]'::jsonb, %s, %s, gen, "
            "'bk-'||n.id||'-'||b "
            "FROM (SELECT %s AS id, %s AS boards, %s::int[] AS gens UNION ALL "
            "      SELECT 'nb-pother-'||lpad(o::text,2,'0'), %s, %s::int[] "
            "      FROM generate_series(1, %s) o) n, "
            "LATERAL unnest(n.gens) gen, LATERAL generate_series(0, n.boards - 1) b",
            (MEMBERS, NOW, NB, BOARDS, list(nb_generations), other_boards,
             list(other_generations), others))
        db.execute(
            "INSERT INTO community_members(canonical_id,notebook_id,level,community_id,"
            "canonical_name,centrality,generation) "
            "SELECT 'K-'||c.id||'-'||k, c.notebook_id, 0, c.id, 'name', 1.0, c.generation "
            "FROM communities c, generate_series(0, %s) k "
            "WHERE c.id LIKE 'cm-%%'", (MEMBERS - 1,))
    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM (ANALYZE) communities, community_members")


def _repeated_scans(node: dict, tables: tuple, inner: bool = False) -> list:
    """Sequential scans of ``tables`` that sit on the inner side of a nested
    loop (re-run once per outer row)."""
    found = []
    if inner and node.get("Node Type") == "Seq Scan" and node.get("Relation Name") in tables:
        found.append(node["Relation Name"])
    children = node.get("Plans", [])
    for position, child in enumerate(children):
        child_inner = inner or (node.get("Node Type") == "Nested Loop" and position == 1)
        found.extend(_repeated_scans(child, tables, child_inner))
    return found


@pytest.mark.xdist_group(name="postgres_board_partition_plan_pins")
def test_previous_partition_read_streams_the_generation_without_repeated_scans(pin_database):
    """The read returns exactly the published generation's member rows, through
    a server-side stream (the statement is captured on its way through
    ``psycopg.Cursor.stream``). The plan shape is the planner's call by share
    (the notebook's rows through its indexes in the minority shape, a
    sequential scan with a hash join in the dominant one -- parallel on the
    production shape, 282 ms server side, 1.41 s read into Python); neither
    shape may scan either table once per row, i.e. a sequential scan on the
    inner side of a nested loop."""
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
        [row] = db.execute(f"EXPLAIN (FORMAT JSON) {sql}", params, prepare=False).fetchall()
    plan = next(iter(dict(row).values()))[0]["Plan"]
    assert _repeated_scans(plan, ("communities", "community_members")) == [], plan


def test_the_repeated_scan_detector_sees_an_inner_seq_scan():
    """The structural check above is only as good as its detector."""
    inner = {"Node Type": "Seq Scan", "Relation Name": "community_members"}
    outer = {"Node Type": "Index Scan", "Relation Name": "communities"}
    assert _repeated_scans({"Node Type": "Nested Loop", "Plans": [outer, inner]},
                           ("communities", "community_members")) == ["community_members"]
    assert _repeated_scans({"Node Type": "Hash Join", "Plans": [
        inner, {"Node Type": "Hash", "Plans": [{"Node Type": "Seq Scan",
                                                  "Relation Name": "communities"}]}]},
        ("communities", "community_members")) == []


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


def test_a_cold_request_during_a_build_survives_its_publish_on_postgres(
    postgres_repository, monkeypatch,
):
    """The twin of the SQLite interleaving case: the build in flight read
    version 1 after its claim; ``--cold-partition`` writes 0 on its own
    connection before the build publishes. The publish's version CAS misses, the
    0 survives (mode records the warm publish, an event says why), and the next
    build starts cold."""
    events: list = []
    monkeypatch.setattr(postgres_repository._runtime.event_log, "emit", events.append)
    lifecycle = postgres_repository._runtime.knowledge_lifecycle
    notebook_id = _seed_graph(postgres_repository)
    lifecycle.rebuild_communities(notebook_id)
    original = lifecycle._partition_boards_for_rebuild

    def partition_then_request(*args, **kwargs):
        out = original(*args, **kwargs)
        postgres_repository.request_cold_community_partition(args[0])
        return out

    with monkeypatch.context() as patched:
        patched.setattr(lifecycle, "_partition_boards_for_rebuild", partition_then_request)
        events.clear()
        lifecycle.rebuild_communities(notebook_id, force=True)
    [held] = [e for e in events if e.get("kind") == "community_partition_version_held"]
    assert held["reason"] == "version_changed_during_build"
    assert cases.partition_state(postgres_repository._runtime, notebook_id) == (0, "warm")
    events.clear()
    lifecycle.rebuild_communities(notebook_id, force=True)
    [rebuilt] = [e for e in events if e.get("kind") == "communities_rebuilt"]
    assert (rebuilt["partition_mode"], rebuilt["partition_cold_reason"]) == (
        "cold", "partition_version_changed")
    assert cases.partition_state(postgres_repository._runtime, notebook_id) == (1, "cold")
