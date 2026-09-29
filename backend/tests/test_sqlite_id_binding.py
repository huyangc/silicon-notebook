"""Pins for ``app.repositories.sqlite.id_binding`` and the statements that bind a
source ceiling through it.  The rule itself is stated in the module docstring
and in ``docs/development.md`` ("Binding id lists in SQL").  The test names
use these labels:

* S1 ``KnowledgeStore.chunk_fts_search`` (chunk FTS candidates);
* S2 / S3 ``KnowledgeStore.fts_search`` (KG FTS: reverse-index gate /
  authoritative evidence gate);
* S4 ``ChunkStore.question_index_rows`` (generated-question rows);
* S5 ``ChunkStore.retrieval_contribution_rows`` (contribution hydration);
* S6 ``ChunkStore.ids_for_sources``;
* S7 / S8 ``UnifiedKgStore.community_member_peers`` /
  ``UnifiedKgStore.comention_peers`` (comparison peers).

Three kinds of pin, each on the real store methods:

* **Deployment variable limit.**  Deployment builds of SQLite cap bound
  parameters at 32,766; the local conda build allows 250,000, so a 49k ceiling
  proves nothing unless the connection is lowered with ``setlimit`` first.
  Every converted statement must succeed at 49k under that cap and equal the
  reference model.
* **Plan shape.**  ``EXPLAIN QUERY PLAN`` of the captured statement: the ceiling
  must filter, not drive (the per-id seek measured at 6.6-8.7 s at 49k ids).
* **Parameter count.**  A ceiling contributes exactly one bound parameter.

The reference model is Python over the unfiltered result (``allowed_source_ids``
omitted) plus the stored support relation, so it holds for ceilings the old
one-``?``-per-id statements could not even run at.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite import database as sqlite_database
from app.repositories.sqlite.chunk_store import ChunkStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.id_binding import (
    JSON_IDS,
    BoundIds,
    bind_ids,
    drive_by,
    member_of,
    not_member_of,
)
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.unified_kg_store import UnifiedKgStore

DEPLOYMENT_VARIABLE_LIMIT = 32_766
NB = "nb-ceiling"
USER = "u-asker"
OTHER_USER = "u-other"
NOW = "2026-09-29T00:00:00+00:00"
WEIRD = ["it's", 'a"b', "back\\slash", "com,ma", "中文来源", "emoji-😀", " lead", "7"]
# Ids the binding layer must carry exactly as given: the empty string, a blank
# id, and one containing PostgreSQL's text-form separator.
ODD = ["", "   ", "sep\x1finside"]
REAL = [f"s-{i:03d}" for i in range(40)] + WEIRD + ODD
OTHER_MEMORY = "s-mem-other"


def _padded(real: list[str], total: int) -> list[str]:
    return real + [f"pad-{i:06d}" for i in range(total - len(real))]


CEILINGS: dict[str, list[str] | None] = {
    "none": None,
    "deny": [],
    "one": ["s-005"],
    "sparse4k": _padded(REAL[::3], 4_000),
    "at_limit": _padded(REAL[::2], DEPLOYMENT_VARIABLE_LIMIT),
    "over_limit": _padded(REAL[::2], DEPLOYMENT_VARIABLE_LIMIT + 1),
    "49k": _padded(REAL[1::2] + [OTHER_MEMORY], 49_020),
    "weird": WEIRD + ["s-001", "s-002"],
    # Reversed, with duplicates, and the odd ids: membership is exactly the
    # listed ids, and an ordered output keeps the first occurrence's order.
    "odd": ["s-009", "", "s-004", "   ", "s-009", "sep\x1finside", "", "s-001"],
}
BOUND = [label for label, ceiling in CEILINGS.items() if ceiling is not None]


# ------------------------------------------------------------------ fixture
# Every test runs in BOTH planner-statistics states:
#
# * ``no_stats`` -- what production has: the repository never runs ANALYZE,
#   so ``sqlite_stat1`` is empty and the planner falls back to its defaults.
# * ``stats_49k`` -- ``sqlite_stat1`` measured on a 49k-source notebook
#   (98k chunks, 49k objects).  The fixture holds a hundred rows; on its own
#   small-table statistics a ceiling-driven plan can look no cheaper than the
#   correct one, and a pin would pass for the wrong reason.
#
# A plan pin must hold in both: a plan that is right only after ANALYZE is a
# plan production does not get.
STATS_STATES = ("no_stats", "stats_49k")
PRODUCTION_STATS = [
    ("chunk_questions", "idx_chunk_questions_nb", "98058 49029 1"),
    ("chunk_questions", "idx_chunk_questions_source", "98058 2 1"),
    ("chunk_questions", "sqlite_autoindex_chunk_questions_1", "98058 1"),
    ("chunk_questions", "sqlite_autoindex_chunk_questions_2", "98058 1 1"),
    ("chunks", "idx_chunks_nb", "98058 49029"),
    ("chunks", "idx_chunks_nb_created", "98058 49029 49029"),
    ("chunks", "idx_chunks_source", "98058 2"),
    ("chunks", "sqlite_autoindex_chunks_1", "98058 1"),
    ("community_members", "idx_commmem_nb_can", "9800 9800 1"),
    ("community_members", "idx_commmem_nb_comm", "9800 9800 490"),
    ("community_members", "uq_community_members_sync_key", "9800 490 1"),
    ("concept_clusters", "idx_clusters_member", "49000 1"),
    ("concept_clusters", "idx_clusters_nb_canonical_member_gen", "49000 49000 5 1 1"),
    ("concept_clusters", "idx_clusters_nb_canonical_name_lower", "49000 49000 5"),
    ("concept_clusters", "idx_clusters_nb_created_gen", "49000 49000 49000 49000"),
    ("concept_clusters", "sqlite_autoindex_concept_clusters_1", "49000 1"),
    ("concept_clusters", "uq_clusters_nb_type_member_generation", "49000 49000 49000 1 1"),
    ("concept_comentions", "idx_comentions_nb_b", "5000 5000 1"),
    ("concept_comentions", "sqlite_autoindex_concept_comentions_1", "5000 5000 5000 1"),
    ("knowledge_object_sources", "idx_kos_notebook", "49008 49008"),
    ("knowledge_object_sources", "idx_kos_object", "49008 1"),
    ("knowledge_object_sources", "idx_kos_source", "49008 1"),
    ("knowledge_object_sources", "idx_kos_source_object", "49008 1 1"),
    ("knowledge_object_sources", "uq_knowledge_object_sources_sync_key", "49008 1 1"),
    ("knowledge_objects", "idx_knowledge_objects_nb_status", "49008 49008 49008"),
    ("knowledge_objects", "idx_knowledge_objects_nb_type_created", "49008 49008 49008 49008 1"),
    ("knowledge_objects", "idx_knowledge_objects_nb_type_status", "49008 49008 49008 49008"),
    ("knowledge_objects", "idx_knowledge_objects_nb_updated", "49008 49008 49008"),
    ("knowledge_objects", "idx_knowledge_objects_source", "49008 1"),
    ("knowledge_objects", "idx_knowledge_objects_source_id", "49008 1 1"),
    ("knowledge_objects", "sqlite_autoindex_knowledge_objects_1", "49008 1"),
    ("memory_items", "idx_memory_agent_candidate", "20 20 20 20 20"),
    ("memory_items", "idx_memory_answer_once", "0 0 0"),
    ("memory_items", "idx_memory_items_notebook", "20 20"),
    ("memory_items", "idx_memory_owner_notebook_status", "20 20 20 20 20"),
    ("memory_items", "sqlite_autoindex_memory_items_1", "20 1"),
    ("sources", "idx_sources_memory_id", "20 1"),
    ("sources", "idx_sources_nb_hidden_type", "20 20 20"),
    ("sources", "idx_sources_nb_parse_status", "49078 24539 24539"),
    ("sources", "idx_sources_nb_parse_status_type", "49078 24539 24539 16360"),
    ("sources", "idx_sources_notebook_created", "49078 24539 24539"),
    ("sources", "idx_sources_notebook_file_hash", "49078 24539 24539"),
    ("sources", "idx_sources_notebook_status", "49078 24539 24539"),
    ("sources", "idx_sources_uploaded_by_created", "0 0 0 0"),
    ("sources", "idx_sources_visible_identity", "49058 24529 24529 1"),
    ("sources", "sqlite_autoindex_sources_1", "49078 1"),
]


def _clear_stats(db: sqlite3.Connection) -> None:
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'"
    ).fetchone():
        db.execute("DELETE FROM sqlite_stat1")


def _install_production_stats(db: sqlite3.Connection) -> None:
    db.execute("ANALYZE")
    tables = sorted({table for table, _index, _stat in PRODUCTION_STATS})
    marks = ",".join("?" * len(tables))
    db.execute(f"DELETE FROM sqlite_stat1 WHERE tbl IN ({marks})", tables)
    has_stat4 = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat4'"
    ).fetchone()
    if has_stat4:
        db.execute(f"DELETE FROM sqlite_stat4 WHERE tbl IN ({marks})", tables)
    known = {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    db.executemany(
        "INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES (?,?,?)",
        [row for row in PRODUCTION_STATS if row[1] in known],
    )


def _seed(db: sqlite3.Connection) -> None:
    db.executemany(
        "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        [(USER, "a@example.test", "A", "admin", NOW, NOW),
         (OTHER_USER, "o@example.test", "O", "member", NOW, NOW)],
    )
    db.execute(
        "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?)", (NB, NB, USER, NOW, NOW),
    )
    db.execute(
        "INSERT INTO unified_kg_state(notebook_id,source_index_backfilled,updated_at) "
        "VALUES (?,1,?)", (NB, NOW),
    )
    db.execute(
        "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
        "content_md,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("mem-other", NB, OTHER_USER, "ask_answer", "confirmed", "m", "m", NOW, NOW),
    )
    sources = [(sid, None, "file") for sid in REAL]
    sources.append((OTHER_MEMORY, "mem-other", "memory"))
    db.executemany(
        "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
        "memory_id,created_at,updated_at) VALUES (?,?,?,?,'ready','ready',?,?,?)",
        [(sid, NB, f"T {sid}", kind, memory, NOW, NOW)
         for sid, memory, kind in sources],
    )
    ordinal = 0
    for index, (sid, _memory, _kind) in enumerate(sources):
        for part in range(2):
            ordinal += 1
            # Every document length is unique, so bm25 never ties and the
            # rank order is fully determined.
            text = "wafer etching " + " ".join(f"w{n}" for n in range(ordinal))
            chunk_id = f"c-{index:03d}-{part}"
            db.execute(
                "INSERT INTO chunks(id,notebook_id,source_id,text,created_at) "
                "VALUES (?,?,?,?,?)", (chunk_id, NB, sid, text, NOW),
            )
            db.execute(
                "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                (chunk_id, NB, text),
            )
            db.execute(
                "INSERT INTO chunk_questions(id,chunk_id,notebook_id,source_id,"
                "question,vector,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"q-{chunk_id}", chunk_id, NB, sid, "q", b"\0" * 8, NOW),
            )
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,"
            "text,created_at) VALUES (?,?,?,?,?,?)",
            (f"el-{index:03d}", sid, "paragraph", "p1", "t", NOW),
        )
        # Object i is supported by its own source and, for every fifth one,
        # also by the next source -- multi-source support must still match.
        supporters = [sid]
        if index % 5 == 0 and index + 1 < len(sources):
            supporters.append(sources[index + 1][0])
        evidence = [{"source_id": s, "element_id": "e"} for s in supporters]
        object_id = f"ko-{index:03d}"
        name = "etching " + " ".join(f"n{n}" for n in range(index + 1))
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,'approved',?,?,?,?,?)",
            (object_id, NB, "concept", json.dumps({"name": name}),
             json.dumps(evidence, ensure_ascii=False), sid, NOW, NOW),
        )
        db.execute(
            "INSERT INTO kg_objects_fts(object_id,notebook_id,name) VALUES (?,?,?)",
            (object_id, NB, name),
        )
        db.executemany(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "VALUES (?,?,?)", [(object_id, s, NB) for s in supporters],
        )
        canonical = f"can-{index // 2:03d}"
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,"
            "member_object_id,canonical_name,created_at) VALUES (?,?,?,?,?,?)",
            (f"cc-{index:03d}", NB, canonical, object_id, f"canon {index // 2}", NOW),
        )
    canonicals = sorted({f"can-{index // 2:03d}" for index in range(len(sources))})
    db.executemany(
        "INSERT INTO community_members(canonical_id,notebook_id,level,community_id,"
        "canonical_name,centrality) VALUES (?,?,0,'com-0',?,?)",
        [(can, NB, f"canon {int(can[4:])}", float(len(canonicals) - n))
         for n, can in enumerate(canonicals)],
    )
    db.executemany(
        "INSERT INTO concept_comentions(notebook_id,canonical_a,canonical_b,"
        "bridge_claims) VALUES (?,?,?,?)",
        [(NB, "can-000", can, 1 + n % 4) for n, can in enumerate(canonicals[1:])],
    )
    db.execute("DELETE FROM sync_change_log")


@pytest.fixture(scope="module", params=STATS_STATES)
def database(request, tmp_path_factory, _sqlite_schema_template) -> SqliteDatabase:
    root: Path = tmp_path_factory.mktemp(f"id-binding-{request.param}")
    shutil.copyfile(_sqlite_schema_template, root / "test.db")
    settings = Settings(database_url=f"sqlite:///{root / 'test.db'}")
    database = SqliteDatabase(settings, root)
    with database.write() as db:
        _seed(db)
        if request.param == "stats_49k":
            _install_production_stats(db)
        else:
            _clear_stats(db)
    # Statistics are read when a connection loads the schema: start fresh.
    database.close_local()
    database = SqliteDatabase(settings, root)
    stats_rows = database.connect().execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'"
    ).fetchone()[0] and database.connect().execute(
        "SELECT COUNT(*) FROM sqlite_stat1"
    ).fetchone()[0]
    assert bool(stats_rows) == (request.param == "stats_49k"), stats_rows
    yield database
    database.close_local()


@pytest.fixture
def conn(database):
    """The thread-local read connection at the deployment variable limit."""
    connection = database.connect()
    connection.setlimit(
        sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, DEPLOYMENT_VARIABLE_LIMIT
    )
    too_many = ",".join("?" * (DEPLOYMENT_VARIABLE_LIMIT + 1))
    with pytest.raises(sqlite3.OperationalError):
        connection.execute(
            f"SELECT 1 WHERE 1 IN ({too_many})",
            [0] * (DEPLOYMENT_VARIABLE_LIMIT + 1),
        )
    yield connection
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 250_000)


@pytest.fixture
def captured(monkeypatch):
    """Every statement that bound an id list, with its parameters."""
    statements: list[tuple[str, tuple]] = []
    original = sqlite_database._Conn.execute

    def recording(self, sql, parameters=(), /):
        if isinstance(sql, str) and "json_each(?)" in sql:
            statements.append((sql, tuple(parameters)))
        return original(self, sql, parameters)

    monkeypatch.setattr(sqlite_database._Conn, "execute", recording)
    return statements


def _support(db, *, authoritative: bool) -> dict[str, set[str]]:
    if authoritative:
        return {
            row["id"]: {ev["source_id"] for ev in json.loads(row["evidence"])}
            for row in db.execute(
                "SELECT id, evidence FROM knowledge_objects WHERE notebook_id=?", (NB,)
            )
        }
    support: dict[str, set[str]] = {}
    for row in db.execute(
        "SELECT object_id, source_id FROM knowledge_object_sources WHERE notebook_id=?",
        (NB,),
    ):
        support.setdefault(row["object_id"], set()).add(row["source_id"])
    return support


def _chunk_sources(db) -> dict[str, str]:
    return {
        row["id"]: row["source_id"]
        for row in db.execute("SELECT id, source_id FROM chunks WHERE notebook_id=?", (NB,))
    }


def _plan(db, sql: str, params: tuple) -> list[str]:
    return [
        row[3] for row in
        # The base class: EXPLAIN must not land in ``captured`` itself.
        sqlite3.Connection.execute(db, "EXPLAIN QUERY PLAN " + sql, params)
    ]


def _single_ceiling_param(
    statements, ceiling, *, bounded: int = 0, sort: bool = True,
) -> None:
    """Exactly one parameter carries the ceiling, as one JSON array; the rest
    is a handful of scalars plus ``bounded`` batched keys (S5's candidate
    window), never a count that grows with the ceiling.  Membership forms
    bind the array sorted; driven forms keep the caller's order."""
    assert statements, "the ceiling statement was not captured"
    # The stores de-duplicate a ceiling once (keeping first occurrences);
    # the binding layer then carries exactly that list.
    payload = bind_ids(list(dict.fromkeys(ceiling)), sort=sort).param
    for sql, params in statements:
        assert sum(p == payload for p in params) == 1, sql
        assert len(params) < 16 + bounded, (len(params), sql[:200])


