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

    seq 用 ``COALESCE(MAX(seq),0)+1`` 现算：``SqliteDatabase.write()`` 靠
    ``write_lock``（``threading.RLock``）把并发写者串行化，但那把锁**只互斥
    本进程内**的写者——同一进程里不会有第二个写事务同时算这张表的 seq。
    离线 CLI 与后端进程共享同一个库文件时（§7.6），跨进程互斥不成立：
    ``write()`` 块内首条 DML 之前的 SELECT 不会自动开启 SQLite 事务（除非
    调用方显式 ``begin_immediate()``），另一进程可能在本进程"读到 next
    seq"与"真正写入"之间插入一条相同 seq 的流水。此时 ``UNIQUE(table_id,
    seq)`` 是唯一的安全网：后提交的一方会因唯一约束直接失败（可重试），
    而不是静默产生重复 seq。
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


def _code_fields(entry: dict) -> dict:
    """从任意携带代码附件的字典（cell_code_put/delete 的 before/after，或
    row_delete/column_delete/revert 里嵌的 code 列表条目）抽出规范的四字段
    形状——后两者的条目还夹带 ``row_id``/``column_id`` 定位键，这里只留
    ``_write_cell_code`` 真正需要写回的四个值字段。"""
    return {
        "code_text": entry["code_text"],
        "language": entry.get("language", ""),
        "updated_by": entry.get("updated_by", ""),
        "cell_content_hash": entry["cell_content_hash"],
    }


class HistoryStale(Exception):
    """调用方看到的 head 已经不是当前 head（有人在这期间改过表）。"""


class HistoryInconsistent(Exception):
    """当前表内容与流水链对不上——有写路径漏挂钩，或有人直接改过库。"""


class RevertVerifyFailed(Exception):
    """逆序重放跑完，但结果指纹不等于目标点的指纹。已回滚。"""


