"""单张 knowhow 表的跨 notebook 传输 SQL：快照 + 单事务插入 + 提交前校验。

镜像 SharingStore 对整本拷贝所做的事，但收窄到一张表 + 它隐藏源的派生产物。
所有 SQL 收在这里（callers_static 约束：原始 SQL 只在 repositories/sqlite 下）。
"""
from __future__ import annotations

import sqlite3
from typing import Callable

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
