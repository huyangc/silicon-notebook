"""Pins for ``app.repositories.sqlite.id_binding`` and the statements that bind a
source ceiling through it (plan ``2026-09-29-ceiling-sql-hazards.md`` S1-S9).

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
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite import database as sqlite_database
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.id_binding import (
    JSON_IDS,
    drive_by,
    ids_param,
    member_of,
    not_member_of,
)
from app.repositories.sqlite.knowledge_store import KnowledgeStore

DEPLOYMENT_VARIABLE_LIMIT = 32_766
NB = "nb-ceiling"
USER = "u-asker"
OTHER_USER = "u-other"
NOW = "2026-09-29T00:00:00+00:00"
WEIRD = ["it's", 'a"b', "back\\slash", "com,ma", "中文来源", "emoji-😀", " lead", "7"]
REAL = [f"s-{i:03d}" for i in range(40)] + WEIRD
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
}
BOUND = [label for label, ceiling in CEILINGS.items() if ceiling is not None]


# ------------------------------------------------------------------ fixture
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
    db.execute("ANALYZE")


@pytest.fixture(scope="module")
def database(tmp_path_factory, _sqlite_schema_template) -> SqliteDatabase:
    root: Path = tmp_path_factory.mktemp("id-binding")
    shutil.copyfile(_sqlite_schema_template, root / "test.db")
    settings = Settings(database_url=f"sqlite:///{root / 'test.db'}")
    database = SqliteDatabase(settings, root)
    with database.write() as db:
        _seed(db)
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
        sqlite_database._Conn.execute(db, "EXPLAIN QUERY PLAN " + sql, params)
    ]


def _single_ceiling_param(statements, ceiling) -> None:
    """Exactly one parameter carries the ceiling, as one JSON array."""
    assert statements, "the ceiling statement was not captured"
    payload = ids_param(ceiling)
    for sql, params in statements:
        assert sql.count(JSON_IDS) == sum(p == payload for p in params), sql
        assert len(params) < 16, (len(params), sql[:200])


# ------------------------------------------------------------ module API
def test_ids_param_is_one_deduplicated_deterministic_json_array():
    assert ids_param(["b", "a", "b", "", "  ", None, "a"]) == '["b","a"]'
    assert ids_param(("b", "a")) == '["b","a"]'
    assert ids_param({"b", "a", "c"}) == '["a","b","c"]'
    assert ids_param(frozenset({"z", "y"})) == '["y","z"]'
    assert ids_param([]) == "[]"
    assert json.loads(ids_param(WEIRD)) == WEIRD
    assert ids_param([7, "7", 7]) == '[7,"7"]'


@pytest.mark.parametrize("bad", ["s-001", b"s-001", bytearray(b"s")])
def test_ids_param_refuses_a_single_string(bad):
    with pytest.raises(TypeError):
        ids_param(bad)


@pytest.mark.parametrize("bad", [[1.5], [True], [b"x"]])
def test_ids_param_refuses_non_id_items(bad):
    with pytest.raises(TypeError):
        ids_param(bad)


def test_predicates_occupy_one_placeholder_and_only_member_forms_carry_plus():
    assert member_of("kos.source_id") == f"+kos.source_id IN {JSON_IDS}"
    assert not_member_of("c.source_id") == f"+c.source_id NOT IN {JSON_IDS}"
    assert drive_by("source_id") == f"source_id IN {JSON_IDS}"
    for fragment in (member_of("x"), not_member_of("x"), drive_by("x")):
        assert fragment.count("?") == 1
    with pytest.raises(ValueError):
        member_of(" ")


def test_integer_ids_still_match_text_ids_through_the_unary_plus():
    """The CAST in ``JSON_IDS``: the old placeholder form matched an integer id
    through the column's TEXT affinity; ``+column`` strips that affinity, so a
    JSON number must be cast back to TEXT to compare equal."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t(id TEXT PRIMARY KEY)")
    db.executemany("INSERT INTO t VALUES (?)", [("7",), ("8",), ("it's",)])
    for predicate in (member_of("t.id"), drive_by("t.id")):
        rows = db.execute(
            f"SELECT id FROM t WHERE {predicate} ORDER BY id",
            (ids_param([7, "it's", "missing"]),),
        ).fetchall()
        assert rows == [("7",), ("it's",)], predicate
    rows = db.execute(
        f"SELECT id FROM t WHERE {not_member_of('t.id')} ORDER BY id",
        (ids_param([7]),),
    ).fetchall()
    assert rows == [("8",), ("it's",)]


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
