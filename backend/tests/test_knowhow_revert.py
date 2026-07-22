from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite import knowhow_fingerprint
from app.repositories.sqlite.knowhow_history_store import (
    HistoryInconsistent, HistoryStale, RevertVerifyFailed,
)
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
    return {
        "id": table_id,
        "anchor": detail["columns"][0]["id"],
        "plain": detail["columns"][1]["id"],
    }
    # 刻意**不**预建行：本文件多条测试断言 rows[0] 或 rows == []，
    # fixture 里塞一行会让它们全部错位。要行的测试自己建。


def _fp(repo, table_id):
    with repo._runtime.database.connect() as db:
        return knowhow_fingerprint.fingerprint_on(db, table_id)


def test_revert_restores_cell_content(repo, store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "第一版")
    good_seq = hist.head_seq(table["id"])
    good_fp = _fp(repo, table["id"])

    store.update_knowhow_cell(row, table["plain"], "第二版")
    store.update_knowhow_cell(row, table["plain"], "第三版")

    hist.revert_to(table["id"], good_seq, hist.head_seq(table["id"]), actor="user-1")

    detail = store.get_knowhow_table(table["id"])
    assert detail["rows"][0]["cells"][table["plain"]] == "第一版"
    assert _fp(repo, table["id"]) == good_fp


def test_revert_appends_a_new_change_and_keeps_the_old_ones(store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "旧")
    good = hist.head_seq(table["id"])
    store.update_knowhow_cell(row, table["plain"], "新")
    head_before = hist.head_seq(table["id"])

    result = hist.revert_to(table["id"], good, head_before, actor="user-1")

    assert result["seq"] == head_before + 1
    assert hist.get_change(table["id"], head_before) is not None, "旧流水必须保留"
    revert = hist.get_change(table["id"], result["seq"])
    assert revert["kind"] == "revert"
    assert revert["origin"] == "revert"
    assert revert["payload"]["target_seq"] == good


def test_revert_of_a_revert_returns_to_the_newer_state(store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "旧")
    good = hist.head_seq(table["id"])
    store.update_knowhow_cell(row, table["plain"], "新")
    newer = hist.head_seq(table["id"])

    hist.revert_to(table["id"], good, newer)
    assert store.get_knowhow_table(table["id"])["rows"][0]["cells"][table["plain"]] == "旧"

    hist.revert_to(table["id"], newer, hist.head_seq(table["id"]))
    assert store.get_knowhow_table(table["id"])["rows"][0]["cells"][table["plain"]] == "新"


def test_revert_rebuilds_a_deleted_row_with_the_same_id(store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "内容")
    store.upsert_knowhow_cell_code(row, table["plain"], "print(1)", "python", "u", "h")
    good = hist.head_seq(table["id"])

    store.delete_knowhow_row(row)
    assert store.get_knowhow_table(table["id"])["rows"] == []

    hist.revert_to(table["id"], good, hist.head_seq(table["id"]))

    rows = store.get_knowhow_table(table["id"])["rows"]
    assert len(rows) == 1
    assert rows[0]["id"] == row, "row_id 必须原样复用——引用跳转与代码附件都挂在它上面"
    assert rows[0]["cells"][table["plain"]] == "内容"
    code = store.get_knowhow_cell_code(row, table["plain"])
    assert code is not None and code["code_text"] == "print(1)"


def test_revert_rebuilds_a_deleted_column_with_all_its_cells(store, hist, table):
    row_a = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    row_b = store.add_knowhow_row(table["id"], {table["anchor"]: "B"})
    store.update_knowhow_cell(row_a, table["plain"], "甲")
    store.update_knowhow_cell(row_b, table["plain"], "乙")
    good = hist.head_seq(table["id"])

    store.delete_knowhow_column(table["plain"])
    hist.revert_to(table["id"], good, hist.head_seq(table["id"]))

    detail = store.get_knowhow_table(table["id"])
    assert [c["id"] for c in detail["columns"]] == [table["anchor"], table["plain"]]
    by_row = {r["id"]: r["cells"] for r in detail["rows"]}
    assert by_row[row_a][table["plain"]] == "甲"
    assert by_row[row_b][table["plain"]] == "乙"


