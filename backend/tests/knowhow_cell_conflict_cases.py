"""The guarded Knowhow cell write's reason codes and post-write values, shared
by both backends (share-edit P1-T11, decision D7, plan PD-1).

``test_knowhow_cell_conflict.py`` runs every case on SQLite and
``postgres/test_knowhow_cell_conflict_pg.py`` runs the same cases on
PostgreSQL. One world: an owner, a named editor, and a table whose first
column is the anchor -- rows ``r1``/``r2`` share anchor value 「振铃」 (a merged
group), ``r3`` stands alone, and ``r3``'s procedure cell was never written.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient


@dataclass
class World:
    client: TestClient
    postgres: bool
    notebook: str
    table: str
    anchor: str
    column: str
    rows: list[str]
    headers: dict[str, dict] = field(default_factory=dict)

    def cell_url(self, row: str) -> str:
        return (f"/api/notebooks/{self.notebook}/knowhow/{self.table}/rows/{row}"
                f"/cells/{self.column}")

    @property
    def batch_url(self) -> str:
        return f"/api/notebooks/{self.notebook}/knowhow/{self.table}/cells"

    def stored(self, row: str) -> str:
        table = _store().get_knowhow_table(self.table)
        return next(r for r in table["rows"] if r["id"] == row)["cells"].get(self.column, "")


def _repository():
    from app.api.deps import repository

    return repository()


def _store():
    return _repository()._runtime.knowhow_store


def _login(client: TestClient, username: str) -> dict:
    client.post("/api/auth/register", json={"username": username, "password": "pw123456"})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw123456"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def build_world(client: TestClient, *, postgres: bool) -> World:
    headers = {name: _login(client, f"k{20261400 + index:08d}")
               for index, name in enumerate(("owner", "editor"))}
    users = {name: client.get("/api/me", headers=h).json()["id"] for name, h in headers.items()}
    owner = headers["owner"]
    notebook = client.post("/api/notebooks", json={"name": "表格库"}, headers=owner).json()["id"]
    _repository()._runtime.groups.set_user_editor_grant(
        notebook, owner_id=users["owner"], target_user_id=users["editor"]
    )
    table = client.post(
        f"/api/notebooks/{notebook}/knowhow",
        json={"title": "判别表", "anchor_index": 0, "columns": [
            {"name": "现象", "kind": "attribute"}, {"name": "方法", "kind": "procedure"},
        ]},
        headers=owner,
    )
    assert table.status_code == 200, table.text
    anchor, column = (c["id"] for c in table.json()["columns"])
    rows = []
    for cells in ({anchor: "振铃", column: "旧方法"}, {anchor: "振铃", column: "旧方法"},
                  {anchor: "过冲"}):
        row = client.post(
            f"/api/notebooks/{notebook}/knowhow/{table.json()['id']}/rows",
            json={"cells": cells}, headers=owner,
        )
        assert row.status_code == 200, row.text
        rows.append(row.json()["id"])
    return World(client=client, postgres=postgres, notebook=notebook,
                 table=table.json()["id"], anchor=anchor, column=column, rows=rows,
                 headers=headers)


def _guarded(world: World, updates, **kwargs) -> dict:
    return _store().update_knowhow_cells_guarded_atomic(world.notebook, updates, **kwargs)


# ---------------------------------------------------------------- cases


def guarded_write_reason_content_changed_returns_current(world: World) -> None:
    r1, r2, _r3 = world.rows
    mutation_seq = _store().update_knowhow_cell(r2, world.column, "别人改的")
    result = _guarded(world, [
        (world.table, r1, world.column, "旧方法", "我的"),
        (world.table, r2, world.column, "旧方法", "我的"),
    ])
    assert result["conflict"] is True and result["written"] == []
    assert result["reason"] == "content_changed"
    current_seq = result["current"][0]["mutation_seq"]
    assert current_seq >= mutation_seq
    assert result["current"] == [
        {"row_id": r1, "column_id": world.column, "content_md": "旧方法", "mutation_seq": current_seq},
        {"row_id": r2, "column_id": world.column, "content_md": "别人改的", "mutation_seq": current_seq},
    ]
    assert world.stored(r1) == "旧方法"


def guarded_write_reason_group_changed_on_anchor_or_membership(world: World) -> None:
    r1, r2, r3 = world.rows
    targets = [(world.table, r1, world.column, "旧方法", "我的"),
               (world.table, r2, world.column, "旧方法", "我的")]
    # an anchor moved out of the group
    _store().update_knowhow_cell(r2, world.anchor, "别的现象")
    moved = _guarded(world, targets, anchor_column_id=world.anchor,
                     expected_anchor=["振铃", "振铃"])
    assert (moved["conflict"], moved["reason"], moved["current"]) == (
        True, "group_changed", []
    )
    _store().update_knowhow_cell(r2, world.anchor, "振铃")
    # a row joined the group
    _store().update_knowhow_cell(r3, world.anchor, "振铃")
    joined = _guarded(world, targets, anchor_column_id=world.anchor,
                      expected_anchor=["振铃", "振铃"])
    assert (joined["conflict"], joined["reason"]) == (True, "group_changed")
    # a row that is gone
    gone = _guarded(world, [(world.table, "khrow-missing", world.column, "", "x")])
    assert (gone["conflict"], gone["reason"]) == (True, "group_changed")
    assert world.stored(r1) == world.stored(r2) == "旧方法"


def single_cell_patch_409_shape(world: World) -> None:
    r1 = world.rows[0]
    mutation_seq = _store().update_knowhow_cell(r1, world.column, "别人改的")
    refused = world.client.patch(
        world.cell_url(r1), json={"content_md": "我的", "expected_before": "旧方法"},
        headers=world.headers["editor"],
    )
    assert refused.status_code == 409
    current_seq = refused.json()["detail"]["current"][0]["mutation_seq"]
    assert current_seq >= mutation_seq
    assert refused.json()["detail"] == {
        "code": "knowhow_cell_conflict",
        "reason": "content_changed",
        "message": "内容已被其他人修改，请刷新后重试",
        "current": [{"row_id": r1, "column_id": world.column, "content_md": "别人改的", "mutation_seq": current_seq}],
    }
    # "overwrite with mine": the current value is the new baseline
    overwrite = world.client.patch(
        world.cell_url(r1), json={"content_md": "我的", "expected_before": "别人改的"},
        headers=world.headers["editor"],
    )
    assert overwrite.status_code == 200, overwrite.text
    assert world.stored(r1) == "我的"


def batch_patch_409_shape(world: World) -> None:
    r1, r2, r3 = world.rows
    body = {"column_id": world.column, "row_ids": [r1, r2], "content_md": "合并格",
            "expected_before": ["旧方法", "旧方法"], "anchor_column_id": world.anchor,
            "expected_anchor": ["振铃", "振铃"]}
    _store().update_knowhow_cell(r1, world.column, "别人改的")
    content = world.client.patch(world.batch_url, json=body, headers=world.headers["editor"])
    assert content.status_code == 409
    detail = content.json()["detail"]
    assert (detail["code"], detail["reason"]) == ("knowhow_cell_conflict", "content_changed")
    assert [item["content_md"] for item in detail["current"]] == ["别人改的", "旧方法"]
    _store().update_knowhow_cell(r1, world.column, "旧方法")
    _store().update_knowhow_cell(r3, world.anchor, "振铃")
    group = world.client.patch(world.batch_url, json=body, headers=world.headers["editor"])
    assert group.status_code == 409
    assert group.json()["detail"] == {
        "code": "knowhow_cell_conflict", "reason": "group_changed",
        "message": "这一组已被重新分组，请刷新后再编辑", "current": [],
    }
    assert world.stored(r1) == world.stored(r2) == "旧方法"


def _assert_structural_refusal(response) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "knowhow_cell_conflict", "reason": "group_changed",
        "message": "这一组已被重新分组，请刷新后再编辑", "current": [],
    }


def guarded_batch_deleted_sibling_is_structural(world: World) -> None:
    r1, r2, _r3 = world.rows
    removed = world.client.delete(
        f"/api/notebooks/{world.notebook}/knowhow/{world.table}/rows/{r2}",
        headers=world.headers["owner"],
    )
    assert removed.status_code == 204, removed.text
    body = {"column_id": world.column, "row_ids": [r1, r2], "content_md": "我的",
            "expected_before": ["旧方法", "旧方法"], "anchor_column_id": world.anchor,
            "expected_anchor": ["振铃", "振铃"]}
    refused = world.client.patch(world.batch_url, json=body, headers=world.headers["editor"])
    _assert_structural_refusal(refused)
    assert world.stored(r1) == "旧方法"
    # Unguarded legacy calls keep their invalid-target contract.
    body.pop("expected_before")
    legacy = world.client.patch(world.batch_url, json=body, headers=world.headers["editor"])
    assert legacy.status_code == 400
    assert world.stored(r1) == "旧方法"


def guarded_single_deleted_row_is_structural(world: World) -> None:
    row = world.rows[0]
    removed = world.client.delete(
        f"/api/notebooks/{world.notebook}/knowhow/{world.table}/rows/{row}",
        headers=world.headers["owner"],
    )
    assert removed.status_code == 204, removed.text
    _assert_structural_refusal(world.client.patch(
        world.cell_url(row), json={"content_md": "我的", "expected_before": "旧方法"},
        headers=world.headers["editor"],
    ))
    assert world.stored(world.rows[1]) == "旧方法"


def guarded_deleted_column_is_structural(world: World) -> None:
    removed = world.client.delete(
        f"/api/notebooks/{world.notebook}/knowhow/{world.table}/columns/{world.column}",
        headers=world.headers["owner"],
    )
    assert removed.status_code == 204, removed.text
    _assert_structural_refusal(world.client.patch(
        world.cell_url(world.rows[0]),
        json={"content_md": "我的", "expected_before": "旧方法"},
        headers=world.headers["editor"],
    ))
    _assert_structural_refusal(world.client.patch(
        world.batch_url,
        json={"column_id": world.column, "row_ids": world.rows[:2], "content_md": "我的",
              "expected_before": ["旧方法", "旧方法"]},
        headers=world.headers["editor"],
    ))
    assert all(world.column not in row["cells"] for row in _store().get_knowhow_table(world.table)["rows"])


def normalized_storage_value_baseline_no_self_conflict(world: World) -> None:
    """One person saving the same cell N times with the RESPONSE's stored
    value as the next baseline never conflicts -- a plain cell, a never-written
    cell (baseline ""), and a merged group through the batch endpoint."""
    r1, r2, r3 = world.rows
    for row, baseline in ((r1, "旧方法"), (r3, "")):
        for n in range(5):
            saved = world.client.patch(
                world.cell_url(row),
                json={"content_md": f"第 {n} 版\n\n  带空白  ", "expected_before": baseline},
                headers=world.headers["editor"],
            )
            assert saved.status_code == 200, (row, n, saved.text)
            baseline = saved.json()["content_md"]
            assert saved.json()["projection_status"] == "pending"
    baselines = ["旧方法", "旧方法"]
    for n in range(5):
        saved = world.client.patch(world.batch_url, json={
            "column_id": world.column, "row_ids": [r1, r2], "content_md": f"合并第 {n} 版",
            "expected_before": baselines, "anchor_column_id": world.anchor,
            "expected_anchor": ["振铃", "振铃"],
        }, headers=world.headers["editor"])
        if n == 0:
            # r1 was saved above: its stored value, not 旧方法, is the baseline
            assert saved.status_code == 409
            baselines = [item["content_md"] for item in saved.json()["detail"]["current"]]
            continue
        assert saved.status_code == 200, (n, saved.text)
        baselines = [item["content_md"] for item in saved.json()]
        assert [item["row_id"] for item in saved.json()] == [r1, r2]


def write_response_returns_own_stored_value_not_a_later_write(world: World) -> None:
    """A later writer landing between this write's commit and the response
    cannot leak into the response (the client's next baseline): the values come
    from the write transaction, and the route never re-reads the table."""
    r1, r2, _r3 = world.rows
    store = _store()
    repo = _repository()
    original = store.update_knowhow_cells_guarded_atomic
    reads = {"after": 0}
    original_read = repo.get_knowhow_table
    committed_sequences = []

    def write_then_someone_else(*args, **kwargs):
        result = original(*args, **kwargs)
        committed_sequences.append(result["stored"][0]["mutation_seq"])
        for row in (r1, r2):
            store.update_knowhow_cell(row, world.column, "后来者")
        reads["after"] = 1
        return result

    def read(table_id):
        if reads["after"]:
            raise AssertionError("the route re-read the table after the write")
        return original_read(table_id)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "update_knowhow_cells_guarded_atomic", write_then_someone_else)
        patch.setattr(repo, "get_knowhow_table", read)
        single = world.client.patch(
            world.cell_url(r1), json={"content_md": "我的", "expected_before": "旧方法"},
            headers=world.headers["editor"],
        )
        assert single.status_code == 200, single.text
        assert single.json()["content_md"] == "我的"
        assert single.json()["mutation_seq"] == committed_sequences[-1]
        reads["after"] = 0
        batch = world.client.patch(world.batch_url, json={
            "column_id": world.column, "row_ids": [r1, r2], "content_md": "合并的",
            "expected_before": ["后来者", "后来者"],
        }, headers=world.headers["editor"])
        assert batch.status_code == 200, batch.text
        assert [item["content_md"] for item in batch.json()] == ["合并的", "合并的"]
        assert [item["mutation_seq"] for item in batch.json()] == [committed_sequences[-1]] * 2
    assert world.stored(r1) == "后来者"
    assert store.get_knowhow_table(world.table)["mutation_seq"] > committed_sequences[-1]


def legacy_write_response_keeps_own_commit_revision(world: World) -> None:
    """Omitting expected_before stays last-write-wins, but must not return a
    later writer's revision or content after the transaction has committed."""
    store, repo = _store(), _repository()
    r1, r2, _r3 = world.rows
    original_single, original_batch = store.update_knowhow_cell, store.update_knowhow_cells
    original_read = repo.get_knowhow_table
    after_write = False
    committed_sequences = []

    def later_write(original, *args, **kwargs):
        nonlocal after_write
        sequence = original(*args, **kwargs)
        committed_sequences.append(sequence)
        original_single(r1, world.column, "后来者")
        original_single(r2, world.column, "后来者")
        after_write = True
        return sequence

    def read(table_id):
        assert not after_write, "a post-commit read cannot label the earlier saved value"
        return original_read(table_id)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "update_knowhow_cell", lambda *a, **kw: later_write(original_single, *a, **kw))
        patch.setattr(store, "update_knowhow_cells", lambda *a, **kw: later_write(original_batch, *a, **kw))
        patch.setattr(repo, "get_knowhow_table", read)
        single = world.client.patch(world.cell_url(r1), json={"content_md": "我的"}, headers=world.headers["editor"])
        assert single.status_code == 200, single.text
        assert single.json()["content_md"] == "我的"
        assert single.json()["mutation_seq"] == committed_sequences[-1]
        after_write = False
        batch = world.client.patch(world.batch_url, json={
            "column_id": world.column, "row_ids": [r1, r2], "content_md": "合并的",
        }, headers=world.headers["editor"])
        assert batch.status_code == 200, batch.text
        assert [item["content_md"] for item in batch.json()] == ["合并的", "合并的"]
        assert [item["mutation_seq"] for item in batch.json()] == [committed_sequences[-1]] * 2
    assert world.stored(r1) == "后来者"
    assert store.get_knowhow_table(world.table)["mutation_seq"] > committed_sequences[-1]


