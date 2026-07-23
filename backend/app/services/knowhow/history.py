"""Knowhow 表版本管理 Task 11：两版对比的纯函数聚合 + 回退编排。

设计见 docs/superpowers/specs/2026-07-22-knowhow-table-version-control-design.md
§6.5（两版对比）与 §6.1 第 7 步（回退提交后必须触发重投影）。

本模块只有两个函数：
- ``aggregate_diff``：纯函数，把 ``KnowhowHistoryStore.changes_between`` 返回
  的一段流水（seq 升序）折叠成一次净变化，不碰数据库、不接受 repo。
- ``revert_table``：薄编排，调 store 的 ``revert_to``（经 facade 的
  ``revert_knowhow_table``）之后触发重投影调度，不做别的（错误码映射是
  Task 12 HTTP 层的事——这里任何异常都原样往上抛）。
"""
from __future__ import annotations

from typing import Any

_COLUMN_SNAPSHOT_FIELDS = ("name", "role", "position")


def aggregate_diff(changes: list[dict]) -> dict:
    """把区间 ``(A, B]`` 的流水（``changes_between`` 的输出，seq 升序）折叠成
    一次净变化。纯函数，只读入参，不碰数据库、不接受 repo（spec §6.5）。

    折叠规则：

    - **格子**：每个 ``(row_id, column_id)`` 取区间内**第一条的 before** 与
      **最后一条的 after**；两者相等（改了又改回去）则不出现在结果里。
    - **行增删**：``dict[row_id] -> "added" | "removed"`` 抵消——同一行在
      区间内先增后删、或先删后增，两边都不出现（不比较行内容，见 Task 11
      brief：无条件抵消）。``row_add``/``import_append`` 同记一次"新增"
      （两者 payload 形状相同，spec §4.4）。
    - **列**：``column_add``/``column_delete`` 用同一套抵消字典处理列的
      整体生死；``column_rename``/``column_kind``/``anchor_set`` 按字段
      （``name``/``role``）折叠，规则与格子相同（首 before/末 after）。
      一列若在区间内彻底"生而复死"（或反过来），无论期间被改名/改类型
      多少次，整条列目都不出现——两种判据（生死抵消、字段无净变化）分别
      判定，互不依赖。
    - **表元**：``table_meta`` 取首 ``before`` 与末 ``after``；相等则整个
      键为 ``None``。
    - **revert**：它自己也是一条普通流水，payload 里嵌套着那次回退实际
      改动的 ``cells``/``rows_removed``/``rows_added``/``columns_removed``/
      ``columns_added``/``columns_changed``/``table_meta``（形状分别与上
      面各 kind 一致，见 ``KnowhowHistoryStore._revert_payload``）——原样
      拆开揉进同一套折叠逻辑，跨越一次回退的两版对比才不会静默丢内容。

    输出固定 5 个键：``cells``/``rows_added``/``rows_removed``/``columns``/
    ``table_meta``。代码附件（``cell_code_put``/``cell_code_delete``）与
    建表创世流水（``table_create``）不在这个纯函数的输出范围内——接口只
    定义了这 5 个键，且两者都不影响它们（``table_create`` 只会出现在
    ``from_seq=0`` 的区间左端，是这个视图特性的已知边界，不是本任务范围）。
    """
    cell_acc: dict[tuple[str, str], list] = {}
    row_state: dict[str, dict] = {}
    # 行的双端点存在性（对称于下面列的 column_existed_before/exists_after）：
    # 专用于过滤幽灵格子（codex 第 2 轮 P2）。row_state 的抵消（add↔delete 都
    # 删）分不清「先增后删（两端点都不存在）」与「先删后增（两端点都存在）」，
    # 而格子该不该出现在净 diff 里恰恰取决于此——只有其行/列两端点都不存在时
    # 才是应剔除的幽灵格子。
    row_existed_before: dict[str, bool] = {}
    row_exists_after: dict[str, bool] = {}
    column_existed_before: dict[str, bool] = {}
    column_exists_after: dict[str, bool] = {}
    column_before_fields: dict[str, dict] = {}
    column_after_fields: dict[str, dict] = {}
    table_meta_before: "dict | None" = None
    table_meta_after: "dict | None" = None
    table_meta_seen = False

    def apply_cell(row_id: str, column_id: str, before: Any, after: Any) -> None:
        key = (row_id, column_id)
        if key not in cell_acc:
            cell_acc[key] = [before, after]
        else:
            cell_acc[key][1] = after

    def apply_row(row_id: str, row: dict, status: str) -> None:
        # 双端点存在性追踪（不影响下面 row_state 的抵消逻辑，只供格子过滤用）。
        if status == "added":
            row_existed_before.setdefault(row_id, False)
            row_exists_after[row_id] = True
        else:  # removed
            row_existed_before.setdefault(row_id, True)
            row_exists_after[row_id] = False
        existing = row_state.get(row_id)
        if existing is not None and existing["status"] != status:
            del row_state[row_id]  # 先增后删/先删后增，两边都清掉（无条件抵消）
        else:
            row_state[row_id] = {"status": status, "row": row}

    def note_field(column_id: str, field: str, before: Any, after: Any) -> None:
        before_fields = column_before_fields.setdefault(column_id, {})
        after_fields = column_after_fields.setdefault(column_id, {})
        if field not in before_fields:
            before_fields[field] = before  # 只留区间内第一次记到的 before
        after_fields[field] = after  # 每次都覆盖，留最后一次的 after

    def apply_column_added(column_id: str, column: dict) -> None:
        column_existed_before.setdefault(column_id, False)
        column_exists_after[column_id] = True
        for field in _COLUMN_SNAPSHOT_FIELDS:
            note_field(column_id, field, None, column.get(field))

    def apply_column_removed(column_id: str, column: dict) -> None:
        column_existed_before.setdefault(column_id, True)
        column_exists_after[column_id] = False
        for field in _COLUMN_SNAPSHOT_FIELDS:
            note_field(column_id, field, column.get(field), None)

    def apply_column_field(column_id: str, field: str, before: Any, after: Any) -> None:
        column_existed_before.setdefault(column_id, True)
        column_exists_after[column_id] = True
        note_field(column_id, field, before, after)

    def apply_table_meta(before: "dict | None", after: "dict | None") -> None:
        nonlocal table_meta_before, table_meta_after, table_meta_seen
        if not table_meta_seen:
            table_meta_before = before
            table_meta_seen = True
        table_meta_after = after

    for change in changes:
        kind = change.get("kind")
        payload = change.get("payload") or {}

        if kind == "cell_update":
            for cell in payload.get("cells", []):
                apply_cell(
                    cell["row_id"], cell["column_id"], cell.get("before"), cell.get("after")
                )
        elif kind in ("row_add", "import_append"):
            for row in payload.get("rows", []):
                apply_row(row["row_id"], row, "added")
        elif kind == "row_delete":
            for row in payload.get("rows", []):
                apply_row(row["row_id"], row, "removed")
        elif kind == "column_add":
            column = payload.get("column") or {}
            apply_column_added(column.get("id"), column)
        elif kind == "column_delete":
            column = payload.get("column") or {}
            apply_column_removed(column.get("id"), column)
        elif kind == "column_rename":
            apply_column_field(
                payload["column_id"], "name", payload.get("before"), payload.get("after")
            )
        elif kind == "column_kind":
            apply_column_field(
                payload["column_id"], "role", payload.get("before"), payload.get("after")
            )
        elif kind == "anchor_set":
            for entry in payload.get("columns", []):
                apply_column_field(
                    entry["column_id"], "role", entry.get("before"), entry.get("after")
                )
        elif kind == "table_meta":
            apply_table_meta(payload.get("before"), payload.get("after"))
        elif kind == "revert":
            for cell in payload.get("cells", []):
                apply_cell(
                    cell["row_id"], cell["column_id"], cell.get("before"), cell.get("after")
                )
            for row in payload.get("rows_removed", []):
                apply_row(row["row_id"], row, "removed")
            for row in payload.get("rows_added", []):
                apply_row(row["row_id"], row, "added")
            for entry in payload.get("columns_removed", []):
                apply_column_removed(entry["column"]["id"], entry["column"])
            for entry in payload.get("columns_added", []):
                apply_column_added(entry["column"]["id"], entry["column"])
            for entry in payload.get("columns_changed", []):
                before_partial = entry.get("before") or {}
                after_partial = entry.get("after") or {}
                for field in set(before_partial) | set(after_partial):
                    apply_column_field(
                        entry["column_id"], field,
                        before_partial.get(field), after_partial.get(field),
                    )
            if "table_meta" in payload:
                apply_table_meta(
                    payload["table_meta"].get("before"), payload["table_meta"].get("after")
                )
        # 其余 kind（table_create/cell_code_put/cell_code_delete）不在本函数
        # 的输出范围内，见函数 docstring 末段。

    def _net_cancelled(existed_before: dict, exists_after: dict, key: str) -> bool:
        """key（行 id 或列 id）在区间内被增删过、且两端点都不存在 → 它承载的
        格子是幽灵格子（净 diff 里不该出现其行/列都不在的格子）。只有被追踪过
        （出现在 existed_before）才判定；从没动过增删的行/列两端点都在，不剔。"""
        return (
            key in existed_before
            and not existed_before[key]
            and not exists_after.get(key, True)
        )

    cells_out = [
        {"row_id": row_id, "column_id": column_id, "before": before, "after": after}
        for (row_id, column_id), (before, after) in sorted(cell_acc.items())
        if before != after
        and not _net_cancelled(row_existed_before, row_exists_after, row_id)
        and not _net_cancelled(column_existed_before, column_exists_after, column_id)
    ]
    rows_added_out = [
        state["row"] for row_id, state in sorted(row_state.items())
        if state["status"] == "added"
    ]
    rows_removed_out = [
        state["row"] for row_id, state in sorted(row_state.items())
        if state["status"] == "removed"
    ]

    columns_out = []
    for column_id in sorted(column_before_fields):
        existed_before = column_existed_before.get(column_id, True)
        exists_after = column_exists_after.get(column_id, True)
        if not existed_before and not exists_after:
            continue  # 区间内"生而复死"（或反过来）：两侧都不出现
        before_fields = column_before_fields[column_id]
        after_fields = column_after_fields[column_id]
        changed = {
            field: value for field, value in before_fields.items()
            if after_fields.get(field) != value
        }
        if not changed:
            continue  # 净变化为空（例如改名又改回去）
        columns_out.append({
            "column_id": column_id,
            "before": {field: before_fields[field] for field in changed},
            "after": {field: after_fields[field] for field in changed},
        })

    table_meta_out = None
    if table_meta_seen and table_meta_before != table_meta_after:
        table_meta_out = {"before": table_meta_before, "after": table_meta_after}

    return {
        "cells": cells_out,
        "rows_added": rows_added_out,
        "rows_removed": rows_removed_out,
        "columns": columns_out,
        "table_meta": table_meta_out,
    }


def revert_table(
    repo: Any, notebook_id: str, table_id: str,
    target_seq: int, expected_head_seq: int, actor: str,
) -> dict:
    """回退 + 触发重投影。

    重投影走 knowhow_api.get_scheduler(repo).schedule(table_id) —— 与
    POST .../reproject 逃生口完全同一个入口。project_table 本身永远是
    全量确定性重投影，不存在需要绕开的增量路径；而绕过 ProjectionScheduler
    会丢掉 per-table 防抖与单飞，和同表并发编辑打架。
    """
    from app.services.knowhow import api as knowhow_api

    result = repo.revert_knowhow_table(table_id, target_seq, expected_head_seq, actor)
    knowhow_api.get_scheduler(repo).schedule(table_id)
    return result