def test_revert_undoes_an_added_row(store, hist, table):
    good = hist.head_seq(table["id"])
    store.add_knowhow_row(table["id"], {table["anchor"]: "多余的行"})

    hist.revert_to(table["id"], good, hist.head_seq(table["id"]))
    assert store.get_knowhow_table(table["id"])["rows"] == []


def test_stale_head_is_rejected(store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    good = hist.head_seq(table["id"])
    store.update_knowhow_cell(row, table["plain"], "别人刚改的")

    with pytest.raises(HistoryStale):
        hist.revert_to(table["id"], good, good)  # 前端以为 head 还是 good


def test_out_of_band_edit_is_detected_and_refused(repo, store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "正常内容")
    good = hist.head_seq(table["id"])
    store.update_knowhow_cell(row, table["plain"], "再改一次")
    head = hist.head_seq(table["id"])

    # 绕过 store 直接改库——模拟"某条写路径漏挂钩"
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE knowhow_cells SET content_md = '偷偷改的' "
            "WHERE row_id = ? AND column_id = ?",
            (row, table["plain"]),
        )

    with pytest.raises(HistoryInconsistent):
        hist.revert_to(table["id"], good, head)

    assert store.get_knowhow_table(table["id"])["rows"][0]["cells"][table["plain"]] == "偷偷改的", (
        "拒绝回退时必须什么都不改"
    )


def test_revert_to_unknown_seq_raises_key_error(store, hist, table):
    with pytest.raises(KeyError):
        hist.revert_to(table["id"], 999, hist.head_seq(table["id"]))


def test_legacy_table_without_a_genesis_change_still_reverts(repo, store, hist, notebook_id):
    """存量表（本特性上线前建的）没有 table_create 流水（spec §7.2）。

    前置指纹守卫拿的是"最新流水的 fingerprint"，而那条流水的指纹本来就是
    那次编辑之后算的，所以在这类表上依然成立——回退最早只能到上线后第一条。
    """
    table_id = store.create_knowhow_table(
        notebook_id, "存量表", "",
        [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
    )
    column_id = store.get_knowhow_table(table_id)["columns"][1]["id"]
    row = store.add_knowhow_row(table_id, {})
    # 抹掉建表以来的全部流水，模拟"上线前就存在的表"
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM knowhow_changes WHERE table_id = ?", (table_id,))
    assert hist.head_seq(table_id) == 0

    store.update_knowhow_cell(row, column_id, "上线后第一次编辑")
    first = hist.head_seq(table_id)
    store.update_knowhow_cell(row, column_id, "第二次编辑")

    hist.revert_to(table_id, first, hist.head_seq(table_id))

    assert store.get_knowhow_table(table_id)["rows"][0]["cells"][column_id] == "上线后第一次编辑"


def test_reverting_cell_content_makes_its_code_attachment_fresh_again(store, hist, table):
    """代码附件新鲜度靠 cell_content_hash vs 当前净文本 hash 推导（spec §7.4）。

    回退格子内容后 hash 变回旧值，代码自动从 stale 回到 fresh——这是**正确**
    行为（内容回到当时，代码就重新对上了），这条测试把它钉死，防止将来有人
    把它当 bug"修"掉。
    """
    from app.services.knowhow.api import cell_content_hash

    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "版本一")
    store.upsert_knowhow_cell_code(
        row, table["plain"], "print(1)", "python", "u", cell_content_hash("版本一"),
    )
    good = hist.head_seq(table["id"])

    store.update_knowhow_cell(row, table["plain"], "版本二")
    code = store.get_knowhow_cell_code(row, table["plain"])
    assert code["cell_content_hash"] != cell_content_hash("版本二"), "此刻应为 stale"

    hist.revert_to(table["id"], good, hist.head_seq(table["id"]))

    code = store.get_knowhow_cell_code(row, table["plain"])
    assert code["cell_content_hash"] == cell_content_hash("版本一"), "回退后应重新对上"


