from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite import knowhow_fingerprint
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


def _table_fingerprint(repo, table_id):
    with repo._runtime.database.connect() as db:
        return knowhow_fingerprint.fingerprint_on(db, table_id)


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


# ---------------------------------------------------------------------------
# 最关键的约束：record_change 必须是写事务的最后一步，因为它内部现算整表
# 指纹，指纹必须反映本次变更之后的状态（回退功能的前后置守卫靠这条成立）。
# 上面几个测试只断言 before/after/actor/origin，从不读 fingerprint 字段——
# 挪动 record_change 的调用位置也能全绿。这里逐个方法补上专门锁 fingerprint
# 的测试。
# ---------------------------------------------------------------------------


def test_cell_update_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "新内容")
    change = _cell_changes(hist, table["id"])[0]
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


def test_cells_batch_update_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.update_knowhow_cells([table["row_a"], table["row_b"]], table["plain"], "共享值")
    change = _cell_changes(hist, table["id"])[0]
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


def test_bulk_guarded_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.update_knowhow_cells_bulk_guarded(
        _notebook_of(store, table["id"]),
        [(table["id"], table["row_a"], table["plain"], None, "新值")],
    )
    change = _cell_changes(hist, table["id"])[0]
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


def test_guarded_atomic_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.update_knowhow_cells_guarded_atomic(
        _notebook_of(store, table["id"]),
        [(table["id"], table["row_a"], table["plain"], None, "新值")],
    )
    change = _cell_changes(hist, table["id"])[0]
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


# ---------------------------------------------------------------------------
# update_knowhow_cells_bulk_guarded 是"逐条校验、失败就 skip 继续跑"的语义。
# 它的流水行为有三条规则：①成功写入的条目记一条流水，payload 的 cells 只含
# 真正写成功的条目；②一条都没写成功时不记流水（不能是一条 cells 为空的
# 流水）；③一次调用跨多张表时按表分组，每张被写的表各记一条，绝不混表。
# ---------------------------------------------------------------------------


def test_bulk_guarded_payload_excludes_skipped_entries(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "A的旧值")
    store.update_knowhow_cell(table["row_b"], table["plain"], "B的旧值")
    before_count = len(_cell_changes(hist, table["id"]))

    result = store.update_knowhow_cells_bulk_guarded(
        _notebook_of(store, table["id"]),
        [
            (table["id"], table["row_a"], table["plain"], "A的旧值", "A的新值"),
            (table["id"], table["row_b"], table["plain"], "过期基线", "B的新值"),
        ],
    )
    assert result["written"] == [(table["row_a"], table["plain"])]
    assert result["skipped"] == [(table["row_b"], table["plain"])]

    changes = _cell_changes(hist, table["id"])
    assert len(changes) == before_count + 1, "成功写入的条目记一条流水"
    assert changes[0]["payload"]["cells"] == [{
        "row_id": table["row_a"], "column_id": table["plain"],
        "before": "A的旧值", "after": "A的新值",
    }], "payload 的 cells 只含真正写成功的条目，被 skip 的不进"


def test_bulk_guarded_records_nothing_when_nothing_written(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "真实值")
    before_count = len(_cell_changes(hist, table["id"]))

    result = store.update_knowhow_cells_bulk_guarded(
        _notebook_of(store, table["id"]),
        [(table["id"], table["row_a"], table["plain"], "过期基线", "新值")],
    )
    assert result["written"] == []
    assert result["skipped"] == [(table["row_a"], table["plain"])]
    assert len(_cell_changes(hist, table["id"])) == before_count, (
        "一条都没写成功时不记流水，不能记一条 cells 为空的流水"
    )


def test_bulk_guarded_records_one_entry_per_table_when_call_spans_tables(store, hist, notebook_id):
    def _make_table(name):
        table_id = store.create_knowhow_table(
            notebook_id, name, "",
            [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
        )
        detail = store.get_knowhow_table(table_id)
        anchor, plain = detail["columns"][0]["id"], detail["columns"][1]["id"]
        row = store.add_knowhow_row(table_id, {anchor: "A"})
        return table_id, plain, row

    table1, plain1, row1 = _make_table("表一")
    table2, plain2, row2 = _make_table("表二")

    result = store.update_knowhow_cells_bulk_guarded(
        notebook_id,
        [
            (table1, row1, plain1, None, "表一新值"),
            (table2, row2, plain2, None, "表二新值"),
        ],
    )
    assert result["written"] == [(row1, plain1), (row2, plain2)]

    changes1 = _cell_changes(hist, table1)
    changes2 = _cell_changes(hist, table2)
    assert len(changes1) == 1, "被写的每张表各记一条"
    assert len(changes2) == 1, "被写的每张表各记一条"
    assert changes1[0]["payload"]["cells"] == [{
        "row_id": row1, "column_id": plain1, "before": None, "after": "表一新值",
    }], "绝不混表：表一的流水只含表一自己的格子"
    assert changes2[0]["payload"]["cells"] == [{
        "row_id": row2, "column_id": plain2, "before": None, "after": "表二新值",
    }], "绝不混表：表二的流水只含表二自己的格子"
