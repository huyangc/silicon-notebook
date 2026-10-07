"""Shared twin cases: the Python viewer rule (``KgViewerScope``) against the
SQL fragments of ``memory_sql`` on ONE fixture set (plan §2 D2, E4-7).

Both backends run the same cases (tests/test_kg_viewer_scope_twins.py and
tests/postgres/test_kg_viewer_scope_twins_pg.py); each passes its own source
and Memory helpers, its placeholder, its JSON cast and its ``memory_sql``
module.

Fixture set (one shared notebook; A owns it, B is a member):

* ``src-plain``  a visible source
* ``src-kh``     a Knowhow projection (notebook-wide, readable by members)
* ``src-ma``     A's confirmed Memory
* ``src-mb``     B's confirmed Memory
* ``src-orphan`` a Memory source without an origin row (``memory_id`` NULL)
* ``src-lost``   a Memory source whose origin row is gone

and one object per source plus four MIXED-evidence objects:

* ``ko-mix-plain-both``  owned by ``src-plain``, cites ``src-mb`` and ``src-plain``
* ``ko-mix-plain-mb``    owned by ``src-plain``, cites ONLY ``src-mb`` -- the
  object the retired evidence rule hid; the SQL fragment keeps it
* ``ko-mix-mb-plain``    owned by ``src-mb``, cites ``src-plain``
* ``ko-mix-ma-mb``       owned by ``src-ma``, cites ``src-mb`` (cross-owner)

and one relation per source.
"""
from __future__ import annotations

import json

SOURCES = ("src-plain", "src-kh", "src-ma", "src-mb", "src-orphan", "src-lost")
MIXED = {
    "ko-mix-plain-both": ("src-plain", ["src-mb", "src-plain"]),
    "ko-mix-plain-mb": ("src-plain", ["src-mb"]),
    "ko-mix-mb-plain": ("src-mb", ["src-plain"]),
    "ko-mix-ma-mb": ("src-ma", ["src-mb"]),
}


def ev(source_id: str) -> dict:
    return {"source_id": source_id, "source_title": source_id,
            "element_id": f"el-{source_id}", "element_type": "paragraph",
            "location_label": "p", "quoted_span": f"quote {source_id}",
            "confidence": 1.0}


def seed_twin_world(repo, *, source, memory, ph: str, cast: str, now) -> dict:
    """Seed the fixture set; return ``{nb, a, b, objects: {id: (owner,
    evidence items)}}``."""
    from app.core.request_context import reset_request_user, set_request_user
    from app.models.notebooks import NotebookCreate

    a = repo.create_user("a00000011", "pw123456")
    b = repo.create_user("b00000012", "pw123456")
    token = set_request_user(a)
    try:
        nb = repo.create_notebook(NotebookCreate(name="twins")).id
        repo.add_member(nb, b.id)
    finally:
        reset_request_user(token)
    database = repo._runtime.database
    with database.write() as db:
        memory(db, nb, "mem-a", a.id)
        memory(db, nb, "mem-b", b.id)
        source(db, nb, "src-plain", elements=[("el-src-plain", "PLAIN")])
        source(db, nb, "src-kh", elements=[("el-src-kh", "KNOWHOW")])
        source(db, nb, "src-ma", memory_id="mem-a", elements=[("el-src-ma", "A")])
        source(db, nb, "src-mb", memory_id="mem-b", elements=[("el-src-mb", "B")])
        source(db, nb, "src-orphan", elements=[("el-src-orphan", "ORPHAN")])
        source(db, nb, "src-lost", elements=[("el-src-lost", "LOST")])
        for sid, stype, mid in (
            ("src-kh", "knowhow", None),
            ("src-orphan", "memory", None),
            ("src-lost", "memory", "mem-gone"),
        ):
            db.execute(
                f"UPDATE sources SET source_type={ph}, memory_id={ph} WHERE id={ph}",
                (stype, mid, sid))
    objects: dict = {}
    for sid in SOURCES:
        objects[f"ko-{sid}"] = (sid, [ev(sid)])
    objects.update({oid: (owner, [ev(s) for s in cited])
                    for oid, (owner, cited) in MIXED.items()})
    with database.write() as db:
        for index, (oid, (owner, items)) in enumerate(sorted(objects.items())):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                "payload,evidence,source_id,created_at,updated_at) VALUES "
                f"({ph},{ph},'concept','approved','',{ph}{cast},{ph}{cast},{ph},{ph},{ph})",
                (oid, nb, json.dumps({"name": oid}), json.dumps(items), owner,
                 now(index), now(index)))
        for index, sid in enumerate(SOURCES):
            db.execute(
                "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
                "source_object_id,target_object_id,edge_type,evidence,created_at) "
                f"VALUES ({ph},{ph},{ph},'ko-src-plain','ko-src-kh','rel',{ph}{cast},{ph})",
                (f"rel-{sid}", nb, sid, "[]", now(index)))
    return {"nb": nb, "a": a, "b": b, "objects": objects}


