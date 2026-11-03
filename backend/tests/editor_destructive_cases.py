"""What a named editor may not destroy, over HTTP, shared by both backends
(share-edit P1-T4, decision D14).

``test_editor_destructive.py`` runs every case on SQLite and
``postgres/test_editor_destructive_pg.py`` runs the same cases on PostgreSQL,
through the real routes. One world: the owner's notebook with a named editor
and a group admin (``group_admins`` admin edge), sources uploaded by the
editor, by the owner and by nobody (an unattributed historical row), and a
Knowhow table with history. Every refusal is checked against its effect too:
the row, the table, the history and the transfer target are exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

_STAMP = "2026-10-28T00:00:00+00:00"
_OLD = "2000-01-01T00:00:00"


@dataclass
class World:
    client: TestClient
    postgres: bool
    notebook: str
    editor_notebook: str
    table: str
    column: str
    row: str
    headers: dict[str, dict] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)

    def sql(self, text: str) -> str:
        return text.replace("?", "%s") if self.postgres else text

    def write(self, text: str, params: tuple = ()) -> None:
        with _repository()._runtime.database.write() as db:
            db.execute(self.sql(text), params)

    def count(self, text: str, params: tuple = ()) -> int:
        with _repository()._runtime.database.connect() as db:
            row = db.execute(self.sql(text), params).fetchone()
        return int(row["c"])


def _repository():
    from app.api.deps import repository

    return repository()


def _login(client: TestClient, username: str) -> dict:
    client.post("/api/auth/register", json={"username": username, "password": "pw123456"})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw123456"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def build_world(client: TestClient, *, postgres: bool) -> World:
    headers = {name: _login(client, f"x{20261200 + index:08d}")
               for index, name in enumerate(("owner", "editor", "deputy", "reader"))}
    users = {name: client.get("/api/me", headers=h).json()["id"] for name, h in headers.items()}
    owner = headers["owner"]
    notebook = client.post("/api/notebooks", json={"name": "协作库"}, headers=owner).json()["id"]
    editor_notebook = client.post(
        "/api/notebooks", json={"name": "编辑者自己的"}, headers=headers["editor"]
    ).json()["id"]
    group = client.post("/api/groups", json={"name": "组"}, headers=owner).json()["id"]
    assert client.put(
        f"/api/groups/{group}/members/{users['deputy']}", json={"role": "admin"},
        headers=owner,
    ).status_code == 200
    assert client.post(
        f"/api/notebooks/{notebook}/grants",
        json={"principal_type": "group_admins", "principal_id": group, "role": "admin"},
        headers=owner,
    ).status_code == 200
    _repository()._runtime.groups.set_user_editor_grant(
        notebook, owner_id=users["owner"], target_user_id=users["editor"]
    )
    _repository()._runtime.sharing.add_member(notebook, users["reader"])
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
    world = World(
        client=client, postgres=postgres, notebook=notebook,
        editor_notebook=editor_notebook, table=table.json()["id"], column=columns[1],
        row=row.json()["id"], headers=headers, users=users,
    )
    for text in ("改一", "改二", "改三"):
        patched = client.patch(
            f"/api/notebooks/{notebook}/knowhow/{world.table}/rows/{world.row}"
            f"/cells/{world.column}",
            json={"content_md": text}, headers=owner,
        )
        assert patched.status_code == 200, patched.text
    return world


def _source(world: World, source_id: str, uploaded_by: str | None) -> str:
    world.write(
        "INSERT INTO sources "
        "(id,notebook_id,title,source_type,file_name,file_path,file_size,"
        "parse_status,created_at,updated_at,uploaded_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (source_id, world.notebook, source_id, "document", f"{source_id}.md", "", 0,
         "parsed", _STAMP, _STAMP, uploaded_by),
    )
    return source_id


def _exists(world: World, source_id: str) -> bool:
    return world.count("SELECT COUNT(*) AS c FROM sources WHERE id=?", (source_id,)) == 1


def _history(world: World) -> int:
    return world.count(
        "SELECT COUNT(*) AS c FROM knowhow_changes WHERE table_id=?", (world.table,)
    )


def _tables(world: World, notebook_id: str) -> int:
    return world.count(
        "SELECT COUNT(*) AS c FROM knowhow_tables WHERE notebook_id=?", (notebook_id,)
    )


# ---------------------------------------------------------------- cases


def editor_deletes_only_own_upload(world: World) -> None:
    client, editor = world.client, world.headers["editor"]
    own = _source(world, "src-by-editor", world.users["editor"])
    owners = _source(world, "src-by-owner", world.users["owner"])
    legacy = _source(world, "src-unattributed", None)
    assert client.delete(f"/api/sources/{owners}", headers=editor).status_code == 404
    assert _exists(world, owners)
    assert client.delete(f"/api/sources/{legacy}", headers=editor).status_code == 404
    assert _exists(world, legacy)
    assert client.delete(f"/api/sources/{own}", headers=editor).status_code == 204
    assert not _exists(world, own)
    # the group admin and the owner are not narrowed
    assert client.delete(
        f"/api/sources/{owners}", headers=world.headers["deputy"]
    ).status_code == 204
    assert client.delete(
        f"/api/sources/{legacy}", headers=world.headers["owner"]
    ).status_code == 204
    # a reader is refused before any of it
    stranger_row = _source(world, "src-by-editor-2", world.users["editor"])
    assert client.delete(
        f"/api/sources/{stranger_row}", headers=world.headers["reader"]
    ).status_code == 404
    assert _exists(world, stranger_row)


def editor_cannot_delete_whole_knowhow_table(world: World) -> None:
    client = world.client
    path = f"/api/notebooks/{world.notebook}/knowhow/{world.table}"
    history = _history(world)
    assert history > 0
    assert client.delete(path, headers=world.headers["editor"]).status_code == 404
    assert _tables(world, world.notebook) == 1
    assert _history(world) == history
    assert client.delete(path, headers=world.headers["deputy"]).status_code == 204
    assert _tables(world, world.notebook) == 0


def editor_cannot_prune_knowhow_history(world: World) -> None:
    client = world.client
    path = f"/api/notebooks/{world.notebook}/knowhow/{world.table}/history/prune"
    world.write(
        "UPDATE knowhow_changes SET created_at=? WHERE table_id=? AND seq <= 2",
        (_OLD, world.table),
    )
    before = _history(world)
    refused = client.post(path, json={"before_days": 1}, headers=world.headers["editor"])
    assert refused.status_code == 404, refused.text
    assert _history(world) == before
    pruned = client.post(path, json={"before_days": 1}, headers=world.headers["deputy"])
    assert pruned.status_code == 200, pruned.text
    assert pruned.json()["removed"] > 0
    assert _history(world) < before


def _editor_becomes_reader_during_destructive_request(world: World, *, prune: bool) -> None:
    repo = _repository()
    editor_id = world.users["editor"]
    repo._runtime.sharing.add_member(world.notebook, editor_id)
    world.write(
        "UPDATE knowhow_changes SET created_at=? WHERE table_id=? AND seq <= 2",
        (_OLD, world.table),
    )
    before = _history(world)
    access_reader = repo._runtime.sharing
    original_edit = access_reader.owner_if_can_edit
    original_level = access_reader.notebook_access_level
    observed = []

    def edit_allowed(notebook_id, user_id):
        owner_id = original_edit(notebook_id, user_id)
        if notebook_id == world.notebook and user_id == editor_id:
            assert owner_id is not None
            observed.append("edit")
        return owner_id

    def revoke_then_read(notebook_id, user_id):
        if notebook_id == world.notebook and user_id == editor_id:
            assert observed == ["edit"]
            assert repo._runtime.groups.delete_user_grant(notebook_id, user_id)
        access = original_level(notebook_id, user_id)
        if notebook_id == world.notebook and user_id == editor_id:
            observed.append(access.level)
        return access

    path = f"/api/notebooks/{world.notebook}/knowhow/{world.table}"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(access_reader, "owner_if_can_edit", edit_allowed)
        patch.setattr(access_reader, "notebook_access_level", revoke_then_read)
        response = (
            world.client.post(path + "/history/prune", json={"before_days": 1}, headers=world.headers["editor"])
            if prune else world.client.delete(path, headers=world.headers["editor"])
        )
    assert observed == ["edit", "read"]
    assert response.status_code == 404, response.text
    assert _tables(world, world.notebook) == 1
    assert _history(world) == before


def editor_becomes_reader_before_table_delete(world: World) -> None:
    _editor_becomes_reader_during_destructive_request(world, prune=False)


def editor_becomes_reader_before_history_prune(world: World) -> None:
    _editor_becomes_reader_during_destructive_request(world, prune=True)


def editor_transfer_copy_ok_move_404(world: World) -> None:
    client, editor = world.client, world.headers["editor"]
    path = f"/api/notebooks/{world.notebook}/knowhow/{world.table}/transfer"
    moved = client.post(
        path, json={"target_notebook_id": world.editor_notebook, "mode": "move"},
        headers=editor,
    )
    assert moved.status_code == 404, moved.text
    assert _tables(world, world.notebook) == 1
    assert _tables(world, world.editor_notebook) == 0
    copied = client.post(
        path, json={"target_notebook_id": world.editor_notebook, "mode": "copy"},
        headers=editor,
    )
    assert copied.status_code == 200, copied.text
    assert _tables(world, world.notebook) == 1
    assert _tables(world, world.editor_notebook) == 1


def upload_response_and_list_carry_uploaded_by_me(world: World) -> None:
    client = world.client
    files = [("files", ("editor-upload.md", b"# by the editor\n\ntext", "text/markdown"))]
    uploaded = client.post(
        f"/api/notebooks/{world.notebook}/sources", files=files,
        headers=world.headers["editor"],
    )
    assert uploaded.status_code == 200, uploaded.text
    assert [item["uploaded_by_me"] for item in uploaded.json()] == [True]
    editors_id = uploaded.json()[0]["id"]
    owners = _source(world, "src-owner-listed", world.users["owner"])
    listed = {
        role: {
            item["id"]: item["uploaded_by_me"]
            for item in client.get(
                f"/api/notebooks/{world.notebook}/sources", headers=world.headers[role]
            ).json()["items"]
        }
        for role in ("editor", "owner", "reader")
    }
    assert listed["editor"] == {editors_id: True, owners: False}
    assert listed["owner"] == {editors_id: False, owners: True}
    assert listed["reader"] == {editors_id: False, owners: False}
    detail = client.get(f"/api/sources/{editors_id}", headers=world.headers["editor"])
    assert detail.status_code == 200 and detail.json()["uploaded_by_me"] is True
    assert "uploaded_by" not in detail.json(), "the uploader id never goes on the wire"


def _quiet_upload(world: World, role: str, name: str, body: bytes) -> dict:
    """Upload one Markdown file as ``role`` with background processing off, so
    a later ``/parse`` is not refused as busy (D8) by the upload's own job."""
    from app.services.kg import scheduler as kg_scheduler

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(kg_scheduler, "submit_job", lambda *args, **kwargs: None)
        response = world.client.post(
            f"/api/notebooks/{world.notebook}/sources",
            files=[("files", (name, body, "text/markdown"))],
            headers=world.headers[role],
        )
    assert response.status_code == 200, response.text
    return response.json()[0]


