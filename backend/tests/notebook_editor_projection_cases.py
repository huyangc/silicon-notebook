"""List / detail / "shared by me" projections for named editors, over HTTP,
shared by both backends (share-edit P1-T7).

``test_notebook_editor_projection.py`` runs every case on SQLite and
``postgres/test_notebook_editor_projection_pg.py`` runs the same cases on
PostgreSQL, through the real routes:

* a notebook shared with a user only by a named editor grant is in their list
  (``access='reader'``, ``can_manage_content`` true, ``can_manage_notebook``
  false, ``collaborator_role='editor'``, no ``granted_via``), and the detail
  agrees;
* a link member who is also an editor appears once, with the editor flags;
* a legacy ``(user, admin)`` row manages content and the notebook, but is not
  an editor; a legacy ``(user, viewer)`` row is a plain reader;
* the owner's card is ``is_shared`` with only a named grant, and "shared by
  me" lists the notebook with its collaborators -- in a number of statements
  that does not grow with the number of notebooks.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi.testclient import TestClient

_STAMP = "2026-10-28T00:00:00+00:00"


@dataclass
class World:
    client: TestClient
    postgres: bool
    notebook: str
    headers: dict[str, dict] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)


def _repository():
    from app.api.deps import repository

    return repository()


def _login(client: TestClient, username: str) -> dict:
    client.post("/api/auth/register", json={"username": username, "password": "pw123456"})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw123456"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _write(postgres: bool, text: str, params: tuple) -> None:
    with _repository()._runtime.database.write() as db:
        db.execute(text.replace("?", "%s") if postgres else text, params)


def build_world(client: TestClient, *, postgres: bool) -> World:
    names = ("owner", "editor", "member_editor", "legacy_admin", "legacy_viewer")
    headers = {name: _login(client, f"y{20261300 + index:08d}")
               for index, name in enumerate(names)}
    users = {name: client.get("/api/me", headers=h).json()["id"] for name, h in headers.items()}
    notebook = client.post(
        "/api/notebooks", json={"name": "点名库"}, headers=headers["owner"]
    ).json()["id"]
    groups = _repository()._runtime.groups
    for name in ("editor", "member_editor"):
        groups.set_user_editor_grant(
            notebook, owner_id=users["owner"], target_user_id=users[name]
        )
    _repository()._runtime.sharing.add_member(notebook, users["member_editor"])
    for name, role in (("legacy_admin", "admin"), ("legacy_viewer", "viewer")):
        _write(
            postgres,
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"gp-{name}", notebook, "user", users[name], role, users["owner"], _STAMP),
        )
    return World(client=client, postgres=postgres, notebook=notebook,
                 headers=headers, users=users)


def _flags(item: dict) -> tuple:
    return (
        item["access"], item["can_manage_content"], item["can_manage_notebook"],
        item["collaborator_role"], item["collaborator_is_member"], item["granted_via"],
    )


def _listed(world: World, who: str) -> list[dict]:
    return [
        item for item in world.client.get("/api/notebooks", headers=world.headers[who]).json()
        if item["id"] == world.notebook
    ]


def user_edge_notebook_appears_in_list(world: World) -> None:
    listed = _listed(world, "editor")
    assert len(listed) == 1
    assert _flags(listed[0]) == ("reader", True, False, "editor", False, [])
    detail = world.client.get(
        f"/api/notebooks/{world.notebook}", headers=world.headers["editor"]
    ).json()
    assert _flags(detail) == ("reader", True, False, "editor", False, [])
    owner = _listed(world, "owner")[0]
    assert _flags(owner) == ("owner", True, True, "", False, [])


def member_plus_editor_dedup_and_flags(world: World) -> None:
    listed = _listed(world, "member_editor")
    assert len(listed) == 1, "a link member who is also an editor is listed once"
    # the cross state: both exits are offered (spec §4.4)
    assert _flags(listed[0]) == ("reader", True, False, "editor", True, [])
    detail = world.client.get(
        f"/api/notebooks/{world.notebook}", headers=world.headers["member_editor"]
    ).json()
    assert _flags(detail) == ("reader", True, False, "editor", True, [])


def legacy_user_admin_edge_flags(world: World) -> None:
    admin = _listed(world, "legacy_admin")
    assert len(admin) == 1
    # legacy named rows report their role: their exit is the named-grant one
    assert _flags(admin[0]) == ("reader", True, True, "admin", False, [])
    viewer = _listed(world, "legacy_viewer")
    assert len(viewer) == 1
    assert _flags(viewer[0]) == ("reader", False, False, "viewer", False, [])
    for who, role in (("legacy_admin", "admin"), ("legacy_viewer", "viewer")):
        detail = world.client.get(
            f"/api/notebooks/{world.notebook}", headers=world.headers[who]
        ).json()
        assert (detail["collaborator_role"], detail["collaborator_is_member"]) == (role, False)


def is_shared_includes_user_edges(world: World) -> None:
    client, owner = world.client, world.headers["owner"]
    alone = client.post("/api/notebooks", json={"name": "只有编辑者"}, headers=owner).json()["id"]
    private = client.post("/api/notebooks", json={"name": "私有"}, headers=owner).json()["id"]
    _repository()._runtime.groups.set_user_editor_grant(
        alone, owner_id=world.users["owner"], target_user_id=world.users["editor"]
    )
    cards = {item["id"]: item for item in client.get("/api/notebooks", headers=owner).json()}
    assert cards[alone]["is_shared"] is True
    assert cards[private]["is_shared"] is False
    shared = {item["id"]: item for item in client.get(
        "/api/notebooks/shared-by-me", headers=owner
    ).json()}
    assert private not in shared
    assert shared[alone]["group_count"] == 0 and shared[alone]["share_token"] == ""
    assert [(c["username"], c["role"]) for c in shared[alone]["collaborators"]] == [
        (client.get("/api/me", headers=world.headers["editor"]).json()["username"], "editor"),
    ]
    roles = sorted(c["role"] for c in shared[world.notebook]["collaborators"])
    assert roles == ["admin", "editor", "editor", "viewer"]


class _Counting:
    def __init__(self, connection, counter):
        self._connection = connection
        self._counter = counter

    def execute(self, *args, **kwargs):
        self._counter[0] += 1
        return self._connection.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _statements(world: World, monkeypatch, fn: Callable[[], Any]) -> int:
    counter = [0]
    database = _repository()._runtime.database
    for name in ("connect", "write"):
        original = getattr(database, name)

        @contextmanager
        def wrapped(*args, _original=original, **kwargs):
            with _original(*args, **kwargs) as connection:
                yield _Counting(connection, counter)

        monkeypatch.setattr(database, name, wrapped)
    try:
        fn()
    finally:
        monkeypatch.undo()
    return counter[0]


def shared_by_me_lists_collaborators_without_n_plus_one(world: World, monkeypatch) -> None:
    client, owner = world.client, world.headers["owner"]

    def call():
        response = client.get("/api/notebooks/shared-by-me", headers=owner)
        assert response.status_code == 200, response.text

    one = _statements(world, monkeypatch, call)
    for index in range(3):
        extra = client.post(
            "/api/notebooks", json={"name": f"再一本{index}"}, headers=owner
        ).json()["id"]
        _repository()._runtime.groups.set_user_editor_grant(
            extra, owner_id=world.users["owner"], target_user_id=world.users["editor"]
        )
    four = _statements(world, monkeypatch, call)
    assert four == one, (one, four)
    items = client.get("/api/notebooks/shared-by-me", headers=owner).json()
    assert len(items) == 4
    assert all(item["collaborators"] for item in items)


CASES: dict[str, Callable[..., Any]] = {
    "user_edge_notebook_appears_in_list": user_edge_notebook_appears_in_list,
    "member_plus_editor_dedup_and_flags": member_plus_editor_dedup_and_flags,
    "legacy_user_admin_edge_flags": legacy_user_admin_edge_flags,
    "is_shared_includes_user_edges": is_shared_includes_user_edges,
}
MONKEYPATCH_CASES: dict[str, Callable[..., Any]] = {
    "shared_by_me_lists_collaborators_without_n_plus_one": (
        shared_by_me_lists_collaborators_without_n_plus_one
    ),
}
