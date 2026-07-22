"""Task 11：服务层——diff 聚合（纯函数）+ 回退编排。

前 5 条测试逐字取自任务 brief（Step 1）。其后补几条 ``aggregate_diff`` 对
列结构变更 / ``revert`` 流水解构的覆盖——brief 的算法描述只点了格子折叠与
行增删抵消，但接口输出形状（``columns``）与设计文档 §6.5"行/列增删取净
集合"同样覆盖列，且一张表一旦被回退过，``changes_between`` 的区间就可能
跨过一条 ``kind == "revert"`` 的流水，不补测会让这部分静默失真也发现不了。

最后一节覆盖 ``revert_table`` 的编排职责本身：它必须在回退成功后触发
``knowhow_api.get_scheduler(repo).schedule(table_id)``——这条链路断了，回退
完 KG 还停在旧内容上，用户看不出来。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.knowhow.history import aggregate_diff, revert_table
from app.services.sqlite_repository import SQLiteRepository


# ---------------------------------------------------------------------------
# aggregate_diff —— brief Step 1，逐字照抄。
# ---------------------------------------------------------------------------


def test_aggregate_folds_repeated_cell_edits_into_one_net_change():
    changes = [
        {"kind": "cell_update", "payload": {"cells": [
            {"row_id": "r1", "column_id": "c1", "before": "A", "after": "B"}]}},
        {"kind": "cell_update", "payload": {"cells": [
            {"row_id": "r1", "column_id": "c1", "before": "B", "after": "C"}]}},
    ]
    result = aggregate_diff(changes)
    assert result["cells"] == [
        {"row_id": "r1", "column_id": "c1", "before": "A", "after": "C"}
    ]


def test_aggregate_drops_cells_that_ended_where_they_started():
    changes = [
        {"kind": "cell_update", "payload": {"cells": [
            {"row_id": "r1", "column_id": "c1", "before": "A", "after": "B"}]}},
        {"kind": "cell_update", "payload": {"cells": [
            {"row_id": "r1", "column_id": "c1", "before": "B", "after": "A"}]}},
    ]
    assert aggregate_diff(changes)["cells"] == []


def test_row_added_then_deleted_cancels_out():
    changes = [
        {"kind": "row_add", "payload": {"rows": [{"row_id": "r9", "position": 0,
                                                  "cells": {}, "code": []}]}},
        {"kind": "row_delete", "payload": {"rows": [{"row_id": "r9", "position": 0,
                                                     "cells": {}, "code": []}]}},
    ]
    result = aggregate_diff(changes)
    assert result["rows_added"] == []
    assert result["rows_removed"] == []


def test_row_deleted_then_restored_cancels_out():
    changes = [
        {"kind": "row_delete", "payload": {"rows": [{"row_id": "r1", "position": 0,
                                                     "cells": {}, "code": []}]}},
        {"kind": "row_add", "payload": {"rows": [{"row_id": "r1", "position": 0,
                                                  "cells": {}, "code": []}]}},
    ]
    result = aggregate_diff(changes)
    assert result["rows_added"] == []
    assert result["rows_removed"] == []


def test_table_meta_takes_first_before_and_last_after():
    changes = [
        {"kind": "table_meta", "payload": {"before": {"title": "一", "description": ""},
                                           "after": {"title": "二", "description": ""}}},
        {"kind": "table_meta", "payload": {"before": {"title": "二", "description": ""},
                                           "after": {"title": "三", "description": ""}}},
    ]
    meta = aggregate_diff(changes)["table_meta"]
    assert meta["before"]["title"] == "一"
    assert meta["after"]["title"] == "三"


# ---------------------------------------------------------------------------
# aggregate_diff —— 补充覆盖：列结构变更（§6.5"行/列增删取净集合"同样覆盖列）
# 与 revert 流水的解构（不补测就没人知道这部分是不是悄悄丢内容）。
# ---------------------------------------------------------------------------


def test_column_added_then_deleted_cancels_out():
    changes = [
        {"kind": "column_add", "payload": {"column": {
            "id": "c9", "name": "新列", "role": "attribute", "position": 3}}},
        {"kind": "column_delete", "payload": {
            "column": {"id": "c9", "name": "新列", "role": "attribute", "position": 3},
            "cells": [], "code": []}},
    ]
    assert aggregate_diff(changes)["columns"] == []


def test_column_rename_folds_to_first_before_and_last_after():
    changes = [
        {"kind": "column_rename", "payload": {
            "column_id": "c1", "before": "旧", "after": "中"}},
        {"kind": "column_rename", "payload": {
            "column_id": "c1", "before": "中", "after": "新"}},
    ]
    assert aggregate_diff(changes)["columns"] == [
        {"column_id": "c1", "before": {"name": "旧"}, "after": {"name": "新"}}
    ]


def test_column_kind_change_that_ends_where_it_started_is_dropped():
    changes = [
        {"kind": "column_kind", "payload": {
            "column_id": "c1", "before": "attribute", "after": "entity"}},
        {"kind": "column_kind", "payload": {
            "column_id": "c1", "before": "entity", "after": "attribute"}},
    ]
    assert aggregate_diff(changes)["columns"] == []


def test_anchor_set_folds_both_columns_role_transitions():
    changes = [
        {"kind": "anchor_set", "payload": {"columns": [
            {"column_id": "old_anchor", "before": "anchor", "after": "attribute"},
            {"column_id": "new_anchor", "before": "attribute", "after": "anchor"},
        ]}},
    ]
    columns = {c["column_id"]: c for c in aggregate_diff(changes)["columns"]}
    assert columns["old_anchor"] == {
        "column_id": "old_anchor", "before": {"role": "anchor"}, "after": {"role": "attribute"},
    }
    assert columns["new_anchor"] == {
        "column_id": "new_anchor", "before": {"role": "attribute"}, "after": {"role": "anchor"},
    }


def test_revert_kind_decomposes_cells_rows_and_table_meta():
    changes = [
        {"kind": "revert", "payload": {
            "target_seq": 3,
            "cells": [{"row_id": "r1", "column_id": "c1", "before": "X", "after": "Y"}],
            "rows_removed": [{"row_id": "r9", "position": 0, "created_at": "t",
                               "cells": {}, "code": []}],
            "table_meta": {"before": {"title": "旧标题", "description": ""},
                           "after": {"title": "新标题", "description": ""}},
        }},
    ]
    result = aggregate_diff(changes)
    assert result["cells"] == [
        {"row_id": "r1", "column_id": "c1", "before": "X", "after": "Y"}
    ]
    assert result["rows_removed"] == [
        {"row_id": "r9", "position": 0, "created_at": "t", "cells": {}, "code": []}
    ]
    assert result["rows_added"] == []
    assert result["table_meta"] == {
        "before": {"title": "旧标题", "description": ""},
        "after": {"title": "新标题", "description": ""},
    }


def test_revert_kind_columns_changed_folds_into_columns_output():
    changes = [
        {"kind": "revert", "payload": {
            "target_seq": 3, "cells": [],
            "columns_changed": [{
                "column_id": "c1",
                "before": {"role": "attribute"}, "after": {"role": "entity"},
            }],
        }},
    ]
    assert aggregate_diff(changes)["columns"] == [
        {"column_id": "c1", "before": {"role": "attribute"}, "after": {"role": "entity"}}
    ]


def test_column_deleted_then_revert_restored_with_same_fields_shows_no_change():
    """删列 → 回退把它建回来（columns_added，字段与删除时逐字相同）：净变化
    为空——列在区间两端都存在且属性未变，不该出现在 diff 里。"""
    same_column = {"id": "c1", "name": "N", "role": "attribute", "position": 1}
    changes = [
        {"kind": "column_delete", "payload": {
            "column": dict(same_column), "cells": [], "code": []}},
        {"kind": "revert", "payload": {
            "target_seq": 1, "cells": [],
            "columns_added": [{
                "column": dict(same_column), "cells": [], "code": [],
            }],
        }},
    ]
    assert aggregate_diff(changes)["columns"] == []


# ---------------------------------------------------------------------------
# revert_table —— 编排必须真的触发重投影调度，否则回退完 KG 还停在旧内容上。
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path}/knowhow.db",
        storage_dir=str(tmp_path / "storage"),
    ))


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id


def test_revert_table_schedules_reprojection_after_a_successful_revert(repo, notebook_id):
    table_id = repo.create_knowhow_table(
        notebook_id, "表", "",
        [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
    )
    columns = repo.get_knowhow_table(table_id)["columns"]
    anchor_id, plain_id = columns[0]["id"], columns[1]["id"]
    row_id = repo.add_knowhow_row(table_id, {anchor_id: "A"})
    repo.update_knowhow_cell(row_id, plain_id, "第一版")
    good_seq = repo.knowhow_history_head_seq(table_id)
    repo.update_knowhow_cell(row_id, plain_id, "第二版")
    head_seq = repo.knowhow_history_head_seq(table_id)

    fake_scheduler = MagicMock()
    with patch(
        "app.services.knowhow.api.get_scheduler", return_value=fake_scheduler
    ) as get_scheduler:
        result = revert_table(
            repo, notebook_id, table_id,
            target_seq=good_seq, expected_head_seq=head_seq, actor="tester",
        )

    get_scheduler.assert_called_once_with(repo)
    fake_scheduler.schedule.assert_called_once_with(table_id)
    assert result["seq"] == head_seq + 1
    # 打桩只拦了投影调度；store 侧的回退必须真的发生了，不是被短路掉。
    assert repo.get_knowhow_table(table_id)["rows"][0]["cells"][plain_id] == "第一版"


def test_revert_table_does_not_schedule_when_the_store_call_raises(repo, notebook_id):
    """反过来钉死：回退本身失败（陈旧 head）时绝不能仍然触发重投影。"""
    from app.repositories.sqlite.knowhow_history_store import HistoryStale

    table_id = repo.create_knowhow_table(
        notebook_id, "表", "", [{"name": "概念", "role": "anchor"}],
    )
    row_id = repo.add_knowhow_row(table_id, {})
    good_seq = repo.knowhow_history_head_seq(table_id)
    repo.update_knowhow_cell(
        row_id, repo.get_knowhow_table(table_id)["columns"][0]["id"], "别人刚改的",
    )

    fake_scheduler = MagicMock()
    with patch(
        "app.services.knowhow.api.get_scheduler", return_value=fake_scheduler
    ):
        with pytest.raises(HistoryStale):
            revert_table(
                repo, notebook_id, table_id,
                target_seq=good_seq, expected_head_seq=good_seq, actor="tester",
            )

    fake_scheduler.schedule.assert_not_called()