def table_detail_revision_and_cells_share_snapshot(world: World) -> None:
    """Commit another writer after the head is read, before the cell query.
    A detail response must contain the old head AND old cells, never a mix."""
    store = _store()
    before = store.get_knowhow_table(world.table)
    original_connect = store.database.connect
    committed = False
    captured_seq = None

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            nonlocal committed, captured_seq
            row = self.cursor.fetchone()
            if row is not None and "mutation_seq" in row.keys() and "title" in row.keys() and not committed:
                committed = True
                captured_seq = row["mutation_seq"]
                store.update_knowhow_cell(world.rows[0], world.column, "读快照之后提交")
            return row

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class Connection:
        def __init__(self, db):
            self.db = db

        def execute(self, *args, **kwargs):
            return Cursor(self.db.execute(*args, **kwargs))

        def __getattr__(self, name):
            return getattr(self.db, name)

    @contextmanager
    def connect():
        with original_connect() as db:
            yield Connection(db)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store.database, "connect", connect)
        snapshot = store.get_knowhow_table(world.table)
    assert committed
    assert snapshot["mutation_seq"] == captured_seq
    assert {row["id"]: row["cells"] for row in snapshot["rows"]} == {row["id"]: row["cells"] for row in before["rows"]}
    after = store.get_knowhow_table(world.table)
    assert after["mutation_seq"] > snapshot["mutation_seq"]
    assert next(row for row in after["rows"] if row["id"] == world.rows[0])["cells"][world.column] == "读快照之后提交"