def _pin_state(database) -> str:
    """Which statistics state this parametrisation of the fixture runs in."""
    rows = database.connect().execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'"
    ).fetchone()[0]
    if rows and database.connect().execute(
        "SELECT COUNT(*) FROM sqlite_stat1"
    ).fetchone()[0]:
        return "stats_49k"
    return "no_stats"


# ------------------------------------------------------------ module API
def test_bind_ids_carries_every_id_exactly_as_given():
    """The binding layer never changes membership: order, duplicates, the
    empty string and blank ids all reach the JSON array unchanged."""
    def decoded(values):
        bound = bind_ids(values)
        assert isinstance(bound, BoundIds) and bound.sql == JSON_IDS
        return json.loads(bound.param)

    assert decoded(["b", "a", "b", "", "  ", "\t", "a"]) == [
        "b", "a", "b", "", "  ", "\t", "a",
    ]
    assert decoded(("b", "a")) == ["b", "a"]
    assert decoded({"b", "a", "c"}) == ["a", "b", "c"]
    assert decoded(frozenset({"z", "y"})) == ["y", "z"]
    assert decoded([]) == []
    assert decoded(WEIRD + ODD) == WEIRD + ODD
    assert decoded(x for x in ["b", "a"]) == ["b", "a"]
    assert bind_ids(["b", "a"]) == bind_ids(["b", "a"])  # byte-stable
    # The membership payload is sorted (a cheaper IN-list build); the ids
    # themselves, duplicates and blanks included, are unchanged.
    assert json.loads(bind_ids(["b", "", "a", "b"], sort=True).param) == [
        "", "a", "b", "b",
    ]


