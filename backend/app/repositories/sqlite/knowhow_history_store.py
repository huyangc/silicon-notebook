"""knowhow 表版本管理的持久化层：变更流水 + 命名里程碑 + 回退重放。

设计见 docs/superpowers/specs/2026-07-22-knowhow-table-version-control-design.md。

record_change 刻意做成**模块级函数而非类方法**：它必须在 KnowhowStore
已经打开的写事务里执行（流水与变更本体同生共死），做成类就要在组合根里
接线并让 KnowhowStore 持有引用。模块级函数零状态、零接线，把 new_id/now
当参数传进来即可。自带事务的操作（查询/里程碑/prune/回退）才归下面的类。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from app.repositories.sqlite import knowhow_fingerprint
from app.repositories.sqlite.database import SqliteDatabase


def record_change(
    db: sqlite3.Connection,
    *,
    new_id: Callable[[str], str],
    now: Callable[[], str],
    table_id: str,
    kind: str,
    payload: dict,
    actor: str = "",
    origin: str = "user",
    note: str = "",
) -> int:
    """在调用方已开的写事务里追加一条流水，返回它的 seq。

    **必须是写事务的最后一步** —— fingerprint 要反映本次变更之后的表状态。
    放在变更 DML 之前会记下变更前的指纹，让回退的前后置守卫全部失准。

    seq 用 ``COALESCE(MAX(seq),0)+1`` 现算：调用方已持写锁，同一张表上不会
    有第二个写事务同时算，UNIQUE(table_id, seq) 是最后一道保险。
    """
    row = db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM knowhow_changes WHERE table_id = ?",
        (table_id,),
    ).fetchone()
    seq = int(row["next"])
    db.execute(
        "INSERT INTO knowhow_changes "
        "(id, table_id, seq, kind, actor, origin, payload_json, fingerprint, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("khchg"),
            table_id,
            seq,
            kind,
            actor or "",
            origin or "user",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            knowhow_fingerprint.fingerprint_on(db, table_id) or "",
            note or "",
            now(),
        ),
    )
    return seq


def _row_to_change(row: sqlite3.Row) -> dict:
    change = dict(row)
    change["payload"] = json.loads(change.pop("payload_json"))
    return change


class KnowhowHistoryStore:
    """流水/里程碑的读侧与自带事务的写侧。"""

    def __init__(
        self,
        database: SqliteDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], str],
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now

    def head_seq(self, table_id: str) -> int:
        """当前最新流水序号；没有历史时返回 0。"""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS head FROM knowhow_changes WHERE table_id = ?",
                (table_id,),
            ).fetchone()
        return int(row["head"])

    def list_changes(
        self, table_id: str, limit: int = 50, before_seq: "int | None" = None
    ) -> list[dict]:
        """时间线：seq 倒序。``before_seq`` 用于向更旧翻页（严格小于）。"""
        sql = "SELECT * FROM knowhow_changes WHERE table_id = ?"
        params: list[Any] = [table_id]
        if before_seq is not None:
            sql += " AND seq < ?"
            params.append(int(before_seq))
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(int(limit))
        with self.database.connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [_row_to_change(row) for row in rows]

    def get_change(self, table_id: str, seq: int) -> "dict | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM knowhow_changes WHERE table_id = ? AND seq = ?",
                (table_id, int(seq)),
            ).fetchone()
        return _row_to_change(row) if row is not None else None

    def changes_between(self, table_id: str, from_seq: int, to_seq: int) -> list[dict]:
        """区间 (from_seq, to_seq] 的流水，seq 升序（供 diff 聚合按时序折叠）。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM knowhow_changes WHERE table_id = ? AND seq > ? AND seq <= ? "
                "ORDER BY seq ASC",
                (table_id, int(from_seq), int(to_seq)),
            ).fetchall()
        return [_row_to_change(row) for row in rows]

    def cell_history(
        self, table_id: str, row_id: str, column_id: str, limit: int = 50
    ) -> list[dict]:
        """一个格子的历次值，最新在前。

        先用 LIKE 把候选缩小到"payload 里提到过这个 row_id"的流水（索引不了
        JSON，但一张表的流水规模在百到千条量级，且 kind 过滤已挡掉结构类），
        再在 Python 侧精确匹配 (row_id, column_id) —— 合并格批量写把多个格子
        放在同一条流水的 cells 数组里，只看第一个会漏。
        """
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM knowhow_changes "
                "WHERE table_id = ? AND kind IN ('cell_update','import_append',"
                "'row_add','row_delete','column_delete','revert') "
                "AND payload_json LIKE ? "
                "ORDER BY seq DESC",
                (table_id, f"%{row_id}%"),
            ).fetchall()

        entries: list[dict] = []
        for row in rows:
            change = _row_to_change(row)
            for cell in change["payload"].get("cells", []):
                if cell.get("row_id") == row_id and cell.get("column_id") == column_id:
                    entries.append({
                        "seq": change["seq"],
                        "actor": change["actor"],
                        "origin": change["origin"],
                        "created_at": change["created_at"],
                        "before": cell.get("before"),
                        "after": cell.get("after"),
                    })
            if len(entries) >= limit:
                break
        return entries[:limit]
