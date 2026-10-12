"""K3 (SQLite mirror of ``postgres/test_memory_live_predicate_explain_pins.py``):
the notebook-lifecycle predicate leaves every reader's query plan unchanged.

The new statement of every Memory reader and of the self-service ask_jobs /
reports reads is captured from the production method that issues it, on a
seeded, ANALYZEd world (a few hundred notebooks, ~10^4 Memory rows, one heavy
user). The old statement is recovered by dropping the lifecycle tail (or the
bell's projected liveness column); ``EXPLAIN QUERY PLAN`` of old and new must
be identical row for row. Runs on whatever SQLite the interpreter links —
CI's 3.45 and the local 3.53 both (``DYLD_LIBRARY_PATH`` to a conda-forge
``libsqlite=3.45.1`` reproduces CI locally).
"""
from __future__ import annotations

import random
from contextlib import contextmanager
from typing import Any

import pytest

from app.core.config import Settings
from app.repositories.sqlite.access_sql import NOTEBOOK_LIVE_SQL
from app.services.sqlite_repository import SQLiteRepository

_NOW = "2026-10-01T00:00:00"
HEAVY = "k3-heavy"
LIVE_TAIL = f" AND access_nb.{NOTEBOOK_LIVE_SQL})"
LIVE_PROJECTION = f", (nb.{NOTEBOOK_LIVE_SQL}) AS notebook_live"
N_NOTEBOOKS, N_LIGHT, N_MEMORIES, N_HEAVY = 300, 40, 12000, 4800


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _owner(i: int) -> str:
    return HEAVY if i % 10 == 0 else f"k3-u-{i % N_LIGHT}"


def _status(i: int) -> str:
    if i % 197 == 5:
        return "deleting"
    if i % 211 == 7:
        return "importing"
    return "ready"


