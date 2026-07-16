from __future__ import annotations

from typing import Callable

from app.repositories.sqlite.database import SqliteDatabase


#: Legal ``knowhow_columns.role`` values post-migration-17 (design doc §①
#: "角色词表(2026-07-15 修订)": domain-neutral behavior kinds replacing the
#: PR-1 time-series-fixup-instance vocabulary; the migration remaps stored
#: legacy values). ``anchor`` marks "this column is the row-title column".
VALID_KINDS = frozenset({"anchor", "procedure", "entity", "attribute"})
#: The content kinds a per-column mutation (``add_knowhow_column`` /
#: ``set_knowhow_column_kind``) may write. ``anchor`` is deliberately absent:
#: post-creation it is a TABLE-level designation ("which column is the row
#: title"), written only by ``set_knowhow_anchor_column`` so the at-most-one
#: invariant has a single enforcement point. ``create_knowhow_table`` is the
#: one exception — its initial column list may name the anchor inline (it
#: validates the at-most-one rule itself).
_NON_ANCHOR_KINDS = frozenset({"procedure", "entity", "attribute"})


class KnowhowStore:
    """SQLite row persistence for the knowhow-table feature (knowhow-tables
    PR-1 Task 2, extended by PR-2+3 Task 1): ``knowhow_tables``/
    ``knowhow_columns``/``knowhow_rows``/``knowhow_cells`` (schema from
    migration 16, see
    ``app.repositories.sqlite.migrations.SqliteMigrator._migration_16``) plus
    ``notebook_assets`` (upload metadata for images embedded in cell
    markdown) and ``knowhow_cell_code`` (migration 17 — per-cell code
    attachments, see ``_migration_17``).

    Row-level only — column-kind-driven KG/chunk projection lives in the
    projector service; the import/table and editing APIs live in the routes
    layer. Every method here is what the facade (``SQLiteRepository``)
    exposes verbatim as a one-hop delegate, so downstream services depend on
    these exact names/signatures.

    ``mutation_seq`` is a monotonic per-table counter (NOT a timestamp — see
    the project's existing ``kg_mutation_seq`` convention) that the projector
    reads to detect "has this table changed since I last projected it".
    ``update_knowhow_cell`` bumps it as part of its one write transaction;
    ``bump_knowhow_mutation_seq`` is also exposed standalone for callers (the
    projector, bulk-import) that need to bump it at a point of their own
    choosing without an accompanying cell write. The structural editing
    methods added in PR-2+3 Task 1 (add/rename/delete column, delete row,
    set-anchor, table-meta) deliberately do NOT bump it themselves — that
    stays the projector's job each time it actually re-projects the table
    (design doc §④: "每次投影 bump 表 mutation_seq"), not every store-level
    mutation.
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
        created_by: str = "",
    ) -> str:
        """Create a table + its column definitions (position = list order).

        Validates: a non-empty (whitespace-stripped) title; every column name
        is non-empty and unique; every column's ``role`` is one of
        ``VALID_KINDS``; and AT MOST ONE column carries the ``anchor`` kind
        (design doc §① "主题列可选(0..1)" — relaxed from PR-1's "exactly one
        concept column": a table with zero anchor columns is a legitimate
        "记录型" table that only participates in retrieval, never the KG).
        Violations raise ``ValueError`` with a Chinese-friendly message —
        nothing is written on failure. The stored title is the stripped form.
        """
        title = str(title or "").strip()
        if not title:
            raise ValueError("表标题不能为空")
        names = [str(column.get("name", "")).strip() for column in columns]
        if any(not name for name in names):
            raise ValueError("列名不能为空")
        if len(names) != len(set(names)):
            raise ValueError("列名不能重复")
        kinds = [column.get("role") or "attribute" for column in columns]
        for kind in kinds:
            if kind not in VALID_KINDS:
                raise ValueError(f"非法的列类型：{kind}")
        anchor_count = sum(1 for kind in kinds if kind == "anchor")
        if anchor_count > 1:
            raise ValueError("至多一列可设为行标题列")

        table_id = self.new_id("khtbl")
        now = self.now()
        with self.database.write() as db:
            db.execute(
                "INSERT INTO knowhow_tables "
                "(id, notebook_id, title, description, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (table_id, notebook_id, title, description or "", created_by or "", now, now),
            )
            for position, column in enumerate(columns):
                db.execute(
                    "INSERT INTO knowhow_columns (id, table_id, name, role, position) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        self.new_id("khcol"),
                        table_id,
                        names[position],
                        kinds[position],
                        position,
                    ),
                )
        return table_id

    def update_knowhow_table_meta(
        self,
        table_id: str,
        title: "str | None" = None,
        description: "str | None" = None,
    ) -> None:
        """Patch title and/or description in place (each ``None`` argument
        leaves that field untouched — the editing API's PATCH semantics for
        an omitted field). A non-``None`` title is validated the same way
        ``create_knowhow_table`` validates its own title (non-empty after
        stripping), raising the same ``ValueError`` message. A ``None``
        description is left alone (there is no "clear description" case
        distinct from passing ``""`` explicitly, unlike title which can never
        legally become empty). Silent no-op if the table is already gone
        (this codebase's zero-row UPDATE convention)."""
        sets: list[str] = []
        params: list[str] = []
        if title is not None:
            title = str(title).strip()
            if not title:
                raise ValueError("表标题不能为空")
            sets.append("title = ?")
            params.append(title)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(self.now())
        params.append(table_id)
        with self.database.write() as db:
            db.execute(
                f"UPDATE knowhow_tables SET {', '.join(sets)} WHERE id = ?", params
            )

    # ------------------------------------------------------------ columns
    def set_knowhow_anchor_column(
        self, table_id: str, column_id: "str | None"
    ) -> "str | None":
        """Designate ``column_id`` as the table's row-title (anchor) column,
        or clear the designation with ``None``. Returns the PREVIOUS anchor
        column's id (``None`` if the table had no anchor) so callers can tell
        whether anything actually moved.

        The single enforcement point for the at-most-one-anchor invariant
        post-creation: the old anchor (if different) is demoted to
        ``attribute`` in the same write transaction that promotes the new one
        — an anchor column carries no content-kind of its own, so demotion
        cannot restore a "previous" kind (there is none to restore).

        A non-``None`` ``column_id`` must belong to this table, else
        ``ValueError`` (friendly Chinese) and nothing is written. Clearing on
        a missing/anchorless table is a silent no-op returning ``None``."""
        with self.database.write() as db:
            old_row = db.execute(
                "SELECT id FROM knowhow_columns WHERE table_id = ? AND role = 'anchor'",
                (table_id,),
            ).fetchone()
            old_id = old_row["id"] if old_row is not None else None
            if column_id is not None:
                target = db.execute(
                    "SELECT table_id FROM knowhow_columns WHERE id = ?", (column_id,)
                ).fetchone()
                if target is None or target["table_id"] != table_id:
                    raise ValueError("指定的列不属于本表")
            if column_id == old_id:
                return old_id  # no-op move (incl. None -> None)
            if old_id is not None:
                db.execute(
                    "UPDATE knowhow_columns SET role = 'attribute' WHERE id = ?",
                    (old_id,),
                )
            if column_id is not None:
                db.execute(
                    "UPDATE knowhow_columns SET role = 'anchor' WHERE id = ?",
                    (column_id,),
                )
            db.execute(
                "UPDATE knowhow_tables SET updated_at = ? WHERE id = ?",
                (self.now(), table_id),
            )
        return old_id

    def add_knowhow_column(
        self, table_id: str, name: str, kind: str, position: "int | None" = None
    ) -> str:
        """Append a column (default position = MAX(position)+1 — NOT the
        column count: ``delete_knowhow_column`` leaves position gaps, and a
        COUNT-based default would collide with a surviving higher position).
        ``kind`` must be a content kind (``_NON_ANCHOR_KINDS``); the anchor
        designation only moves via ``set_knowhow_anchor_column``. The name is
        validated like ``create_knowhow_table``'s (non-empty, unique within
        the table). Existing rows get NO backfilled cells — cells are sparse
        by contract (see ``get_knowhow_table``). Raises ``KeyError`` for a
        missing table."""
        name = str(name or "").strip()
        if not name:
            raise ValueError("列名不能为空")
        if kind == "anchor":
            raise ValueError("行标题列请通过表设置指定，不能作为列类型添加")
        if kind not in _NON_ANCHOR_KINDS:
            raise ValueError(f"非法的列类型：{kind}")
        column_id = self.new_id("khcol")
        with self.database.write() as db:
            if db.execute(
                "SELECT 1 FROM knowhow_tables WHERE id = ?", (table_id,)
            ).fetchone() is None:
                raise KeyError(table_id)
            duplicate = db.execute(
                "SELECT 1 FROM knowhow_columns WHERE table_id = ? AND name = ?",
                (table_id, name),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("列名不能重复")
            if position is None:
                max_row = db.execute(
                    "SELECT COALESCE(MAX(position) + 1, 0) AS p FROM knowhow_columns "
                    "WHERE table_id = ?",
                    (table_id,),
                ).fetchone()
                position = max_row["p"]
            db.execute(
                "INSERT INTO knowhow_columns (id, table_id, name, role, position) "
                "VALUES (?, ?, ?, ?, ?)",
                (column_id, table_id, name, kind, position),
            )
        return column_id

    def rename_knowhow_column(self, column_id: str, name: str) -> None:
        """Rename one column (stored stripped). Raises ``KeyError`` for a
        missing column; ``ValueError`` for a blank name or a name already
        used by a SIBLING column (renaming to its own current name is a
        silent success, not a duplicate)."""
        name = str(name or "").strip()
        if not name:
            raise ValueError("列名不能为空")
        with self.database.write() as db:
            row = db.execute(
                "SELECT table_id FROM knowhow_columns WHERE id = ?", (column_id,)
            ).fetchone()
            if row is None:
                raise KeyError(column_id)
            duplicate = db.execute(
                "SELECT 1 FROM knowhow_columns WHERE table_id = ? AND name = ? AND id != ?",
                (row["table_id"], name, column_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("列名不能重复")
            db.execute(
                "UPDATE knowhow_columns SET name = ? WHERE id = ?", (name, column_id)
            )

    def set_knowhow_column_kind(self, column_id: str, kind: str) -> None:
        """Change one column's content kind. ``anchor`` is rejected here —
        the anchor designation is table-level state with its own setter
        (``set_knowhow_anchor_column``), which is also the only way a column
        currently marked ``anchor`` legitimately changes kind (clearing or
        moving the anchor demotes it to ``attribute``). Raises ``KeyError``
        for a missing column."""
        if kind == "anchor":
            raise ValueError("行标题列请通过表设置指定，不能作为列类型修改")
        if kind not in _NON_ANCHOR_KINDS:
            raise ValueError(f"非法的列类型：{kind}")
        with self.database.write() as db:
            row = db.execute(
                "SELECT 1 FROM knowhow_columns WHERE id = ?", (column_id,)
            ).fetchone()
            if row is None:
                raise KeyError(column_id)
            db.execute(
                "UPDATE knowhow_columns SET role = ? WHERE id = ?", (kind, column_id)
            )

    def delete_knowhow_column(self, column_id: str) -> None:
        """Delete a column; its cells and cell-code attachments go with it
        (``knowhow_cells.column_id`` and ``knowhow_cell_code.column_id`` are
        both ``ON DELETE CASCADE`` onto knowhow_columns, and ``PRAGMA
        foreign_keys = ON`` is set on every connection by SqliteDatabase).
        Deleting the anchor column simply leaves the table anchorless — a
        legal state (see ``create_knowhow_table``). Silent no-op when already
        gone. Remaining positions are NOT renumbered (ordering reads sort by
        position and tolerate gaps)."""
        with self.database.write() as db:
            db.execute("DELETE FROM knowhow_columns WHERE id = ?", (column_id,))

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

    def delete_knowhow_row(self, row_id: str) -> None:
        """Delete a row; its cells and cell-code attachments go with it
        (``knowhow_cells.row_id`` and ``knowhow_cell_code.row_id`` are both
        ``ON DELETE CASCADE`` onto knowhow_rows). Silent no-op when already
        gone. Surviving rows' positions are NOT renumbered (ordering reads
        sort by position and tolerate gaps)."""
        with self.database.write() as db:
            db.execute("DELETE FROM knowhow_rows WHERE id = ?", (row_id,))

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

    def validate_cell_target(self, row_id: str, column_id: str) -> None:
        """Assert (row, column) name a real cell slot: both exist AND belong
        to the SAME table, else ``ValueError("格子定位不合法")`` uniformly (a
        missing row, a missing column, and a cross-table pair are all the
        same "this cell address is not real" to a caller — no oracle for
        which part was wrong). The editing/code endpoints call this before
        ``update_knowhow_cell``/``upsert_knowhow_cell_code``, whose own FKs
        only guarantee existence, not same-table pairing."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT r.table_id AS row_table, c.table_id AS column_table "
                "FROM knowhow_rows r JOIN knowhow_columns c "
                "ON r.id = ? AND c.id = ?",
                (row_id, column_id),
            ).fetchone()
        if row is None or row["row_table"] != row["column_table"]:
            raise ValueError("格子定位不合法")

    # ---------------------------------------------------------- cell code
    def upsert_knowhow_cell_code(
        self,
        row_id: str,
        column_id: str,
        code_text: str,
        language: str,
        updated_by: str,
        cell_content_hash: str,
    ) -> str:
        """Insert-or-replace the ONE code attachment a cell may carry
        (``UNIQUE(row_id, column_id)`` conflict target). On update the row
        keeps its original id and ``created_at``; ``code_text``/``language``/
        ``updated_by``/``cell_content_hash``/``updated_at`` are replaced.
        Returns the attachment's stable id. ``cell_content_hash`` is the
        caller-computed hash of the cell's net text at write time — freshness
        (implemented/stale) is derived at READ time by comparing it against
        the cell's current hash, never stored. Same-table pairing of
        (row, column) is the caller's job via ``validate_cell_target``
        (mirrors ``update_knowhow_cell``'s contract); the FKs here only
        guarantee both halves exist."""
        now = self.now()
        with self.database.write() as db:
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
                    self.new_id("khcode"), row_id, column_id, code_text,
                    language or "", updated_by or "", cell_content_hash, now, now,
                ),
            )
            row = db.execute(
                "SELECT id FROM knowhow_cell_code WHERE row_id = ? AND column_id = ?",
                (row_id, column_id),
            ).fetchone()
        return row["id"]

    def get_knowhow_cell_code(self, row_id: str, column_id: str) -> "dict | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM knowhow_cell_code WHERE row_id = ? AND column_id = ?",
                (row_id, column_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def delete_knowhow_cell_code(self, row_id: str, column_id: str) -> None:
        """Silent no-op when there is nothing to delete (zero-row DELETE
        convention)."""
        with self.database.write() as db:
            db.execute(
                "DELETE FROM knowhow_cell_code WHERE row_id = ? AND column_id = ?",
                (row_id, column_id),
            )

    def get_knowhow_row_location(self, row_id: str) -> "dict | None":
        """Resolve a bare ``row_id`` to its owning ``table_id`` + that
        table's ``notebook_id`` (PR-2+3 Task 10: the agent surface's row/
        cell-scoped HTTP endpoints — ``GET/PUT/DELETE .../rows/{row}...`` —
        carry ONLY ``row_id``/``column_id`` in their URL, no ``notebook_id``
        or ``table_id`` segment at all, unlike every session-facing knowhow
        route. The request's notebook-access guard must resolve
        row -> table -> notebook BEFORE it can even check access, since
        there is no other source of that information in the request).
        Returns ``None`` when the row does not exist — mirrors
        ``get_notebook_asset``'s "auxiliary lookup, caller decides" contract
        rather than ``get_knowhow_table``'s KeyError-on-missing convention
        (this is a lookup a caller is expected to probe defensively, not one
        that assumes its target already exists)."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT r.table_id AS table_id, t.notebook_id AS notebook_id "
                "FROM knowhow_rows r JOIN knowhow_tables t ON t.id = r.table_id "
                "WHERE r.id = ?",
                (row_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_knowhow_cell_code(self, table_id: str) -> list[dict]:
        """Every code attachment in one table, in (row position, column
        position) order — the grid's own reading order, so UI badge
        aggregation and the agent surface consume it without re-sorting.
        Joins resolve the table scope through knowhow_rows (cell_code rows
        carry no table_id of their own)."""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT cc.* FROM knowhow_cell_code cc "
                "JOIN knowhow_rows r ON r.id = cc.row_id "
                "JOIN knowhow_columns col ON col.id = cc.column_id "
                "WHERE r.table_id = ? "
                "ORDER BY r.position, r.id, col.position, col.id",
                (table_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------- assets
    def insert_notebook_asset(
        self,
        notebook_id: str,
        filename: str,
        mime: str,
        size: int,
        created_by: str,
        source_id: str | None = None,
    ) -> str:
        asset_id = self.new_id("asset")
        with self.database.write() as db:
            db.execute(
                "INSERT INTO notebook_assets "
                "(id, notebook_id, filename, mime, size, created_by, created_at, source_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (asset_id, notebook_id, filename, mime, size, created_by, self.now(), source_id),
            )
        return asset_id

    def get_notebook_asset(self, asset_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM notebook_assets WHERE id = ?", (asset_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def source_asset_ids(self, source_id: str) -> list[str]:
        """Every ``notebook_assets.id`` linked to ``source_id`` (MinerU
        embedded-image extraction) — the read half of the source-view
        rendering + delete/reparse cascade cleanup pair below."""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT id FROM notebook_assets WHERE source_id = ?", (source_id,)
            ).fetchall()
        return [row[0] for row in rows]

    def delete_source_asset_rows(self, source_id: str) -> list[str]:
        """Delete every ``notebook_assets`` row linked to ``source_id`` and
        return the deleted asset ids so the caller can also remove their
        on-disk files (this store is rows-only, same division of labor as
        the notebook-delete/sweep_orphan_assets asset-GC paths in
        ``AssetService``/``maintenance``)."""
        ids = self.source_asset_ids(source_id)
        if ids:
            with self.database.write() as db:
                db.execute(
                    "DELETE FROM notebook_assets WHERE source_id = ?", (source_id,)
                )
        return ids


__all__ = ["KnowhowStore", "VALID_KINDS"]
