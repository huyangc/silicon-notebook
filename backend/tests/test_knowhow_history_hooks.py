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


# ---------------------------------------------------------------------------
# Task 5：行与列的增删改（add/delete row，add/rename/set-kind/delete column，
# set-anchor）也要各自挂上流水。删除类是核心风险——CASCADE 会带走格子和代码
# 附件，不在 DELETE 之前存进 payload 就永远回不来。既有的静默 no-op 语义
# （目标已不存在 / 改成当前值）必须保留，且 no-op 不产生流水噪声。
# ---------------------------------------------------------------------------


def _kinds(hist, table_id):
    return [c["kind"] for c in hist.list_changes(table_id, limit=100)]


def test_delete_row_stores_whole_row_for_reversal(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "要被删掉的内容")
    store.upsert_knowhow_cell_code(
        table["row_a"], table["plain"], "print(1)", "python", "user-1", "hash-x"
    )
    store.delete_knowhow_row(table["row_a"])

    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "row_delete"
    row = change["payload"]["rows"][0]
    assert row["row_id"] == table["row_a"]
    assert row["cells"][table["plain"]] == "要被删掉的内容"
    assert row["cells"][table["anchor"]] == "A"
    assert row["code"][0]["code_text"] == "print(1)", (
        "代码附件随行 CASCADE 消失，不存进 payload 就永远回不来"
    )
    assert isinstance(row["position"], int)


def test_delete_missing_row_records_nothing(store, hist, table):
    before = len(hist.list_changes(table["id"], limit=100))
    store.delete_knowhow_row("khrow-does-not-exist")
    assert len(hist.list_changes(table["id"], limit=100)) == before


def test_delete_column_stores_column_and_all_its_cells(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "甲")
    store.update_knowhow_cell(table["row_b"], table["plain"], "乙")
    store.delete_knowhow_column(table["plain"])

    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "column_delete"
    assert change["payload"]["column"]["id"] == table["plain"]
    assert change["payload"]["column"]["name"] == "做法"
    assert change["payload"]["column"]["role"] == "attribute"
    contents = {c["row_id"]: c["content_md"] for c in change["payload"]["cells"]}
    assert contents == {table["row_a"]: "甲", table["row_b"]: "乙"}


def test_add_row_records_its_cells(store, hist, table):
    row_id = store.add_knowhow_row(table["id"], {table["anchor"]: "新概念"})
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "row_add"
    assert change["payload"]["rows"][0]["row_id"] == row_id
    assert change["payload"]["rows"][0]["cells"][table["anchor"]] == "新概念"


def test_column_rename_records_before_after(store, hist, table):
    store.rename_knowhow_column(table["plain"], "新列名")
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "column_rename"
    assert change["payload"] == {
        "column_id": table["plain"], "before": "做法", "after": "新列名",
    }


def test_renaming_to_the_same_name_records_nothing(store, hist, table):
    before = len(hist.list_changes(table["id"], limit=100))
    store.rename_knowhow_column(table["plain"], "做法")
    assert len(hist.list_changes(table["id"], limit=100)) == before, (
        "同名改名是既有的静默成功语义，不该产生噪声流水"
    )


def test_anchor_move_records_both_columns(store, hist, table):
    store.set_knowhow_anchor_column(table["id"], table["plain"])
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "anchor_set"
    moves = {c["column_id"]: (c["before"], c["after"]) for c in change["payload"]["columns"]}
    assert moves[table["anchor"]] == ("anchor", "attribute")
    assert moves[table["plain"]] == ("attribute", "anchor")


def test_anchor_noop_records_nothing(store, hist, table):
    before = len(hist.list_changes(table["id"], limit=100))
    store.set_knowhow_anchor_column(table["id"], table["anchor"])
    assert len(hist.list_changes(table["id"], limit=100)) == before


# ---------------------------------------------------------------------------
# 同一条硬约束（record_change 必须是写事务的最后一步）在 Task 5 的 7 个方法
# 上各补一条 fingerprint 时序测试，照 test_cell_update_records_the_
# fingerprint_of_the_state_after_the_write 的写法：断言"记下的 fingerprint
# == 操作完成后的整表指纹"。挪动 record_change 到 DML 之前应该让这些测试真红
# （变异验证见 task-5-report.md）。
# ---------------------------------------------------------------------------


def test_row_add_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.add_knowhow_row(table["id"], {table["anchor"]: "新概念"})
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "row_add"
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


def test_row_delete_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "内容")
    store.delete_knowhow_row(table["row_a"])
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "row_delete"
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


def test_column_add_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.add_knowhow_column(table["id"], "新列", "entity")
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "column_add"
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


def test_column_rename_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.rename_knowhow_column(table["plain"], "新列名")
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "column_rename"
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


def test_column_kind_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.set_knowhow_column_kind(table["plain"], "entity")
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "column_kind"
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


