"""Capability tier ``edit`` over HTTP, shared by both backends (share-edit P1-T2).

``test_notebook_editor_capabilities.py`` runs every case on SQLite and
``postgres/test_notebook_editor_capabilities_pg.py`` runs the same cases on
PostgreSQL, through the real routes of a fresh app.

Decision D6: the session write branch of ``user_or_agent_scope`` (the cell
code attachment PUT/DELETE of ``knowhow_agent_routes``) follows
``knowhow:write`` -- the tier of the cell content it is attached to -- instead
of owner-only. The group admin cell flips from 404 to 2xx (a registered,
expected change); the named editor gets 2xx too; a read-only member stays 404;
on a mirrored notebook the write is refused with the mirror 409.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi.testclient import TestClient

_STAMP = "2026-10-28T00:00:00+00:00"


@dataclass
class World:
    client: TestClient
    postgres: bool
    notebook: str
    row: str
    column: str
    headers: dict[str, dict]

    @property
    def code_url(self) -> str:
        return f"/api/agent/knowhow/rows/{self.row}/cells/{self.column}/code"


def _login(client: TestClient, username: str) -> dict:
    client.post("/api/auth/register", json={"username": username, "password": "pw123456"})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw123456"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _user_id(client: TestClient, headers: dict) -> str:
    return client.get("/api/me", headers=headers).json()["id"]


def _repository():
    from app.api.deps import repository

    return repository()


def _write(postgres: bool, statement: str, params: tuple) -> None:
    text = statement.replace("?", "%s") if postgres else statement
    with _repository()._runtime.database.write() as db:
        db.execute(text, params)


def build_world(client: TestClient, *, postgres: bool) -> World:
    owner = _login(client, "d00000001")
    deputy = _login(client, "d00000002")
    editor = _login(client, "d00000003")
    reader = _login(client, "d00000004")
    stranger = _login(client, "d00000005")
    notebook = client.post("/api/notebooks", json={"name": "D6"}, headers=owner).json()["id"]
    table = client.post(
        f"/api/notebooks/{notebook}/knowhow",
        json={
            "title": "判别表",
            "columns": [{"name": "现象", "kind": "attribute"}, {"name": "方法", "kind": "procedure"}],
            "anchor_index": 0,
        },
        headers=owner,
    )
    assert table.status_code == 200, table.text
    table_body = table.json()
    columns = [column["id"] for column in table_body["columns"]]
    row = client.post(
        f"/api/notebooks/{notebook}/knowhow/{table_body['id']}/rows",
        json={"cells": {columns[0]: "振铃", columns[1]: "1. 看眼图"}},
        headers=owner,
    )
    assert row.status_code == 200, row.text

    group = client.post("/api/groups", json={"name": "项目组"}, headers=owner).json()["id"]
    assert client.put(
        f"/api/groups/{group}/members/{_user_id(client, deputy)}",
        json={"role": "admin"}, headers=owner,
    ).status_code == 200
    granted = client.post(
        f"/api/notebooks/{notebook}/grants",
        json={"principal_type": "group_admins", "principal_id": group, "role": "admin"},
        headers=owner,
    )
    assert granted.status_code == 200, granted.text
    _write(
        postgres,
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("gd6-editor", notebook, "user", _user_id(client, editor), "editor", None, _STAMP),
    )
    _repository()._runtime.sharing.add_member(notebook, _user_id(client, reader))
    return World(
        client=client, postgres=postgres, notebook=notebook, row=row.json()["id"],
        column=columns[1],
        headers={
            "owner": owner, "deputy": deputy, "editor": editor,
            "reader": reader, "stranger": stranger,
        },
    )


def d6_session_code_attachment_write_follows_knowhow_write(world: World) -> None:
    client = world.client
    body = {"code_text": "print('d6')", "language": "python"}
    for who in ("owner", "deputy", "editor"):
        put = client.put(world.code_url, json=body, headers=world.headers[who])
        assert put.status_code == 200, (who, put.status_code, put.text)
        deleted = client.delete(world.code_url, headers=world.headers[who])
        assert deleted.status_code == 204, (who, deleted.status_code, deleted.text)
    for who in ("reader", "stranger"):
        put = client.put(world.code_url, json=body, headers=world.headers[who])
        assert put.status_code == 404, (who, put.status_code, put.text)
        assert client.delete(
            world.code_url, headers=world.headers[who]
        ).status_code == 404, who
    # the reader still reads the attachment (the read branch is unchanged)
    assert client.get(world.code_url, headers=world.headers["reader"]).status_code == 200

    # mirror fence after the capability: the editor gets the mirror 409, the
    # reader keeps the 404 (no existence leak)
    _repository()._runtime.sharing_store.set_notebook_sync_origin(
        world.notebook, "prod-shanghai"
    )
    for who in ("owner", "deputy", "editor"):
        put = client.put(world.code_url, json=body, headers=world.headers[who])
        assert put.status_code == 409, (who, put.status_code, put.text)
        assert put.json()["detail"]["code"] == "notebook_mirrored", who
    assert client.put(
        world.code_url, json=body, headers=world.headers["reader"]
    ).status_code == 404


CASES: dict[str, Callable[[World], Any]] = {
    "d6_session_code_attachment_write_follows_knowhow_write": (
        d6_session_code_attachment_write_follows_knowhow_write
    ),
}
