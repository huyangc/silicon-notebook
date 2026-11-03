"""``closes_when_shared`` on the mount projection, shared by both backends
(share-edit P1-T8, decision D13).

``test_mount_closes_when_shared.py`` runs the case on SQLite and
``postgres/test_mount_closes_when_shared_pg.py`` on PostgreSQL. The flag says
an edge is valid now and only through the borrowed arm (a library someone else
shared with the mounting owner), so sharing the mounting notebook -- adding
its first editor -- would close it for everybody. It is read through the
production entry point ``notebook_store.list_mount_edges_for_notebook`` with
``with_closes_when_shared=True`` (what the owner's ``/bases`` projection
passes); without it -- the retrieval path -- the column is not computed at all.

Alice mounts on her unshared notebook ``nb-x``:

* her own library                                  -> False (same owner)
* a public library (``tier='base'``)               -> False
* a library readable through an ``everyone`` grant -> False (that arm has no gate)
* a library also everyone-readable besides being shared with her -> False
* a library shared with her by name (user grant)   -> True
* a library she reads as a link member             -> True
* a library shared with her through a group grant  -> True
* an owner-less library (``created_by`` NULL) shared with her by name -> True
  (the same-owner test is NULL-safe: such a library is valid only through the
  borrowed arm)

and the same borrowed library on her already-shared notebook ``nb-shared``
(a member row there) -> False: the gate is already closed, the edge already
inactive, so it is not listed again as "will close".
"""
from __future__ import annotations

from typing import Any

_STAMP = "2026-10-28T00:00:00+00:00"

USERS = ("u-alice", "u-carol", "u-bob")
NOTEBOOKS = (
    # (id, owner, tier)
    ("nb-x", "u-alice", "personal"),
    ("nb-shared", "u-alice", "personal"),
    ("nb-own", "u-alice", "personal"),
    ("nb-public", "u-carol", "base"),
    ("nb-everyone", "u-carol", "personal"),
    ("nb-everyone-named", "u-carol", "personal"),
    ("nb-named", "u-carol", "personal"),
    ("nb-member", "u-carol", "personal"),
    ("nb-group", "u-carol", "personal"),
    ("nb-ownerless", None, "personal"),
)
GRANTS = (
    # (id, notebook, principal_type, principal_id)
    ("g-everyone", "nb-everyone", "everyone", ""),
    ("g-everyone-2", "nb-everyone-named", "everyone", ""),
    ("g-everyone-named", "nb-everyone-named", "user", "u-alice"),
    ("g-named", "nb-named", "user", "u-alice"),
    ("g-group", "nb-group", "group", "grp-alice"),
    ("g-ownerless", "nb-ownerless", "user", "u-alice"),
)
MEMBERS = (("nb-member", "u-alice"), ("nb-shared", "u-bob"))
MOUNTED = (
    "nb-own", "nb-public", "nb-everyone", "nb-everyone-named", "nb-named",
    "nb-member", "nb-group", "nb-ownerless",
)

#: mounting notebook -> {mounted library: (active, closes_when_shared)}
EXPECTED = {
    "nb-x": {
        "nb-own": (True, False),
        "nb-public": (True, False),
        "nb-everyone": (True, False),
        "nb-everyone-named": (True, False),
        "nb-named": (True, True),
        "nb-member": (True, True),
        "nb-group": (True, True),
        "nb-ownerless": (True, True),
    },
    "nb-shared": {
        "nb-own": (True, False),
        "nb-public": (True, False),
        "nb-everyone": (True, False),
        "nb-everyone-named": (True, False),
        "nb-named": (False, False),
        "nb-member": (False, False),
        "nb-group": (False, False),
        "nb-ownerless": (False, False),
    },
}


def seed(repo: Any, *, postgres: bool) -> None:
    def text(sql: str) -> str:
        return sql.replace("?", "%s") if postgres else sql

    with repo._runtime.database.write() as db:
        for uid in USERS:
            db.execute(text(
                "INSERT INTO users "
                "(id,email,display_name,username,password_hash,role,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)"
            ), (uid, f"{uid}@t", uid, uid, "x", "user", _STAMP, _STAMP))
        db.execute(text(
            "INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)"
        ), ("grp-alice", "grp", "project", "", "u-carol", _STAMP, _STAMP))
        db.execute(text(
            "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
            "VALUES (?,?,?,?,?)"
        ), ("grp-alice", "u-alice", "member", _STAMP, "u-carol"))
        for notebook_id, owner, tier in NOTEBOOKS:
            db.execute(text(
                "INSERT INTO notebooks "
                "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,tier) "
                "VALUES (?,?,?,?,?,?,?,?,?)"
            ), (notebook_id, notebook_id, "", "Semiconductor", "draft", owner, _STAMP,
                _STAMP, tier))
        for grant_id, notebook_id, principal_type, principal_id in GRANTS:
            db.execute(text(
                "INSERT INTO notebook_grants "
                "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?)"
            ), (grant_id, notebook_id, principal_type, principal_id, "viewer", "u-carol",
                _STAMP))
        for notebook_id, user_id in MEMBERS:
            db.execute(text(
                "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
                "VALUES (?,?,?,?)"
            ), (notebook_id, user_id, "reader", _STAMP))
        for mounter in EXPECTED:
            for base in MOUNTED:
                db.execute(text(
                    "INSERT INTO notebook_bases "
                    "(notebook_id,base_notebook_id,created_at,created_by) VALUES (?,?,?,?)"
                ), (mounter, base, _STAMP, "u-alice"))


def closes_when_shared_truth_table(repo: Any, *, postgres: bool) -> None:
    seed(repo, postgres=postgres)
    store = repo._runtime.notebook_store
    for mounter, expected in EXPECTED.items():
        edges = {
            edge["id"]: edge
            for edge in store.list_mount_edges_for_notebook(
                mounter, "u-alice", with_closes_when_shared=True
            )
        }
        assert set(edges) == set(expected), mounter
        for base, (active, closes) in expected.items():
            edge = edges[base]
            assert (edge["active"], edge["closes_when_shared"]) == (active, closes), (
                mounter, base, edge,
            )
            assert edge["closes_when_shared"] is closes
        # the facade (owner's /bases projection) computes it
        assert {
            edge["id"]: edge["closes_when_shared"]
            for edge in repo.list_notebook_bases(mounter, "u-alice")
        } == {base: closes for base, (_active, closes) in expected.items()}


class _Recording:
    def __init__(self, connection, statements):
        self._connection = connection
        self._statements = statements

    def execute(self, query, params=None, *args, **kwargs):
        self._statements.append(str(query))
        return self._connection.execute(query, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def retrieval_path_does_not_compute_it(repo: Any, *, postgres: bool, monkeypatch) -> None:
    """The retrieval path's read of the same edges (``_valid_participant_ids``,
    no flag) issues the statement it issued before the column existed: one
    statement, no closes_when_shared expression in it."""
    from contextlib import contextmanager

    seed(repo, postgres=postgres)
    store = repo._runtime.notebook_store
    statements: list[str] = []
    original = store.database.connect

    @contextmanager
    def recording(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            yield _Recording(connection, statements)

    monkeypatch.setattr(store.database, "connect", recording)
    edges = store.list_mount_edges_for_notebook("nb-x")
    monkeypatch.undo()
    assert len(statements) == 1
    assert "closes_when_shared" not in statements[0]
    assert {edge["id"] for edge in edges if edge["active"]} == set(EXPECTED["nb-x"])
    assert not any(edge["closes_when_shared"] for edge in edges)
