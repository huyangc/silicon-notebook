from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id


@pytest.fixture
def store(repo):
    return repo._runtime.knowhow_store


@pytest.fixture
def hist(repo):
    return repo._runtime.knowhow_history_store


@pytest.fixture
def table(repo, store, notebook_id):
    table_id = store.create_knowhow_table(
        notebook_id, "表", "",
        [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
    )
    detail = store.get_knowhow_table(table_id)
    anchor = detail["columns"][0]["id"]
    plain = detail["columns"][1]["id"]
    row_a = store.add_knowhow_row(table_id, {anchor: "A"})
    row_b = store.add_knowhow_row(table_id, {anchor: "A"})
    return {
        "id": table_id, "anchor": anchor, "plain": plain,
        "row_a": row_a, "row_b": row_b,
    }


def _cell_changes(hist, table_id):
    return [c for c in hist.list_changes(table_id, limit=100) if c["kind"] == "cell_update"]


def test_update_cell_records_before_and_after(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "第一版")
    store.update_knowhow_cell(table["row_a"], table["plain"], "第二版")

    changes = _cell_changes(hist, table["id"])
    assert len(changes) == 2
    latest = changes[0]["payload"]["cells"]
    assert latest == [{
        "row_id": table["row_a"], "column_id": table["plain"],
        "before": "第一版", "after": "第二版",
    }]
    first = changes[1]["payload"]["cells"]
    assert first[0]["before"] is None, "格子当时不存在，before 必须是 None 而非空串"
    assert first[0]["after"] == "第一版"


def test_update_cell_carries_actor_and_origin(store, hist, table):
    store.update_knowhow_cell(
        table["row_a"], table["plain"], "x", actor="user-1", origin="llm_optimize"
    )
    change = _cell_changes(hist, table["id"])[0]
    assert change["actor"] == "user-1"
    assert change["origin"] == "llm_optimize"


def test_batch_write_is_one_change_with_every_row(store, hist, table):
    store.update_knowhow_cells(
        [table["row_a"], table["row_b"]], table["plain"], "共享值"
    )
    changes = _cell_changes(hist, table["id"])
    assert len(changes) == 1, "合并格批量写必须记一条，不是每行一条"
    cells = sorted(changes[0]["payload"]["cells"], key=lambda c: c["row_id"])
    assert [c["row_id"] for c in cells] == sorted([table["row_a"], table["row_b"]])
    assert all(c["after"] == "共享值" for c in cells)


def test_empty_batch_records_nothing(store, hist, table):
    store.update_knowhow_cells([], table["plain"], "x")
    assert _cell_changes(hist, table["id"]) == []


def test_guarded_atomic_records_one_change_on_success(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "基线")
    before_count = len(_cell_changes(hist, table["id"]))

    result = store.update_knowhow_cells_guarded_atomic(
        _notebook_of(store, table["id"]),
        [(table["id"], table["row_a"], table["plain"], "基线", "新值")],
    )
    assert result["conflict"] is False
    changes = _cell_changes(hist, table["id"])
    assert len(changes) == before_count + 1
    assert changes[0]["payload"]["cells"][0]["before"] == "基线"
    assert changes[0]["payload"]["cells"][0]["after"] == "新值"


def test_guarded_atomic_conflict_records_nothing(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "真实值")
    before_count = len(_cell_changes(hist, table["id"]))

    result = store.update_knowhow_cells_guarded_atomic(
        _notebook_of(store, table["id"]),
        [(table["id"], table["row_a"], table["plain"], "过期基线", "新值")],
    )
    assert result["conflict"] is True
    assert len(_cell_changes(hist, table["id"])) == before_count, (
        "冲突时什么都没写，流水也不能有"
    )


def _notebook_of(store, table_id: str) -> str:
    return store.get_knowhow_table(table_id)["notebook_id"]
