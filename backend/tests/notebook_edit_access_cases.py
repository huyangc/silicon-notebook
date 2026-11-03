"""Edit permission predicate and access-level lookup, shared by both backends
(share-edit P1-T1).

``test_notebook_edit_access.py`` runs every case on SQLite and
``postgres/test_notebook_edit_access_pg.py`` runs the same cases on
PostgreSQL. The world puts one user in each of the eight access shapes the
predicates distinguish (owner, named editor, group admin, group viewer, link
member, stranger, and the two legacy named rows ``(user, admin)`` /
``(user, viewer)``) on one notebook, and every case reads through the
production entry points: the sharing store's ``user_can_*`` /
``notebook_access_level`` and the published ``access_sql`` statements.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.request_context import reset_request_user, set_request_user
from app.models.schemas import NotebookCreate

_STAMP = "2026-10-28T00:00:00+00:00"


class _Rollback(Exception):
    """Unwinds a write transaction a case opened only to observe a deletion."""


def access_sql_module(postgres: bool):
    if postgres:
        from app.repositories.postgres import access_sql
    else:
        from app.repositories.sqlite import access_sql
    return access_sql


@dataclass
class World:
    repo: Any
    postgres: bool
    users: dict[str, str] = field(default_factory=dict)
    profiles: dict[str, Any] = field(default_factory=dict)
    groups: dict[str, str] = field(default_factory=dict)
    notebook: str = ""
    owner_id: str = ""

    def sql(self, text: str) -> str:
        return text.replace("?", "%s") if self.postgres else text

    def write(self, statements: list[tuple[str, tuple]]) -> None:
        with self.repo._runtime.database.write() as db:
            for text, params in statements:
                db.execute(self.sql(text), params)

    @property
    def store(self):
        return self.repo._runtime.sharing_store

    @property
    def access_sql(self):
        return access_sql_module(self.postgres)


def _as_user(user: Any, fn: Callable[[], Any]) -> Any:
    token = set_request_user(user)
    try:
        return fn()
    finally:
        reset_request_user(token)


def _grant_row(notebook_id: str, principal_type: str, principal_id: str, role: str,
               grant_id: str) -> tuple[str, tuple]:
    return (
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (grant_id, notebook_id, principal_type, principal_id, role, None, _STAMP),
    )


# The eight shapes and what every predicate must answer for them on the world
# notebook: (level, read, edit, admin, write).
SHAPES: dict[str, tuple[str | None, bool, bool, bool, bool]] = {
    "owner": ("owner", True, True, True, True),
    "editor": ("edit", True, True, False, False),
    "group_admin": ("admin", True, True, True, False),
    "group_viewer": ("read", True, False, False, False),
    "link_member": ("read", True, False, False, False),
    "stranger": (None, False, False, False, False),
    "legacy_user_admin": ("admin", True, True, True, False),
    "legacy_user_viewer": ("read", True, False, False, False),
}


def build_world(repo: Any, *, postgres: bool) -> World:
    world = World(repo=repo, postgres=postgres)
    identity = repo._runtime.identity
    profiles = {}
    for index, shape in enumerate(SHAPES):
        profile = identity.create_user(f"e{20261000 + index:08d}", "pw123456")
        profiles[shape] = profile
        world.profiles[shape] = profile
        world.users[shape] = profile.id
    owner = profiles["owner"]
    world.owner_id = owner.id
    world.notebook = _as_user(
        owner, lambda: repo.create_notebook(NotebookCreate(name="Edit world"))
    ).id
    nb = world.notebook
    world.groups = {"admins": "grp-edit-admins", "viewers": "grp-edit-viewers"}
    statements: list[tuple[str, tuple]] = []
    for group_id in world.groups.values():
        statements.append((
            "INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (group_id, group_id, "project", "", owner.id, _STAMP, _STAMP),
        ))
    for group_key, shape, role in (
        ("admins", "group_admin", "admin"),
        ("viewers", "group_viewer", "member"),
        # a plain member of the admins group: the group_admins edge must not reach it
        ("admins", "stranger", "member"),
    ):
        statements.append((
            "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
            "VALUES (?,?,?,?,?)",
            (world.groups[group_key], world.users[shape], role, _STAMP, owner.id),
        ))
    statements += [
        _grant_row(nb, "user", world.users["editor"], "editor", "gw-editor"),
        _grant_row(nb, "group_admins", world.groups["admins"], "admin", "gw-gadmins"),
        _grant_row(nb, "group", world.groups["viewers"], "viewer", "gw-gviewers"),
        _grant_row(nb, "user", world.users["legacy_user_admin"], "admin", "gw-uadmin"),
        _grant_row(nb, "user", world.users["legacy_user_viewer"], "viewer", "gw-uviewer"),
    ]
    world.write(statements)
    repo._runtime.sharing.add_member(nb, world.users["link_member"])
    return world


def _predicates(world: World, notebook_id: str, user_id: str) -> tuple[bool, bool, bool, bool]:
    store = world.store
    return (
        store.user_can_read_notebook(notebook_id, user_id),
        store.user_can_edit_notebook(notebook_id, user_id),
        store.user_can_admin_notebook(notebook_id, user_id),
        store.user_can_access_notebook(notebook_id, user_id),
    )


# ---------------------------------------------------------------- cases


def edit_predicate_parity_sqlite_pg(world: World) -> None:
    """Each backend answers the same table (both twins run this one expectation):
    the store's edit check, the service's and the bare ``NOTEBOOK_EDIT_SQL``."""
    access_sql = world.access_sql
    with world.repo._runtime.database.connect() as db:
        for shape, (_level, _read, edit, _admin, _write) in SHAPES.items():
            user_id = world.users[shape]
            raw = db.execute(
                access_sql.NOTEBOOK_EDIT_SQL,
                (world.notebook, *access_sql.edit_access_params(user_id)),
            ).fetchone() is not None
            assert raw is edit, shape
            assert world.store.user_can_edit_notebook(world.notebook, user_id) is edit, shape
            assert world.repo._runtime.sharing.user_can_edit_notebook(
                world.notebook, user_id
            ) is edit, shape
    assert not world.store.user_can_edit_notebook("nb-missing", world.owner_id)


def access_level_truth_table(world: World) -> None:
    """Eight shapes, one statement each: the level is the highest that holds and
    ``owner_id`` is the notebook's ``created_by``; the booleans agree with it."""
    for shape, (level, read, edit, admin, write) in SHAPES.items():
        user_id = world.users[shape]
        access = world.store.notebook_access_level(world.notebook, user_id)
        assert world.repo._runtime.sharing.notebook_access_level(
            world.notebook, user_id
        ) == access, shape
        if level is None:
            assert access is None, shape
        else:
            assert access is not None, shape
            assert access.level == level, (shape, access)
            assert access.owner_id == world.owner_id, shape
        assert _predicates(world, world.notebook, user_id) == (read, edit, admin, write), shape
    assert world.store.notebook_access_level("nb-missing", world.owner_id) is None
    # a notebook that is not live answers as missing, owner included
    world.write([(
        "UPDATE notebooks SET status='copying' WHERE id=?", (world.notebook,)
    )])
    for shape in SHAPES:
        assert world.store.notebook_access_level(
            world.notebook, world.users[shape]
        ) is None, shape
        # every predicate filters the lifecycle on its own, the new edit one too
        assert _predicates(world, world.notebook, world.users[shape]) == (
            False, False, False, False,
        ), shape