def uploaded_by_me_when_an_upload_reuses_a_source(world: World) -> None:
    """Same-content dedup returns the existing row: it is the caller's only
    when the caller uploaded it the first time."""
    body = b"# reused\n\nsame bytes"
    first = _quiet_upload(world, "editor", "reuse.md", body)
    again = _quiet_upload(world, "editor", "reuse-again.md", body)
    assert (again["id"], again["reused"], again["uploaded_by_me"]) == (first["id"], True, True)
    by_owner = _quiet_upload(world, "owner", "reuse-owner.md", body)
    assert (by_owner["id"], by_owner["reused"], by_owner["uploaded_by_me"]) == (
        first["id"], True, False,
    )


def uploaded_by_me_on_parse(world: World) -> None:
    editors = _quiet_upload(world, "editor", "parse-editor.md", b"# e\n\neditor text")
    owners = _quiet_upload(world, "owner", "parse-owner.md", b"# o\n\nowner text")
    for source, expected in ((editors, True), (owners, False)):
        parsed = world.client.post(
            f"/api/sources/{source['id']}/parse", headers=world.headers["editor"]
        )
        assert parsed.status_code == 200, parsed.text
        assert parsed.json()["uploaded_by_me"] is expected


def uploaded_by_me_on_import(world: World) -> None:
    from app.services.kg import scheduler as kg_scheduler

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(kg_scheduler, "submit_job", lambda *args, **kwargs: None)
        imported = world.client.post(
            f"/api/notebooks/{world.notebook}/sources/import",
            json={"files": [{"file_name": "imported.md", "file_size": 1}]},
            headers=world.headers["editor"],
        )
    assert imported.status_code == 200, imported.text
    assert [item["uploaded_by_me"] for item in imported.json()] == [True]


