"""单张 knowhow 表的跨 notebook 传输 SQL：快照 + 单事务插入 + 提交前校验。

镜像 SharingStore 对整本拷贝所做的事，但收窄到一张表 + 它隐藏源的派生产物。
所有 SQL 收在这里（callers_static 约束：原始 SQL 只在 repositories/sqlite 下）。
"""
from __future__ import annotations

import sqlite3

from app.repositories.sqlite.database import SqliteDatabase

# 插入 FK 顺序：表→列/行→资产→格/代码→隐藏源→元素→chunk→向量
_BUSINESS_ORDER = ("columns", "rows", "assets", "cells", "cell_code")
_DERIVED_ORDER = ("elements", "chunks", "chunk_embeddings")


def _insert_rows(db: sqlite3.Connection, table: str, rows: list) -> None:
    for row in rows:
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        db.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )


# 逻辑表名 → 真实表名（键在 payload/校验里用逻辑名）
_TABLE_NAMES = {
    "columns": "knowhow_columns",
    "rows": "knowhow_rows",
    "assets": "notebook_assets",
    "cells": "knowhow_cells",
    "cell_code": "knowhow_cell_code",
    "elements": "source_elements",
    "chunks": "chunks",
    "chunk_embeddings": "chunk_embeddings",
}


class KnowhowTransferStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def snapshot_table(self, table_id: str) -> dict:
        with self.database.connect() as db:
            table = db.execute(
                "SELECT * FROM knowhow_tables WHERE id = ?", (table_id,)
            ).fetchone()
            if table is None:
                raise KeyError(table_id)
            table = dict(table)

            def rows_for(sql: str, params: tuple) -> list:
                return [dict(r) for r in db.execute(sql, params).fetchall()]

            columns = rows_for(
                "SELECT * FROM knowhow_columns WHERE table_id = ? ORDER BY position, id",
                (table_id,),
            )
            rows = rows_for(
                "SELECT * FROM knowhow_rows WHERE table_id = ? ORDER BY position, id",
                (table_id,),
            )
            cells = rows_for(
                "SELECT c.* FROM knowhow_cells c JOIN knowhow_rows r ON r.id = c.row_id "
                "WHERE r.table_id = ?",
                (table_id,),
            )
            cell_code = rows_for(
                "SELECT cc.* FROM knowhow_cell_code cc JOIN knowhow_rows r ON r.id = cc.row_id "
                "WHERE r.table_id = ?",
                (table_id,),
            )

            hidden = table.get("hidden_source_id")
            elements: list = []
            chunks: list = []
            chunk_embeddings: list = []
            source = None
            if hidden:
                source_row = db.execute(
                    "SELECT * FROM sources WHERE id = ?", (hidden,)
                ).fetchone()
                source = dict(source_row) if source_row is not None else None
                if source is not None:
                    elements = rows_for(
                        "SELECT * FROM source_elements WHERE source_id = ?", (hidden,)
                    )
                    chunks = rows_for(
                        "SELECT * FROM chunks WHERE source_id = ?", (hidden,)
                    )
                    chunk_embeddings = rows_for(
                        "SELECT ce.* FROM chunk_embeddings ce "
                        "JOIN chunks c ON c.id = ce.chunk_id WHERE c.source_id = ?",
                        (hidden,),
                    )
        return {
            "table": table,
            "columns": columns,
            "rows": rows,
            "cells": cells,
            "cell_code": cell_code,
            "source": source,
            "elements": elements,
            "chunks": chunks,
            "chunk_embeddings": chunk_embeddings,
        }

    def insert_transfer(self, payload: dict, expected_counts: dict) -> None:
        table = payload["table"]
        new_table_id = table["id"]
        with self.database.write() as db:
            db.execute("BEGIN IMMEDIATE")
            _insert_rows(db, "knowhow_tables", [table])
            for key in _BUSINESS_ORDER:
                _insert_rows(db, _TABLE_NAMES[key], payload.get(key) or [])
            if payload.get("source"):
                _insert_rows(db, "sources", [payload["source"]])
            for key in _DERIVED_ORDER:
                _insert_rows(db, _TABLE_NAMES[key], payload.get(key) or [])

            # chunks_fts 必须在这里显式补，且必须在同一个事务里。
            #
            # chunks_fts 是「无触发器、手工维护」的 FTS5 虚表（migrations.py
            # 只给 memory_items_fts 建了触发器）——正常投影路径靠
            # ChunkStore.insert_rows 末尾那行显式写入（chunk_store.py:139）。
            # 而拷贝出来的 chunk 走不到那行：copy_table 之后调度的重投影，对
            # 每个副本 chunk 都满足 `old_specs == new_specs` → _write_chunks
            # 直接 `continue`（projection.py），insert_rows/delete_by_ids 这两
            # 个仅有的写 FTS 路径一个都不会被调用。所以不在这里补，副本就只剩
            # 向量召回、词法检索永久搜不到，而且没有任何自愈路径（向量那边有
            # self-heal probe，FTS 没有）。
            #
            # 语句与列序照抄 ChunkStore._insert_fts_rows（chunk_store.py:75-78,
            # 由 insert_rows:139 调用），不另发明 SQL；同事务保证「FTS 写失败
            # 连 chunk 行一起回滚」，与 replace_source_chunks 的既有不变量一致。
            # notebook_sharing.py:487-491 对整本拷贝做的是同一件事，原因相同。
            chunk_rows = payload.get("chunks") or []
            if chunk_rows:
                db.executemany(
                    "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                    [
                        (c["id"], c["notebook_id"], c.get("text") or "")
                        for c in chunk_rows
                    ],
                )

            # 提交前校验：落库计数须等于源表快照计数（不一致 → 抛错 → 回滚，不留半份副本）
            def count(sql: str) -> int:
                return int(db.execute(sql, (new_table_id,)).fetchone()[0])

            checks = {
                "columns": count("SELECT COUNT(*) FROM knowhow_columns WHERE table_id=?"),
                "rows": count("SELECT COUNT(*) FROM knowhow_rows WHERE table_id=?"),
                "cells": count(
                    "SELECT COUNT(*) FROM knowhow_cells c JOIN knowhow_rows r "
                    "ON r.id=c.row_id WHERE r.table_id=?"
                ),
                "cell_code": count(
                    "SELECT COUNT(*) FROM knowhow_cell_code cc JOIN knowhow_rows r "
                    "ON r.id=cc.row_id WHERE r.table_id=?"
                ),
            }
            expected = {k: int(expected_counts.get(k, 0)) for k in checks}
            if checks != expected:
                raise RuntimeError(f"knowhow transfer 校验失败：{checks} != {expected}")

    #: Shared by ``table_fingerprint`` (own ``connect()``) and
    #: ``delete_table_if_unchanged`` (caller-supplied connection inside its
    #: own ``write()``) so the two never drift apart — see both methods'
    #: docstrings for why they must run the byte-identical query.
    _FINGERPRINT_SQL = (
        "SELECT t.mutation_seq AS mutation_seq, "
        "(SELECT COUNT(*) FROM knowhow_columns WHERE table_id = t.id) "
        "AS col_count, "
        "(SELECT COUNT(*) FROM knowhow_rows WHERE table_id = t.id) "
        "AS row_count, "
        "(SELECT COUNT(*) FROM knowhow_cells c JOIN knowhow_rows r "
        " ON r.id = c.row_id WHERE r.table_id = t.id) AS cell_count, "
        "(SELECT COUNT(*) FROM knowhow_cell_code cc JOIN knowhow_rows r "
        " ON r.id = cc.row_id WHERE r.table_id = t.id) AS cell_code_count, "
        "(SELECT group_concat(sig, '|') FROM ("
        "  SELECT cc.id || ':' || cc.cell_content_hash || ':' || cc.updated_at AS sig "
        "  FROM knowhow_cell_code cc JOIN knowhow_rows r ON r.id = cc.row_id "
        "  WHERE r.table_id = t.id ORDER BY cc.id"
        ")) AS cell_code_signal "
        "FROM knowhow_tables t WHERE t.id = ?"
    )

    @classmethod
    def _fingerprint_on(cls, db: sqlite3.Connection, table_id: str) -> "dict | None":
        row = db.execute(cls._FINGERPRINT_SQL, (table_id,)).fetchone()
        return dict(row) if row is not None else None

    def table_fingerprint(self, table_id: str) -> "dict | None":
        """Cheap source-version probe for ``table_id``: ``mutation_seq``,
        live columns/rows/cells/cell_code counts, AND a cell_code CONTENT
        signal, in ONE top-level SELECT (SQLite gives a single statement's
        scalar subqueries a consistent snapshot as of when it starts
        executing, so this can't observe a torn read the way separate
        queries could). Returns ``None`` if the table no longer exists.

        Used by ``move_table``'s snapshot-vs-delete concurrent-edit guard
        (PR review round 2 P1-2, data loss): ``copy_table`` snapshots the
        source, and the source is deleted afterward — if a cell/row/column/
        code edit commits in between, the target holds a stale copy and the
        newly-edited source is destroyed forever. ``knowhow_tables.
        mutation_seq`` alone under-covers this: per ``KnowhowStore``'s own
        docstring, the structural editing methods (add/delete row, add/
        delete column — see ``add_knowhow_row``/``delete_knowhow_row``/
        ``add_knowhow_column``/``delete_knowhow_column``) deliberately do
        NOT bump it, only ``update_knowhow_cell``/``update_knowhow_cells``
        do. A mutation_seq-only comparison would miss a concurrently added
        or deleted row/column entirely. The four counts close exactly that
        gap: any add/delete changes at least one of them (columns/rows
        cascade-delete their cells and cell_code, so those counts move too).

        PR review round 3 P1-3 (fingerprint gap): the four counts still miss
        an in-place CODE edit. ``upsert_knowhow_cell_code`` is an
        ``INSERT ... ON CONFLICT(row_id, column_id) DO UPDATE`` — editing an
        already-attached cell's code keeps the same row/column pair, so
        ``cell_code_count`` doesn't move, and (like every other structural-
        vs-content distinction here) it does NOT bump ``mutation_seq``
        either. ``cell_code_signal`` closes that gap: a deterministic
        ``group_concat`` of ``id:cell_content_hash:updated_at`` per code row,
        under a stable ``ORDER BY cc.id`` (group_concat has no defined input
        order on its own; ordering the source subquery is what makes this
        reproducible across calls — see ``test_fingerprint_stable_when_
        nothing_changes``). Any edit changes that row's hash and/or
        updated_at, which changes the concatenated string. ``NULL`` when the
        table has no code rows (group_concat of zero rows), which compares
        equal to itself and unequal to any non-empty signal, both correct.

        Known, accepted scope boundary: this does NOT catch a pure metadata
        edit that changes neither content nor cardinality -- renaming a
        column (``rename_knowhow_column``), moving the anchor designation
        (``set_knowhow_anchor_column``), changing a column's kind
        (``set_knowhow_column_kind``), or patching the table's title/
        description (``update_knowhow_table_meta``). None of those touch
        row/cell/code CONTENT -- the knowledge a move could actually lose --
        only how it is labeled, so a stale label surviving a move is a much
        smaller, cosmetic gap than the data-loss this guard exists to close.
        """
        with self.database.connect() as db:
            return self._fingerprint_on(db, table_id)

    def delete_table_if_unchanged(
        self, table_id: str, expected_fingerprint: "dict | None"
    ) -> bool:
        """Atomic conditional delete for ``move_table``'s cleanup step (PR
        review round 3 P1-1): recomputes ``table_fingerprint`` and deletes
        the ``knowhow_tables`` row (cascade — see migration 16/17's
        ``ON DELETE CASCADE`` FKs from columns/rows onto the table, and from
        cells/cell_code onto rows) in ONE ``database.write()``. The whole
        method body runs under ``database.write_lock`` continuously (see
        ``SqliteDatabase.write``'s docstring: writers are process-wide
        serialized), so no concurrent writer's edit can land between "the
        recheck says unchanged" and "the row is gone" the way it could
        across two separate calls (``table_fingerprint()`` then a separate
        unconditional delete) — that gap is exactly PR review round 2's
        P1-2 fix left open: it re-checked the fingerprint, but the check and
        the eventual delete were still two independent statements with an
        arbitrary amount of other work (projection teardown) running in
        between, so an edit landing in THAT window still sailed past the
        check and got deleted along with the row. Returns whether the row
        was actually deleted (``False`` means a concurrent edit changed the
        fingerprint since ``expected_fingerprint`` was captured — the table
        is left fully intact, caller should surface this as a recoverable
        cleanup failure, not silently retry the delete)."""
        with self.database.write() as db:
            if self._fingerprint_on(db, table_id) != expected_fingerprint:
                return False
            cursor = db.execute("DELETE FROM knowhow_tables WHERE id = ?", (table_id,))
            return cursor.rowcount == 1