def seed(repo) -> tuple[str, str, list[str]]:
    rng = random.Random(3)
    users = [HEAVY, *(f"k3-u-{g}" for g in range(N_LIGHT))]
    heavy_nbs = [
        f"k3-nb-{i}" for i in range(N_NOTEBOOKS) if i % 10 == 0 or i % 20 == 3
    ]
    with repo._runtime.database.write() as db:
        db.executemany(
            "INSERT INTO users (id,email,display_name,role,status,username,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            [(u, f"{u}@x", u, "user", "active", u, _NOW, _NOW) for u in users],
        )
        db.executemany(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            [(f"k3-nb-{i}", f"NB {i}", _owner(i), _status(i), _NOW, _NOW)
             for i in range(N_NOTEBOOKS)],
        )
        members = [
            (f"k3-nb-{i}", f"k3-u-{(i + 1) % N_LIGHT}", "reader", _NOW)
            for i in range(N_NOTEBOOKS) if i % 10
        ] + [
            (f"k3-nb-{i}", HEAVY, "reader", _NOW)
            for i in range(N_NOTEBOOKS) if i % 20 == 3
        ]
        db.executemany(
            "INSERT OR IGNORE INTO notebook_members (notebook_id,user_id,role,added_at) "
            "VALUES (?,?,?,?)",
            members,
        )
        db.execute(
            "INSERT INTO groups (id,name,kind,description,created_by,owner_id,"
            "created_at,updated_at) VALUES ('k3-grp','g','project','',?,?,?,?)",
            (HEAVY, HEAVY, _NOW, _NOW),
        )
        db.executemany(
            "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
            "VALUES ('k3-grp',?,'member',?,?)",
            [(u, _NOW, HEAVY) for u in users[::7]],
        )
        db.executemany(
            "INSERT INTO notebook_grants (id,notebook_id,principal_type,principal_id,"
            "role,created_by,created_at) VALUES (?,?,'group','k3-grp','viewer',?,?)",
            [(f"k3-gnt-{i}", f"k3-nb-{i}", HEAVY, _NOW)
             for i in range(N_NOTEBOOKS) if i % 25 == 7],
        )
        memories = []
        for g in range(N_MEMORIES):
            if g < N_HEAVY:
                nb, creator = heavy_nbs[(g * 31) % len(heavy_nbs)], HEAVY
            else:
                i = g % N_NOTEBOOKS
                i = i + 1 if i % 10 == 0 else i
                nb, creator = f"k3-nb-{i}", _owner(i)
            status = "confirmed" if g % 10 < 6 else "candidate" if g % 10 < 9 else "deprecated"
            stamp = f"2026-09-{1 + g % 28:02d}T{g % 24:02d}:{g % 60:02d}:00"
            memories.append((
                f"k3-mem-{g}", nb, creator, f"ans-{g}" if g % 3 == 0 else None,
                "ask_answer", status, f"memory {g}",
                f"body {rng.random()}" + (" zebra" if g % 50 == 0 else ""),
                stamp, stamp,
            ))
        db.executemany(
            "INSERT INTO memory_items (id,notebook_id,created_by,source_answer_id,"
            "origin,status,title,content_md,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            memories,
        )
        db.executemany(
            "INSERT INTO memory_provenance (id,memory_id,origin,payload_json,created_at) "
            "VALUES (?,?,'ask_answer','{}',?)",
            [(f"prov-{m[0]}", m[0], m[8]) for m in memories],
        )
        db.executemany(
            "INSERT INTO memory_embeddings (memory_id,model,dimension,vector,updated_at) "
            "VALUES (?,'fake',16,?,?)",
            [(m[0], bytes(64), m[9]) for m in memories if m[5] == "confirmed"],
        )
        for g in range(4000):
            i = g % N_NOTEBOOKS
            creator = HEAVY if g % 5 == 0 and (i % 10 == 0 or i % 20 == 3) else _owner(i)
            stamp = f"2026-09-{1 + g % 28:02d}T{g % 24:02d}:00:00"
            db.execute(
                "INSERT INTO ask_jobs (id,notebook_id,created_by,mode,question,status,"
                "created_at,updated_at) VALUES (?,?,?,'chunk','q?','completed',?,?)",
                (f"k3-ask-{g}", f"k3-nb-{i}", creator, stamp, stamp),
            )
            if g < 1500:
                db.execute(
                    "INSERT INTO reports (id,notebook_id,question,status,created_by,"
                    "created_at,updated_at) VALUES (?,?,'r?',?,?,?,?)",
                    (f"k3-rep-{g}", f"k3-nb-{i}",
                     "outline_ready" if g % 17 == 0 else "done", creator, stamp, stamp),
                )
        db.execute("ANALYZE")
    with repo._runtime.database.connect() as db:
        target = db.execute(
            "SELECT m.notebook_id FROM memory_items m JOIN notebooks n "
            "ON n.id=m.notebook_id AND n.status='ready' WHERE m.created_by=? "
            "GROUP BY m.notebook_id ORDER BY COUNT(*) DESC, m.notebook_id LIMIT 1",
            (HEAVY,),
        ).fetchone()["notebook_id"]
        memory_id = db.execute(
            "SELECT id FROM memory_items WHERE created_by=? AND notebook_id=? "
            "AND status='confirmed' ORDER BY id LIMIT 1",
            (HEAVY, target),
        ).fetchone()["id"]
        answers = [
            row["source_answer_id"] for row in db.execute(
                "SELECT source_answer_id FROM memory_items WHERE created_by=? "
                "AND notebook_id=? AND source_answer_id IS NOT NULL ORDER BY id LIMIT 20",
                (HEAVY, target),
            ).fetchall()
        ]
    return target, memory_id, answers


