from __future__ import annotations

import json

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


# ---------------------------------------------------------------------------
# Task 8 修复轮（评审发现的问题）回归测试。
#
# 问题 1（P0）：_revert_payload 的行/列定义必须取 head/target 侧的真实状态，
# 不能从"事件那一刻"折叠——折叠会把区间内的过渡态误当成 head/target 真值
# 写进 payload，多级回退时后置指纹校验失败（甚至更隐蔽：静默写坏历史，
# 只有下一次"回退的回退"才会暴露）。三条测试分别覆盖三种触发这个折叠 bug
# 的操作：改名、改内容类型、anchor 移动（含"提升到区间内新建的列"这个更刁
# 钻的子场景）。
# ---------------------------------------------------------------------------


def test_revert_of_a_revert_across_a_column_rename_uses_head_side_name(store, hist, notebook_id):
    """精确复现评审报告的失败序列：建列"旧" → 改名"新" → 回退 → 再回退。

    旧实现（事件折叠）在第三级回退时抛 ``RevertVerifyFailed``：第一次回退
    产生的 revert 流水（下面的 seq4）里，``columns_removed[0].column.name``
    被错误地折叠成列被创建时的名字"旧"，而不是 head 侧（seq3 时）真实的
    "新"；第二次回退（回退的回退）拿这个错误定义重建列，指纹自然对不上。
    新实现直接比较两侧真实快照，不存在这个折叠步骤。
    """
    table_id = store.create_knowhow_table(
        notebook_id, "表", "", [{"name": "概念", "role": "anchor"}]
    )
    col = store.add_knowhow_column(table_id, "旧", "attribute")  # seq2
    store.rename_knowhow_column(col, "新")  # seq3
    head3 = hist.head_seq(table_id)
    assert head3 == 3

    result4 = hist.revert_to(table_id, 1, head3)  # -> seq4：回到列还不存在的那一刻
    head4 = hist.head_seq(table_id)
    remaining = [c["id"] for c in store.get_knowhow_table(table_id)["columns"]]
    assert col not in remaining, '"旧"/"新"那一列必须已被回退删除，只剩建表自带的锚列'

    # payload 必须存 head（seq3）侧的真实定义"新"，不是创建时的"旧"。
    revert4 = hist.get_change(table_id, result4["seq"])
    assert revert4["payload"]["columns_removed"][0]["column"]["name"] == "新", (
        "revert 流水的 columns_removed 必须存 head 侧真实列名，不是事件折叠出的创建时名字"
    )

    hist.revert_to(table_id, 3, head4)  # 三级回退：回退的回退，重建该列

    names = {c["id"]: c["name"] for c in store.get_knowhow_table(table_id)["columns"]}
    assert names[col] == "新", "回退的回退必须恢复 head 侧真实列名"


def test_revert_of_a_revert_across_a_column_kind_change_uses_head_side_role(store, hist, notebook_id):
    """同一类折叠 bug 的第二种触发方式：``set_knowhow_column_kind``。"""
    table_id = store.create_knowhow_table(
        notebook_id, "表", "", [{"name": "概念", "role": "anchor"}]
    )
    col = store.add_knowhow_column(table_id, "字段", "attribute")  # seq2
    store.set_knowhow_column_kind(col, "entity")  # seq3
    head3 = hist.head_seq(table_id)

    result4 = hist.revert_to(table_id, 1, head3)  # -> seq4
    head4 = hist.head_seq(table_id)

    revert4 = hist.get_change(table_id, result4["seq"])
    assert revert4["payload"]["columns_removed"][0]["column"]["role"] == "entity", (
        "columns_removed 必须存 head 侧真实 role（entity），不是创建时的 attribute"
    )

    hist.revert_to(table_id, 3, head4)  # 三级回退

    roles = {c["id"]: c["role"] for c in store.get_knowhow_table(table_id)["columns"]}
    assert roles[col] == "entity"