@pytest.mark.parametrize("bad", ["s-001", b"s-001", bytearray(b"s")])
def test_bind_ids_refuses_a_single_string(bad):
    with pytest.raises(TypeError):
        bind_ids(bad)


@pytest.mark.parametrize("bad", [[None], [7], [1.5], [True], [b"x"]])
def test_bind_ids_refuses_non_string_ids(bad):
    with pytest.raises(TypeError):
        bind_ids(["s-001", *bad])


def test_predicates_occupy_one_placeholder_and_only_member_forms_carry_plus():
    bound = bind_ids(["s-001"])
    assert member_of("kos.source_id", bound) == f"+kos.source_id IN {JSON_IDS}"
    assert not_member_of("c.source_id", bound) == f"+c.source_id NOT IN {JSON_IDS}"
    assert drive_by("source_id", bound) == f"source_id IN {JSON_IDS}"
    for fragment in (
        member_of("x", bound), not_member_of("x", bound), drive_by("x", bound),
    ):
        assert fragment.count("?") == 1
    with pytest.raises(ValueError):
        member_of(" ", bound)


def test_membership_is_exact_for_odd_ids():
    """Empty, blank and separator-bearing ids match only themselves, in both
    the membership and the exclusion form (the JSON array never holds null)."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t(id TEXT PRIMARY KEY)")
    stored = ["", "   ", " ", "sep\x1finside", "sep", "inside", "7", "it's"]
    db.executemany("INSERT INTO t VALUES (?)", [(value,) for value in stored])
    wanted = ["   ", "", "sep\x1finside", "", "missing"]
    bound = bind_ids(wanted)
    for predicate in (member_of("t.id", bound), drive_by("t.id", bound)):
        rows = {row[0] for row in db.execute(
            f"SELECT id FROM t WHERE {predicate}", (bound.param,),
        )}
        assert rows == {"", "   ", "sep\x1finside"}, predicate
    rows = {row[0] for row in db.execute(
        f"SELECT id FROM t WHERE {not_member_of('t.id', bound)}", (bound.param,),
    )}
    assert rows == set(stored) - {"", "   ", "sep\x1finside"}


# ------------------------------------------------- S1 chunk FTS candidates
@pytest.mark.parametrize("label", list(CEILINGS))
def test_s1_chunk_fts_matches_reference_under_deployment_limit(conn, label):
    ceiling = CEILINGS[label]
    store = KnowledgeStore
    everything = store.chunk_fts_search(conn, NB, "etching", k=10_000)
    kwargs = {} if ceiling is None else {"allowed_source_ids": ceiling}
    got = store.chunk_fts_search(conn, NB, "etching", k=30, **kwargs)
    sources = _chunk_sources(conn)
    allowed = None if ceiling is None else set(ceiling)
    expected = [
        hit for hit in everything
        if allowed is None or sources[hit["chunk_id"]] in allowed
    ][:30]
    assert got == expected
    if label == "weird":
        assert {sources[h["chunk_id"]] for h in got} >= set(WEIRD)


def test_s1_chunk_fts_stays_fts_driven_with_one_ceiling_param(conn, captured):
    ceiling = CEILINGS["49k"]
    KnowledgeStore.chunk_fts_search(
        conn, NB, "etching", k=30, allowed_source_ids=ceiling
    )
    _single_ceiling_param(captured, ceiling)
    plan = _plan(conn, *captured[-1])
    assert plan[0].startswith("SCAN f VIRTUAL TABLE"), plan
    assert any("sqlite_autoindex_chunks_1 (id=?)" in step for step in plan), plan
    assert not any("idx_chunks_source" in step for step in plan), plan


# ------------------------------------------------ S2/S3 KG lexical gates
@pytest.mark.parametrize("authoritative", [False, True])
@pytest.mark.parametrize("label", list(CEILINGS))
def test_s2_s3_kg_fts_gate_matches_reference_under_deployment_limit(
    conn, label, authoritative
):
    ceiling = CEILINGS[label]
    store = KnowledgeStore
    everything = store.fts_search(conn, NB, "etching", k=10_000)
    kwargs = {} if ceiling is None else {
        "allowed_source_ids": ceiling,
        "authoritative_source_filter": authoritative,
    }
    got = store.fts_search(conn, NB, "etching", k=30, **kwargs)
    support = _support(conn, authoritative=authoritative)
    allowed = None if ceiling is None else set(ceiling)
    expected = [
        hit for hit in everything
        if allowed is None or support[hit["object_id"]] & allowed
    ][:30]
    assert got == expected


@pytest.mark.parametrize("authoritative", [False, True])
def test_s2_s3_kg_fts_gate_binds_the_ceiling_once(conn, captured, authoritative):
    ceiling = CEILINGS["49k"]
    KnowledgeStore.fts_search(
        conn, NB, "etching", k=30, allowed_source_ids=ceiling,
        authoritative_source_filter=authoritative,
    )
    _single_ceiling_param(captured, ceiling)


def test_s2_kg_fts_gate_probes_by_object_only(conn, captured):
    """S2: with the ceiling able to drive, the reverse-index probe becomes
    ``(object_id=? AND source_id=?)`` once per ceiling id per FTS hit."""
    KnowledgeStore.fts_search(
        conn, NB, "etching", k=30, allowed_source_ids=CEILINGS["49k"],
    )
    plan = " / ".join(_plan(conn, *captured[-1]))
    assert "(object_id=?)" in plan, plan
    assert "(object_id=? AND source_id=?)" not in plan, plan
    assert "LIST SUBQUERY" in plan, plan


# --------------------------------------- S7/S8 comparison-peer source gates
@pytest.fixture(params=[True, False], ids=["reverse_index", "authoritative"])
def backfilled(request, database):
    """Both gate branches: the reverse index, and the evidence scan used while
    ``source_index_backfilled`` is 0."""
    with database.write() as db:
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=? WHERE notebook_id=?",
            (int(request.param), NB),
        )
    yield request.param
    with database.write() as db:
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=1 WHERE notebook_id=?",
            (NB,),
        )


def _supported_canonicals(db, ceiling, *, authoritative: bool) -> set[str] | None:
    if ceiling is None:
        return None
    support = _support(db, authoritative=authoritative)
    allowed = set(ceiling)
    return {
        row["canonical_id"]
        for row in db.execute(
            "SELECT canonical_id, member_object_id FROM concept_clusters "
            "WHERE notebook_id=?", (NB,),
        )
        if support[row["member_object_id"]] & allowed
    }


def _canonical_by_name(db) -> dict[str, str]:
    return {
        row["canonical_name"]: row["canonical_id"]
        for row in db.execute(
            "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
            "WHERE notebook_id=?", (NB,),
        )
    }


@pytest.mark.parametrize("label", list(CEILINGS))
def test_s7_community_peers_match_reference_under_deployment_limit(
    database, conn, backfilled, label
):
    ceiling = CEILINGS[label]
    store = UnifiedKgStore(database)
    everything = [tuple(row) for row in store.community_member_peers(
        NB, "com-0", "can-000", 10_000
    )]
    kwargs = {} if ceiling is None else {"allowed_source_ids": ceiling}
    got = [tuple(row) for row in store.community_member_peers(
        NB, "com-0", "can-000", 5, **kwargs
    )]
    supported = _supported_canonicals(conn, ceiling, authoritative=not backfilled)
    names = _canonical_by_name(conn)
    expected = [
        row for row in everything
        if supported is None or names[row[0]] in supported
    ][:5]
    assert got == expected


@pytest.mark.parametrize("label", list(CEILINGS))
def test_s8_comention_peers_match_reference_under_deployment_limit(
    database, conn, backfilled, label
):
    ceiling = CEILINGS[label]
    store = UnifiedKgStore(database)
    everything = store.comention_peers(NB, "can-000", 1, 10_000)
    kwargs = {} if ceiling is None else {"allowed_source_ids": ceiling}
    got = store.comention_peers(NB, "can-000", 1, 5, **kwargs)
    supported = _supported_canonicals(conn, ceiling, authoritative=not backfilled)
    names = _canonical_by_name(conn)
    expected = [
        (name, claims) for name, claims in everything
        if supported is None or names[name] in supported
    ][:5]
    assert got == expected


def test_s7_s8_bind_the_ceiling_once(database, conn, captured, backfilled):
    ceiling = CEILINGS["49k"]
    store = UnifiedKgStore(database)
    store.community_member_peers(
        NB, "com-0", "can-000", 5, allowed_source_ids=ceiling
    )
    store.comention_peers(NB, "can-000", 1, 5, allowed_source_ids=ceiling)
    payload = bind_ids(list(dict.fromkeys(ceiling)), sort=True).param
    ceiling_statements = [
        (sql, params) for sql, params in captured if payload in params
    ]
    assert len(ceiling_statements) == 3  # S7, S8 limit gate, S8 names
    _single_ceiling_param(ceiling_statements, ceiling)


def test_s7_s8_support_probe_goes_by_object_only(database, conn, captured):
    """S7/S8: a ceiling able to drive turns the reverse-index probe into
    ``(object_id=? AND source_id=?)`` per ceiling id per member (4k: 2 s)."""
    ceiling = CEILINGS["49k"]
    store = UnifiedKgStore(database)
    store.community_member_peers(
        NB, "com-0", "can-000", 5, allowed_source_ids=ceiling
    )
    store.comention_peers(NB, "can-000", 1, 5, allowed_source_ids=ceiling)
    gated = [
        (sql, params) for sql, params in captured
        if "knowledge_object_sources kos" in sql
    ]
    assert len(gated) == 3
    for sql, params in gated:
        plan = " / ".join(_plan(conn, sql, params))
        assert re.search(r"SEARCH kos (EXISTS )?USING INDEX \S+ \(object_id=\?\)", plan), plan
        assert "(object_id=? AND source_id=?)" not in plan, plan


def test_s8_peer_names_are_driven_by_the_bounded_peer_list(
    database, conn, captured, backfilled
):
    """The name lookup's ``drive_by``: at most ``limit`` peer canonical ids,
    each one an index seek.  As a membership filter the same statement walks
    every cluster row of the notebook instead."""
    store = UnifiedKgStore(database)
    for ceiling in (None, CEILINGS["49k"]):
        kwargs = {} if ceiling is None else {"allowed_source_ids": ceiling}
        assert store.comention_peers(NB, "can-000", 1, 5, **kwargs)
    names = [
        (sql, params) for sql, params in captured if "MIN(canonical_name)" in sql
    ]
    assert len(names) == 2
    for sql, params in names:
        plan = " / ".join(_plan(conn, sql, params))
        assert re.search(
            r"SEARCH concept_clusters USING (COVERING )?INDEX \S+ "
            r"\(notebook_id=\? AND canonical_id=\?", plan,
        ), (_pin_state(database), plan)


# ------------------------------------------- S4-S6 chunk-store ceilings
def _rows(rows) -> list[dict]:
    return [dict(row) for row in rows]


def _by_id(rows) -> list[dict]:
    return sorted(_rows(rows), key=lambda row: row["id"])


@pytest.mark.parametrize("label", list(CEILINGS))
def test_s4_question_rows_match_reference_under_deployment_limit(
    database, conn, label
):
    ceiling = CEILINGS[label]
    store = ChunkStore(database)
    everything = _rows(store.question_index_rows(
        NB, actor_id=USER, allowed_source_ids=None, limit=10_000
    ))
    got = _rows(store.question_index_rows(
        NB, actor_id=USER, allowed_source_ids=ceiling, limit=7
    ))
    allowed = None if ceiling is None else set(ceiling)
    expected = [
        row for row in everything
        if allowed is None or row["source_id"] in allowed
    ][:7]
    assert got == expected
    assert OTHER_MEMORY not in {row["source_id"] for row in got}


def _candidate_chunks(db) -> list[str]:
    return sorted(_chunk_sources(db))


@pytest.mark.parametrize("mode", ["include", "exclude"])
@pytest.mark.parametrize("label", BOUND)
def test_s5_contribution_rows_match_reference_under_deployment_limit(
    conn, label, mode
):
    ceiling = CEILINGS[label]
    candidates = _candidate_chunks(conn)
    everything = _by_id(ChunkStore.retrieval_contribution_rows(
        conn, NB, candidates, actor_id=USER, source_mode=None, source_ids=(),
    ))
    got = _by_id(ChunkStore.retrieval_contribution_rows(
        conn, NB, candidates, actor_id=USER, source_mode=mode,
        source_ids=ceiling,
    ))
    allowed = set(ceiling)
    if mode == "include":
        expected = [row for row in everything if row["source_id"] in allowed]
    else:
        expected = [row for row in everything if row["source_id"] not in allowed]
    # The statement has no ORDER BY: row order is the plan's, never a contract.
    assert got == expected


def test_s5_contribution_rows_are_driven_by_candidate_keys(conn, captured):
    """S5: the <= 900 candidate primary keys drive; a driving ceiling walked
    ``idx_chunks_source`` for every ceiling id instead (49k: 48 ms)."""
    ceiling = CEILINGS["49k"]
    for mode in ("include", "exclude"):
        ChunkStore.retrieval_contribution_rows(
            conn, NB, _candidate_chunks(conn)[:64], actor_id=USER,
            source_mode=mode, source_ids=ceiling,
        )
    _single_ceiling_param(captured, ceiling, bounded=64)
    assert len(captured) == 2
    for sql, params in captured:
        plan = " / ".join(_plan(conn, sql, params))
        assert "SEARCH c USING INDEX sqlite_autoindex_chunks_1 (id=?)" in plan, plan
        assert "idx_chunks_source" not in plan, plan


def test_s4_question_rows_bind_once_and_walk_question_order(
    database, conn, captured
):
    ceiling = CEILINGS["49k"]
    ChunkStore(database).question_index_rows(
        NB, actor_id=USER, allowed_source_ids=ceiling, limit=7
    )
    _single_ceiling_param(captured, ceiling)
    plan = " / ".join(_plan(conn, *captured[-1]))
    assert "idx_chunks_source" not in plan, plan


@pytest.mark.parametrize("presence_only", [False, True])
@pytest.mark.parametrize("label", BOUND)
def test_s6_ids_for_sources_match_reference_under_deployment_limit(
    conn, label, presence_only
):
    ceiling = CEILINGS[label]
    sources = _chunk_sources(conn)
    got = [
        tuple(row) for row in ChunkStore.ids_for_sources(
            conn, NB, ceiling, presence_only=presence_only
        )
    ]
    allowed = set(ceiling)
    if presence_only:
        present = set(sources.values())
        assert got == [(s,) for s in dict.fromkeys(ceiling) if s in present]
    else:
        assert sorted(got) == sorted(
            (chunk_id,) for chunk_id, source in sources.items() if source in allowed
        )


@pytest.mark.parametrize("label", ["49k", "sparse4k", "odd"])
def test_s6_ids_for_sources_seek_each_listed_source(database, conn, captured, label):
    """S6 is the intended ``drive_by`` (every chunk of the listed sources is
    the answer) and the presence probe asks one question per listed id: both
    must seek ``idx_chunks_source`` per id in both statistics states.  Without
    ``+notebook_id`` the statistics-free planner picked the notebook index and
    rescanned the notebook's chunks for every id (2,000 ids: 2.6 s); as a
    membership filter (``member_of``) the driven statement scans them all."""
    ceiling = CEILINGS[label]
    for presence_only in (False, True):
        ChunkStore.ids_for_sources(
            conn, NB, ceiling, presence_only=presence_only
        )
    _single_ceiling_param(captured, ceiling, sort=False)
    assert len(captured) == 2
    for sql, params in captured:
        plan = " / ".join(_plan(conn, sql, params))
        assert "USING INDEX idx_chunks_source (source_id=?)" in plan, (
            _pin_state(database), plan,
        )
        assert "idx_chunks_nb" not in plan, (_pin_state(database), plan)