class _Recording:
    def __init__(self, inner, sink):
        self._inner, self._sink = inner, sink

    def execute(self, sql, params=(), *args, **kwargs):
        self._sink.append((str(sql), params))
        return self._inner.execute(sql, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    # ``SqliteDatabase.connect()`` returns the thread's connection itself: it
    # is used both bare and as ``with connect() as db``.
    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


@contextmanager
def _recorded(repo, monkeypatch):
    database = repo._runtime.database
    statements: list[tuple[str, Any]] = []
    with monkeypatch.context() as patch:
        connect, write = database.connect, database.write

        def recording_write(*args, **kwargs):
            @contextmanager
            def manager():
                with write(*args, **kwargs) as db:
                    yield _Recording(db, statements)

            return manager()

        patch.setattr(database, "connect", lambda: _Recording(connect(), statements))
        patch.setattr(database, "write", recording_write)
        yield statements


def _old(sql: str) -> str:
    if LIVE_TAIL in sql:
        return sql.replace(LIVE_TAIL, ")")
    return sql.replace(LIVE_PROJECTION, "")


def _plan(db, sql: str, params) -> list[tuple[int, str]]:
    """The plan tree as (parent row index, detail) per row. The raw ids are
    VDBE addresses: the extra filter opcodes shift them without any change
    to the plan, so they are renumbered by row order."""
    rows = db.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    order = {row[0]: index for index, row in enumerate(rows)}
    return [(order.get(row[1], -1), row[3]) for row in rows]


def test_live_predicate_keeps_every_sqlite_reader_plan(repo, monkeypatch):
    target, mid, answers = seed(repo)
    store = repo._runtime.memory_store
    queries = repo._runtime.queries
    stale = store.memory_for_user(mid, HEAVY)
    stale = stale.model_copy(update={"title": stale.title + " (stale)"})

    def promote():
        try:
            with repo._runtime.database.write() as db:
                store.propose_promotion_on(db, mid, HEAVY, "p-k3", [], [], stale, _NOW)
        except ValueError:
            pass

    def transition_fallback():
        try:
            store.transition(mid, HEAVY, {"rejected"}, "deprecated")
        except ValueError:
            pass

    def page(**kwargs):
        args = dict(notebook_id=None, status=None, origin=None, query="",
                    offset=0, limit=50)
        args.update(kwargs)
        return store.list_memories(HEAVY, **args)

    readers = {
        "memory_for_user": lambda: store.memory_for_user(mid, HEAVY),
        "answer_memory_links": lambda: store.answer_memory_links(target, HEAVY, answers),
        "propose_promotion_on": promote,
        "update_fields": lambda: store.update_fields(mid, HEAVY, {"title": "t"}),
        "transition": transition_fallback,
        "export": lambda: store.memory_export_snapshot(
            target, HEAVY, page_size=500, max_bytes=10**9
        ),
        "list_memories.all": lambda: page(),
        "list_memories.notebook": lambda: page(notebook_id=target, status="confirmed"),
        "list_memories.search": lambda: page(query="zebra"),
        "memory_retrieval_rows": lambda: store.memory_retrieval_rows(
            HEAVY, target, ("candidate", "confirmed"), "zebra",
            lexical_limit=40, vector_limit=200,
        ),
        "activity.ask_only": lambda: queries.list_user_activity(HEAVY, activity_type="ask"),
        "activity.report_only": lambda: queries.list_user_activity(
            HEAVY, activity_type="report"
        ),
        "bell.report_outlines": lambda: queries.pending_actions_projection_rows(HEAVY),
    }
    differing = {}
    compared = 0
    for label, call in readers.items():
        with _recorded(repo, monkeypatch) as statements:
            call()
        live = [
            (sql, params) for sql, params in statements
            if LIVE_TAIL in sql or LIVE_PROJECTION in sql
        ]
        assert live, f"{label}: no statement carries the lifecycle predicate"
        with repo._runtime.database.connect() as db:
            for index, (sql, params) in enumerate(live):
                old_sql = _old(sql)
                assert old_sql != sql
                old, new = _plan(db, old_sql, params), _plan(db, sql, params)
                compared += 1
                if old != new:
                    differing[(label, index)] = (old, new)
    assert compared >= 25, compared
    assert differing == {}, differing