def test_revert_of_a_revert_across_an_anchor_promotion_of_a_newly_added_column(
    store, hist, notebook_id
):
    """第三种触发方式——评审实测会炸的场景：anchor 提升到区间内新建的列。

    B 列在区间内先被创建（role=attribute），随后被提升为 anchor（同时把原
    anchor 列 A 降级为 attribute）。两级回退（先回到 B 还不存在的那一刻，
    再回退回来）必须让 A/B 的 role 都精确复原——旧实现的"首次/末次遇到"折叠
    对"先诞生、又被另一种事件（anchor_set）改动"的列会算错。
    """
    table_id = store.create_knowhow_table(
        notebook_id, "表", "", [{"name": "概念", "role": "anchor"}]
    )
    anchor_a = store.get_knowhow_table(table_id)["columns"][0]["id"]
    before_add = hist.head_seq(table_id)  # seq1：B 还不存在，A 是 anchor

    col_b = store.add_knowhow_column(table_id, "B", "attribute")  # seq2
    store.set_knowhow_anchor_column(table_id, col_b)  # seq3：B 提升为 anchor，A 降级
    head3 = hist.head_seq(table_id)

    hist.revert_to(table_id, before_add, head3)  # -> seq4：回到 B 还不存在的那一刻
    head4 = hist.head_seq(table_id)
    detail = store.get_knowhow_table(table_id)
    assert [c["id"] for c in detail["columns"]] == [anchor_a]
    assert detail["columns"][0]["role"] == "anchor"

    hist.revert_to(table_id, head3, head4)  # 三级回退：重建 B，且 B 必须带着 anchor 角色

    detail = store.get_knowhow_table(table_id)
    by_id = {c["id"]: c for c in detail["columns"]}
    assert by_id[anchor_a]["role"] == "attribute", "A 必须仍是降级后的 attribute，不是折叠误判的原值"
    assert by_id[col_b]["role"] == "anchor", "B 重建时必须带着 anchor 角色，不是它创建时的 attribute"


# ---------------------------------------------------------------------------
# 问题 2（P1）：后置指纹守卫此前零测试覆盖（既有的
# test_out_of_band_edit_is_detected_and_refused 测的是前置守卫）。这里直接
# 改坏一条已落库流水的 payload（模拟"流水被损坏/被手工编辑"），跨越它回退，
# 断言后置守卫拦截且整表分毫不变。
# ---------------------------------------------------------------------------


def test_corrupted_payload_before_value_fails_post_replay_verification_and_leaves_table_untouched(
    repo, store, hist, table
):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "第一版")
    good = hist.head_seq(table["id"])
    store.update_knowhow_cell(row, table["plain"], "第二版")
    corrupt_seq = hist.head_seq(table["id"])
    store.update_knowhow_cell(row, table["plain"], "第三版")
    head = hist.head_seq(table["id"])

    # 绕过 store 直接改一条已落库流水的 payload——模拟流水被损坏/被手工编辑。
    with repo._runtime.database.write() as db:
        row_data = db.execute(
            "SELECT payload_json FROM knowhow_changes WHERE table_id = ? AND seq = ?",
            (table["id"], corrupt_seq),
        ).fetchone()
        payload = json.loads(row_data["payload_json"])
        payload["cells"][0]["before"] = "被篡改的值"
        db.execute(
            "UPDATE knowhow_changes SET payload_json = ? WHERE table_id = ? AND seq = ?",
            (json.dumps(payload), table["id"], corrupt_seq),
        )

    before_fp = _fp(repo, table["id"])
    with pytest.raises(RevertVerifyFailed):
        hist.revert_to(table["id"], good, head)

    assert _fp(repo, table["id"]) == before_fp, "后置校验失败必须整事务回滚，表分毫不变"
    assert store.get_knowhow_table(table["id"])["rows"][0]["cells"][table["plain"]] == "第三版", (
        "回滚之后必须还是回退前的最新内容"
    )


# ---------------------------------------------------------------------------
# 问题 3（P1）：None（格子不存在）与 ""（空串）必须被 _write_cell 严格区分
# ——三态往返（不存在 → "" → 真实内容）逐点回退都要准确，且"不存在"那一点
# 上 knowhow_cells 里真的不能有这一行（不是巧合地读出空字符串）。
# ---------------------------------------------------------------------------