def test_cell_history_finds_cells_reborn_via_revert_rows_added(store, hist, table):
    """revert 的 payload 里 rows_removed/rows_added 嵌着格子内容——cell_history
    必须也能从这里面挖出条目，不能只看顶层 cells[]（任务 8 派单第 5 点，与
    Task 3 已修的 row_add/row_delete 抽取同一套语义）。

    这条覆盖 rows_added 分支：删除一行再回退，行被 _rebuild_row 复活——
    对这条 revert 流水而言，该行在 head 侧（回退前，即刚删完）不存在、在
    target 侧（回退后）存在，格子诞生：before=None。
    """
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "内容")
    good = hist.head_seq(table["id"])

    store.delete_knowhow_row(row)
    hist.revert_to(table["id"], good, hist.head_seq(table["id"]))

    entries = hist.cell_history(table["id"], row, table["plain"])
    revert_entries = [e for e in entries if e["origin"] == "revert"]
    assert revert_entries, "cell_history 必须能挖到 revert 流水 rows_added 里的格子"
    assert revert_entries[0]["before"] is None, "格子诞生：before=None"
    assert revert_entries[0]["after"] == "内容"


def test_cell_history_finds_cells_erased_via_revert_rows_removed(store, hist, table):
    """覆盖 rows_removed 分支：加一行再回退撤销它——对这条 revert 流水而言，
    该行在 head 侧（回退前，即刚加完）存在、在 target 侧（回退后）不存在，
    格子消失：after=None。"""
    good = hist.head_seq(table["id"])
    row = store.add_knowhow_row(
        table["id"], {table["anchor"]: "A", table["plain"]: "内容"}
    )

    hist.revert_to(table["id"], good, hist.head_seq(table["id"]))

    entries = hist.cell_history(table["id"], row, table["plain"])
    revert_entries = [e for e in entries if e["origin"] == "revert"]
    assert revert_entries, "cell_history 必须能挖到 revert 流水 rows_removed 里的格子"
    assert revert_entries[0]["before"] == "内容"
    assert revert_entries[0]["after"] is None, "格子消失：after=None"


def test_revert_of_a_revert_with_row_and_column_both_removed_and_overlapping(
    store, hist, table
):
    """回归测试：一次回退同时把一整行和一整列都撤销掉，且这行恰好在这列上
    有格子（行列都在 head 侧存在、target 侧不存在，即都进 _revert_payload
    的 rows_removed/columns_removed，且重叠格子只出现在 columns_removed 那
    一侧——见 _apply_revert_before 的排除规则）。

    这条覆盖 _apply_revert_before 重建顺序：必须先重建行、再重建列，否则
    columns_removed 里那个引用了"同批被移除的行"的格子会在行还不存在时
    写入，触发 FOREIGN KEY constraint failed（曾经在实现时写反过顺序，
    被一个手写的压力脚本当场炸出来）。"""
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "R"})
    store.update_knowhow_cell(row, table["plain"], "R-on-plain")
    good = hist.head_seq(table["id"])

    row2 = store.add_knowhow_row(table["id"], {table["anchor"]: "R2"})
    col2 = store.add_knowhow_column(table["id"], "C2", "attribute")
    store.update_knowhow_cell(row2, col2, "R2-on-C2")
    store.update_knowhow_cell(row, col2, "R-on-C2")
    store.update_knowhow_cell(row2, table["plain"], "R2-on-plain")
    head = hist.head_seq(table["id"])

    result = hist.revert_to(table["id"], good, head)
    detail = store.get_knowhow_table(table["id"])
    assert [c["id"] for c in detail["columns"]] == [table["anchor"], table["plain"]]
    assert [r["id"] for r in detail["rows"]] == [row]

    # 回退的回退：不应该在重建行/列的顺序上炸 FOREIGN KEY。
    hist.revert_to(table["id"], head, hist.head_seq(table["id"]))

    detail = store.get_knowhow_table(table["id"])
    by_row = {r["id"]: r["cells"] for r in detail["rows"]}
    assert set(by_row) == {row, row2}
    assert by_row[row2][col2] == "R2-on-C2"
    assert by_row[row][col2] == "R-on-C2"
    assert by_row[row2][table["plain"]] == "R2-on-plain"

    revert_change = hist.get_change(table["id"], result["seq"])
    payload = revert_change["payload"]
    assert [r["row_id"] for r in payload["rows_removed"]] == [row2]
    assert [c["column"]["id"] for c in payload["columns_removed"]] == [col2]
    # 重叠格子 (row2, col2) 只在 columns_removed 一侧（"完整"侧），
    # rows_removed 一侧（"部分"侧）里被排除。
    assert col2 not in payload["rows_removed"][0]["cells"]
    col2_cells = {c["row_id"]: c["content_md"] for c in payload["columns_removed"][0]["cells"]}
    assert col2_cells[row2] == "R2-on-C2"
