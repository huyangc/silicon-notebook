"""Capability x role matrix over the whole notebook write surface, shared by
both backends (share-edit P1-T3).

``test_notebook_capability_matrix.py`` runs every case on SQLite and
``postgres/test_notebook_capability_matrix_pg.py`` runs the same cases on
PostgreSQL. One world, eight access shapes on one notebook (owner, named
editor, group admin, group viewer, link member, stranger, and the two legacy
named rows ``(user, admin)`` / ``(user, viewer)``), four layers:

1. the pure function ``notebook_capability_allowed`` (and its level-returning
   twin) for every capability x shape;
2. an HTTP refusal sweep: every route of the census
   (``notebook_route_inventory``) for every shape ``ROUTE_LEVELS`` says it must
   not admit answers 404 -- by path, so a route whose guard was deleted is
   still swept and comes back 2xx or 422; ``Capability`` routes must also carry
   the guard of their capability;
3. admission samples: one representative route per capability, every admitted
   shape gets past the guard (not 404);
4. safe-method samples: the editor reads none of the admin / owner GETs.

The group-admin column is pinned against the capability set it held before
share-edit P1 (all nine former admin cells); its one registered change is
decision D6 (the session write of a cell code attachment), which
``notebook_editor_capability_cases`` pins.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi.params import File, Form
from fastapi.testclient import TestClient

from tests.notebook_route_inventory import (
    BODY_LOOKUP_WRITE_ROUTES,
    route_for,
    unsafe_notebook_routes,
)
from tests.notebook_route_levels import (
    ROUTE_LEVELS,
    Capability,
    ReaderOnly,
    SelfAuthorized,
)

_STAMP = "2026-10-28T00:00:00+00:00"

SHAPES = (
    "owner", "editor", "group_admin", "group_viewer", "link_member", "stranger",
    "legacy_user_admin", "legacy_user_viewer",
)
_LEVEL_SHAPES = {
    "owner": {"owner"},
    "admin": {"owner", "group_admin", "legacy_user_admin"},
    "edit": {"owner", "group_admin", "legacy_user_admin", "editor"},
}
_READERS = set(SHAPES) - {"stranger"}

#: What the group admin held before share-edit P1: every cell then at "admin".
#: Moving seven of them to "edit" (edit contains admin) must leave this column
#: exactly as it was.
GROUP_ADMIN_CAPABILITIES_BEFORE_P1 = frozenset({
    "sources:write", "kg:write", "knowhow:write", "knowledge:write", "catalog:write",
    "scale_index:write", "notebook:manage", "notebook:grant", "agent_profile:write",
})


@dataclass
class World:
    client: TestClient
    postgres: bool
    notebook: str
    target_notebook: str
    source: str
    table: str
    row: str
    column: str
    #: Two approved shared knowledge objects of one type, written by the owner
    #: (not derived from anyone's Memory): the body-judged knowledge routes
    #: (PATCH, merge) are swept against REAL objects whose write branch succeeds,
    #: so a reader's 404 can only come from the capability check.
    knowledge: str = ""
    knowledge_peer: str = ""
    headers: dict[str, dict] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)


def _repository():
    from app.api.deps import repository

    return repository()


def _write(postgres: bool, statement: str, params: tuple) -> None:
    text = statement.replace("?", "%s") if postgres else statement
    with _repository()._runtime.database.write() as db:
        db.execute(text, params)


def _login(client: TestClient, username: str) -> dict:
    client.post("/api/auth/register", json={"username": username, "password": "pw123456"})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw123456"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _grant(postgres: bool, notebook: str, principal_type: str, principal_id: str,
           role: str, grant_id: str) -> None:
    _write(
        postgres,
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (grant_id, notebook, principal_type, principal_id, role, None, _STAMP),
    )


def build_world(client: TestClient, *, postgres: bool) -> World:
    headers = {shape: _login(client, f"m{index + 30000001:08d}")
               for index, shape in enumerate(SHAPES)}
    users = {shape: client.get("/api/me", headers=h).json()["id"]
             for shape, h in headers.items()}
    owner = headers["owner"]
    notebook = client.post("/api/notebooks", json={"name": "矩阵"}, headers=owner).json()["id"]
    target = client.post("/api/notebooks", json={"name": "目标"}, headers=owner).json()["id"]
    for name, shape, edge_type, edge_role, member_role in (
        ("管理组", "group_admin", "group_admins", "admin", "admin"),
        ("只读组", "group_viewer", "group", "viewer", "member"),
    ):
        group = client.post("/api/groups", json={"name": name}, headers=owner).json()["id"]
        assert client.put(
            f"/api/groups/{group}/members/{users[shape]}",
            json={"role": member_role}, headers=owner,
        ).status_code == 200
        granted = client.post(
            f"/api/notebooks/{notebook}/grants",
            json={"principal_type": edge_type, "principal_id": group, "role": edge_role},
            headers=owner,
        )
        assert granted.status_code == 200, granted.text
    _grant(postgres, notebook, "user", users["editor"], "editor", "gm-editor")
    _grant(postgres, notebook, "user", users["legacy_user_admin"], "admin", "gm-uadmin")
    _grant(postgres, notebook, "user", users["legacy_user_viewer"], "viewer", "gm-uviewer")
    _repository()._runtime.sharing.add_member(notebook, users["link_member"])

    source = f"src-matrix-{notebook[-6:]}"
    _write(
        postgres,
        "INSERT INTO sources "
        "(id,notebook_id,title,source_type,file_name,file_path,file_size,"
        "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source, notebook, "S", "document", "s.md", "", 0, "parsed", _STAMP, _STAMP),
    )
    table = client.post(
        f"/api/notebooks/{notebook}/knowhow",
        json={"title": "判别表", "anchor_index": 0, "columns": [
            {"name": "现象", "kind": "attribute"}, {"name": "方法", "kind": "procedure"},
        ]},
        headers=owner,
    )
    assert table.status_code == 200, table.text
    columns = [column["id"] for column in table.json()["columns"]]
    row = client.post(
        f"/api/notebooks/{notebook}/knowhow/{table.json()['id']}/rows",
        json={"cells": {columns[0]: "振铃", columns[1]: "1. 看眼图"}}, headers=owner,
    )
    assert row.status_code == 200, row.text
    knowledge = ("ko-matrix-a", "ko-matrix-b")
    for object_id in knowledge:
        _write(
            postgres,
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,owner,"
            "payload,evidence,source_id,created_at,updated_at,last_reviewed) "
            "VALUES(?,?,'rule','approved','',?,'[]','',?,?,?)",
            (object_id, notebook, '{"title": "%s"}' % object_id, _STAMP, _STAMP,
             None if postgres else ""),
        )
    return World(
        client=client, postgres=postgres, notebook=notebook, target_notebook=target,
        source=source, table=table.json()["id"], row=row.json()["id"], column=columns[1],
        knowledge=knowledge[0], knowledge_peer=knowledge[1],
        headers=headers, users=users,
    )


def admitted_shapes(level) -> set[str]:
    from app.api import deps

    if isinstance(level, ReaderOnly):
        return set(_READERS)
    if isinstance(level, SelfAuthorized):
        return set(_READERS) if level.stranger_refused else set(SHAPES)
    capability = level.name if isinstance(level, Capability) else level.capability
    return set(_LEVEL_SHAPES[deps._CAPABILITY_LEVELS[capability]])


# ------------------------------------------------------------- requests


def _fill(world: World, template: str) -> str:
    values = {
        "notebook_id": world.notebook, "source_id": world.source,
        "table_id": world.table, "row_id": world.row, "column_id": world.column,
        "label": "corpus_shape", "knowledge_id": world.knowledge,
    }

    def value(match: re.Match) -> str:
        name = match.group(1).split(":", 1)[0]
        return values.get(name, f"x-{name}")

    return re.sub(r"\{([^}]+)\}", value, template)


def _request_kwargs(world: World, method: str, template: str) -> dict:
    """Legal JSON ``{}`` for JSON routes (a parse failure would answer 422
    before any dependency runs), an empty form for multipart routes, and a body
    that reaches the write branch for the body-judged routes."""
    overrides: dict[tuple[str, str], dict] = {
        ("POST", "/api/notebooks/{notebook_id}/knowhow/{table_id}/transfer"): {
            "json": {"target_notebook_id": world.target_notebook, "mode": "move"},
        },
        ("POST", "/api/notebooks/{notebook_id}/knowledge/{knowledge_id}/merge"): {
            "json": {"into_id": world.knowledge_peer},
        },
        ("PATCH", "/api/notebooks/{notebook_id}/knowledge/{knowledge_id}"): {
            "json": {"status": "deprecated"},
        },
        ("PUT", "/api/notebooks/{notebook_id}/understanding/{label}"): {
            "json": {"scope": "shared", "value": "x", "expected_revision": 0},
        },
        ("DELETE", "/api/notebooks/{notebook_id}/understanding/{label}"): {
            "params": {"scope": "shared", "expected_revision": 0},
        },
        ("POST", "/api/notebooks/{notebook_id}/understanding/rebuild"): {
            "json": {"scope": "shared"},
        },
        ("PUT", "/api/agent/knowhow/rows/{row_id}/cells/{column_id}/code"): {
            "json": {"code_text": "x = 1", "language": "python"},
        },
    }
    if (method, template) in overrides:
        return overrides[(method, template)]
    route = route_for(world.client.app, method, template)
    multipart = any(
        isinstance(param.field_info, (File, Form)) for param in route.dependant.body_params
    )
    if multipart:
        return {"data": {}}
    if route.dependant.body_params:
        return {"json": {}}
    return {}


def call(world: World, shape: str, method: str, template: str):
    kwargs = _request_kwargs(world, method, template)
    return world.client.request(
        method, _fill(world, template), headers=world.headers[shape], **kwargs
    )


# ---------------------------------------------------------------- layers


def capability_matrix_pure_function(world: World, monkeypatch) -> None:
    from app.api import deps

    nb = world.notebook
    for capability, level in deps._CAPABILITY_LEVELS.items():
        for shape in SHAPES:
            expected = shape in _LEVEL_SHAPES[level]
            user_id = world.users[shape]
            assert deps.notebook_capability_allowed(capability, nb, user_id) is expected, (
                capability, shape,
            )
            access = deps.notebook_capability_level(capability, nb, user_id)
            assert (access is not None) is expected, (capability, shape)
    group_admin = {
        capability for capability in deps._CAPABILITY_LEVELS
        if deps.notebook_capability_allowed(capability, nb, world.users["group_admin"])
    }
    assert group_admin == GROUP_ADMIN_CAPABILITIES_BEFORE_P1 | {"notebook:activity"}
    editor = {
        capability for capability in deps._CAPABILITY_LEVELS
        if deps.notebook_capability_allowed(capability, nb, world.users["editor"])
    }
    assert editor == (GROUP_ADMIN_CAPABILITIES_BEFORE_P1 - {"notebook:manage", "notebook:grant"}) | {"notebook:activity"}


def _dependency_calls(route) -> set:
    calls: set = set()

    def walk(dependant) -> None:
        calls.add(dependant.call)
        for sub in dependant.dependencies:
            walk(sub)

    walk(route.dependant)
    return calls


def _expected_guards(capability: str) -> set:
    from app.api import deps

    if capability == "notebook:delete":
        return {deps.require_notebook_delete, deps.require_notebook_cancel_delete}
    return {deps.require_notebook_capability(capability)}


def http_refusal_sweep(world: World, monkeypatch) -> None:
    from app.api import deps

    app = world.client.app
    inventory = unsafe_notebook_routes(app)
    assert set(ROUTE_LEVELS) == inventory
    from app.api.memory_routes import require_memory_export_access

    level_guards = {
        deps.require_notebook_write, deps.require_notebook_admin, deps.require_notebook_edit,
    }
    # The read guards a reader route may carry: the plain one, and the Memory
    # export guard (read access, or a caller's own Memory inside a deletion
    # window -- the delete-window transfer).
    read_guards = {
        deps.require_notebook_read, deps.require_notebook_read_or_delete_notice,
        require_memory_export_access,
    }
    from app.api.collab_routes import _pulse_access
    read_guards.add(_pulse_access)
    swept = 0
    for (method, template), level in sorted(ROUTE_LEVELS.items(), key=lambda kv: kv[0][1]):
        # The HTTP refusal first: it holds on its own, whatever the dependency
        # tree looks like (a deleted guard answers 2xx / 409 / 422 here).
        for shape in sorted(set(SHAPES) - admitted_shapes(level)):
            response = call(world, shape, method, template)
            assert response.status_code == 404, (
                method, template, shape, response.status_code, response.text[:200],
            )
            swept += 1
        route = route_for(app, method, template)
        calls = _dependency_calls(route)
        if isinstance(level, Capability) and (method, template) not in BODY_LOOKUP_WRITE_ROUTES:
            assert calls & _expected_guards(level.name), (method, template, level)
        elif not isinstance(level, Capability):
            # read-guarded or self-authorizing: no capability tier guard here,
            # or the route is misclassified
            assert not (calls & level_guards), (method, template, level)
            fenced = {deps.require_notebook_capability(c) for c in deps._CAPABILITY_LEVELS}
            assert not (calls & fenced), (method, template, level)
            if isinstance(level, SelfAuthorized) or getattr(level, "read_in_body", False):
                assert not (calls & read_guards), (method, template, level)
            else:
                assert calls & read_guards, (method, template, level)
    assert swept > 300, swept
    # The body-judged knowledge routes were swept against objects whose write
    # branch really succeeds: here it does, for the owner.
    for method, template, body in (
        ("PATCH", "/api/notebooks/{notebook_id}/knowledge/{knowledge_id}",
         {"status": "deprecated"}),
        ("POST", "/api/notebooks/{notebook_id}/knowledge/{knowledge_id}/merge",
         {"into_id": world.knowledge_peer}),
    ):
        response = world.client.request(
            method, _fill(world, template), headers=world.headers["owner"], json=body,
        )
        assert response.status_code == 200, (method, template, response.text[:200])


#: One representative route per capability for the admission samples.
def _admission_samples(world: World) -> dict[str, tuple[str, str]]:
    nb = "/api/notebooks/{notebook_id}"
    return {
        "sources:write": ("POST", nb + "/backfill-vectors"),
        "kg:write": ("POST", nb + "/kg/build"),
        "knowhow:write": ("POST", nb + "/knowhow"),
        "knowledge:write": ("POST", nb + "/object-schemas"),
        "catalog:write": ("POST", nb + "/sources/{source_id}/command-catalog"),
        "scale_index:write": ("POST", nb + "/scale-index/cancel"),
        "agent_profile:write": ("POST", nb + "/understanding/experiences/distill"),
        "notebook:manage": ("PATCH", nb),
        "notebook:grant": ("POST", nb + "/share-requests"),
        "notebook:configure": ("POST", nb + "/share"),
        "notebook:mount": ("PUT", nb + "/bases"),
        "notebook:export": ("POST", nb + "/export"),
        "notebook:collaborate": ("PUT", nb + "/collaborators/{user_id}"),
        "notebook:delete": ("DELETE", nb),
    }


def http_admission_samples(world: World, monkeypatch) -> None:
    from app.api import deps

    jobs = deps.repository()._runtime.notebook_transfers
    monkeypatch.setattr(jobs, "_submit", lambda *args, **kwargs: None)
    samples = _admission_samples(world)
    used = {
        level.name if isinstance(level, Capability) else level.capability
        for level in ROUTE_LEVELS.values()
        if not isinstance(level, (ReaderOnly, SelfAuthorized))
    }
    assert set(samples) == used, sorted(set(samples) ^ used)
    for capability, (method, template) in samples.items():
        level = ROUTE_LEVELS[(method, template)]
        assert (level.name if isinstance(level, Capability) else level.capability) == (
            capability
        ), (method, template)
        if capability == "notebook:delete":
            continue  # last: it removes the notebook every other sample needs
        for shape in sorted(_LEVEL_SHAPES[deps._CAPABILITY_LEVELS[capability]]):
            response = call(world, shape, method, template)
            assert response.status_code != 404, (
                capability, shape, response.status_code, response.text[:200],
            )
    method, template = samples["notebook:delete"]
    deleted = call(world, "owner", method, template)
    assert deleted.status_code == 202, deleted.text


def safe_method_samples(world: World, monkeypatch) -> None:
    for template in ("/grants", "/bases", "/mountable", "/share", "/mounted-by-count"):
        response = world.client.get(
            f"/api/notebooks/{world.notebook}{template}", headers=world.headers["editor"]
        )
        assert response.status_code == 404, (template, response.status_code)
    assert world.client.get(
        f"/api/notebooks/{world.notebook}", headers=world.headers["editor"]
    ).status_code == 200


CASES: dict[str, Callable[..., Any]] = {
    "capability_matrix_pure_function": capability_matrix_pure_function,
    "http_refusal_sweep": http_refusal_sweep,
    "http_admission_samples": http_admission_samples,
    "safe_method_samples": safe_method_samples,
}