def rollback_predicates_downgrade_editor_to_reader(world: World) -> None:
    """An older binary only knows the write / admin / read statements. Run on an
    editor row they must answer read-only: no admin, no write, but read — the
    editor edge never escalates after a rollback. They do not mention the new
    role at all, which is what makes that true."""
    access_sql = world.access_sql
    editor = world.users["editor"]
    for statement in (access_sql.NOTEBOOK_WRITE_SQL, access_sql.NOTEBOOK_ADMIN_SQL,
                      access_sql.NOTEBOOK_READ_SQL):
        assert "editor" not in statement
    with world.repo._runtime.database.connect() as db:
        def holds(statement: str, params: tuple) -> bool:
            return db.execute(statement, params).fetchone() is not None

        assert not holds(access_sql.NOTEBOOK_WRITE_SQL, (world.notebook, editor))
        assert not holds(
            access_sql.NOTEBOOK_ADMIN_SQL,
            (world.notebook, *access_sql.admin_access_params(editor)),
        )
        assert holds(
            access_sql.NOTEBOOK_READ_SQL,
            (world.notebook, *access_sql.read_access_params(editor)),
        )


def everyone_editor_edge_confers_no_edit(world: World) -> None:
    """An ``(everyone, editor)`` row cannot be written by any entry point, but a
    parked or hand-made one must still give nobody the edit level: everyone
    reads, nobody edits."""
    owner = world.profiles["owner"]
    notebook_id = _as_user(
        owner, lambda: world.repo.create_notebook(NotebookCreate(name="Everyone"))
    ).id
    world.write([_grant_row(notebook_id, "everyone", "", "editor", "gw-everyone-ed")])
    for shape in SHAPES:
        if shape == "owner":
            continue
        user_id = world.users[shape]
        assert world.store.user_can_read_notebook(notebook_id, user_id), shape
        assert not world.store.user_can_edit_notebook(notebook_id, user_id), shape
        access = world.store.notebook_access_level(notebook_id, user_id)
        assert access is not None and access.level == "read", (shape, access)