def test_column_delete_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "甲")
    store.delete_knowhow_column(table["plain"])
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "column_delete"
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


def test_anchor_set_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.set_knowhow_anchor_column(table["id"], table["plain"])
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "anchor_set"
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


# ---------------------------------------------------------------------------
# Task 6：最后 4 个写方法——建表、表元信息、格子代码附件的写入与删除。
# create_knowhow_table 是唯一产生"创世"流水（seq==1，本表此前不可能有别
# 的流水）的方法；update_knowhow_table_meta 的 PATCH 语义（未传字段维持
# before 值）必须体现在 after 里；upsert/delete_knowhow_cell_code 都没有
# table_id 参数，要靠 row_id 反查。
# ---------------------------------------------------------------------------


def test_create_table_records_genesis_change(store, hist, notebook_id):
    table_id = store.create_knowhow_table(
        notebook_id, "新表", "说明", [{"name": "概念", "role": "anchor"}]
    )
    changes = hist.list_changes(table_id, limit=10)
    assert len(changes) == 1
    assert changes[0]["kind"] == "table_create"
    assert changes[0]["seq"] == 1
    assert changes[0]["payload"]["table"] == {"title": "新表", "description": "说明"}
    assert changes[0]["payload"]["columns"][0]["name"] == "概念"
    assert changes[0]["payload"]["rows"] == []


def test_table_meta_records_before_after(store, hist, table):
    store.update_knowhow_table_meta(table["id"], title="改了标题")
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "table_meta"
    assert change["payload"]["before"]["title"] == "表"
    assert change["payload"]["after"]["title"] == "改了标题"
    assert change["payload"]["before"]["description"] == ""
    assert change["payload"]["after"]["description"] == ""


def test_table_meta_noop_records_nothing(store, hist, table):
    before = len(hist.list_changes(table["id"], limit=100))
    store.update_knowhow_table_meta(table["id"])
    assert len(hist.list_changes(table["id"], limit=100)) == before


def test_cell_code_put_and_delete_round_trip(store, hist, table):
    store.upsert_knowhow_cell_code(
        table["row_a"], table["plain"], "print(1)", "python", "user-1", "h1"
    )
    put = hist.list_changes(table["id"], limit=1)[0]
    assert put["kind"] == "cell_code_put"
    assert put["payload"]["before"] is None
    assert put["payload"]["after"]["code_text"] == "print(1)"

    store.upsert_knowhow_cell_code(
        table["row_a"], table["plain"], "print(2)", "python", "user-1", "h2"
    )
    updated = hist.list_changes(table["id"], limit=1)[0]
    assert updated["payload"]["before"]["code_text"] == "print(1)"
    assert updated["payload"]["after"]["code_text"] == "print(2)"

    store.delete_knowhow_cell_code(table["row_a"], table["plain"])
    removed = hist.list_changes(table["id"], limit=1)[0]
    assert removed["kind"] == "cell_code_delete"
    assert removed["payload"]["before"]["code_text"] == "print(2)"
    assert removed["payload"]["after"] is None


def test_deleting_absent_cell_code_records_nothing(store, hist, table):
    before = len(hist.list_changes(table["id"], limit=100))
    store.delete_knowhow_cell_code(table["row_a"], table["plain"])
    assert len(hist.list_changes(table["id"], limit=100)) == before


# ---------------------------------------------------------------------------
# 同一条硬约束（record_change 必须是写事务的最后一步）在 Task 6 的 4 个
# 方法上各补一条 fingerprint 时序测试，照既有模式：断言"记下的
# fingerprint == 操作完成后的整表指纹"（变异验证见 task-6-report.md）。
# ---------------------------------------------------------------------------


def test_table_create_records_the_fingerprint_of_the_state_after_the_write(repo, store, notebook_id):
    table_id = store.create_knowhow_table(
        notebook_id, "新表", "说明", [{"name": "概念", "role": "anchor"}]
    )
    change = repo._runtime.knowhow_history_store.list_changes(table_id, limit=1)[0]
    assert change["kind"] == "table_create"
    assert change["fingerprint"] == _table_fingerprint(repo, table_id)


def test_table_meta_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.update_knowhow_table_meta(table["id"], title="改了标题")
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "table_meta"
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


def test_cell_code_put_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.upsert_knowhow_cell_code(
        table["row_a"], table["plain"], "print(1)", "python", "user-1", "h1"
    )
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "cell_code_put"
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])


def test_cell_code_delete_records_the_fingerprint_of_the_state_after_the_write(repo, store, hist, table):
    store.upsert_knowhow_cell_code(
        table["row_a"], table["plain"], "print(1)", "python", "user-1", "h1"
    )
    store.delete_knowhow_cell_code(table["row_a"], table["plain"])
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "cell_code_delete"
    assert change["fingerprint"] == _table_fingerprint(repo, table["id"])