def uploaded_by_me_on_url_import(world: World) -> None:
    import app.api.source_routes as source_routes
    from app.core.config import get_settings
    from app.services import remote_sources
    from app.services.kg import scheduler as kg_scheduler

    proxy = "http://127.0.0.1:8100"
    settings = get_settings().model_copy(update={"url_import_trusted_proxy_hosts": proxy})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(source_routes, "get_settings", lambda: settings)
        patch.setattr(kg_scheduler, "submit_job", lambda *args, **kwargs: None)
        patch.setattr(
            remote_sources, "probe_pdf",
            lambda url, **kwargs: remote_sources.PdfProbe(True, "", 9, "snapshot.md"),
        )
        added = world.client.post(
            f"/api/notebooks/{world.notebook}/sources/url",
            json={"urls": [f"{proxy}/export/snapshot.md"]},
            headers=world.headers["editor"],
        )
    assert added.status_code == 200, added.text
    assert [item["uploaded_by_me"] for item in added.json()["created"]] == [True]


def uploaded_by_me_on_scoped_detail(world: World) -> None:
    """``GET /notebooks/{id}/sources/{source_id}`` rebuilds the detail through
    ``ScopedSourceDetail.of``: the uploader survives it, so an in-notebook read
    is shaped like ``/sources/{id}``."""
    editors = _quiet_upload(world, "editor", "scoped-editor.md", b"# s\n\nscoped")
    owners = _source(world, "src-owner-scoped", world.users["owner"])
    for source_id, role, expected in (
        (editors["id"], "editor", True), (owners, "editor", False),
        (owners, "owner", True), (editors["id"], "owner", False),
    ):
        detail = world.client.get(
            f"/api/notebooks/{world.notebook}/sources/{source_id}",
            headers=world.headers[role],
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["uploaded_by_me"] is expected, (source_id, role)
        assert "uploaded_by" not in detail.json()


CASES: dict[str, Callable[[World], Any]] = {
    "uploaded_by_me_when_an_upload_reuses_a_source": (
        uploaded_by_me_when_an_upload_reuses_a_source
    ),
    "uploaded_by_me_on_parse": uploaded_by_me_on_parse,
    "uploaded_by_me_on_import": uploaded_by_me_on_import,
    "uploaded_by_me_on_url_import": uploaded_by_me_on_url_import,
    "uploaded_by_me_on_scoped_detail": uploaded_by_me_on_scoped_detail,
    "editor_deletes_only_own_upload": editor_deletes_only_own_upload,
    "editor_cannot_delete_whole_knowhow_table": editor_cannot_delete_whole_knowhow_table,
    "editor_cannot_prune_knowhow_history": editor_cannot_prune_knowhow_history,
    "editor_becomes_reader_before_table_delete": editor_becomes_reader_before_table_delete,
    "editor_becomes_reader_before_history_prune": editor_becomes_reader_before_history_prune,
    "editor_transfer_copy_ok_move_404": editor_transfer_copy_ok_move_404,
    "upload_response_and_list_carry_uploaded_by_me": (
        upload_response_and_list_carry_uploaded_by_me
    ),
}