def versioned_table_detail_works_inside_read_budget(world: World) -> None:
    from app.repositories.read_budget import read_budget

    with read_budget(time.monotonic() + 30):
        table = _store().get_knowhow_table(world.table)
    assert table["mutation_seq"] >= 0
    assert next(row for row in table["rows"] if row["id"] == world.rows[0])["cells"][world.column] == "旧方法"


CASES: dict[str, Callable[[World], Any]] = {
    "guarded_batch_deleted_sibling_is_structural": guarded_batch_deleted_sibling_is_structural,
    "guarded_single_deleted_row_is_structural": guarded_single_deleted_row_is_structural,
    "guarded_deleted_column_is_structural": guarded_deleted_column_is_structural,
    "guarded_write_reason_content_changed_returns_current": (
        guarded_write_reason_content_changed_returns_current
    ),
    "guarded_write_reason_group_changed_on_anchor_or_membership": (
        guarded_write_reason_group_changed_on_anchor_or_membership
    ),
    "single_cell_patch_409_shape": single_cell_patch_409_shape,
    "batch_patch_409_shape": batch_patch_409_shape,
    "normalized_storage_value_baseline_no_self_conflict": (
        normalized_storage_value_baseline_no_self_conflict
    ),
    "write_response_returns_own_stored_value_not_a_later_write": (
        write_response_returns_own_stored_value_not_a_later_write
    ),
    "legacy_write_response_keeps_own_commit_revision": legacy_write_response_keeps_own_commit_revision,
    "table_detail_revision_and_cells_share_snapshot": table_detail_revision_and_cells_share_snapshot,
    "versioned_table_detail_works_inside_read_budget": versioned_table_detail_works_inside_read_budget,
}