def test_cell_none_empty_and_content_round_trip_through_revert(repo, store, hist, table):
    def _cell_row_exists(row_id, column_id):
        with repo._runtime.database.connect() as db:
            return db.execute(
                "SELECT 1 FROM knowhow_cells WHERE row_id = ? AND column_id = ?",
                (row_id, column_id),
            ).fetchone() is not None

    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    absent_seq = hist.head_seq(table["id"])
    absent_fp = _fp(repo, table["id"])
    assert not _cell_row_exists(row, table["plain"]), "刚建行时这个格子还没写过，不该有行"

    store.update_knowhow_cell(row, table["plain"], "")
    empty_seq = hist.head_seq(table["id"])
    empty_fp = _fp(repo, table["id"])
    assert _cell_row_exists(row, table["plain"]), "写过空串之后格子行必须存在（写了值，只是值是空串）"

    store.update_knowhow_cell(row, table["plain"], "真实内容")
    content_seq = hist.head_seq(table["id"])
    content_fp = _fp(repo, table["id"])

    # 回退到"空串"
    hist.revert_to(table["id"], empty_seq, hist.head_seq(table["id"]))
    assert store.get_knowhow_table(table["id"])["rows"][0]["cells"][table["plain"]] == ""
    assert _fp(repo, table["id"]) == empty_fp
    assert _cell_row_exists(row, table["plain"]), "空串状态下格子行必须存在"

    # 回退到"不存在"
    hist.revert_to(table["id"], absent_seq, hist.head_seq(table["id"]))
    detail = store.get_knowhow_table(table["id"])
    assert table["plain"] not in detail["rows"][0]["cells"], (
        "格子不存在时不能出现在 cells 字典里（get_knowhow_table 的稀疏格约定）"
    )
    assert _fp(repo, table["id"]) == absent_fp
    assert not _cell_row_exists(row, table["plain"]), (
        "回退到「不存在」那一点时，knowhow_cells 里不该有这一行"
    )

    # 再回到"真实内容"那一点（content_seq 是固定 seq 号，不受中途两次 revert
    # 追加的新流水影响；expected_head 用当前 head，即上一次回退产生的流水）。
    hist.revert_to(table_id := table["id"], content_seq, hist.head_seq(table_id))
    assert store.get_knowhow_table(table["id"])["rows"][0]["cells"][table["plain"]] == "真实内容"
    assert _fp(repo, table["id"]) == content_fp
    assert _cell_row_exists(row, table["plain"])


# ---------------------------------------------------------------------------
# 问题 4（P2）：前置指纹守卫必须排在 head==target 早退之前——否则一张被
# 越权改脏的表做"回退到当前点"（target_seq == expected_head_seq == head）
# 会绕过守卫直接返回成功。
# ---------------------------------------------------------------------------


def test_out_of_band_edit_is_detected_even_when_target_equals_head(repo, store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "正常内容")
    head = hist.head_seq(table["id"])

    # 绕过 store 直接改库——模拟"某条写路径漏挂钩"。
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE knowhow_cells SET content_md = '偷偷改的' "
            "WHERE row_id = ? AND column_id = ?",
            (row, table["plain"]),
        )

    with pytest.raises(HistoryInconsistent):
        # target_seq == expected_head_seq == head：早退分支必须先过前置守卫，
        # 不能让"回退到当前点"绕过它静默放行。
        hist.revert_to(table["id"], head, head)

    assert store.get_knowhow_table(table["id"])["rows"][0]["cells"][table["plain"]] == "偷偷改的", (
        "拒绝回退时必须什么都不改"
    )


# ---------------------------------------------------------------------------
# 问题 5（P3）：row_delete 的 payload 必须存原始 created_at，_rebuild_row
# 必须照原样恢复——不能让"回退"把行的创建时间悄悄改写成重建那一刻的
# now()。指纹不覆盖 created_at（spec §4.3），直接改库不会触碰前后置守卫；
# 用一个 now() 绝不可能产生的旧时间戳，避免 _now() 只有秒精度导致"重建
# 时间"与"原始创建时间"在同一次测试运行内巧合相等、让测试失去鉴别力。
# ---------------------------------------------------------------------------


def test_revert_rebuilds_a_deleted_row_with_its_original_created_at(repo, store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE knowhow_rows SET created_at = ? WHERE id = ?",
            ("2001-01-01T00:00:00", row),
        )
    good = hist.head_seq(table["id"])

    store.delete_knowhow_row(row)
    hist.revert_to(table["id"], good, hist.head_seq(table["id"]))

    rebuilt = store.get_knowhow_table(table["id"])["rows"][0]
    assert rebuilt["id"] == row
    assert rebuilt["created_at"] == "2001-01-01T00:00:00", (
        "回退重建的行必须保留真实的创建时间，不是重建那一刻的 now()"
    )