def read_except_grant_equals_read_without_that_grant(world: World) -> None:
    """For every grant row and every user: the clause evaluated now equals the
    read check after that row is really deleted (in a transaction that is then
    rolled back). At least one user must flip, or the world proves nothing."""
    access_sql = world.access_sql
    with world.repo._runtime.database.connect() as db:
        grant_ids = [
            row["id"] for row in db.execute(
                world.sql("SELECT id FROM notebook_grants WHERE notebook_id=?"),
                (world.notebook,),
            ).fetchall()
        ]
        predicted = {
            (grant_id, shape): db.execute(
                access_sql.NOTEBOOK_READ_EXCEPT_GRANT_SQL,
                access_sql.read_except_grant_params(
                    world.notebook, world.users[shape], grant_id
                ),
            ).fetchone() is not None
            for grant_id in grant_ids
            for shape in SHAPES
        }
    assert len(grant_ids) == 5
    flips = 0
    for grant_id in grant_ids:
        actual: dict[str, bool] = {}
        try:
            with world.repo._runtime.database.write() as db:
                db.execute(
                    world.sql("DELETE FROM notebook_grants WHERE id=?"), (grant_id,)
                )
                for shape in SHAPES:
                    actual[shape] = db.execute(
                        access_sql.NOTEBOOK_READ_SQL,
                        (world.notebook, *access_sql.read_access_params(world.users[shape])),
                    ).fetchone() is not None
                raise _Rollback
        except _Rollback:
            pass
        for shape in SHAPES:
            assert predicted[(grant_id, shape)] is actual[shape], (grant_id, shape)
            before = world.store.user_can_read_notebook(world.notebook, world.users[shape])
            flips += before and not actual[shape]
    assert flips >= 4
    # the rolled-back deletions left every row in place
    with world.repo._runtime.database.connect() as db:
        assert db.execute(
            world.sql("SELECT COUNT(*) AS c FROM notebook_grants WHERE notebook_id=?"),
            (world.notebook,),
        ).fetchone()["c"] == 5


_PRINCIPALS = ("user", "group", "group_admins", "everyone", "__parked__")
_ROLES = ("viewer", "admin", "editor")


def predicate_containment_chain_property(world: World) -> None:
    """Fixed seed, 200 random edge sets on one notebook: for every user, write ⊆
    admin ⊆ edit ⊆ read, and the one-statement level is the highest that holds
    (owner ⇔ write, admin ⇔ admin ∧ ¬write, edit ⇔ edit ∧ ¬admin, read ⇔ read ∧
    ¬edit, none ⇔ ¬read)."""
    access_sql = world.access_sql
    rng = random.Random(20261028)
    nb = world.notebook
    user_ids = list(world.users.values())
    group_ids = list(world.groups.values())
    for trial in range(200):
        edges: dict[tuple[str, str], str] = {}
        for _ in range(rng.randint(0, 6)):
            principal_type = rng.choice(_PRINCIPALS)
            if principal_type == "user":
                principal_id = rng.choice(user_ids)
            elif principal_type in ("group", "group_admins"):
                principal_id = rng.choice(group_ids)
            else:
                principal_id = rng.choice(("", rng.choice(user_ids)))
            edges[(principal_type, principal_id)] = rng.choice(_ROLES)
        members = [uid for uid in user_ids if uid != world.owner_id and rng.random() < 0.2]
        statements: list[tuple[str, tuple]] = [
            ("DELETE FROM notebook_grants WHERE notebook_id=?", (nb,)),
            ("DELETE FROM notebook_members WHERE notebook_id=?", (nb,)),
        ]
        for index, ((principal_type, principal_id), role) in enumerate(sorted(edges.items())):
            statements.append(
                _grant_row(nb, principal_type, principal_id, role, f"gp-{trial}-{index}")
            )
        for uid in members:
            statements.append((
                "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
                "VALUES (?,?,?,?)",
                (nb, uid, "reader", _STAMP),
            ))
        world.write(statements)
        with world.repo._runtime.database.connect() as db:
            def holds(statement: str, params: tuple) -> bool:
                return db.execute(statement, params).fetchone() is not None

            for uid in user_ids:
                write = holds(access_sql.NOTEBOOK_WRITE_SQL, (nb, uid))
                admin = holds(
                    access_sql.NOTEBOOK_ADMIN_SQL, (nb, *access_sql.admin_access_params(uid))
                )
                edit = holds(
                    access_sql.NOTEBOOK_EDIT_SQL, (nb, *access_sql.edit_access_params(uid))
                )
                read = holds(
                    access_sql.NOTEBOOK_READ_SQL, (nb, *access_sql.read_access_params(uid))
                )
                case = (trial, uid, sorted(edges.items()), members)
                assert (not write or admin) and (not admin or edit) and (not edit or read), case
                row = db.execute(
                    access_sql.NOTEBOOK_ACCESS_LEVEL_SQL,
                    access_sql.access_level_params(nb, uid),
                ).fetchone()
                expected = (
                    "owner" if write else "admin" if admin else "edit" if edit
                    else "read" if read else None
                )
                assert row is not None and row["level"] == expected, (case, dict(row or {}))


CASES: dict[str, Callable[[World], None]] = {
    "edit_predicate_parity_sqlite_pg": edit_predicate_parity_sqlite_pg,
    "access_level_truth_table": access_level_truth_table,
    "rollback_predicates_downgrade_editor_to_reader": (
        rollback_predicates_downgrade_editor_to_reader
    ),
    "everyone_editor_edge_confers_no_edit": everyone_editor_edge_confers_no_edit,
    "read_except_grant_equals_read_without_that_grant": (
        read_except_grant_equals_read_without_that_grant
    ),
    "predicate_containment_chain_property": predicate_containment_chain_property,
}
