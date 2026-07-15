from __future__ import annotations

from typing import Callable

from app.repositories.sqlite.database import SqliteDatabase


class KnowhowStore:
    """SQLite row persistence for the knowhow-table feature (knowhow-tables
    PR-1 Task 2): ``knowhow_tables``/``knowhow_columns``/``knowhow_rows``/
    ``knowhow_cells`` (schema from migration 16, see
    ``app.repositories.sqlite.migrations.SqliteMigrator._migration_16``) plus
    ``notebook_assets`` (upload metadata for images embedded in cell
    markdown).

    Row-level only — column-role-driven KG/chunk projection lives in Task 5's
    projector service; the import/table API lives in Task 6's routes. Every
    method here is what the facade (``SQLiteRepository``) exposes verbatim as
    a one-hop delegate, so Task 5/6 depend on these exact names/signatures.

    ``mutation_seq`` is a monotonic per-table counter (NOT a timestamp — see
    the project's existing ``kg_mutation_seq`` convention) that Task 5 reads
    to detect "has this table changed since I last projected it".
    ``update_knowhow_cell`` bumps it as part of its one write transaction;
    ``bump_knowhow_mutation_seq`` is also exposed standalone for callers (the
    projector, bulk-import) that need to bump it at a point of their own
    choosing without an accompanying cell write.
    """

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

    # ------------------------------------------------------------- tables
    def create_knowhow_table(
        self,
        notebook_id: str,
        title: str,
        description: str,
        columns: list[dict],
    ) -> str:
        """Create a table + its column definitions (position = list order).

        Validates: a non-empty (whitespace-stripped) title; every column name
        is non-empty and unique; and exactly one column carries the ``concept``
        role (the row-anchor Task 5's projector keys on). Violations raise
        ``ValueError`` with a Chinese-friendly message — nothing is written on
        failure. The stored title is the stripped form.
        """
        title = str(title or "").strip()
        if not title:
            raise ValueError("表标题不能为空")
        names = [str(column.get("name", "")).strip() for column in columns]
        if any(not name for name in names):
            raise ValueError("列名不能为空")
        if len(names) != len(set(names)):
            raise ValueError("列名不能重复")
        concept_count = sum(1 for column in columns if column.get("role") == "concept")
        if concept_count != 1:
            raise ValueError("必须恰好有一列 concept（概念）角色")

        table_id = self.new_id("khtbl")
        now = self.now()
        with self.database.write() as db:
            db.execute(
                "INSERT INTO knowhow_tables "
                "(id, notebook_id, title, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (table_id, notebook_id, title, description or "", now, now),
            )
            for position, column in enumerate(columns):
                db.execute(
                    "INSERT INTO knowhow_columns (id, table_id, name, role, position) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        self.new_id("khcol"),
                        table_id,
                        names[position],
                        column.get("role") or "plain",
                        position,
                    ),
                )
        return table_id

    def list_knowhow_tables(self, notebook_id: str) -> list[dict]:
        """Table summaries for one notebook (created_at order) with each
        table's current row count (one batched COUNT, not N+1)."""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM knowhow_tables WHERE notebook_id = ? "
                "ORDER BY created_at, id",
                (notebook_id,),
            ).fetchall()
            table_ids = [row["id"] for row in rows]
            counts: dict[str, int] = {}
            if table_ids:
                placeholders = ",".join("?" for _ in table_ids)
                count_rows = db.execute(
                    "SELECT table_id, COUNT(*) AS n FROM knowhow_rows "
                    f"WHERE table_id IN ({placeholders}) GROUP BY table_id",
                    table_ids,
                ).fetchall()
                counts = {row["table_id"]: row["n"] for row in count_rows}
        return [
            {
                "id": row["id"],
                "notebook_id": row["notebook_id"],
                "title": row["title"],
                "description": row["description"],
                "mutation_seq": row["mutation_seq"],
                "hidden_source_id": row["hidden_source_id"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "row_count": counts.get(row["id"], 0),
            }
            for row in rows
        ]

    def get_knowhow_table(self, table_id: str) -> dict:
        """Full table detail: columns ordered by position, rows ordered by
        position, each row carrying ``cells: {column_id: content_md}`` (only
        columns with an actual cell row appear — a never-edited cell is
        simply absent, not an empty-string placeholder) and
        ``projection_status``. Raises ``KeyError`` if the table is gone."""
        with self.database.connect() as db:
            table_row = db.execute(
                "SELECT * FROM knowhow_tables WHERE id = ?", (table_id,)
            ).fetchone()
            if table_row is None:
                raise KeyError(table_id)
            column_rows = db.execute(
                "SELECT id, name, role, position FROM knowhow_columns "
                "WHERE table_id = ? ORDER BY position, id",
                (table_id,),
            ).fetchall()
            row_rows = db.execute(
                "SELECT id, position, projection_status, created_at, updated_at "
                "FROM knowhow_rows WHERE table_id = ? ORDER BY position, id",
                (table_id,),
            ).fetchall()
            row_ids = [row["id"] for row in row_rows]
            cells_by_row: dict[str, dict[str, str]] = {rid: {} for rid in row_ids}
            if row_ids:
                placeholders = ",".join("?" for _ in row_ids)
                cell_rows = db.execute(
                    "SELECT row_id, column_id, content_md FROM knowhow_cells "
                    f"WHERE row_id IN ({placeholders})",
                    row_ids,
                ).fetchall()
                for cell_row in cell_rows:
                    cells_by_row[cell_row["row_id"]][cell_row["column_id"]] = (
                        cell_row["content_md"]
                    )
        return {
            "id": table_row["id"],
            "notebook_id": table_row["notebook_id"],
            "title": table_row["title"],
            "description": table_row["description"],
            "mutation_seq": table_row["mutation_seq"],
            "hidden_source_id": table_row["hidden_source_id"],
            "created_by": table_row["created_by"],
            "created_at": table_row["created_at"],
            "updated_at": table_row["updated_at"],
            "columns": [
                {
                    "id": column["id"],
                    "name": column["name"],
                    "role": column["role"],
                    "position": column["position"],
                }
                for column in column_rows
            ],
            "rows": [
                {
                    "id": row["id"],
                    "position": row["position"],
                    "projection_status": row["projection_status"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "cells": cells_by_row[row["id"]],
                }
                for row in row_rows
            ],
        }

    def delete_knowhow_table(self, table_id: str) -> dict:
        """Cascade-delete a table (columns/rows/cells all carry ``ON DELETE
        CASCADE`` FKs to it in migration 16, so one DELETE is enough with
        ``PRAGMA foreign_keys = ON`` — set on every connection by
        ``SqliteDatabase``). Returns the table's ``hidden_source_id`` (may be
        ``None``) so the caller can clean up its projection. Deleting an
        already-gone table is a silent no-op returning ``{"hidden_source_id":
        None}`` — mirrors this codebase's zero-row UPDATE/DELETE convention."""
        with self.database.write() as db:
            row = db.execute(
                "SELECT hidden_source_id FROM knowhow_tables WHERE id = ?",
                (table_id,),
            ).fetchone()
            hidden_source_id = row["hidden_source_id"] if row is not None else None
            db.execute("DELETE FROM knowhow_tables WHERE id = ?", (table_id,))
        return {"hidden_source_id": hidden_source_id}

    def set_knowhow_hidden_source(self, table_id: str, source_id: str) -> None:
        with self.database.write() as db:
            db.execute(
                "UPDATE knowhow_tables SET hidden_source_id = ?, updated_at = ? "
                "WHERE id = ?",
                (source_id, self.now(), table_id),
            )

    def bump_knowhow_mutation_seq(self, table_id: str) -> int:
        """Increment and return the table's monotonic ``mutation_seq``.
        Raises ``KeyError`` if the table does not exist."""
        with self.database.write() as db:
            db.execute(
                "UPDATE knowhow_tables SET mutation_seq = mutation_seq + 1 WHERE id = ?",
                (table_id,),
            )
            row = db.execute(
                "SELECT mutation_seq FROM knowhow_tables WHERE id = ?", (table_id,)
            ).fetchone()
        if row is None:
            raise KeyError(table_id)
        return row["mutation_seq"]

    # --------------------------------------------------------------- rows
    def add_knowhow_row(
        self, table_id: str, cells: dict[str, str], position: int | None = None
    ) -> str:
        """Insert a row (default position = current row count, i.e.
        append) plus any provided cells, in one write transaction.
        ``projection_status`` starts at its schema default (``'pending'``).
        Does not bump the table's ``mutation_seq`` — callers doing bulk
        inserts (Task 6's import) bump once at the end via
        ``bump_knowhow_mutation_seq``."""
        row_id = self.new_id("khrow")
        now = self.now()
        with self.database.write() as db:
            if position is None:
                count_row = db.execute(
                    "SELECT COUNT(*) AS n FROM knowhow_rows WHERE table_id = ?",
                    (table_id,),
                ).fetchone()
                position = count_row["n"]
            db.execute(
                "INSERT INTO knowhow_rows (id, table_id, position, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (row_id, table_id, position, now, now),
            )
            for column_id, content_md in (cells or {}).items():
                db.execute(
                    "INSERT INTO knowhow_cells (id, row_id, column_id, content_md, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (self.new_id("khcel"), row_id, column_id, content_md, now),
                )
        return row_id

    def set_knowhow_row_projection(self, row_id: str, status: str) -> None:
        with self.database.write() as db:
            db.execute(
                "UPDATE knowhow_rows SET projection_status = ? WHERE id = ?",
                (status, row_id),
            )

    # -------------------------------------------------------------- cells
    def update_knowhow_cell(self, row_id: str, column_id: str, content_md: str) -> None:
        """Upsert one cell's content. One write transaction: the cell
        (insert or update-in-place via the ``UNIQUE(row_id, column_id)``
        conflict target), the row's ``updated_at`` + ``projection_status`` ->
        ``'pending'``, and the owning table's ``mutation_seq`` += 1 (table_id
        resolved from the row — the public signature has no table_id)."""
        now = self.now()
        with self.database.write() as db:
            db.execute(
                "INSERT INTO knowhow_cells (id, row_id, column_id, content_md, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(row_id, column_id) DO UPDATE SET "
                "content_md = excluded.content_md, updated_at = excluded.updated_at",
                (self.new_id("khcel"), row_id, column_id, content_md, now),
            )
            db.execute(
                "UPDATE knowhow_rows SET updated_at = ?, projection_status = 'pending' "
                "WHERE id = ?",
                (now, row_id),
            )
            db.execute(
                "UPDATE knowhow_tables SET mutation_seq = mutation_seq + 1 "
                "WHERE id = (SELECT table_id FROM knowhow_rows WHERE id = ?)",
                (row_id,),
            )

    # ------------------------------------------------------------- assets
    def insert_notebook_asset(
        self, notebook_id: str, filename: str, mime: str, size: int, created_by: str
    ) -> str:
        asset_id = self.new_id("asset")
        with self.database.write() as db:
            db.execute(
                "INSERT INTO notebook_assets "
                "(id, notebook_id, filename, mime, size, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (asset_id, notebook_id, filename, mime, size, created_by, self.now()),
            )
        return asset_id

    def get_notebook_asset(self, asset_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM notebook_assets WHERE id = ?", (asset_id,)
            ).fetchone()
        return dict(row) if row is not None else None


__all__ = ["KnowhowStore"]
