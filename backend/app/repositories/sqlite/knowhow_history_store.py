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


def _rows_content(entries: "list[dict] | None") -> list:
    """``rows``-shaped entries (``row_add``/``import_append``/``row_delete``/
    ``table_create``'s own ``rows``, and ``revert``'s ``rows_added``/
    ``rows_removed``): each entry's ``cells`` dict values are content_md,
    each entry's ``code`` list is a row's remembered CODE ATTACHMENTS
    (CASCADE-doomed alongside the row) — deliberately never consulted here,
    see ``content_strings_in_payload``'s own docstring for why."""
    texts: list = []
    for entry in entries or []:
        texts.extend((entry.get("cells") or {}).values())
        # entry.get("code") intentionally never read — code_text, not content.
    return texts


def _column_cells_content(cells: "list[dict] | None") -> list:
    """``column_delete``-shaped ``cells`` entries (also ``revert``'s
    ``columns_added``/``columns_removed[].cells``): ``{row_id,
    content_md}`` — the sibling ``code`` list (if present on the same
    entry) is that column's remembered code attachments and is, again,
    deliberately never consulted."""
    texts: list = []
    for cell in cells or []:
        content = cell.get("content_md")
        if content is not None:
            texts.append(content)
    return texts


def content_strings_in_payload(kind: str, payload: dict) -> list:
    """Every ``content_md``-shaped string embedded ANYWHERE in one change's
    payload — the reference corpus ``SQLiteMaintenanceAdapter.
    sweep_orphan_assets`` (knowhow 表版本管理 Task 13, spec §7.1) searches
    for a surviving ``asset://<id>`` substring in HISTORY.

    Deliberately narrower than "the whole payload_json blob". Several kinds
    (``row_delete``/``column_delete``/``revert``) carry a row's or column's
    remembered CODE ATTACHMENT (``knowhow_cell_code``, CASCADE-doomed
    alongside the row/column it belonged to) in the SAME payload as its
    genuine, rendered ``content_md`` — a code attachment's ``code_text`` is
    source-code text, not rendered markdown, the exact same "not a keeper
    reference" boundary this module's sibling sweep has always drawn for a
    LIVE ``knowhow_cell_code`` row (migration 17). A prior implementation
    tried to draw this line at the ``kind`` level (blanket-excluding only
    ``cell_code_put``/``cell_code_delete`` and then LIKE-scanning every
    OTHER kind's ``payload_json`` in full) — that missed exactly this case:
    ``row_delete``'s own payload embeds BOTH the row's ``cells`` (genuine
    content) AND its ``code`` array (code attachments) side by side, so a
    code-only reference inside a DELETED row's payload kept its asset alive
    forever. This function instead dispatches on ``kind`` and pulls out only
    the fields that are actually ``content_md``-shaped, mirroring (but not
    scoped to one row/column, unlike) ``_cell_entries_in_change`` above's
    per-``kind`` payload-shape knowledge — a ``code``/``code_text`` field is
    NEVER read by any branch below, whichever kind it rides along in.

    Kinds with no content_md-shaped field at all (``column_add``,
    ``column_rename``, ``column_kind``, ``anchor_set``, ``table_meta``,
    ``cell_code_put``, ``cell_code_delete``) fall through every
    branch below and correctly yield ``[]`` — column/table names and roles are
    never scanned (asset refs only ever live in rendered cell markdown, never
    a column name or table title, and the pre-Task-13 implementation never
    scanned those either). Adding a new ``record_change`` ``kind`` that DOES
    carry cell content requires adding it here too — there is no generic
    fallback, by design: guessing "unknown kind, treat the whole payload as
    content" would silently reintroduce the very code_text-leaks-through-
    unrelated-payload bug this function exists to close, the moment that new
    kind ALSO happens to carry a ``code``/``code_text`` field alongside its
    content."""
    # Closed set of kinds with explicit routing — failure on unknown kind
    # catches runtime errors from future changes that add a new kind carrying
    # cell content but forget to register it here, which would silently leak
    # asset references and cause images to be incorrectly garbage-collected.
    # 与 spec §4.1 的 kind 枚举一一对应。⚠️ 别把 md_normalize.py 里 markdown
    # 分词器的 kind（"autolink" 等）扫进来——那是同名不同物，与变更流水无关。
    REGISTERED_KINDS = frozenset({
        "cell_update", "revert", "row_add", "import_append", "row_delete",
        "table_create", "column_delete", "column_add", "column_rename",
        "column_kind", "anchor_set", "table_meta", "cell_code_put",
        "cell_code_delete",
    })
    if kind not in REGISTERED_KINDS:
        raise ValueError(f"content_strings_in_payload: 未登记的 kind={kind!r}")

    texts: list = []

    if kind in ("cell_update", "revert"):
        for cell in payload.get("cells", []) or []:
            for side in ("before", "after"):
                value = cell.get(side)
                if value is not None:
                    texts.append(value)

    if kind in ("row_add", "import_append", "row_delete", "table_create"):
        texts.extend(_rows_content(payload.get("rows")))

    if kind == "column_delete":
        texts.extend(_column_cells_content(payload.get("cells")))

    if kind == "revert":
        texts.extend(_rows_content(payload.get("rows_added")))
        texts.extend(_rows_content(payload.get("rows_removed")))
        for entry in payload.get("columns_added", []) or []:
            texts.extend(_column_cells_content(entry.get("cells")))
        for entry in payload.get("columns_removed", []) or []:
            texts.extend(_column_cells_content(entry.get("cells")))
        # payload.get("code") — revert's OWN top-level code-attachment
        # before/after list (mirrors cell_code_put/delete's shape) — is
        # deliberately never consulted, same reason as every branch above.

    return texts


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

    # --------------------------------------------------------- milestones
    def create_milestone(
        self, table_id: str, seq: int, name: str, note: str, created_by: str
    ) -> dict:
        """给某个 ``seq`` 起名——零快照，只是一条指针记录（spec §4.2）。

        ``UNIQUE(table_id, name)`` 由建表 DDL 保证；重名直接让调用方吃
        ``sqlite3.IntegrityError``，这里不做预检省一次往返（也避免 TOCTOU：
        预检通过和 INSERT 之间另一个写者抢先用了同一个名字）。
        """
        milestone_id = self.new_id("khms")
        created_at = self.now()
        with self.database.write() as db:
            db.execute(
                "INSERT INTO knowhow_milestones "
                "(id, table_id, seq, name, note, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (milestone_id, table_id, int(seq), name, note or "", created_by or "", created_at),
            )
        return {
            "id": milestone_id,
            "table_id": table_id,
            "seq": int(seq),
            "name": name,
            "note": note or "",
            "created_by": created_by or "",
            "created_at": created_at,
        }

    def delete_milestone(self, table_id: str, milestone_id: str) -> None:
        """删掉一个里程碑（只是指针，不牵动任何流水）。已经不存在时静默
        no-op——与本文件其余 delete 方法（见 ``KnowhowStore.delete_knowhow_row``
        /``delete_knowhow_column`` 同款约定）保持一致。"""
        with self.database.write() as db:
            db.execute(
                "DELETE FROM knowhow_milestones WHERE table_id = ? AND id = ?",
                (table_id, milestone_id),
            )

    def list_milestones(self, table_id: str) -> list[dict]:
        """本表全部里程碑，最新（``seq`` 最大）在前。``stale`` = 它指向的
        ``seq`` 已经被 ``prune`` 删掉——``knowhow_milestones.seq`` 刻意不设
        FK（§4.2），LEFT JOIN 落空即失效，但行本身仍然保留，供前端灰显。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT m.*, (c.seq IS NULL) AS stale FROM knowhow_milestones m "
                "LEFT JOIN knowhow_changes c ON c.table_id = m.table_id AND c.seq = m.seq "
                "WHERE m.table_id = ? ORDER BY m.seq DESC",
                (table_id,),
            ).fetchall()
        return [{**dict(r), "stale": bool(r["stale"])} for r in rows]

    # -------------------------------------------------------------- prune
    def prune(self, table_id: str, before_iso: str) -> dict:
        """删掉最老的连续前缀。见 spec §7.7。

        为什么按 seq 而不是直接 ``DELETE WHERE created_at < ?``：反向重放
        要求流水链从 head 起连续，中间挖洞会让重放走到缺口就断，而前置
        指纹守卫看的是 head、**发现不了这个洞**。先用时间求出 cutoff_seq、
        再按 seq 删，即便时钟回拨导致 created_at 局部乱序，删的也一定是前缀。

        head 永远保留：前置指纹守卫拿它当参照，删了整表回退直接不可用。
        """
        with self.database.write() as db:
            head_row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS head FROM knowhow_changes WHERE table_id = ?",
                (table_id,),
            ).fetchone()
            head = int(head_row["head"])
            if head == 0:
                return {"removed": 0}
            cutoff_row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS cutoff FROM knowhow_changes "
                "WHERE table_id = ? AND created_at < ?",
                (table_id, before_iso),
            ).fetchone()
            cutoff = min(int(cutoff_row["cutoff"]), head - 1)
            if cutoff <= 0:
                return {"removed": 0}
            cursor = db.execute(
                "DELETE FROM knowhow_changes WHERE table_id = ? AND seq <= ?",
                (table_id, cutoff),
            )
        return {"removed": cursor.rowcount}

    # ------------------------------------------------------------- revert
    def revert_to(
        self, table_id: str, target_seq: int, expected_head_seq: int, actor: str = ""
    ) -> dict:
        """把整张表逆序重放回 ``target_seq`` 那一刻。见 spec §6.1。

        前置/后置两道指纹守卫是这个方法的核心：delta 重放的正确性不能靠
        "看起来对"，必须被独立判据证明。任一守卫不过就中止（前置）或
        整事务回滚（后置），绝不留下半改的表。

        检查顺序（Task 8 修复轮 P2）：陈旧校验 → 目标存在性 → **前置指纹
        守卫** → ``head == target`` 早退。前置守卫必须排在早退之前——早前
        的实现把早退放在守卫之前，一张被越权改脏的表做"回退到当前点"
        （``target_seq == expected_head_seq == head``）会绕过守卫直接返回
        成功，守卫形同虚设。早退分支本身是 no-op：不产生新流水，返回值里
        的 ``seq`` 就是"当前 head"，不是"新 revert 流水的 seq"（那要求
        确实发生了一次回退）——调用方不应把这个分支误读成"回退到了 head
        这条流水"。
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

            head_change = db.execute(
                "SELECT fingerprint FROM knowhow_changes WHERE table_id = ? AND seq = ?",
                (table_id, head),
            ).fetchone()
            current = knowhow_fingerprint.fingerprint_on(db, table_id)
            if current != head_change["fingerprint"]:
                raise HistoryInconsistent(table_id)

            if head == int(target_seq):
                return {"seq": head, "target_seq": int(target_seq)}  # no-op，见上面 docstring

            head_snapshot = self._snapshot(db, table_id)

            rows = db.execute(
                "SELECT * FROM knowhow_changes WHERE table_id = ? AND seq > ? "
                "ORDER BY seq DESC",
                (table_id, int(target_seq)),
            ).fetchall()

            for row in rows:
                change = _row_to_change(row)
                self._apply_before(db, table_id, change)

            after = knowhow_fingerprint.fingerprint_on(db, table_id)
            if after != target["fingerprint"]:
                raise RevertVerifyFailed(
                    f"table={table_id} target={target_seq} got={after} want={target['fingerprint']}"
                )

            target_snapshot = self._snapshot(db, table_id)

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
                payload=self._revert_payload(int(target_seq), head_snapshot, target_snapshot),
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
            if "position" in before:
                sets.append("position = ?")
                params.append(before["position"])
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
        """用 payload 里存的 ``row_id``/``position``/``created_at`` 原样重建
        一整行，及它当时的全部格子与代码附件。id 绝不能换新——引用跳转与
        代码附件都挂在它上面（spec §6.2）。

        ``created_at``（Task 8 修复轮 P3）：取 payload 里存的原始创建时间；
        缺失时退回 ``now``——本修复上线前写的 ``row_delete``/``revert``
        流水没有这个字段，重放到那些老流水时不该炸，静默退化成旧行为
        （写 ``now``）即可，指纹本来就不覆盖这一列。``updated_at`` 始终是
        这次重建发生的时间（不是历史值）。"""
        created_at = row.get("created_at") or now
        db.execute(
            "INSERT INTO knowhow_rows "
            "(id, table_id, position, projection_status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (row["row_id"], table_id, row["position"], created_at, now),
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

    def _snapshot(self, db, table_id: str) -> dict:
        """整表的结构化真值快照：列/行/格子/代码附件/表元。``revert_to`` 在
        重放前后各拍一次，``_revert_payload`` 靠**比较两次真实状态**（而不
        是折叠事件流水）算出这条 revert 流水该记什么（Task 8 修复轮 P0）。

        这是替换掉"事件折叠"旧实现的根本修法：旧实现从 ``undone`` 事件
        列表折叠推导行/列的定义，把"首次/末次遇到某个事件"当作 head/
        target 侧的真值来源——这个假设不成立。一列的定义（name/role）只在
        它自己那次改名/改类型事件发生的那一刻正确；如果同一个 (target,
        head] 区间里它后来又被改了一次，折叠出的"head 侧值"其实是中间某
        个过渡态，不是 head 真正的状态（这正是问题的原始复现：建列"旧"→
        改名"新"→回退→再回退，旧实现在 columns_added/columns_removed 里
        存的列名会是过渡态"旧"，而不是 head 侧真实的"新"，导致再次回退时
        后置指纹校验失败）。直接从数据库读两侧真实状态再做差，天然规避
        整类问题——不管中间发生了多少跳、多少种事件交替，读到的都是那
        一刻数据库里躺着的真值，无需对事件序列做任何"存在性折叠"的推导
        或穷举验证。

        代价是两次全表 SELECT（列/行/格子/代码/表元各一条），表规模在
        百行级、回退是低频操作，spec §3.2 已判定这个代价可接受。

        返回的 dict 键：
        - ``columns``: ``{column_id: {"name","role","position"}}``
        - ``rows``:    ``{row_id: {"position","created_at"}}``——
          ``created_at`` 供 ``rows_removed``/``rows_added`` 里的行在被
          "回退的回退"重建时原样恢复（Task 8 修复轮 P3，见 ``_rebuild_
          row``），不是重建那一刻的 ``now``。
        - ``cells``:   ``{(row_id, column_id): content_md}``
        - ``code``:    ``{(row_id, column_id): {code_text,language,
          updated_by,cell_content_hash}}``
        - ``table_meta``: ``{"title","description"}``
        """
        columns = {
            row["id"]: {
                "name": row["name"], "role": row["role"], "position": row["position"],
            }
            for row in db.execute(
                "SELECT id, name, role, position FROM knowhow_columns WHERE table_id = ?",
                (table_id,),
            ).fetchall()
        }
        rows = {
            row["id"]: {"position": row["position"], "created_at": row["created_at"]}
            for row in db.execute(
                "SELECT id, position, created_at FROM knowhow_rows WHERE table_id = ?",
                (table_id,),
            ).fetchall()
        }
        cells = {
            (row["row_id"], row["column_id"]): row["content_md"]
            for row in db.execute(
                "SELECT c.row_id AS row_id, c.column_id AS column_id, "
                "c.content_md AS content_md FROM knowhow_cells c "
                "JOIN knowhow_rows r ON r.id = c.row_id WHERE r.table_id = ?",
                (table_id,),
            ).fetchall()
        }
        code = {
            (row["row_id"], row["column_id"]): {
                "code_text": row["code_text"], "language": row["language"],
                "updated_by": row["updated_by"],
                "cell_content_hash": row["cell_content_hash"],
            }
            for row in db.execute(
                "SELECT cc.row_id AS row_id, cc.column_id AS column_id, "
                "cc.code_text AS code_text, cc.language AS language, "
                "cc.updated_by AS updated_by, cc.cell_content_hash AS cell_content_hash "
                "FROM knowhow_cell_code cc "
                "JOIN knowhow_rows r ON r.id = cc.row_id WHERE r.table_id = ?",
                (table_id,),
            ).fetchall()
        }
        table_row = db.execute(
            "SELECT title, description FROM knowhow_tables WHERE id = ?", (table_id,)
        ).fetchone()
        return {
            "columns": columns, "rows": rows, "cells": cells, "code": code,
            "table_meta": {
                "title": table_row["title"], "description": table_row["description"],
            },
        }

    def _revert_payload(self, target_seq: int, head: dict, target: dict) -> dict:
        """比较 ``head``（回退前，``_snapshot`` 在重放之前拍的）与
        ``target``（回退后，重放并通过后置指纹校验之后拍的）两份真实快照，
        算出这条 revert 自己的 payload（spec §4.4 的 ``revert`` 形状）。

        **方向约定**（不变）：这条 revert 流水的 ``before`` = 回退前
        （head）状态，``after`` = 回退后（target）状态。

        判定规则——直接比较两侧快照里"这个 id 在不在、值是否相同"，不再
        折叠事件序列：

        - 只在 head 出现 → 本次 revert 把它删了：行进 ``rows_removed``、
          列进 ``columns_removed``，定义与内容取 **head 侧**（供"回退的
          回退"按原样重建）。
        - 只在 target 出现 → 本次 revert 把它建回来了：行进
          ``rows_added``、列进 ``columns_added``，定义与内容取
          **target 侧**。
        - 两侧都出现（稳定实体）：列的 name/role/position 逐字段比较，
          任一不同就产出一条 ``columns_changed``——**这次三个字段都是两侧
          真实快照值**，不再像旧实现那样只能填被 column_rename/column_
          kind/anchor_set 事件"顺手触碰"到的字段（``position`` 从未被这
          三种操作改过，旧实现因此永远不填它；现在直接读快照，改了就能
          看见，没改就不出现，和 name/role 同等对待）；格子/代码内容不同
          则分别计入顶层 ``cells``/``code``。
        - 两侧都不出现（区间内"生而复死"或反过来）：对这条 revert 完全
          不可见——因为它压根不会被两侧任何一个快照收录，这一点是快照
          读法本身自然给出的，不需要专门再判一次"不可见"。

        行/列若同时被移除（都在 head、都不在 target），且某个被移除行
        恰好在某个被移除列上有格子——为了 ``_apply_revert_before`` 重建
        时不出现"列外键还不存在"的失败，这个重叠格子只放进
        ``columns_removed``（"完整"一侧，重建顺序里行先于列，见
        ``_apply_revert_before`` 的文档字符串），从 ``rows_removed``
        （"部分"一侧）里排除。"新建"方向（``rows_added``/``columns_added``）
        只会被删除消费、不重建，没有这个顾虑，两侧都存全量。

        嵌套 ``revert``（"回退的回退"再被回退）不需要任何特殊处理——它
        改过的行/列/格子在这次的 head/target 快照里就是最终真实状态，
        直接被上面的规则覆盖，不用像旧实现那样对 ``kind == "revert"``
        单独展开、也不用逐事件维护"首次/末次遇到"的状态机。这正是这次
        重写要根除的那一整类"多跳生死交替折叠未穷举验证"的顾虑——不是
        把折叠逻辑写得更细，而是让折叠这件事本身不再必要。

        为避免 Python 的 ``set``/``dict`` 遍历顺序受哈希随机化影响导致
        payload 里的列表顺序在不同进程间飘移，所有集合遍历都先 ``sorted``
        成确定顺序。
        """
        head_cols, target_cols = head["columns"], target["columns"]
        head_rows, target_rows = head["rows"], target["rows"]
        head_cells, target_cells = head["cells"], target["cells"]
        head_code, target_code = head["code"], target["code"]

        removed_row_ids = set(head_rows) - set(target_rows)
        added_row_ids = set(target_rows) - set(head_rows)
        stable_row_ids = set(head_rows) & set(target_rows)

        removed_col_ids = set(head_cols) - set(target_cols)
        added_col_ids = set(target_cols) - set(head_cols)
        stable_col_ids = set(head_cols) & set(target_cols)

        rows_removed = []
        for rid in sorted(removed_row_ids):
            cells = {
                col: value for (r, col), value in head_cells.items()
                if r == rid and col not in removed_col_ids
            }
            code = [
                {"column_id": col, **value}
                for (r, col), value in head_code.items()
                if r == rid and col not in removed_col_ids
            ]
            rows_removed.append({
                "row_id": rid, "position": head_rows[rid]["position"],
                "created_at": head_rows[rid]["created_at"],
                "cells": cells, "code": code,
            })

        rows_added = []
        for rid in sorted(added_row_ids):
            cells = {col: value for (r, col), value in target_cells.items() if r == rid}
            code = [
                {"column_id": col, **value}
                for (r, col), value in target_code.items()
                if r == rid
            ]
            rows_added.append({
                "row_id": rid, "position": target_rows[rid]["position"],
                "created_at": target_rows[rid]["created_at"],
                "cells": cells, "code": code,
            })

        columns_removed = []
        for cid in sorted(removed_col_ids):
            cells = [
                {"row_id": r, "content_md": value}
                for (r, col), value in head_cells.items()
                if col == cid
            ]
            code = [
                {"row_id": r, **value}
                for (r, col), value in head_code.items()
                if col == cid
            ]
            columns_removed.append({
                "column": {"id": cid, **head_cols[cid]}, "cells": cells, "code": code,
            })

        columns_added = []
        for cid in sorted(added_col_ids):
            cells = [
                {"row_id": r, "content_md": value}
                for (r, col), value in target_cells.items()
                if col == cid
            ]
            code = [
                {"row_id": r, **value}
                for (r, col), value in target_code.items()
                if col == cid
            ]
            columns_added.append({
                "column": {"id": cid, **target_cols[cid]}, "cells": cells, "code": code,
            })

        columns_changed = []
        for cid in sorted(stable_col_ids):
            hd, tg = head_cols[cid], target_cols[cid]
            if hd != tg:
                before = {k: v for k, v in hd.items() if v != tg.get(k)}
                after = {k: v for k, v in tg.items() if v != hd.get(k)}
                columns_changed.append({"column_id": cid, "before": before, "after": after})

        cell_keys = {
            key for key in set(head_cells) | set(target_cells)
            if key[0] in stable_row_ids and key[1] in stable_col_ids
        }
        cells = [
            {
                "row_id": r, "column_id": c,
                "before": head_cells.get((r, c)), "after": target_cells.get((r, c)),
            }
            for (r, c) in sorted(cell_keys)
            if head_cells.get((r, c)) != target_cells.get((r, c))
        ]

        code_keys = {
            key for key in set(head_code) | set(target_code)
            if key[0] in stable_row_ids and key[1] in stable_col_ids
        }
        code_list = [
            {
                "row_id": r, "column_id": c,
                "before": head_code.get((r, c)), "after": target_code.get((r, c)),
            }
            for (r, c) in sorted(code_keys)
            if head_code.get((r, c)) != target_code.get((r, c))
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
        if head["table_meta"] != target["table_meta"]:
            payload["table_meta"] = {
                "before": head["table_meta"], "after": target["table_meta"],
            }
        if code_list:
            payload["code"] = code_list
        return payload