def _sql_sets(repo, memory_sql, nb: str, viewer: str, ph: str):
    """What the SQL fragments decide for ``viewer``: hidden objects, hidden
    relations, unreadable sources."""
    with repo._runtime.database.connect() as db:
        objects = {r["id"] for r in db.execute(
            "SELECT o.id FROM knowledge_objects o WHERE o.notebook_id=" + ph
            + " AND NOT " + memory_sql.foreign_memory_object_excluded("o"),
            (nb, viewer)).fetchall()}
        relations = {r["id"] for r in db.execute(
            "SELECT r.id FROM knowledge_relations r WHERE r.notebook_id=" + ph
            + " AND NOT " + memory_sql.foreign_memory_relation_excluded("r"),
            (nb, viewer)).fetchall()}
        unreadable = {r["id"] for r in db.execute(
            "SELECT s.id FROM sources s WHERE s.notebook_id=" + ph
            + " AND NOT " + memory_sql.memory_source_readable("s"),
            (nb, viewer)).fetchall()}
    return objects, relations, unreadable


def _spliced_forms(memory_sql, viewer: str):
    """The fragment forms the KG store READERS splice (E4-4), as far as this
    tree has them -- each ``(label, clause(alias) -> (sql, params))`` whose
    clause KEEPS a row: ``foreign_memory_in_notebook_excluded`` for a user,
    ``NOT memory_derived_in_notebook`` for the empty identity (the closed
    channel; also E4-2's), and ``memory_viewer_filter`` -- the one place the
    readers resolve ``viewer_id`` -- for both.  Before those land the list is
    empty; each form is checked from the moment it exists."""
    forms = []
    if viewer and hasattr(memory_sql, "foreign_memory_in_notebook_excluded"):
        forms.append(("foreign_memory_in_notebook_excluded", lambda alias: (
            " AND " + memory_sql.foreign_memory_in_notebook_excluded(alias), (viewer,))))
    if not viewer and hasattr(memory_sql, "memory_derived_in_notebook"):
        forms.append(("memory_derived_in_notebook", lambda alias: (
            " AND NOT " + memory_sql.memory_derived_in_notebook(alias), ())))
    if hasattr(memory_sql, "memory_viewer_filter"):
        forms.append(("memory_viewer_filter",
                      lambda alias: memory_sql.memory_viewer_filter(alias, viewer)))
    return forms


def _spliced_sets(repo, memory_sql, nb: str, viewer: str, ph: str) -> dict:
    """``{form: (hidden objects, hidden relations)}`` for every spliced form
    of this tree (``_spliced_forms``)."""
    out = {}
    with repo._runtime.database.connect() as db:
        every = {
            table: {r["id"] for r in db.execute(
                f"SELECT t.id FROM {table} t WHERE t.notebook_id=" + ph, (nb,)).fetchall()}
            for table in ("knowledge_objects", "knowledge_relations")
        }
        for label, clause in _spliced_forms(memory_sql, viewer):
            hidden = []
            for table in ("knowledge_objects", "knowledge_relations"):
                sql, params = clause("t")
                kept = {r["id"] for r in db.execute(
                    f"SELECT t.id FROM {table} t WHERE t.notebook_id=" + ph + sql,
                    (nb, *params)).fetchall()}
                hidden.append(every[table] - kept)
            out[label] = tuple(hidden)
    return out


def _python_sets(scope, world):
    objects = {
        oid for oid, (owner, _items) in world["objects"].items()
        if scope is not None and scope.row_hidden(owner)
    }
    relations = {
        f"rel-{sid}" for sid in SOURCES
        if scope is not None and scope.relation_hidden(sid)
    }
    unreadable = {
        sid for sid in SOURCES
        if scope is not None and scope.evidence_hidden(ev(sid))
    }
    return objects, relations, unreadable


def reader_of(repo):
    return repo._runtime.knowledge_query.viewer_scope.__self__


def as_user(user, fn, *args, **kwargs):
    from app.core.request_context import reset_request_user, set_request_user

    token = set_request_user(user)
    try:
        return fn(*args, **kwargs)
    finally:
        reset_request_user(token)


def assert_twins_agree(repo, world, memory_sql, ph: str, *, channel_open: bool,
                       monkeypatch) -> dict:
    """For A and for B: the Python rule and the SQL fragments hide the same
    objects and relations and find the same evidence sources unreadable.
    Returns the per-viewer hidden object sets for further assertions."""
    from app.services import kg_viewer_scope

    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed",
                        lambda: channel_open)
    out = {}
    for name in ("a", "b"):
        user = world[name]
        scope = as_user(user, reader_of(repo).for_notebook, world["nb"])
        viewer = user.id if channel_open else ""
        if scope is not None:
            assert scope.viewer_id == viewer
        python = _python_sets(scope, world)
        sql = _sql_sets(repo, memory_sql, world["nb"], viewer, ph)
        assert python == sql, (name, channel_open, python, sql)
        # S4: the forms the readers actually splice, once they exist.
        for label, spliced in _spliced_sets(
            repo, memory_sql, world["nb"], viewer, ph
        ).items():
            assert spliced == python[:2], (name, channel_open, label, python, spliced)
        out[name] = python[0]
    return out


def expected_hidden(name: str, *, channel_open: bool) -> set:
    """The objects the D2 rule hides from ``name`` -- written out, so a rule
    change that both twins made the same way still goes red."""
    orphans = {"ko-src-orphan", "ko-src-lost"}
    if not channel_open:
        return orphans | {"ko-src-ma", "ko-src-mb", "ko-mix-mb-plain", "ko-mix-ma-mb"}
    if name == "a":
        return orphans | {"ko-src-mb", "ko-mix-mb-plain"}
    return orphans | {"ko-src-ma", "ko-mix-ma-mb"}