def _cell_entries_in_change(change: dict, row_id: str, column_id: str) -> list[dict]:
    """从一条流水的 payload 里抽出这个 (row_id, column_id) 的历次值。

    §4.4 里 6 种携带格子内容的 kind 用了 3 种不同的 payload 形状，不能只读
    顶层 ``cells`` 列表：

    - ``cell_update`` / ``revert``：顶层 ``cells`` 是列表，条目本身就是
      ``{row_id, column_id, before, after}``，直接匹配。
    - ``row_add`` / ``import_append``：顶层是 ``rows`` 列表，格子内容嵌在
      ``rows[i]['cells']`` —— 一个 ``{column_id: content_md}`` 字典，不是
      顶层 cells。格子诞生：``before=None``。
    - ``row_delete``：同 ``row_add`` 形状（整行快照），但语义相反——格子
      消失：``after=None``。
    - ``column_delete``：顶层 ``cells`` 列表存在，但条目形状是
      ``{row_id, content_md}``，没有 ``column_id`` —— 整个 payload 只对应
      ``payload['column']`` 这一列，必须先核对列 id 相符，否则会把任意
      列的删除历史错配给别的列。格子消失：``after=None``。
    - ``revert``：顶层 ``cells`` 走上面 ``cell_update`` 同款分支；但回退时
      重建/删除的整行整列另外嵌在 ``rows_added``/``rows_removed``（row_add
      形状，语义同 row_add/row_delete）与 ``columns_added``/
      ``columns_removed``（column_delete 形状，语义同 column_delete）里，
      不查这四个字段会漏掉"回退顺带重建/删除的格子"这类历史条目。
    """
    kind = change["kind"]
    payload = change["payload"]
    values: list[tuple[Any, Any]] = []

    if kind in ("cell_update", "revert"):
        for cell in payload.get("cells", []):
            if cell.get("row_id") == row_id and cell.get("column_id") == column_id:
                values.append((cell.get("before"), cell.get("after")))

    if kind in ("row_add", "import_append"):
        for entry in payload.get("rows", []):
            if entry.get("row_id") != row_id:
                continue
            cells = entry.get("cells") or {}
            if column_id in cells:
                values.append((None, cells[column_id]))
    elif kind == "row_delete":
        for entry in payload.get("rows", []):
            if entry.get("row_id") != row_id:
                continue
            cells = entry.get("cells") or {}
            if column_id in cells:
                values.append((cells[column_id], None))
    elif kind == "column_delete":
        column = payload.get("column") or {}
        if column.get("id") == column_id:
            for cell in payload.get("cells", []):
                if cell.get("row_id") == row_id:
                    values.append((cell.get("content_md"), None))
    elif kind == "revert":
        # rows_added 形状同 row_add（诞生：before=None）；rows_removed 形状
        # 同 row_delete（消失：after=None）——见 KnowhowHistoryStore._revert_
        # payload 的方向约定注释。
        for entry in payload.get("rows_added", []):
            if entry.get("row_id") != row_id:
                continue
            cells = entry.get("cells") or {}
            if column_id in cells:
                values.append((None, cells[column_id]))
        for entry in payload.get("rows_removed", []):
            if entry.get("row_id") != row_id:
                continue
            cells = entry.get("cells") or {}
            if column_id in cells:
                values.append((cells[column_id], None))
        # columns_added/columns_removed 形状同 column_delete：条目是
        # {row_id, content_md}，必须先核对 column id，道理同上面 column_delete
        # 分支。
        for col_entry in payload.get("columns_added", []) or []:
            column = col_entry.get("column") or {}
            if column.get("id") == column_id:
                for cell in col_entry.get("cells", []):
                    if cell.get("row_id") == row_id:
                        values.append((None, cell.get("content_md")))
        for col_entry in payload.get("columns_removed", []) or []:
            column = col_entry.get("column") or {}
            if column.get("id") == column_id:
                for cell in col_entry.get("cells", []):
                    if cell.get("row_id") == row_id:
                        values.append((cell.get("content_md"), None))

    return [
        {
            "seq": change["seq"],
            "actor": change["actor"],
            "origin": change["origin"],
            "created_at": change["created_at"],
            "before": before,
            "after": after,
        }
        for before, after in values
    ]


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
            entries.extend(_cell_entries_in_change(change, row_id, column_id))
            if len(entries) >= limit:
                break
        return entries[:limit]

    # ------------------------------------------------------------- revert
    def revert_to(
        self, table_id: str, target_seq: int, expected_head_seq: int, actor: str = ""
    ) -> dict:
        """把整张表逆序重放回 ``target_seq`` 那一刻。见 spec §6.1。

        前置/后置两道指纹守卫是这个方法的核心：delta 重放的正确性不能靠
        "看起来对"，必须被独立判据证明。任一守卫不过就中止（前置）或
        整事务回滚（后置），绝不留下半改的表。
        """
        with self.database.write() as db:
            db.execute("BEGIN IMMEDIATE")

            head_row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS head FROM knowhow_changes WHERE table_id = ?",
                (table_id,),
            ).fetchone()
            head = int(head_row["head"])
            if head != int(expected_head_seq):
                raise HistoryStale(f"head={head} expected={expected_head_seq}")

            target = db.execute(
                "SELECT seq, fingerprint FROM knowhow_changes WHERE table_id = ? AND seq = ?",
                (table_id, int(target_seq)),
            ).fetchone()
            if target is None:
                raise KeyError(target_seq)
            if head == int(target_seq):
                return {"seq": head, "target_seq": int(target_seq)}  # 已经在目标点

            head_change = db.execute(
                "SELECT fingerprint FROM knowhow_changes WHERE table_id = ? AND seq = ?",
                (table_id, head),
            ).fetchone()
            current = knowhow_fingerprint.fingerprint_on(db, table_id)
            if current != head_change["fingerprint"]:
                raise HistoryInconsistent(table_id)

            rows = db.execute(
                "SELECT * FROM knowhow_changes WHERE table_id = ? AND seq > ? "
                "ORDER BY seq DESC",
                (table_id, int(target_seq)),
            ).fetchall()

            undone: list[dict] = []
            for row in rows:
                change = _row_to_change(row)
                self._apply_before(db, table_id, change)
                undone.append(change)

            after = knowhow_fingerprint.fingerprint_on(db, table_id)
            if after != target["fingerprint"]:
                raise RevertVerifyFailed(
                    f"table={table_id} target={target_seq} got={after} want={target['fingerprint']}"
                )

            db.execute(
                "UPDATE knowhow_rows SET projection_status = 'pending' WHERE table_id = ?",
                (table_id,),
            )
            db.execute(
                "UPDATE knowhow_tables SET mutation_seq = mutation_seq + 1, updated_at = ? "
                "WHERE id = ?",
                (self.now(), table_id),
            )
            seq = record_change(
                db, new_id=self.new_id, now=self.now, table_id=table_id,
                kind="revert",
                payload=self._revert_payload(int(target_seq), undone),
                actor=actor, origin="revert",
                note=f"回退到 #{int(target_seq)}",
            )
        return {"seq": seq, "target_seq": int(target_seq)}

    def _apply_before(self, db, table_id: str, change: dict) -> None:
        """把 ``change`` 的 ``before`` 写回表——逆序重放的一步。按 kind 分派
        （spec §6.1 的逆操作表）。"""
        kind = change["kind"]
        payload = change["payload"]
        now = self.now()

        if kind in ("cell_update",):
            for cell in payload.get("cells", []):
                self._write_cell(db, cell["row_id"], cell["column_id"], cell["before"], now)

        elif kind in ("row_add", "import_append"):
            for row in payload.get("rows", []):
                db.execute("DELETE FROM knowhow_rows WHERE id = ?", (row["row_id"],))

        elif kind == "row_delete":
            for row in payload.get("rows", []):
                self._rebuild_row(db, table_id, row, now)

        elif kind == "column_add":
            db.execute(
                "DELETE FROM knowhow_columns WHERE id = ?", (payload["column"]["id"],)
            )

        elif kind == "column_delete":
            self._rebuild_column(db, table_id, payload, now)

        elif kind in ("column_rename",):
            db.execute(
                "UPDATE knowhow_columns SET name = ? WHERE id = ?",
                (payload["before"], payload["column_id"]),
            )

        elif kind == "column_kind":
            db.execute(
                "UPDATE knowhow_columns SET role = ? WHERE id = ?",
                (payload["before"], payload["column_id"]),
            )

        elif kind == "anchor_set":
            for entry in payload.get("columns", []):
                db.execute(
                    "UPDATE knowhow_columns SET role = ? WHERE id = ?",
                    (entry["before"], entry["column_id"]),
                )

        elif kind == "table_meta":
            db.execute(
                "UPDATE knowhow_tables SET title = ?, description = ?, updated_at = ? "
                "WHERE id = ?",
                (payload["before"]["title"], payload["before"]["description"], now, table_id),
            )

        elif kind in ("cell_code_put", "cell_code_delete"):
            self._write_cell_code(
                db, payload["row_id"], payload["column_id"], payload["before"], now
            )

        elif kind == "revert":
            self._apply_revert_before(db, table_id, payload, now)

        elif kind == "table_create":
            raise RevertVerifyFailed("不能跨越建表流水")

        else:
            raise RevertVerifyFailed(f"未知的变更类型：{kind}")

    def _apply_revert_before(self, db, table_id: str, payload: dict, now: str) -> None:
        """撤销一条 ``kind='revert'`` 流水本身——即"回退的回退"。把表从这条
        revert 的 ``after``（target 侧）状态写回它的 ``before``（head 侧）
        状态，用的是 ``_revert_payload`` 汇总出的那份净变化。

        行必须先于列重建（这条顺序被一个"行列同时被移除且恰好有重叠格子"
        的真实场景实测踩过一次——最初写反过，行→列互换会在第二次回退时
        直接 FOREIGN KEY constraint failed，已用脚本复现并改正）：
        ``_revert_payload`` 把重叠格子的内容只放进 ``columns_removed``
        （"完整"一侧，包含全部行，含同批被移除的行）而不放进
        ``rows_removed``（"部分"一侧，为此排除了同批被移除的列，见该方法
        文档字符串）。所以：重建行时它引用的列必然是稳定列（一直都在，
        重叠列已被排除），不需要列先存在；重建列时它的格子可能引用同批被
        移除的行，必须那些行已经存在——先行后列，两个方向都不会碰到还不
        存在的外键目标。"""
        table_meta = payload.get("table_meta")
        if table_meta:
            db.execute(
                "UPDATE knowhow_tables SET title = ?, description = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    table_meta["before"]["title"], table_meta["before"]["description"],
                    now, table_id,
                ),
            )

        for entry in payload.get("columns_changed", []):
            before = entry["before"]
            sets: list[str] = []
            params: list[Any] = []
            if "name" in before:
                sets.append("name = ?")
                params.append(before["name"])
            if "role" in before:
                sets.append("role = ?")
                params.append(before["role"])
            if sets:
                params.append(entry["column_id"])
                db.execute(
                    f"UPDATE knowhow_columns SET {', '.join(sets)} WHERE id = ?", params
                )

        for row in payload.get("rows_added", []):
            db.execute("DELETE FROM knowhow_rows WHERE id = ?", (row["row_id"],))

        for col in payload.get("columns_added", []):
            db.execute("DELETE FROM knowhow_columns WHERE id = ?", (col["column"]["id"],))

        # 行先于列：见本方法文档字符串。
        for row in payload.get("rows_removed", []):
            self._rebuild_row(db, table_id, row, now)

        for col in payload.get("columns_removed", []):
            self._rebuild_column(db, table_id, col, now)

        for cell in payload.get("cells", []):
            self._write_cell(db, cell["row_id"], cell["column_id"], cell["before"], now)

        for code in payload.get("code", []):
            self._write_cell_code(db, code["row_id"], code["column_id"], code["before"], now)

    def _write_cell(self, db, row_id: str, column_id: str, content: "str | None", now: str) -> None:
        """写回一个格子的历史值。``content is None`` 表示那个格子当时不存在
        （与空串严格区分）——删掉它；否则 UPSERT。新写入用新 id（格子的真
        实身份是 ``(row_id, column_id)`` 这个 UNIQUE 对，不是这个代理 id）。"""
        if content is None:
            db.execute(
                "DELETE FROM knowhow_cells WHERE row_id = ? AND column_id = ?",
                (row_id, column_id),
            )
            return
        db.execute(
            "INSERT INTO knowhow_cells (id, row_id, column_id, content_md, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(row_id, column_id) DO UPDATE SET "
            "content_md = excluded.content_md, updated_at = excluded.updated_at",
            (self.new_id("khcel"), row_id, column_id, content, now),
        )

    def _write_cell_code(
        self, db, row_id: str, column_id: str, before: "dict | None", now: str
    ) -> None:
        """写回一个代码附件的历史值。``before is None`` 表示当时没有附件——
        删掉它；否则 UPSERT 全部四个值字段（指纹覆盖它们，缺一都会让后置
        校验失败）。"""
        if before is None:
            db.execute(
                "DELETE FROM knowhow_cell_code WHERE row_id = ? AND column_id = ?",
                (row_id, column_id),
            )
            return
        db.execute(
            "INSERT INTO knowhow_cell_code "
            "(id, row_id, column_id, code_text, language, updated_by, "
            " cell_content_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(row_id, column_id) DO UPDATE SET "
            "code_text = excluded.code_text, language = excluded.language, "
            "updated_by = excluded.updated_by, "
            "cell_content_hash = excluded.cell_content_hash, "
            "updated_at = excluded.updated_at",
            (
                self.new_id("khcode"), row_id, column_id,
                before["code_text"], before.get("language", ""), before.get("updated_by", ""),
                before["cell_content_hash"], now, now,
            ),
        )

    def _rebuild_row(self, db, table_id: str, row: dict, now: str) -> None:
        """用 payload 里存的 ``row_id``/``position`` 原样重建一整行，及它当
        时的全部格子与代码附件。id 绝不能换新——引用跳转与代码附件都挂在
        它上面（spec §6.2）。"""
        db.execute(
            "INSERT INTO knowhow_rows "
            "(id, table_id, position, projection_status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (row["row_id"], table_id, row["position"], now, now),
        )
        for column_id, content_md in (row.get("cells") or {}).items():
            self._write_cell(db, row["row_id"], column_id, content_md, now)
        for code in row.get("code") or []:
            self._write_cell_code(db, row["row_id"], code["column_id"], _code_fields(code), now)

    def _rebuild_column(self, db, table_id: str, payload: dict, now: str) -> None:
        """用 payload 里存的 ``column['id']``/``name``/``role``/``position``
        原样重建一整列，及该列当时在每一行上的格子与代码附件。id 绝不能
        换新（同 ``_rebuild_row``，spec §6.2）。"""
        column = payload["column"]
        db.execute(
            "INSERT INTO knowhow_columns (id, table_id, name, role, position) "
            "VALUES (?, ?, ?, ?, ?)",
            (column["id"], table_id, column["name"], column["role"], column["position"]),
        )
        for cell in payload.get("cells", []):
            self._write_cell(db, cell["row_id"], column["id"], cell["content_md"], now)
        for code in payload.get("code", []):
            self._write_cell_code(db, code["row_id"], column["id"], _code_fields(code), now)

    def _revert_payload(self, target_seq: int, undone: list[dict]) -> dict:
        """把 ``undone``（head→target 途中已应用的原始流水，seq 降序）汇总
        成这条 revert 自己的 payload（spec §4.4 的 ``revert`` 形状）。

        **方向约定**：这条 revert 流水的 ``before`` = 回退前（head）状态，
        ``after`` = 回退后（target）状态——与 ``undone`` 里每条原始流水自己
        的 before/after 相反：原始流水的 ``after`` 才是"回退前"的值（它就
        是被回退掉的那个状态），原始流水的 ``before`` 才是"回退后"要抵达
        的值。所以按实体（格子/代码/行/列/表元）折叠时要把每条原始触碰的
        before/after **对调**后再取值。

        折叠规则（沿 ``undone`` 的降序自然顺序——降序意味着先遇到的离 head
        更近）：对每个被触碰的实体键，**首次遇到**记录该次触碰的 ``after``
        作为 head 侧取值（只记一次，不覆盖）；**每次遇到**都用该次触碰的
        ``before`` 覆盖 target 侧取值（覆盖到底 = 最后一次遇到 = ``undone``
        里最小 seq 的那次，也就是离 target 最近的一次）。这与 §6.5"两版
        对比"的净变化算法同构（那里是升序区间取首 before/末 after），只是
        遍历方向相反，故取值的首尾也相反。

        行/列的"新建/删除"净判定同理，但判的是存在性而非值：一个 row_id
        在 ``undone`` 里可能出现多次（例如先被删、又被一条嵌套 revert
        重建）；只看**首次遇到**触碰的类型（诞生/消失）决定它在 head 侧是
        否存在，只看**最后一次遇到**触碰的类型决定它在 target 侧是否存
        在。四种组合：head 有/target 无 → 本次 revert 把它删了
        （``rows_removed``，需要 head 侧全量内容以便"回退的回退"能重建）；
        head 无/target 有 → 本次 revert 把它建回来了（``rows_added``，需要
        target 侧全量内容）；head/target 都有 → 稳定，只把它的格子净变化
        计入顶层 ``cells``；都没有 → 在区间内"生而复死"或"死而复生"，双端
        都不存在，对本次 revert 而言完全不可见（不出现在任何字段里；这一
        点会由 before==after 的等值判据自然过滤，无需另外判定"不可见"）。

        嵌套 ``revert``（"回退的回退"再被回退）按同一套语义递归展开：它自
        己的 ``rows_removed``/``columns_removed``（它撤销时靠重建复活）等
        价于 ``row_delete``/``column_delete``；它的 ``rows_added``/
        ``columns_added``（它撤销时靠删除消灭）等价于 ``row_add``/
        ``column_add``。

        行/列若同时被移除（都在 head 侧存在、都在 target 侧不存在），且
        某个被移除行恰好在某个被移除列上有格子——为了 ``_apply_revert_
        before`` 重建时不出现"列外键还不存在"的失败，这个重叠格子只放进
        ``columns_removed``（"完整"一侧，重建顺序里列先于行），从
        ``rows_removed``（"部分"一侧）里排除。"新建"方向（``rows_added``/
        ``columns_added``）只会被删除消费，不重建，没有这个顾虑，两侧都存
        全量。

        ``columns_changed`` 的 ``before``/``after`` 只携带真正被
        column_rename/column_kind/anchor_set 触碰过的字段（``name``/
        ``role`` 之一或两者都有）——不伪造未被触碰字段的值（比如
        ``position``：这三种操作都不会改它，纯 undone 折叠也拿不到它当时
        的真实值，宁可不写这个字段，也不编一个可能就是错的）。
        """
        cell_head: dict[tuple[str, str], Any] = {}
        cell_target: dict[tuple[str, str], Any] = {}
        code_head: dict[tuple[str, str], Any] = {}
        code_target: dict[tuple[str, str], Any] = {}
        row_first_birth: dict[str, bool] = {}
        row_last_death: dict[str, bool] = {}
        row_head_position: dict[str, int] = {}
        row_target_position: dict[str, int] = {}
        col_first_birth: dict[str, bool] = {}
        col_last_death: dict[str, bool] = {}
        col_head_def: dict[str, dict] = {}
        col_target_def: dict[str, dict] = {}
        name_head: dict[str, Any] = {}
        name_target: dict[str, Any] = {}
        role_head: dict[str, Any] = {}
        role_target: dict[str, Any] = {}
        table_meta_head: "dict | None" = None
        table_meta_target: "dict | None" = None

        def touch_cell(row_id: str, column_id: str, before: Any, after: Any) -> None:
            key = (row_id, column_id)
            if key not in cell_head:
                cell_head[key] = after
            cell_target[key] = before

        def touch_code(row_id: str, column_id: str, before: Any, after: Any) -> None:
            key = (row_id, column_id)
            if key not in code_head:
                code_head[key] = after
            code_target[key] = before

        def touch_row_birth(row_id: str, position: int, cells: "dict | None", code: "list | None") -> None:
            if row_id not in row_first_birth:
                row_first_birth[row_id] = True
                row_head_position[row_id] = position
            row_last_death[row_id] = False
            row_target_position[row_id] = position
            for column_id, content_md in (cells or {}).items():
                touch_cell(row_id, column_id, None, content_md)
            for entry in code or []:
                touch_code(row_id, entry["column_id"], None, _code_fields(entry))

        def touch_row_death(row_id: str, position: int, cells: "dict | None", code: "list | None") -> None:
            if row_id not in row_first_birth:
                row_first_birth[row_id] = False
                row_head_position[row_id] = position
            row_last_death[row_id] = True
            row_target_position[row_id] = position
            for column_id, content_md in (cells or {}).items():
                touch_cell(row_id, column_id, content_md, None)
            for entry in code or []:
                touch_code(row_id, entry["column_id"], _code_fields(entry), None)

        def touch_column_birth(column: dict) -> None:
            cid = column["id"]
            if cid not in col_first_birth:
                col_first_birth[cid] = True
                col_head_def[cid] = dict(column)
            col_last_death[cid] = False
            col_target_def[cid] = dict(column)

        def touch_column_death(column: dict, cells: "list | None", code: "list | None") -> None:
            cid = column["id"]
            if cid not in col_first_birth:
                col_first_birth[cid] = False
                col_head_def[cid] = dict(column)
            col_last_death[cid] = True
            col_target_def[cid] = dict(column)
            for cell in cells or []:
                touch_cell(cell["row_id"], cid, cell["content_md"], None)
            for entry in code or []:
                touch_code(entry["row_id"], cid, _code_fields(entry), None)

        def touch_column_field(column_id: str, field: str, before: Any, after: Any) -> None:
            head_map, target_map = (
                (name_head, name_target) if field == "name" else (role_head, role_target)
            )
            if column_id not in head_map:
                head_map[column_id] = after
            target_map[column_id] = before

        for change in undone:
            kind = change["kind"]
            payload = change["payload"]

            if kind == "cell_update":
                for cell in payload.get("cells", []):
                    touch_cell(cell["row_id"], cell["column_id"], cell.get("before"), cell.get("after"))
            elif kind in ("row_add", "import_append"):
                for row in payload.get("rows", []):
                    touch_row_birth(row["row_id"], row["position"], row.get("cells"), row.get("code"))
            elif kind == "row_delete":
                for row in payload.get("rows", []):
                    touch_row_death(row["row_id"], row["position"], row.get("cells"), row.get("code"))
            elif kind == "column_add":
                touch_column_birth(payload["column"])
            elif kind == "column_delete":
                touch_column_death(payload["column"], payload.get("cells"), payload.get("code"))
            elif kind == "column_rename":
                touch_column_field(payload["column_id"], "name", payload["before"], payload["after"])
            elif kind == "column_kind":
                touch_column_field(payload["column_id"], "role", payload["before"], payload["after"])
            elif kind == "anchor_set":
                for entry in payload.get("columns", []):
                    touch_column_field(entry["column_id"], "role", entry["before"], entry["after"])
            elif kind == "table_meta":
                if table_meta_head is None:
                    table_meta_head = payload["after"]
                table_meta_target = payload["before"]
            elif kind in ("cell_code_put", "cell_code_delete"):
                touch_code(payload["row_id"], payload["column_id"], payload.get("before"), payload.get("after"))
            elif kind == "revert":
                nested = payload
                for cell in nested.get("cells", []):
                    touch_cell(cell["row_id"], cell["column_id"], cell.get("before"), cell.get("after"))
                for row in nested.get("rows_added", []):
                    touch_row_birth(row["row_id"], row["position"], row.get("cells"), row.get("code"))
                for row in nested.get("rows_removed", []):
                    touch_row_death(row["row_id"], row["position"], row.get("cells"), row.get("code"))
                for col in nested.get("columns_added", []):
                    column = col["column"]
                    touch_column_birth(column)
                    for cell in col.get("cells", []):
                        touch_cell(cell["row_id"], column["id"], None, cell["content_md"])
                    for entry in col.get("code", []):
                        touch_code(entry["row_id"], column["id"], None, _code_fields(entry))
                for col in nested.get("columns_removed", []):
                    touch_column_death(col["column"], col.get("cells"), col.get("code"))
                for entry in nested.get("columns_changed", []):
                    if "name" in entry["before"] or "name" in entry["after"]:
                        touch_column_field(
                            entry["column_id"], "name",
                            entry["before"].get("name"), entry["after"].get("name"),
                        )
                    if "role" in entry["before"] or "role" in entry["after"]:
                        touch_column_field(
                            entry["column_id"], "role",
                            entry["before"].get("role"), entry["after"].get("role"),
                        )
                nested_tm = nested.get("table_meta")
                if nested_tm:
                    if table_meta_head is None:
                        table_meta_head = nested_tm["after"]
                    table_meta_target = nested_tm["before"]
                for code in nested.get("code", []):
                    touch_code(code["row_id"], code["column_id"], code.get("before"), code.get("after"))
            # kind == "table_create" 不会出现在 undone 里——revert_to 的
            # _apply_before 已经在遇到它时抢先抛异常，走不到这里。

        removed_row_ids = {
            rid for rid, born in row_first_birth.items()
            if born and not row_last_death.get(rid, False)
        }
        added_row_ids = {
            rid for rid, born in row_first_birth.items()
            if not born and row_last_death.get(rid, False)
        }
        removed_col_ids = {
            cid for cid, born in col_first_birth.items()
            if born and not col_last_death.get(cid, False)
        }
        added_col_ids = {
            cid for cid, born in col_first_birth.items()
            if not born and col_last_death.get(cid, False)
        }

        rows_removed = []
        for rid in removed_row_ids:
            cells = {
                col: value for (r, col), value in cell_head.items()
                if r == rid and value is not None and col not in removed_col_ids
            }
            code = [
                {"column_id": col, **value}
                for (r, col), value in code_head.items()
                if r == rid and value is not None and col not in removed_col_ids
            ]
            rows_removed.append({
                "row_id": rid, "position": row_head_position[rid],
                "cells": cells, "code": code,
            })

        rows_added = []
        for rid in added_row_ids:
            cells = {
                col: value for (r, col), value in cell_target.items()
                if r == rid and value is not None
            }
            code = [
                {"column_id": col, **value}
                for (r, col), value in code_target.items()
                if r == rid and value is not None
            ]
            rows_added.append({
                "row_id": rid, "position": row_target_position[rid],
                "cells": cells, "code": code,
            })

        columns_removed = []
        for cid in removed_col_ids:
            cells = [
                {"row_id": r, "content_md": value}
                for (r, col), value in cell_head.items()
                if col == cid and value is not None
            ]
            code = [
                {"row_id": r, **value}
                for (r, col), value in code_head.items()
                if col == cid and value is not None
            ]
            columns_removed.append({"column": col_head_def[cid], "cells": cells, "code": code})

        columns_added = []
        for cid in added_col_ids:
            cells = [
                {"row_id": r, "content_md": value}
                for (r, col), value in cell_target.items()
                if col == cid and value is not None
            ]
            code = [
                {"row_id": r, **value}
                for (r, col), value in code_target.items()
                if col == cid and value is not None
            ]
            columns_added.append({"column": col_target_def[cid], "cells": cells, "code": code})

        columns_changed = []
        for cid in (set(name_head) | set(role_head)) - removed_col_ids - added_col_ids:
            before: dict = {}
            after: dict = {}
            if cid in name_head and name_head[cid] != name_target.get(cid):
                before["name"] = name_head[cid]
                after["name"] = name_target[cid]
            if cid in role_head and role_head[cid] != role_target.get(cid):
                before["role"] = role_head[cid]
                after["role"] = role_target[cid]
            if before:
                columns_changed.append({"column_id": cid, "before": before, "after": after})

        cells = [
            {"row_id": r, "column_id": c, "before": head_value, "after": cell_target[(r, c)]}
            for (r, c), head_value in cell_head.items()
            if r not in removed_row_ids and r not in added_row_ids
            and c not in removed_col_ids and c not in added_col_ids
            and head_value != cell_target[(r, c)]
        ]
        code_list = [
            {"row_id": r, "column_id": c, "before": head_value, "after": code_target[(r, c)]}
            for (r, c), head_value in code_head.items()
            if r not in removed_row_ids and r not in added_row_ids
            and c not in removed_col_ids and c not in added_col_ids
            and head_value != code_target[(r, c)]
        ]

        payload: dict = {"target_seq": int(target_seq), "cells": cells}
        if rows_removed:
            payload["rows_removed"] = rows_removed
        if rows_added:
            payload["rows_added"] = rows_added
        if columns_removed:
            payload["columns_removed"] = columns_removed
        if columns_added:
            payload["columns_added"] = columns_added
        if columns_changed:
            payload["columns_changed"] = columns_changed
        if table_meta_head is not None and table_meta_head != table_meta_target:
            payload["table_meta"] = {"before": table_meta_head, "after": table_meta_target}
        if code_list:
            payload["code"] = code_list
        return payload
