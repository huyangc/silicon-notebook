"""F2: the notebook list reads its per-notebook summary facts in one batch.

``NotebookSummaryQuery.list_for_user`` used to hydrate every listed notebook
with its own reads (visible source count, cached type counts, mounts,
``kg_ready``, pending count: 5-7 statements a notebook).  It now asks the
query store once (``notebook_list_summary_facts``) and hands each notebook's
entry to ``from_row(prefetched=...)``.  The ORACLE is the per-notebook path
itself -- ``from_row`` with ``prefetched=None`` reads one primitive at a time
(``_row_facts``), which is what the list did before and what every
single-notebook summary still does -- and the two lists must serialise
byte for byte (``[nb.model_dump_json()]``) across the cells the batch could
get wrong: the three list parts and a notebook in two of them, the viewer's
own Memory and another member's, the Memory channel closed (a token without
``memory:read``) and the store seam off, mounts the viewer may and may not
read, a base library and an ``everyone`` library, a notebook whose only KG
objects are deprecated, pending sources, the copying sentinel, and the count
cache cold and warm.

SQLite batches nothing: its ``notebook_list_summary_facts`` calls the same
primitives in the same order, which the statement trace pins.  PostgreSQL
twin (the batched statement, its statement count, the count cache's hit
check and its EXPLAIN pins): tests/postgres/test_notebook_list_prefetch_pg.py.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.request_context import reset_request_user, set_request_user
from app.models.schemas import NotebookCreate
from app.repositories.sqlite import knowledge_counts_cache as sqlite_counts_cache
from app.repositories.postgres import mount_sql as pg_mount_sql
from app.services import kg_viewer_scope, notebook_catalog
from app.services.source_scope import memory_access_context
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def sqlite_q(sql: str) -> str:
    return sql


def as_user(user, fn, *args, **kwargs):
    token = set_request_user(user)
    try:
        return fn(*args, **kwargs)
    finally:
        reset_request_user(token)


_T0 = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)


def _now(offset: int = 0) -> str:
    return (_T0 + datetime.timedelta(seconds=offset)).isoformat()


_TYPES = ("rule", "case", "checklist", "method", "risk", "glossary", "concept")


def build_list_world(repo, q) -> SimpleNamespace:
    """Three users and every cell the batched list could get wrong (see the
    module docstring).  ``q`` adapts ``?`` placeholders to the backend."""
    runtime = repo._runtime
    a = runtime.identity.create_user("a00000011", "pw123456")
    b = runtime.identity.create_user("b00000012", "pw123456")
    c = runtime.identity.create_user("c00000013", "pw123456")

    def notebook(user, name):
        return as_user(user, runtime.catalog.create_notebook, NotebookCreate(name=name)).id

    w = SimpleNamespace(a=a, b=b, c=c)
    w.a_plain = notebook(a, "a plain")
    w.a_memory = notebook(a, "a memory")
    w.a_deprecated = notebook(a, "a deprecated only")
    w.a_pending = notebook(a, "a pending")
    w.a_mounts = notebook(a, "a mounts")
    w.a_copying = notebook(a, "a copying")
    w.b_shared = notebook(b, "b shared")
    w.b_private = notebook(b, "b private")
    w.c_granted = notebook(c, "c granted")
    w.c_cross = notebook(c, "c cross")
    w.c_base = notebook(c, "c base library")
    w.c_public = notebook(c, "c everyone")
    w.c_private = notebook(c, "c private")
    runtime.sharing.add_member(w.b_shared, a.id)
    runtime.sharing.add_member(w.c_cross, a.id)

    with runtime.database.write() as db:
        def ex(sql, params=()):
            db.execute(q(sql), params)

        def source(nb, sid, *, kind="markdown", memory_id=None, element=True):
            ex("INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
               "file_name,file_path,file_size,file_hash,summary,doc_type,memory_id,"
               "created_at,updated_at) VALUES (?,?,?,?,'extracted','parsed','d.md','',0,'',"
               "'','',?,?,?)", (sid, nb, sid, kind, memory_id, _now(), _now()))
            if element:
                ex("INSERT INTO source_elements (id,source_id,element_type,location_label,"
                   "text,metadata,created_at) VALUES (?,?,'paragraph','p1','text','{}',?)",
                   (sid + "-el", sid, _now()))

        def objects(nb, sid, count, *, status="approved", tag=""):
            for i in range(count):
                ex("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
                   "payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,'{}','[]',?,?,?)",
                   (f"{sid}-{tag}o{i}", nb, _TYPES[i % len(_TYPES)], status, sid,
                    _now(i), _now(i)))

        def memory(nb, mid, owner, sid, count):
            ex("INSERT INTO memory_items (id,notebook_id,created_by,origin,status,title,"
               "content_md,created_at,updated_at) VALUES (?,?,?,'ask_answer','confirmed',"
               "'t','c',?,?)", (mid, nb, owner, _now(), _now()))
            source(nb, sid, kind="memory", memory_id=mid)
            objects(nb, sid, count)

        def mount(nb, base):
            ex("INSERT INTO notebook_bases (notebook_id,base_notebook_id,created_at,created_by) "
               "VALUES (?,?,?,NULL)", (nb, base, _now()))

        # Own notebooks: sources (one knowhow, one never parsed), objects of
        # every usable status and a deprecated one.
        for i in range(4):
            source(w.a_plain, f"src-ap-{i}")
        source(w.a_plain, "src-ap-kh", kind="knowhow")
        source(w.a_plain, "src-ap-raw", element=False)
        objects(w.a_plain, "src-ap-0", 9)
        objects(w.a_plain, "src-ap-1", 3, status="candidate", tag="c")
        objects(w.a_plain, "src-ap-2", 2, status="deprecated", tag="d")
        source(w.a_memory, "src-am-0")
        objects(w.a_memory, "src-am-0", 4)
        memory(w.a_memory, "mem-a-own", a.id, "src-am-mem", 5)
        source(w.a_deprecated, "src-ad-0")
        objects(w.a_deprecated, "src-ad-0", 3, status="deprecated")
        for i in range(3):
            source(w.a_pending, f"src-apd-{i}")
        objects(w.a_pending, "src-apd-0", 2)
        # b_shared: B's Memory and A's own Memory; B mounts B's private
        # library (A may not read it) and the base library.
        source(w.b_shared, "src-bs-0")
        objects(w.b_shared, "src-bs-0", 6)
        memory(w.b_shared, "mem-b-own", b.id, "src-bs-mem-b", 4)
        memory(w.b_shared, "mem-a-in-b", a.id, "src-bs-mem-a", 2)
        source(w.b_private, "src-bp-0")
        objects(w.b_private, "src-bp-0", 2)
        # c_granted: only C's Memory (A reads it as a holder without own).
        source(w.c_granted, "src-cg-0")
        memory(w.c_granted, "mem-c-own", c.id, "src-cg-mem", 3)
        source(w.c_base, "src-cb-0")
        objects(w.c_base, "src-cb-0", 2)
        ex("UPDATE notebooks SET tier='base' WHERE id=?", (w.c_base,))
        ex("UPDATE notebooks SET status='copying' WHERE id=?", (w.a_copying,))
        # A group holding A; c_granted granted to it, c_cross both a member
        # notebook and granted (admin edge), so it sits in two list parts.
        ex("INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
           "VALUES ('grp-list','list group','project','',?,?,?)", (c.id, _now(), _now()))
        ex("INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
           "VALUES ('grp-list',?,'member',?,?)", (a.id, _now(), c.id))
        ex("INSERT INTO notebook_grants (id,notebook_id,principal_type,principal_id,role,"
           "created_by,created_at) VALUES ('gr-cg',?,'group','grp-list','viewer',?,?)",
           (w.c_granted, c.id, _now()))
        ex("INSERT INTO notebook_grants (id,notebook_id,principal_type,principal_id,role,"
           "created_by,created_at) VALUES ('gr-cx',?,'group_admins','grp-list','admin',?,?)",
           (w.c_cross, c.id, _now()))
        ex("INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
           "VALUES ('grp-list',?,'admin',?,?)", (b.id, _now(), c.id))
        ex("INSERT INTO notebook_grants (id,notebook_id,principal_type,principal_id,role,"
           "created_by,created_at) VALUES ('gr-pub',?,'everyone','','viewer',?,?)",
           (w.c_public, c.id, _now()))
        source(w.c_public, "src-cp-0")
        objects(w.c_public, "src-cp-0", 1)
        # Mounts: readable (own, base, everyone), unreadable to the viewer
        # (B's and C's private libraries).
        mount(w.a_mounts, w.c_base)
        mount(w.a_mounts, w.a_plain)
        mount(w.a_mounts, w.c_public)
        mount(w.a_mounts, w.c_private)
        mount(w.b_shared, w.b_private)
        mount(w.b_shared, w.c_base)
        mount(w.c_granted, w.c_private)
        mount(w.c_granted, w.c_base)
    return w


def old_list(summaries, user_id, monkeypatch):
    """The per-notebook path: no prefetched facts, every ``from_row`` reads
    one primitive at a time (the list before F2)."""
    with monkeypatch.context() as patch:
        patch.setattr(summaries, "_list_facts", lambda *_a, **_k: {})
        return summaries.list_for_user(user_id)


def dumps(listed) -> list[str]:
    return [nb.model_dump_json() for nb in listed]


def assert_list_matches_the_per_notebook_path(repo, w, cache, q, monkeypatch) -> None:
    """Every viewer, cold and warm caches, channel open/closed, seam on/off:
    the batched list and the per-notebook list are byte-identical.  The
    small-list gate is opened (every list batched) so each viewer's list
    exercises the batch; ``test_*_small_list_gate`` pins the gate itself.
    Last, the world changes between two warm lists (objects added and
    removed, ``kg_mutation_seq`` advanced): the batched list must show it."""
    monkeypatch.setattr(notebook_catalog, "LIST_PREFETCH_MIN_NOTEBOOKS", 0)
    summaries = repo._runtime.notebook_summaries

    def both(user_id, *, cold):
        if cold:
            cache.invalidate(None)
        old = dumps(old_list(summaries, user_id, monkeypatch))
        if cold:
            cache.invalidate(None)
        new = dumps(summaries.list_for_user(user_id))
        return old, new

    cells = {}
    for seam in (True, False):
        monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", seam)
        for user in (w.a, w.b, w.c):
            for cold in (True, False, False):
                for channel in (True, False):
                    with memory_access_context(channel):
                        old, new = both(user.id, cold=cold)
                    assert new == old, (seam, user.username, cold, channel)
                    cells[(seam, user.username, channel)] = new
    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", True)

    # The cells discriminate: the scenario is not one in which the batch
    # could only ever agree.
    open_a = {nb["id"]: nb for nb in map(_load, cells[(True, w.a.username, True)])}
    closed_a = {nb["id"]: nb for nb in map(_load, cells[(True, w.a.username, False)])}
    assert set(open_a) == {
        w.a_plain, w.a_memory, w.a_deprecated, w.a_pending, w.a_mounts,
        w.b_shared, w.c_cross, w.c_granted,
    }
    assert [nb["id"] for nb in map(_load, cells[(True, w.a.username, True)])].count(
        w.c_cross) == 1
    assert open_a[w.a_memory]["counts"] != closed_a[w.a_memory]["counts"]
    assert open_a[w.b_shared]["counts"] != closed_a[w.b_shared]["counts"]
    assert open_a[w.a_deprecated]["kg_ready"] is True
    assert sum(open_a[w.a_deprecated]["counts"][k] for k in _COUNT_KEYS) == 0
    assert open_a[w.a_pending]["kg_pending_sources"] == 2
    assert open_a[w.a_plain]["counts"]["sources"] == 5
    assert [ref["id"] for ref in open_a[w.a_mounts]["base_notebooks"]] == [
        w.c_base, w.a_plain, w.c_public]
    assert open_a[w.a_mounts]["base_kg_notebook_ids"] == [w.c_base, w.a_plain, w.c_public]
    assert [ref["id"] for ref in open_a[w.b_shared]["base_notebooks"]] == [w.c_base]
    open_b = {nb["id"]: nb for nb in map(_load, cells[(True, w.b.username, True)])}
    assert [ref["id"] for ref in open_b[w.b_shared]["base_notebooks"]] == [
        w.c_base, w.b_private]
    assert open_a[w.c_cross]["access"] == "reader"
    assert open_a[w.c_granted]["granted_via"] and not open_a[w.c_cross]["granted_via"]
    assert_a_warm_list_follows_a_change(repo, w, q, monkeypatch)


def assert_a_warm_list_follows_a_change(repo, w, q, monkeypatch) -> None:
    """Warm memos hold the old key; the world changes and the notebooks'
    ``kg_mutation_seq`` advances: the batched list (read FIRST, before the
    per-notebook path could refresh any memo) shows the new counts."""
    summaries = repo._runtime.notebook_summaries
    for _ in range(2):
        before = {nb.id: nb for nb in summaries.list_for_user(w.a.id)}
    with repo._runtime.database.write() as db:
        for i in range(3):
            db.execute(q(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
                "payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,'rule','approved','{}','[]','src-ap-3',?,?)"),
                (f"src-ap-3-new{i}", w.a_plain, _now(), _now()))
        db.execute(q("DELETE FROM knowledge_objects WHERE id=?"), ("src-am-0-o0",))
        db.execute(q(
            "UPDATE unified_kg_state SET kg_mutation_seq = kg_mutation_seq + 1 "
            "WHERE notebook_id IN (?,?)"), (w.a_plain, w.a_memory))
    after = {nb.id: nb for nb in summaries.list_for_user(w.a.id)}
    assert after[w.a_plain].counts["rules"] == before[w.a_plain].counts["rules"] + 3
    assert after[w.a_memory].counts["rules"] == before[w.a_memory].counts["rules"] - 1
    assert dumps(after.values()) == dumps(old_list(summaries, w.a.id, monkeypatch))


_COUNT_KEYS = ("rules", "cases", "checklist_items", "methods", "risks", "glossary")


def _load(text: str) -> dict:
    import json

    return json.loads(text)


def test_sqlite_list_matches_the_per_notebook_path(repo, monkeypatch):
    w = build_list_world(repo, sqlite_q)
    assert_list_matches_the_per_notebook_path(
        repo, w, sqlite_counts_cache, sqlite_q, monkeypatch)


def test_sqlite_small_list_gate(repo, monkeypatch):
    """A list shorter than ``LIST_PREFETCH_MIN_NOTEBOOKS`` never calls the
    batched read (its statement may be unprepared; see the constant); a
    list of exactly that many does, once."""
    runtime = repo._runtime
    queries = runtime.queries
    calls: list[int] = []
    real = queries.notebook_list_summary_facts

    def spy(db, ids, *args, **kwargs):
        calls.append(len(ids))
        return real(db, ids, *args, **kwargs)

    monkeypatch.setattr(queries, "notebook_list_summary_facts", spy)
    user = runtime.identity.create_user("g00000031", "pw123456")
    gate = notebook_catalog.LIST_PREFETCH_MIN_NOTEBOOKS
    assert gate == 5
    for i in range(gate - 1):
        as_user(user, runtime.catalog.create_notebook, NotebookCreate(name=f"g{i}"))
    assert len(runtime.notebook_summaries.list_for_user(user.id)) == gate - 1
    assert calls == []
    as_user(user, runtime.catalog.create_notebook, NotebookCreate(name="last"))
    assert len(runtime.notebook_summaries.list_for_user(user.id)) == gate
    assert calls == [gate]


def test_the_mount_where_has_one_spelling():
    """``MOUNT_VIEWER_JOIN`` IS ``mount_viewer_join_on("%s")`` (bytes
    unchanged), and the mounter can only be ``%s`` or a column reference."""
    assert pg_mount_sql.MOUNT_VIEWER_JOIN == (
        "FROM notebook_bases e "
        "JOIN notebooks b ON b.id = e.base_notebook_id "
        "JOIN notebooks a ON a.id = e.notebook_id "
        "CROSS JOIN (SELECT NULLIF(CAST(%s AS text), '') AS uid) v "
        "WHERE e.notebook_id = %s AND b.id != e.notebook_id"
    )
    assert pg_mount_sql.mount_viewer_join_on("n.id").endswith(
        "WHERE e.notebook_id = n.id AND b.id != e.notebook_id")
    for bad in ("'nb1'", "n.id OR TRUE", "n.id; DROP TABLE x", "", "1"):
        with pytest.raises(ValueError):
            pg_mount_sql.mount_viewer_join_on(bad)


def test_sqlite_list_issues_the_per_notebook_statements_in_order(repo, monkeypatch):
    """SQLite batches nothing: the list's statements, cold and warm, are the
    per-notebook path's statements in the same order (``from_row`` per
    notebook), so its plans and costs are unchanged by construction."""
    w = build_list_world(repo, sqlite_q)
    summaries = repo._runtime.notebook_summaries
    database = repo._runtime.database
    real_connect = database.connect

    def traced(run):
        statements: list[str] = []

        def connect(*args, **kwargs):
            manager = real_connect(*args, **kwargs)

            class Traced:
                def __enter__(self):
                    db = manager.__enter__()
                    db.set_trace_callback(statements.append)
                    return db

                def __exit__(self, *exc):
                    return manager.__exit__(*exc)

            return Traced()

        with monkeypatch.context() as patch:
            patch.setattr(database, "connect", connect)
            run()
        return statements

    for cold in (True, False):
        if cold:
            sqlite_counts_cache.invalidate(None)
        old = traced(lambda: old_list(summaries, w.a.id, monkeypatch))
        if cold:
            sqlite_counts_cache.invalidate(None)
        new = traced(lambda: summaries.list_for_user(w.a.id))
        assert new == old, cold
        assert len(new) > 8 * 5
