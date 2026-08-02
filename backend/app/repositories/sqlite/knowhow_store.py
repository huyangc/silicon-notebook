from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Callable, Sequence

from app.repositories.knowhow_asset_refs import required_asset_ids
from app.repositories.ports import CATALOG_MAX_CANDIDATE_PAGE, KNOWHOW_COLUMN_KINDS
from app.repositories.sqlite.anchor_normalization import js_trim, sqlite_js_trim_expression
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.knowhow_history_store import record_change


#: Legal ``knowhow_columns.role`` values post-migration-17 (design doc §①
#: "角色词表(2026-07-15 修订)": domain-neutral behavior kinds replacing the
#: PR-1 time-series-fixup-instance vocabulary; the migration remaps stored
#: legacy values). ``anchor`` marks "this column is the row-title column".
VALID_KINDS = KNOWHOW_COLUMN_KINDS
#: The content kinds a per-column mutation (``add_knowhow_column`` /
#: ``set_knowhow_column_kind``) may write. ``anchor`` is deliberately absent:
#: post-creation it is a TABLE-level designation ("which column is the row
#: title"), written only by ``set_knowhow_anchor_column`` so the at-most-one
#: invariant has a single enforcement point. ``create_knowhow_table`` is the
#: one exception — its initial column list may name the anchor inline (it
#: validates the at-most-one rule itself).
_NON_ANCHOR_KINDS = frozenset({"procedure", "entity", "attribute"})

# Complete-set Ask retrieval advances through physical Knowhow rows instead
# of reusing the unbounded editor detail read.  These limits are deliberately
# store-owned protocol constants: callers may request a smaller page, never a
# larger one, and must first select a small table/column scope.
KNOWHOW_ENUMERATION_PAGE_SIZE_DEFAULT = 25
KNOWHOW_ENUMERATION_PAGE_SIZE_MAX = 50
KNOWHOW_ENUMERATION_MAX_TABLE_IDS = 8
KNOWHOW_ENUMERATION_MAX_COLUMN_IDS = 8


def _enumeration_ids(
    values: Sequence[str] | None,
    *,
    label: str,
    maximum: int,
    required: bool = False,
) -> tuple[str, ...] | None:
    """Normalize a bounded ID filter without silently widening its scope."""
    if values is None:
        if required:
            raise ValueError(f"{label} is required")
        return None
    if isinstance(values, str):
        values = (values,)
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw)
        if not value:
            raise ValueError(f"{label} must not contain an empty id")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    if required and not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > maximum:
        raise ValueError(f"{label} exceeds the maximum of {maximum}")
    return tuple(normalized)


def _enumeration_cursor(cursor: Mapping[str, object] | None) -> tuple[str, int, str] | None:
    """Validate the complete-set cursor's total ordering key.

    A row ``position`` is only stable within one table, so the table id is a
    required part of the cursor even though the row-local portion is the
    familiar ``(position, id)`` pair.
    """
    if cursor is None:
        return None
    table_id = cursor.get("table_id")
    position = cursor.get("position")
    row_id = cursor.get("id")
    if (
        not isinstance(table_id, str)
        or not table_id
        or isinstance(position, bool)
        or not isinstance(position, int)
        or not isinstance(row_id, str)
        or not row_id
    ):
        raise ValueError("cursor must contain table_id, integer position, and id")
    return table_id, position, row_id

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
    ``bump_knowhow_mutation_seq`` is also exposed standalone for callers such
    as the projector that need to bump it at a point of their own
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

    @staticmethod
    def _validated_table_definition(
        title: str, columns: list[dict]
    ) -> tuple[str, list[str], list[str]]:
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
        if sum(1 for kind in kinds if kind == "anchor") > 1:
            raise ValueError("至多一列可设为行标题列")
        return title, names, kinds

    def _insert_table_on(
        self,
        db,
        notebook_id: str,
        title: str,
        description: str,
        names: list[str],
        kinds: list[str],
        created_by: str,
        now: str,
    ) -> tuple[str, list[str]]:
        table_id = self.new_id("khtbl")
        db.execute(
            "INSERT INTO knowhow_tables "
            "(id, notebook_id, title, description, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (table_id, notebook_id, title, description or "", created_by or "", now, now),
        )
        column_ids: list[str] = []
        for position, name in enumerate(names):
            column_id = self.new_id("khcol")
            column_ids.append(column_id)
            db.execute(
                "INSERT INTO knowhow_columns (id, table_id, name, role, position) "
                "VALUES (?, ?, ?, ?, ?)",
                (column_id, table_id, name, kinds[position], position),
            )
        return table_id, column_ids

    def _insert_knowhow_row_on(
        self,
        db,
        table_id: str,
        cells: dict[str, str],
        position: int,
        now: str,
    ) -> str:
        row_id = self.new_id("khrow")
        db.execute(
            "INSERT INTO knowhow_rows (id, table_id, position, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (row_id, table_id, position, now, now),
        )
        for column_id, content_md in cells.items():
            db.execute(
                "INSERT INTO knowhow_cells (id, row_id, column_id, content_md, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.new_id("khcel"), row_id, column_id, content_md, now),
            )
        return row_id

    # ------------------------------------------------------------- tables
    def create_knowhow_table(
        self,
        notebook_id: str,
        title: str,
        description: str,
        columns: list[dict],
        created_by: str = "",
        actor: str = "",
        origin: str = "user",
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
        The table and its genesis history entry are committed atomically.
        """
        title, names, kinds = self._validated_table_definition(title, columns)
        now = self.now()
        description = description or ""
        with self.database.write() as db:
            table_id, column_ids = self._insert_table_on(
                db,
                notebook_id,
                title,
                description,
                names,
                kinds,
                created_by,
                now,
            )
            record_change(
                db,
                new_id=self.new_id,
                now=self.now,
                table_id=table_id,
                kind="table_create",
                payload={
                    "table": {"title": title, "description": description},
                    "columns": [
                        {
                            "id": column_id,
                            "name": names[position],
                            "role": kinds[position],
                            "position": position,
                        }
                        for position, column_id in enumerate(column_ids)
                    ],
                    "rows": [],
                },
                actor=actor,
                origin=origin,
            )
        return table_id

    def create_knowhow_table_with_rows(
        self,
        notebook_id: str,
        title: str,
        description: str,
        columns: list[dict],
        rows: Sequence[Sequence[str]],
        created_by: str = "",
        actor: str = "",
        origin: str = "user",
    ) -> str:
        """Atomically create an imported table, rows, and one history entry."""
        title, names, kinds = self._validated_table_definition(title, columns)
        normalized_rows = [list(row) for row in rows]
        if any(len(row) != len(names) for row in normalized_rows):
            raise ValueError("导入行列数与表结构不一致")
        now = self.now()
        with self.database.write() as db:
            self.database.begin_immediate(db)
            table_id, column_ids = self._insert_table_on(
                db,
                notebook_id,
                title,
                description,
                names,
                kinds,
                created_by,
                now,
            )
            self._require_assets_for_notebook(
                db,
                notebook_id,
                required_asset_ids(
                    (),
                    (
                        ("", content)
                        for row in normalized_rows
                        for content in row
                    ),
                ),
            )
            created_rows: list[dict] = []
            for position, row in enumerate(normalized_rows):
                cells = {
                    column_ids[index]: content
                    for index, content in enumerate(row)
                    if content
                }
                row_id = self._insert_knowhow_row_on(
                    db, table_id, cells, position, now
                )
                created_rows.append(
                    {
                        "row_id": row_id,
                        "position": position,
                        "created_at": now,
                        "cells": cells,
                        "code": [],
                    }
                )
            db.execute(
                "UPDATE knowhow_tables SET mutation_seq=mutation_seq+1 WHERE id=?",
                (table_id,),
            )
            record_change(
                db,
                new_id=self.new_id,
                now=self.now,
                table_id=table_id,
                kind="table_create",
                payload={
                    "table": {"title": title, "description": description},
                    "columns": [
                        {
                            "id": column_id,
                            "name": names[position],
                            "role": kinds[position],
                            "position": position,
                        }
                        for position, column_id in enumerate(column_ids)
                    ],
                    "rows": created_rows,
                },
                actor=actor,
                origin=origin,
            )
        return table_id

    def update_knowhow_table_meta(
        self,
        table_id: str,
        title: "str | None" = None,
        description: "str | None" = None,
        actor: str = "",
        origin: str = "user",
    ) -> None:
        """Patch title and/or description in place (each ``None`` argument
        leaves that field untouched — the editing API's PATCH semantics for
        an omitted field). A non-``None`` title is validated the same way
        ``create_knowhow_table`` validates its own title (non-empty after
        stripping), raising the same ``ValueError`` message. A ``None``
        description is left alone (there is no "clear description" case
        distinct from passing ``""`` explicitly, unlike title which can never
        legally become empty). Silent no-op if the table is already gone
        (this codebase's zero-row UPDATE convention).

        版本管理（spec §5）：the all-``None`` early return below stays
        UNRECORDED — no transaction even opens, nothing changed. Once inside
        the transaction, ``before`` is read via a SELECT preceding the
        UPDATE; ``after`` is "before overlaid with this call's patch" (a
        field left ``None`` keeps its ``before`` value in ``after`` too —
        it never collapses to ``null``, matching the PATCH semantics above).
        A table that vanished between the caller's check and this call hits
        the same silent no-op the bare UPDATE always was — the SELECT
        finding nothing skips both the UPDATE and the record."""
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
            before_row = db.execute(
                "SELECT title, description FROM knowhow_tables WHERE id = ?",
                (table_id,),
            ).fetchone()
            if before_row is None:
                return  # already gone — nothing updated, nothing to record
            db.execute(
                f"UPDATE knowhow_tables SET {', '.join(sets)} WHERE id = ?", params
            )
            record_change(
                db, new_id=self.new_id, now=self.now, table_id=table_id,
                kind="table_meta",
                payload={
                    "before": {
                        "title": before_row["title"],
                        "description": before_row["description"],
                    },
                    "after": {
                        "title": title if title is not None else before_row["title"],
                        "description": (
                            description if description is not None
                            else before_row["description"]
                        ),
                    },
                },
                actor=actor, origin=origin,
            )

    # ------------------------------------------------------------ columns
    def set_knowhow_anchor_column(
        self,
        table_id: str,
        column_id: "str | None",
        actor: str = "",
        origin: str = "user",
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
        a missing/anchorless table is a silent no-op returning ``None``.

        版本管理（spec §5）：a real move records ONE ``anchor_set`` entry
        whose ``columns`` carries up to two sub-entries — the demoted OLD
        anchor (``before="anchor"``, ``after="attribute"``) and the promoted
        NEW column (``before=`` its role read BEFORE promotion, ``after=
        "anchor"``). Clearing the anchor (``column_id=None``) only ever
        produces the demotion half. The no-op branch above (``column_id ==
        old_id``, including ``None -> None``) returns before this point on
        purpose — nothing changed, so nothing is recorded."""
        with self.database.write() as db:
            old_row = db.execute(
                "SELECT id FROM knowhow_columns WHERE table_id = ? AND role = 'anchor'",
                (table_id,),
            ).fetchone()
            old_id = old_row["id"] if old_row is not None else None
            new_role_before = None
            if column_id is not None:
                target = db.execute(
                    "SELECT table_id, role FROM knowhow_columns WHERE id = ?",
                    (column_id,),
                ).fetchone()
                if target is None or target["table_id"] != table_id:
                    raise ValueError("指定的列不属于本表")
                new_role_before = target["role"]
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
            columns = []
            if old_id is not None:
                columns.append({
                    "column_id": old_id, "before": "anchor", "after": "attribute",
                })
            if column_id is not None:
                columns.append({
                    "column_id": column_id, "before": new_role_before, "after": "anchor",
                })
            record_change(
                db, new_id=self.new_id, now=self.now, table_id=table_id,
                kind="anchor_set", payload={"columns": columns},
                actor=actor, origin=origin,
            )
        return old_id

    def add_knowhow_column(
        self,
        table_id: str,
        name: str,
        kind: str,
        position: "int | None" = None,
        actor: str = "",
        origin: str = "user",
    ) -> str:
        """Append a column (default position = MAX(position)+1 — NOT the
        column count: ``delete_knowhow_column`` leaves position gaps, and a
        COUNT-based default would collide with a surviving higher position).
        ``kind`` must be a content kind (``_NON_ANCHOR_KINDS``); the anchor
        designation only moves via ``set_knowhow_anchor_column``. The name is
        validated like ``create_knowhow_table``'s (non-empty, unique within
        the table). Existing rows get NO backfilled cells — cells are sparse
        by contract (see ``get_knowhow_table``). Raises ``KeyError`` for a
        missing table.

        版本管理（spec §5）：records ``column_add`` in the same transaction as
        the INSERT — ``before`` is implicitly empty (the column did not exist),
        so the payload only carries the new definition."""
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
            record_change(
                db, new_id=self.new_id, now=self.now, table_id=table_id,
                kind="column_add",
                payload={"column": {
                    "id": column_id, "name": name, "role": kind, "position": position,
                }},
                actor=actor, origin=origin,
            )
        return column_id

    def rename_knowhow_column(
        self, column_id: str, name: str, actor: str = "", origin: str = "user"
    ) -> None:
        """Rename one column (stored stripped). Raises ``KeyError`` for a
        missing column; ``ValueError`` for a blank name or a name already
        used by a SIBLING column (renaming to its own current name is a
        silent success, not a duplicate).

        版本管理（spec §5）：records ``column_rename`` with the pre-write
        name as ``before``. Renaming to the SAME name is the silent-success
        no-op documented above — it returns before touching the row, so it
        neither writes nor records anything (not even a same-value UPDATE)."""
        name = str(name or "").strip()
        if not name:
            raise ValueError("列名不能为空")
        with self.database.write() as db:
            row = db.execute(
                "SELECT table_id, name FROM knowhow_columns WHERE id = ?", (column_id,)
            ).fetchone()
            if row is None:
                raise KeyError(column_id)
            before = row["name"]
            if name == before:
                return  # silent success, nothing changed — see docstring
            duplicate = db.execute(
                "SELECT 1 FROM knowhow_columns WHERE table_id = ? AND name = ? AND id != ?",
                (row["table_id"], name, column_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("列名不能重复")
            db.execute(
                "UPDATE knowhow_columns SET name = ? WHERE id = ?", (name, column_id)
            )
            record_change(
                db, new_id=self.new_id, now=self.now, table_id=row["table_id"],
                kind="column_rename",
                payload={"column_id": column_id, "before": before, "after": name},
                actor=actor, origin=origin,
            )

    def set_knowhow_column_kind(
        self, column_id: str, kind: str, actor: str = "", origin: str = "user"
    ) -> None:
        """Change one column's content kind. ``anchor`` is rejected here —
        the anchor designation is table-level state with its own setter
        (``set_knowhow_anchor_column``), which is also the only way a column
        currently marked ``anchor`` legitimately changes kind (clearing or
        moving the anchor demotes it to ``attribute``). Raises ``KeyError``
        for a missing column.

        版本管理（spec §5）：records ``column_kind`` with the pre-write role
        as ``before``. Setting the SAME kind it already has is a silent
        no-op — mirrors ``rename_knowhow_column``'s same-value convention —
        and records nothing."""
        if kind == "anchor":
            raise ValueError("行标题列请通过表设置指定，不能作为列类型修改")
        if kind not in _NON_ANCHOR_KINDS:
            raise ValueError(f"非法的列类型：{kind}")
        with self.database.write() as db:
            row = db.execute(
                "SELECT table_id, role FROM knowhow_columns WHERE id = ?", (column_id,)
            ).fetchone()
            if row is None:
                raise KeyError(column_id)
            before = row["role"]
            if kind == before:
                return  # silent no-op, nothing changed — see docstring
            db.execute(
                "UPDATE knowhow_columns SET role = ? WHERE id = ?", (kind, column_id)
            )
            record_change(
                db, new_id=self.new_id, now=self.now, table_id=row["table_id"],
                kind="column_kind",
                payload={"column_id": column_id, "before": before, "after": kind},
                actor=actor, origin=origin,
            )

    def delete_knowhow_column(
        self, column_id: str, actor: str = "", origin: str = "user"
    ) -> None:
        """Delete a column; its cells and cell-code attachments go with it
        (``knowhow_cells.column_id`` and ``knowhow_cell_code.column_id`` are
        both ``ON DELETE CASCADE`` onto knowhow_columns, and ``PRAGMA
        foreign_keys = ON`` is set on every connection by SqliteDatabase).
        Deleting the anchor column simply leaves the table anchorless — a
        legal state (see ``create_knowhow_table``). Silent no-op when already
        gone. Remaining positions are NOT renumbered (ordering reads sort by
        position and tolerate gaps).

        版本管理（spec §5）：CASCADE means the column's cells and code
        attachments are gone the instant the DELETE below commits, so
        EVERYTHING needed to rebuild the column (its own definition, every
        cell it held across every row, every code attachment) is read BEFORE
        the DELETE and stored in the ``column_delete`` payload — the only way
        this change stays reversible. The silent no-op above is preserved
        verbatim: nothing was deleted, so nothing is recorded."""
        with self.database.write() as db:
            column = db.execute(
                "SELECT id, table_id, name, role, position FROM knowhow_columns "
                "WHERE id = ?",
                (column_id,),
            ).fetchone()
            if column is None:
                return  # already gone — nothing deleted, nothing to record
            cells = [
                dict(c)
                for c in db.execute(
                    "SELECT row_id, content_md FROM knowhow_cells WHERE column_id = ?",
                    (column_id,),
                ).fetchall()
            ]
            code = [
                dict(c)
                for c in db.execute(
                    "SELECT row_id, code_text, language, updated_by, cell_content_hash "
                    "FROM knowhow_cell_code WHERE column_id = ?",
                    (column_id,),
                ).fetchall()
            ]
            db.execute("DELETE FROM knowhow_columns WHERE id = ?", (column_id,))
            record_change(
                db, new_id=self.new_id, now=self.now, table_id=column["table_id"],
                kind="column_delete",
                payload={
                    "column": {
                        "id": column["id"], "name": column["name"],
                        "role": column["role"], "position": column["position"],
                    },
                    "cells": cells, "code": code,
                },
                actor=actor, origin=origin,
            )

    def knowhow_table_health_inputs(self, notebook_id: str) -> list[dict]:
        with self.database.connect() as db:
            tables = db.execute(
                "SELECT id,notebook_id,title,description,created_at,updated_at,"
                "mutation_seq,hidden_source_id,created_by "
                "FROM knowhow_tables WHERE notebook_id=? ORDER BY created_at,id",
                (notebook_id,),
            ).fetchall()
            table_ids = [row["id"] for row in tables]
            row_stats = {}
            cell_activity = {}
            code_inputs = {table_id: [] for table_id in table_ids}
            if table_ids:
                placeholders = ",".join("?" for _ in table_ids)
                row_stats = {
                    row["table_id"]: dict(row)
                    for row in db.execute(
                        "SELECT table_id,COUNT(*) AS row_count,"
                        "COALESCE(SUM(CASE WHEN projection_status IN ('pending','syncing') "
                        "THEN 1 ELSE 0 END),0) "
                        "AS projection_pending,"
                        "COALESCE(SUM(CASE WHEN projection_status='failed' THEN 1 ELSE 0 END),0) "
                        "AS projection_failed,MAX(updated_at) AS row_activity_at "
                        f"FROM knowhow_rows WHERE table_id IN ({placeholders}) GROUP BY table_id",
                        table_ids,
                    ).fetchall()
                }
                cell_activity = {
                    row["table_id"]: row["cell_activity_at"] or ""
                    for row in db.execute(
                        "SELECT r.table_id,MAX(c.updated_at) AS cell_activity_at "
                        "FROM knowhow_cells c JOIN knowhow_rows r ON r.id=c.row_id "
                        f"WHERE r.table_id IN ({placeholders}) GROUP BY r.table_id",
                        table_ids,
                    ).fetchall()
                }
                for row in db.execute(
                    "SELECT r.table_id,cc.cell_content_hash AS saved_hash,"
                    "COALESCE(c.content_md,'') AS current_content_md,cc.updated_at "
                    "FROM knowhow_cell_code cc "
                    "JOIN knowhow_rows r ON r.id=cc.row_id "
                    "LEFT JOIN knowhow_cells c "
                    "ON c.row_id=cc.row_id AND c.column_id=cc.column_id "
                    f"WHERE r.table_id IN ({placeholders})",
                    table_ids,
                ).fetchall():
                    code_inputs[row["table_id"]].append(dict(row))
        result = []
        for table in tables:
            stats = row_stats.get(table["id"], {})
            result.append({
                **dict(table),
                "row_count": int(stats.get("row_count", 0)),
                "projection_pending": int(stats.get("projection_pending", 0)),
                "projection_failed": int(stats.get("projection_failed", 0)),
                "row_activity_at": stats.get("row_activity_at") or "",
                "cell_activity_at": cell_activity.get(table["id"], ""),
                "code_inputs": code_inputs[table["id"]],
            })
        return result

    def list_knowhow_tables(self, notebook_id: str) -> list[dict]:
        return self.knowhow_table_health_inputs(notebook_id)

    def knowhow_table_id_by_title(self, notebook_id: str, title: str) -> str:
        """See ``KnowhowStorePort.knowhow_table_id_by_title`` for the
        contract. A bounded point lookup on ``(notebook_id, title)`` — never
        the health-aggregated ``list_knowhow_tables`` scan a title lookup
        (command-catalog's by-title table resolution) has no use for.

        Ties (more than one table sharing the title) resolve to the same row
        ``list_knowhow_tables`` would surface first: earliest ``created_at``,
        then ``id`` — matching that method's own ``ORDER BY``.
        """
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id FROM knowhow_tables WHERE notebook_id=? AND title=? "
                "ORDER BY created_at,id LIMIT 1",
                (notebook_id, title),
            ).fetchone()
        return str(row["id"]) if row is not None else ""

    def knowhow_enumeration_catalog(
        self, notebook_id: str, *, limit: int = 8, query: str = ""
    ) -> dict:
        """Read bounded enumeration metadata without health/code hydration."""
        effective_limit = max(1, min(int(limit), 8))
        query_key = str(query or "").casefold()
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT t.id,t.title,t.mutation_seq,"
                "(SELECT COUNT(*) FROM knowhow_rows r WHERE r.table_id=t.id) AS row_count,"
                "(SELECT COALESCE(MAX(ch.seq),0) FROM knowhow_changes ch "
                "WHERE ch.table_id=t.id) AS enumeration_seq "
                "FROM knowhow_tables t WHERE t.notebook_id=? "
                "ORDER BY CASE WHEN ?<>'' AND instr(?,lower(t.title))>0 "
                "THEN 0 ELSE 1 END,lower(t.title),t.id COLLATE BINARY LIMIT ?",
                (notebook_id, query_key, query_key, effective_limit),
            ).fetchall()
            aggregate = db.execute(
                "WITH catalog AS ("
                "SELECT t.id,t.mutation_seq,"
                "(SELECT COUNT(*) FROM knowhow_rows r WHERE r.table_id=t.id) AS row_count,"
                "(SELECT COALESCE(MAX(ch.seq),0) FROM knowhow_changes ch "
                "WHERE ch.table_id=t.id) AS enumeration_seq "
                "FROM knowhow_tables t WHERE t.notebook_id=?"
                ") SELECT COUNT(*) AS known_tables,"
                "COALESCE(SUM(row_count),0) AS known_total_rows,"
                "COALESCE(SUM(mutation_seq),0) AS mutation_sum,"
                "COALESCE(SUM(enumeration_seq),0) AS enumeration_sum FROM catalog",
                (notebook_id,),
            ).fetchone()
        return {
            "tables": [dict(row) for row in rows],
            "known_tables": int(aggregate["known_tables"] or 0),
            "known_total_rows": int(aggregate["known_total_rows"] or 0),
            "fingerprint": [
                int(aggregate["known_tables"] or 0),
                int(aggregate["known_total_rows"] or 0),
                int(aggregate["mutation_sum"] or 0),
                int(aggregate["enumeration_sum"] or 0),
            ],
        }

    def enumerate_knowhow_rows(
        self,
        notebook_id: str,
        *,
        table_ids: Sequence[str],
        cursor: Mapping[str, object] | None = None,
        page_size: int = KNOWHOW_ENUMERATION_PAGE_SIZE_DEFAULT,
        column_ids: Sequence[str] | None = None,
    ) -> dict:
        """Read one bounded, stable page for complete-set Knowhow retrieval.

        ``table_ids`` is deliberately required and capped at
        :data:`KNOWHOW_ENUMERATION_MAX_TABLE_IDS`; the caller must select the
        relevant tables from the cheap summary list before it can enumerate
        rows.  The returned catalog includes those selected tables only when
        they belong to ``notebook_id`` (foreign ids are indistinguishable from
        missing ids).  ``column_ids=None`` means every column in that catalog;
        a supplied list is deduplicated and capped at
        :data:`KNOWHOW_ENUMERATION_MAX_COLUMN_IDS`.

        Pagination orders physical rows by ``table_id, position, id``.  A
        page always advances through physical rows, including rows with no
        selected non-empty cells, so callers can prove coverage rather than
        mistaking a relevance-style result window for an exhaustive scan.
        ``page_size`` is clamped to ``1..50`` (default 25).  No transaction is
        held across pages: callers that need a retry on concurrent edits can
        compare the catalog's per-table ``mutation_seq`` values.
        """
        selected_tables = _enumeration_ids(
            table_ids,
            label="table_ids",
            maximum=KNOWHOW_ENUMERATION_MAX_TABLE_IDS,
            required=True,
        )
        selected_columns = _enumeration_ids(
            column_ids,
            label="column_ids",
            maximum=KNOWHOW_ENUMERATION_MAX_COLUMN_IDS,
        )
        stable_cursor = _enumeration_cursor(cursor)
        effective_page_size = max(
            1, min(int(page_size), KNOWHOW_ENUMERATION_PAGE_SIZE_MAX)
        )
        table_placeholders = ",".join("?" for _ in selected_tables)
        table_scope = (
            f"t.notebook_id = ? AND t.id IN ({table_placeholders})"
        )
        table_scope_params: list[object] = [notebook_id, *selected_tables]

        with self.database.connect() as db:
            catalog_rows = db.execute(
                "SELECT t.id,t.notebook_id,t.title,t.description,t.mutation_seq,"
                "(SELECT COUNT(*) FROM knowhow_rows rr WHERE rr.table_id=t.id) "
                "AS row_count,"
                "(SELECT COALESCE(MAX(ch.seq),0) FROM knowhow_changes ch "
                "WHERE ch.table_id=t.id) AS enumeration_seq "
                "FROM knowhow_tables t "
                f"WHERE {table_scope} ORDER BY t.id COLLATE BINARY",
                table_scope_params,
            ).fetchall()
            visible_table_ids = [str(row["id"]) for row in catalog_rows]
            if not visible_table_ids:
                return {
                    "tables": [],
                    "rows": [],
                    "next_cursor": None,
                    "has_more": False,
                    "page_size": effective_page_size,
                    "counts": {
                        "scope_total_rows": 0,
                        "scope_nonempty_rows": 0,
                        "page_rows": 0,
                    },
                }

            visible_placeholders = ",".join("?" for _ in visible_table_ids)
            column_rows = db.execute(
                "SELECT id,table_id,name,role,position FROM knowhow_columns "
                f"WHERE table_id IN ({visible_placeholders}) "
                "ORDER BY table_id COLLATE BINARY,position,id COLLATE BINARY",
                visible_table_ids,
            ).fetchall()
            columns_by_table: dict[str, list[dict]] = {
                table_id: [] for table_id in visible_table_ids
            }
            for column in column_rows:
                columns_by_table[str(column["table_id"])].append(
                    {
                        "id": column["id"],
                        "name": column["name"],
                        "role": column["role"],
                        "position": column["position"],
                    }
                )

            visible_scope = (
                f"t.notebook_id = ? AND t.id IN ({visible_placeholders})"
            )
            visible_scope_params: list[object] = [notebook_id, *visible_table_ids]
            selected_cell_clause = ""
            selected_cell_params: list[object] = []
            if selected_columns is not None:
                if not selected_columns:
                    selected_cell_clause = " AND 1=0"
                else:
                    selected_cell_clause = (
                        " AND c.column_id IN ("
                        + ",".join("?" for _ in selected_columns)
                        + ")"
                    )
                    selected_cell_params.extend(selected_columns)

            counts = db.execute(
                "SELECT COUNT(*) AS scope_total_rows,COALESCE(SUM(CASE WHEN EXISTS ("
                "SELECT 1 FROM knowhow_cells c "
                "JOIN knowhow_columns kc "
                "ON kc.id=c.column_id AND kc.table_id=r.table_id "
                "WHERE c.row_id=r.id AND c.content_md<>''"
                f"{selected_cell_clause}"
                ") THEN 1 ELSE 0 END),0) AS scope_nonempty_rows "
                "FROM knowhow_rows r JOIN knowhow_tables t ON t.id=r.table_id "
                f"WHERE {visible_scope}",
                [*selected_cell_params, *visible_scope_params],
            ).fetchone()

            cursor_clause = ""
            cursor_params: list[object] = []
            if stable_cursor is not None:
                cursor_table_id, cursor_position, cursor_row_id = stable_cursor
                cursor_clause = (
                    " AND (t.id COLLATE BINARY > ? OR "
                    "(t.id = ? AND (r.position > ? OR "
                    "(r.position = ? AND r.id COLLATE BINARY > ?))))"
                )
                cursor_params.extend(
                    [
                        cursor_table_id,
                        cursor_table_id,
                        cursor_position,
                        cursor_position,
                        cursor_row_id,
                    ]
                )
            row_rows = db.execute(
                "SELECT r.id,r.table_id,r.position FROM knowhow_rows r "
                "JOIN knowhow_tables t ON t.id=r.table_id "
                f"WHERE {visible_scope}{cursor_clause} "
                "ORDER BY t.id COLLATE BINARY,r.position,r.id COLLATE BINARY LIMIT ?",
                [*visible_scope_params, *cursor_params, effective_page_size + 1],
            ).fetchall()
            has_more = len(row_rows) > effective_page_size
            page_row_rows = row_rows[:effective_page_size]
            page_row_ids = [str(row["id"]) for row in page_row_rows]
            cells_by_row: dict[str, dict[str, str]] = {
                row_id: {} for row_id in page_row_ids
            }
            if page_row_ids:
                row_placeholders = ",".join("?" for _ in page_row_ids)
                page_cell_clause = ""
                page_cell_params: list[object] = []
                if selected_columns is not None:
                    if not selected_columns:
                        page_cell_clause = " AND 1=0"
                    else:
                        page_cell_clause = (
                            " AND c.column_id IN ("
                            + ",".join("?" for _ in selected_columns)
                            + ")"
                        )
                        page_cell_params.extend(selected_columns)
                cell_rows = db.execute(
                    "SELECT c.row_id,c.column_id,c.content_md FROM knowhow_cells c "
                    "JOIN knowhow_rows r ON r.id=c.row_id "
                    "JOIN knowhow_columns kc "
                    "ON kc.id=c.column_id AND kc.table_id=r.table_id "
                    f"WHERE c.row_id IN ({row_placeholders}) AND c.content_md<>''"
                    f"{page_cell_clause} "
                    "ORDER BY r.table_id COLLATE BINARY,r.position,"
                    "r.id COLLATE BINARY,kc.position,kc.id COLLATE BINARY",
                    [*page_row_ids, *page_cell_params],
                ).fetchall()
                for cell in cell_rows:
                    cells_by_row[str(cell["row_id"])][str(cell["column_id"])] = (
                        str(cell["content_md"])
                    )

        next_cursor = None
        if has_more and page_row_rows:
            last_row = page_row_rows[-1]
            next_cursor = {
                "table_id": last_row["table_id"],
                "position": int(last_row["position"]),
                "id": last_row["id"],
            }
        return {
            "tables": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "description": row["description"],
                    "mutation_seq": int(row["mutation_seq"]),
                    "enumeration_seq": int(row["enumeration_seq"]),
                    "row_count": int(row["row_count"]),
                    "columns": columns_by_table[str(row["id"])],
                }
                for row in catalog_rows
            ],
            "rows": [
                {
                    "id": row["id"],
                    "table_id": row["table_id"],
                    "position": int(row["position"]),
                    "cells": cells_by_row[str(row["id"])],
                }
                for row in page_row_rows
            ],
            "next_cursor": next_cursor,
            "has_more": has_more,
            "page_size": effective_page_size,
            "counts": {
                "scope_total_rows": int(counts["scope_total_rows"]),
                "scope_nonempty_rows": int(counts["scope_nonempty_rows"]),
                "page_rows": len(page_row_rows),
            },
        }

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

    def knowhow_table_columns(self, table_id: str) -> list[dict]:
        """A table's columns alone — never its rows or cells. Raises
        ``KeyError`` if the table is gone, same as ``get_knowhow_table``, so a
        caller that only needs "does this still exist, and what are its
        columns" (command-catalog apply resolving a remembered target) never
        pays for hydrating rows/cells it does not need — a table can hold
        thousands of rows; its column list never scales with them."""
        with self.database.connect() as db:
            if db.execute(
                "SELECT 1 FROM knowhow_tables WHERE id = ?", (table_id,)
            ).fetchone() is None:
                raise KeyError(table_id)
            column_rows = db.execute(
                "SELECT id, name, role, position FROM knowhow_columns "
                "WHERE table_id = ? ORDER BY position, id",
                (table_id,),
            ).fetchall()
        return [
            {
                "id": column["id"],
                "name": column["name"],
                "role": column["role"],
                "position": column["position"],
            }
            for column in column_rows
        ]

    def knowhow_table_title(self, table_id: str) -> str:
        """See ``KnowhowStorePort.knowhow_table_title``. One point lookup by
        primary key, no rows/cells/columns touched."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT title FROM knowhow_tables WHERE id = ?", (table_id,)
            ).fetchone()
        if row is None:
            raise KeyError(table_id)
        return str(row["title"])

    def knowhow_anchor_existing_values(
        self, column_id: str, values: Sequence[str]
    ) -> set[str]:
        """Which of `values` already have a row in `column_id`'s column,
        bounded to `CATALOG_MAX_CANDIDATE_PAGE` values and answered with one
        indexed lookup — never a scan of the target table's rows.

        `column_id` alone scopes the query to exactly one table (a column
        belongs to exactly one table), so this reuses the v21 normalized-
        anchor expression index (`column_id`, JS-trimmed `content_md`,
        `row_id`) on its leading two columns without joining `knowhow_rows`
        at all. Trimmed on both sides so a manually-edited cell carrying
        incidental whitespace still matches — the same normalization the
        guarded-write anchor-membership check already applies.
        """
        wanted = [
            value
            for value in dict.fromkeys(
                js_trim(str(item)) for item in list(values)[:CATALOG_MAX_CANDIDATE_PAGE]
            )
            if value
        ]
        if not wanted:
            return set()
        normalized = sqlite_js_trim_expression("content_md")
        placeholders = ",".join("?" for _ in wanted)
        with self.database.connect() as db:
            rows = db.execute(
                f"SELECT DISTINCT {normalized} AS value FROM knowhow_cells "
                f"WHERE column_id = ? AND {normalized} IN ({placeholders})",
                (column_id, *wanted),
            ).fetchall()
        return {row["value"] for row in rows}

    def table_exists(self, table_id: str) -> bool:
        """Cheap existence probe (a single ``SELECT 1``, not the full
        columns/rows/cells hydration ``get_knowhow_table`` does) — for a
        caller that only needs to know whether the row is still there, e.g.
        ``KnowhowProjector.project_table``'s pre-terminal-write re-check
        guarding against a concurrent ``delete_knowhow_table`` (a move or a
        plain delete) landing mid-pass."""
        with self.database.connect() as db:
            return db.execute(
                "SELECT 1 FROM knowhow_tables WHERE id = ?", (table_id,)
            ).fetchone() is not None

    @staticmethod
    def table_exists_tx(connection: sqlite3.Connection, table_id: str) -> bool:
        """Same probe as ``table_exists``, but against a caller-supplied
        connection instead of opening its own ``database.connect()`` —
        mirrors ``KnowledgeStore``'s tx-scoped statics (e.g.
        ``delete_relations_by_source``). For a caller that must check-then-
        write atomically inside its OWN ``database.write()`` block, so the
        check and the write share one lock-held transaction with no gap a
        concurrent delete could land in.

        PR review round 2 P1-1: ``project_table``'s terminal existence guard
        used to call ``table_exists``/``source_exists`` BEFORE opening its
        ``database.write()`` block — a real check-then-act gap (a concurrent
        move's ``delete_knowhow_table`` could land between the check
        returning True and the write lock being acquired, letting the
        terminal write commit an orphan the guard existed to prevent). Moving
        the check to run on the write block's own connection, as the first
        thing inside it, closes that gap: the whole block holds
        ``database.write_lock`` continuously, so a concurrent delete (which
        needs its own ``write()``) cannot land inside the window at all —
        it either fully lands before this block starts (check correctly
        observes "gone") or blocks until this block finishes."""
        return connection.execute(
            "SELECT 1 FROM knowhow_tables WHERE id = ?", (table_id,)
        ).fetchone() is not None

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

    def set_knowhow_hidden_source(
        self,
        table_id: str,
        source_id: str,
        *,
        connection: "sqlite3.Connection | None" = None,
    ) -> None:
        """Attach ``source_id`` as ``table_id``'s hidden source.

        PR review round 7 P2-A: pass ``connection`` to join a caller-owned
        write transaction — mirrors ``SourceStore.insert_source``'s/
        ``update_file_hash``'s own ``connection=`` idiom exactly (build the
        statement/params once, execute directly on the caller's connection
        if given, otherwise open this method's own ``write()``). Without a
        connection, the update commits in its own transaction; ``Knowhow
        Projector.ensure_hidden_source`` passes the SAME connection it used
        to insert the ``sources`` row a moment earlier, so creation and
        attachment land in ONE transaction — see that method's own
        docstring for why the two used to be separate and what that gap
        could leak.

        PR review round 8 P1-A: on the ``connection=`` path, raises
        ``KeyError(table_id)`` if the UPDATE's ``rowcount`` is 0 — i.e.
        ``table_id`` no longer exists. Round 7 made the sibling
        ``insert_source`` call and this UPDATE atomic (one transaction),
        but atomicity alone doesn't stop either statement from succeeding
        on its own terms: SQLite does not raise on a zero-row UPDATE, so
        without this check, a concurrent full delete landing between
        ``ensure_hidden_source``'s existence check and this transaction
        actually acquiring the write lock would let ``insert_source``'s
        INSERT commit anyway — an unreferenced, permanently orphaned
        ``source_type='knowhow'`` row, born AFTER the move's one-shot
        orphan sweep already ran (so nothing will ever clean it up; see
        ``KnowhowProjector.ensure_hidden_source``'s own docstring for the
        full race). Raising here rolls back the WHOLE caller-owned
        transaction — the ``with conn:`` both calls share rolls back on any
        exception — so ``insert_source``'s INSERT is undone too; no source
        row survives.

        Deliberately scoped to the ``connection=`` path only. The public,
        no-connection form below keeps its original silently-tolerant
        zero-row behavior verbatim — this codebase's default zero-row
        UPDATE/DELETE convention — for its OTHER callers (grepped:
        ``test_knowhow_store.py``'s three direct calls all target a table
        that still exists at call time; nothing exercises, or depends on,
        the old tolerant behavior for an already-gone ``table_id``), so
        this new invariant applies exactly where it was found missing and
        nowhere else."""
        statement = (
            "UPDATE knowhow_tables SET hidden_source_id = ?, updated_at = ? "
            "WHERE id = ?"
        )
        values = (source_id, self.now(), table_id)
        if connection is not None:
            cursor = connection.execute(statement, values)
            if cursor.rowcount == 0:
                raise KeyError(table_id)
            return
        with self.database.write() as db:
            db.execute(statement, values)

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
        self,
        table_id: str,
        cells: dict[str, str],
        position: int | None = None,
        actor: str = "",
        origin: str = "user",
    ) -> str:
        """Insert a row (default position = current row count, i.e.
        append) plus any provided cells, in one write transaction.
        ``projection_status`` starts at its schema default (``'pending'``).
        It records one ``row_add`` entry in the same transaction and does not
        bump ``mutation_seq``; aggregate callers use the atomic batch port.
        """
        now = self.now()
        with self.database.write() as db:
            self.database.begin_immediate(db)
            table = db.execute(
                "SELECT notebook_id FROM knowhow_tables WHERE id=?", (table_id,)
            ).fetchone()
            if table is None:
                raise KeyError(table_id)
            valid_columns = {
                row["id"]
                for row in db.execute(
                    "SELECT id FROM knowhow_columns WHERE table_id=?", (table_id,)
                ).fetchall()
            }
            if any(column_id not in valid_columns for column_id in (cells or {})):
                raise ValueError("单元格列不属于本表")
            self._require_assets_for_notebook(
                db,
                str(table["notebook_id"]),
                required_asset_ids(
                    (), (("", content) for content in (cells or {}).values())
                ),
            )
            if position is None:
                count_row = db.execute(
                    "SELECT COUNT(*) AS n FROM knowhow_rows WHERE table_id = ?",
                    (table_id,),
                ).fetchone()
                position = count_row["n"]
            written_cells = dict(cells or {})
            row_id = self._insert_knowhow_row_on(
                db, table_id, written_cells, position, now
            )
            record_change(
                db,
                new_id=self.new_id,
                now=self.now,
                table_id=table_id,
                kind="row_add",
                payload={
                    "rows": [
                        {
                            "row_id": row_id,
                            "position": position,
                            "created_at": now,
                            "cells": written_cells,
                            "code": [],
                        }
                    ]
                },
                actor=actor,
                origin=origin,
            )
        return row_id

    def append_knowhow_rows(
        self,
        table_id: str,
        rows: Sequence[dict[str, str]],
        actor: str = "",
        origin: str = "user",
    ) -> list[str]:
        """Append a batch, bump once, and record one entry atomically."""
        batch_rows = [dict(row) for row in rows]
        if not batch_rows:
            return []
        now = self.now()
        with self.database.write() as db:
            self.database.begin_immediate(db)
            table = db.execute(
                "SELECT notebook_id FROM knowhow_tables WHERE id=?", (table_id,)
            ).fetchone()
            if table is None:
                raise KeyError(table_id)
            valid_columns = {
                row["id"]
                for row in db.execute(
                    "SELECT id FROM knowhow_columns WHERE table_id=?", (table_id,)
                ).fetchall()
            }
            if any(
                column_id not in valid_columns
                for cells in batch_rows
                for column_id in cells
            ):
                raise ValueError("单元格列不属于本表")
            self._require_assets_for_notebook(
                db,
                str(table["notebook_id"]),
                required_asset_ids(
                    (),
                    (
                        ("", content)
                        for cells in batch_rows
                        for content in cells.values()
                    ),
                ),
            )
            count_row = db.execute(
                "SELECT COUNT(*) AS n FROM knowhow_rows WHERE table_id=?", (table_id,)
            ).fetchone()
            start_position = int(count_row["n"])
            row_ids: list[str] = []
            payload_rows: list[dict] = []
            for offset, cells in enumerate(batch_rows):
                position = start_position + offset
                row_id = self._insert_knowhow_row_on(
                    db, table_id, cells, position, now
                )
                row_ids.append(row_id)
                payload_rows.append(
                    {
                        "row_id": row_id,
                        "position": position,
                        "created_at": now,
                        "cells": cells,
                        "code": [],
                    }
                )
            db.execute(
                "UPDATE knowhow_tables SET mutation_seq=mutation_seq+1 WHERE id=?",
                (table_id,),
            )
            record_change(
                db,
                new_id=self.new_id,
                now=self.now,
                table_id=table_id,
                kind="import_append",
                payload={"rows": payload_rows},
                actor=actor,
                origin=origin,
            )
        return row_ids

    def append_knowhow_rows_skipping_existing_anchors(
        self,
        table_id: str,
        anchor_column_id: str,
        rows: Sequence[dict[str, str]],
        actor: str = "",
        origin: str = "user",
    ) -> dict:
        """``append_knowhow_rows``, but the anchor-membership check and the
        insert share ONE write transaction instead of a caller doing its own
        pre-read (``knowhow_anchor_existing_values``) before a separate call
        here.

        That split used to leave a real gap: the command catalog's apply
        lock (``CatalogJobService._apply_lock``) serializes concurrent
        *catalog* applies for a notebook, but does NOT cover the ordinary
        knowhow row/cell edit endpoints — a user can add a row naming the
        same command through the live table UI in the window between the
        pre-read and the write, and the old two-step apply would double it
        up. Folding the check into this method's own ``database.write()``
        block closes that window the same way SQLite's process-wide write
        lock already closes it for ``append_knowhow_rows`` vs.
        ``add_knowhow_row``: no other writer can observe or mutate this
        table's cells between the SELECT and the INSERTs below.

        ``anchor_column_id`` is re-verified as `table_id`'s CURRENT anchor
        column under this same lock — not trusted from a caller's earlier
        read — mirroring the guarded-write anchor check's own re-verification
        (see ``update_knowhow_cells_bulk_guarded``'s docstring) for the same
        reason: the anchor designation can move between a caller's read and
        this call.

        Matching is by each row's OWN value at ``anchor_column_id`` in its
        ``cells`` dict, js-trim normalized — the identical normalization
        ``knowhow_anchor_existing_values`` and the anchor guarded-write path
        already use, so a row already present with incidental whitespace
        still counts as a match. A blank/missing anchor value has nothing to
        collide with and is inserted unconditionally, same as
        ``knowhow_anchor_existing_values`` treating blank as absent. Two rows
        in THIS SAME batch that normalize to the same anchor value: the
        first wins, the rest are skipped — a batch must not double-insert a
        name it saw twice, independent of what already exists.

        Returns ``{"row_ids": {anchor_value: row_id}, "skipped_anchor_values":
        set[str]}`` — keyed by NORMALIZED anchor value rather than a
        positional list, since the only caller (command-catalog apply)
        already de-duplicates candidate names before calling this, so each
        value maps to at most one candidate; that lets it re-attribute
        results to candidate ids without a second positional zip.

        Zero-row and skip-everything batches are silent no-ops — no
        transaction opens (empty ``rows``) or nothing is recorded (every row
        skipped), mirroring ``append_knowhow_rows``'s own empty-batch
        convention and this store's zero-row-write convention generally: a
        confirm that lands nothing new must not fabricate a history entry.
        """
        batch_rows = [dict(row) for row in rows]
        if not batch_rows:
            return {"row_ids": {}, "skipped_anchor_values": set()}
        now = self.now()
        with self.database.write() as db:
            self.database.begin_immediate(db)
            table = db.execute(
                "SELECT notebook_id FROM knowhow_tables WHERE id=?", (table_id,)
            ).fetchone()
            if table is None:
                raise KeyError(table_id)
            anchor_row = db.execute(
                "SELECT role FROM knowhow_columns WHERE id=? AND table_id=?",
                (anchor_column_id, table_id),
            ).fetchone()
            if anchor_row is None or anchor_row["role"] != "anchor":
                raise ValueError("锚点列不属于本表或已不是锚点列")
            valid_columns = {
                row["id"]
                for row in db.execute(
                    "SELECT id FROM knowhow_columns WHERE table_id=?", (table_id,)
                ).fetchall()
            }
            if any(
                column_id not in valid_columns
                for cells in batch_rows
                for column_id in cells
            ):
                raise ValueError("单元格列不属于本表")
            self._require_assets_for_notebook(
                db,
                str(table["notebook_id"]),
                required_asset_ids(
                    (),
                    (
                        ("", content)
                        for cells in batch_rows
                        for content in cells.values()
                    ),
                ),
            )
            wanted = [
                value
                for value in dict.fromkeys(
                    js_trim(str(cells.get(anchor_column_id) or ""))
                    for cells in batch_rows
                )
                if value
            ]
            existing: set[str] = set()
            if wanted:
                normalized = sqlite_js_trim_expression("content_md")
                placeholders = ",".join("?" for _ in wanted)
                existing = {
                    row["value"]
                    for row in db.execute(
                        f"SELECT DISTINCT {normalized} AS value FROM knowhow_cells "
                        f"WHERE column_id = ? AND {normalized} IN ({placeholders})",
                        (anchor_column_id, *wanted),
                    ).fetchall()
                }
            count_row = db.execute(
                "SELECT COUNT(*) AS n FROM knowhow_rows WHERE table_id=?", (table_id,)
            ).fetchone()
            start_position = int(count_row["n"])
            row_ids: dict[str, str] = {}
            skipped: set[str] = set()
            payload_rows: list[dict] = []
            claimed: set[str] = set()
            offset = 0
            for cells in batch_rows:
                anchor_value = js_trim(str(cells.get(anchor_column_id) or ""))
                if anchor_value and (anchor_value in existing or anchor_value in claimed):
                    skipped.add(anchor_value)
                    continue
                if anchor_value:
                    claimed.add(anchor_value)
                position = start_position + offset
                row_id = self._insert_knowhow_row_on(
                    db, table_id, cells, position, now
                )
                offset += 1
                if anchor_value:
                    row_ids[anchor_value] = row_id
                payload_rows.append(
                    {
                        "row_id": row_id,
                        "position": position,
                        "created_at": now,
                        "cells": cells,
                        "code": [],
                    }
                )
            if payload_rows:
                db.execute(
                    "UPDATE knowhow_tables SET mutation_seq=mutation_seq+1 WHERE id=?",
                    (table_id,),
                )
                record_change(
                    db,
                    new_id=self.new_id,
                    now=self.now,
                    table_id=table_id,
                    kind="import_append",
                    payload={"rows": payload_rows},
                    actor=actor,
                    origin=origin,
                )
        return {"row_ids": row_ids, "skipped_anchor_values": skipped}

    def add_knowhow_rows(
        self,
        table_id: str,
        rows: list[dict[str, str]],
        actor: str = "",
        origin: str = "user",
    ) -> list[str]:
        """Bulk-insert MULTIPLE rows in ONE write transaction, recording a
        SINGLE ``import_append`` flow entry covering all of them — the
        batch-import counterpart to ``add_knowhow_row`` (spec §5.4:
        "commit_append（导入追加几十行）→ 一条 import_append，payload 含
        所有新行"). Each element of ``rows`` is a ``cells`` dict, exactly
        like ``add_knowhow_row``'s own ``cells`` parameter; position is
        always append order (current row count + its index in ``rows``).
        Empty ``rows`` is a no-op (no transaction opened, nothing recorded)
        — mirrors ``update_knowhow_cells``'s empty-batch convention. Does
        NOT bump the table's ``mutation_seq`` itself — same division of
        labor as ``add_knowhow_row``: bulk callers (commit_append) bump once
        at the end via ``bump_knowhow_mutation_seq``.

        This is a SEPARATE method from ``add_knowhow_row`` (rather than a
        loop calling it N times) precisely so the N new rows land as ONE
        flow entry instead of N ``row_add`` entries — looping over the
        single-row method would call ``record_change`` once per row,
        exactly the per-row noise spec §5.4 says a batch import must not
        produce, and would leave the revert engine's ``import_append``
        branch of ``_apply_before`` permanently unreachable (it already
        exists but nothing has ever produced this ``kind`` of flow entry)."""
        if not rows:
            return []
        now = self.now()
        row_ids: list[str] = []
        payload_rows: list[dict] = []
        with self.database.write() as db:
            count_row = db.execute(
                "SELECT COUNT(*) AS n FROM knowhow_rows WHERE table_id = ?",
                (table_id,),
            ).fetchone()
            base_position = count_row["n"]
            for offset, cells in enumerate(rows):
                row_id = self.new_id("khrow")
                position = base_position + offset
                db.execute(
                    "INSERT INTO knowhow_rows (id, table_id, position, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row_id, table_id, position, now, now),
                )
                written_cells = dict(cells or {})
                for column_id, content_md in written_cells.items():
                    db.execute(
                        "INSERT INTO knowhow_cells (id, row_id, column_id, content_md, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (self.new_id("khcel"), row_id, column_id, content_md, now),
                    )
                row_ids.append(row_id)
                payload_rows.append({
                    "row_id": row_id, "position": position, "created_at": now,
                    "cells": written_cells, "code": [],
                })
            record_change(
                db, new_id=self.new_id, now=self.now, table_id=table_id,
                kind="import_append",
                payload={"rows": payload_rows},
                actor=actor, origin=origin,
            )
        return row_ids

    def set_knowhow_row_projection(self, row_id: str, status: str) -> None:
        with self.database.write() as db:
            db.execute(
                "UPDATE knowhow_rows SET projection_status = ? WHERE id = ?",
                (status, row_id),
            )

    def set_knowhow_row_projection_if_table_seq(
        self, row_id: str, status: str, expected_mutation_seq: int
    ) -> bool:
        """Publish a projection status only for the table version that ran.

        Cell/row/column mutations bump ``knowhow_tables.mutation_seq`` and mark
        affected rows pending. A background projection working from an older
        hydrated table snapshot must not overwrite that newer pending marker
        while its scheduler rerun is still queued. The version predicate and
        status update share one write transaction, closing the check/write
        race; ``False`` means the caller's snapshot is stale or the row/table
        was deleted.
        """
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE knowhow_rows SET projection_status = ? "
                "WHERE id = ? AND EXISTS ("
                "SELECT 1 FROM knowhow_tables t "
                "WHERE t.id = knowhow_rows.table_id AND t.mutation_seq = ?)",
                (status, row_id, expected_mutation_seq),
            )
        return cursor.rowcount > 0

    def delete_knowhow_row(
        self, row_id: str, actor: str = "", origin: str = "user"
    ) -> None:
        """Delete a row; its cells and cell-code attachments go with it
        (``knowhow_cells.row_id`` and ``knowhow_cell_code.row_id`` are both
        ``ON DELETE CASCADE`` onto knowhow_rows). Silent no-op when already
        gone. Surviving rows' positions are NOT renumbered (ordering reads
        sort by position and tolerate gaps).

        版本管理（spec §5）：CASCADE means the row's cells and code
        attachments are gone the instant the DELETE below commits, so
        EVERYTHING needed to rebuild the row (its position, ``created_at``,
        every cell, every code attachment) is read BEFORE the DELETE and
        stored in the ``row_delete`` payload — the only way this change
        stays reversible. ``created_at`` (Task 8 fix round P3) is the row's
        REAL creation timestamp, not the delete-time ``now()`` — without it,
        ``_rebuild_row`` had no choice but to stamp the moment of rebuild,
        so "revert to that point in time" silently lied about when the row
        was actually created (the fingerprint doesn't cover this column, so
        the post-revert guard couldn't see the drift either, but
        ``get_knowhow_table`` hands the wrong value straight to the
        frontend). The silent no-op above is preserved verbatim: nothing was
        deleted, so nothing is recorded."""
        with self.database.write() as db:
            row = db.execute(
                "SELECT id, table_id, position, created_at FROM knowhow_rows WHERE id = ?",
                (row_id,),
            ).fetchone()
            if row is None:
                return  # already gone — nothing deleted, nothing to record
            cells = {
                c["column_id"]: c["content_md"]
                for c in db.execute(
                    "SELECT column_id, content_md FROM knowhow_cells WHERE row_id = ?",
                    (row_id,),
                ).fetchall()
            }
            code = [
                dict(c)
                for c in db.execute(
                    "SELECT column_id, code_text, language, updated_by, cell_content_hash "
                    "FROM knowhow_cell_code WHERE row_id = ?",
                    (row_id,),
                ).fetchall()
            ]
            db.execute("DELETE FROM knowhow_rows WHERE id = ?", (row_id,))
            record_change(
                db, new_id=self.new_id, now=self.now, table_id=row["table_id"],
                kind="row_delete",
                payload={"rows": [{
                    "row_id": row["id"], "position": row["position"],
                    "created_at": row["created_at"],
                    "cells": cells, "code": code,
                }]},
                actor=actor, origin=origin,
            )

    # -------------------------------------------------------------- cells
    @staticmethod
    def _require_assets_for_notebook(
        db, notebook_id: str, require_assets: Sequence[str]
    ) -> tuple[str, ...]:
        ordered = tuple(sorted({str(asset_id) for asset_id in require_assets}))
        missing: list[str] = []
        for asset_id in ordered:
            row = db.execute(
                "SELECT 1 FROM notebook_assets "
                "WHERE id=? AND notebook_id=? LIMIT 1",
                (asset_id, notebook_id),
            ).fetchone()
            if row is None:
                missing.append(asset_id)
        if missing:
            raise ValueError(f"asset {missing[0]} is not available here")
        return ordered

    def _require_assets_exist(
        self, db, row_id: str, require_assets: Sequence[str]
    ) -> None:
        """Assert every id in ``require_assets`` is a live ``notebook_assets``
        row **of the notebook this cell belongs to**, else ``ValueError``. Takes
        the CALLER's open write connection on purpose — see
        ``update_knowhow_cell``.

        Scoped to the cell's own notebook, not global: the asset endpoint serves
        a file only when its ``notebook_id`` matches the requesting notebook, so
        a reference to some OTHER notebook's asset saves fine and then renders as
        a permanently broken image — exactly the dead link this check exists to
        refuse. That is reachable without any malice: copying a cell's markdown
        from one notebook and pasting it into another carries the ``asset://``
        ids along. Scoping also keeps the check from answering "does this id
        exist somewhere in the database" for ids the caller cannot otherwise
        see."""
        if not require_assets:
            return  # keep the common path free of the notebook lookup
        owner = db.execute(
            "SELECT t.notebook_id AS notebook_id FROM knowhow_rows r "
            "JOIN knowhow_tables t ON t.id = r.table_id WHERE r.id = ?",
            (row_id,),
        ).fetchone()
        if owner is None:
            raise ValueError(f"row {row_id} does not exist")
        self._require_assets_for_notebook(
            db, str(owner["notebook_id"]), require_assets
        )

    def update_knowhow_cell(
        self,
        row_id: str,
        column_id: str,
        content_md: str,
        require_assets: Sequence[str] = (),
        actor: str = "",
        origin: str = "user",
    ) -> None:
        """Upsert one cell's content. One write transaction: the cell
        (insert or update-in-place via the ``UNIQUE(row_id, column_id)``
        conflict target), the row's ``updated_at`` + ``projection_status`` ->
        ``'pending'``, and the owning table's ``mutation_seq`` += 1 (table_id
        resolved from the row — the public signature has no table_id).

        ``require_assets`` names asset ids the new content references and that
        must still exist at COMMIT time (``ValueError`` and a full rollback
        otherwise). The check runs inside this transaction rather than in the
        caller because the orphan-asset sweeper deletes rows concurrently: a
        caller that verified existence first would be reading state that the
        sweeper can invalidate before this write lands, committing a live cell
        reference to a deleted asset. Both sides now decide under the same
        write lock, so one of them always loses cleanly — the sweeper's own
        re-check sees the new reference and spares the asset, or this write
        sees the deletion and refuses.

        版本管理（spec §5）：本方法在同一事务的最后追加一条 ``cell_update``
        流水，``before`` 取写入前的 ``content_md``（格子当时不存在则为
        ``None``，与空串区分——回退时 ``None`` 意味着"把这格删掉"）。"""
        now = self.now()
        with self.database.write() as db:
            self.database.begin_immediate(db)
            before_row = db.execute(
                "SELECT content_md FROM knowhow_cells "
                "WHERE row_id=? AND column_id=?",
                (row_id, column_id),
            ).fetchone()
            before = before_row["content_md"] if before_row is not None else None
            self._require_assets_exist(
                db,
                row_id,
                required_asset_ids(require_assets, ((before or "", content_md),)),
            )
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
            table_row = db.execute(
                "SELECT table_id FROM knowhow_rows WHERE id = ?", (row_id,)
            ).fetchone()
            if table_row is not None:
                record_change(
                    db,
                    new_id=self.new_id,
                    now=self.now,
                    table_id=table_row["table_id"],
                    kind="cell_update",
                    payload={"cells": [{
                        "row_id": row_id, "column_id": column_id,
                        "before": before, "after": content_md,
                    }]},
                    actor=actor,
                    origin=origin,
                )

    def update_knowhow_cells(
        self,
        row_ids: list[str],
        column_id: str,
        content_md: str,
        require_assets: Sequence[str] = (),
        actor: str = "",
        origin: str = "user",
    ) -> None:
        """Batch upsert the SAME (column, content) across MULTIPLE rows in
        ONE write transaction — the merged-cell "write the whole concept
        group" case (anchor-grouping spec §6, followup A): editing a shared
        cell must write every branch row or none, never half. Each row gets
        the identical per-row effect ``update_knowhow_cell`` gives one row
        (cell upsert via the same ``ON CONFLICT(row_id, column_id)`` target +
        that row's ``updated_at``/``projection_status`` -> ``'pending'``),
        but the owning table's ``mutation_seq`` bumps only ONCE for the whole
        batch (not once per row) since this is a single logical edit —
        table_id is resolved from row_ids[0] (any row in the batch works;
        callers guarantee same-table membership, exactly like
        ``update_knowhow_cell`` takes no table_id argument and trusts its
        one row). Empty ``row_ids`` is a no-op: no transaction opened, no
        mutation_seq bump. All-or-nothing by construction — everything runs
        inside a single ``with self.database.write() as db:`` block, so any
        failure partway through (e.g. a row_id that doesn't exist, tripping
        the ``knowhow_cells.row_id`` FK) rolls back every write already made
        in this call, including earlier rows already looped over.

        ``require_assets`` behaves exactly as in ``update_knowhow_cell`` and
        is checked once for the whole batch (every row receives the same
        ``content_md``, so they reference the same assets); a missing asset
        rolls back the entire batch, preserving all-or-nothing.

        版本管理（spec §5）：records ONE ``cell_update`` flow entry covering
        every row in the batch (not one per row — a merged-cell batch write
        is a single logical edit), appended in the same transaction after the
        ``mutation_seq`` bump. Each row's ``before`` is its own pre-write
        ``content_md`` (``None`` when the cell did not exist yet)."""
        if not row_ids:
            return
        now = self.now()
        with self.database.write() as db:
            self.database.begin_immediate(db)
            # row_ids share a table (documented above), so any of them resolves
            # the same notebook.
            current_by_row: dict[str, str] = {}
            for row_id in row_ids:
                current_row = db.execute(
                    "SELECT content_md FROM knowhow_cells "
                    "WHERE row_id=? AND column_id=?",
                    (row_id, column_id),
                ).fetchone()
                if current_row is not None:
                    current_by_row[row_id] = current_row["content_md"]
            self._require_assets_exist(
                db,
                row_ids[0],
                required_asset_ids(
                    require_assets,
                    (
                        (current_by_row.get(row_id, ""), content_md)
                        for row_id in row_ids
                    ),
                ),
            )
            cells: list[dict] = []
            for row_id in row_ids:
                before_row = db.execute(
                    "SELECT content_md FROM knowhow_cells WHERE row_id = ? AND column_id = ?",
                    (row_id, column_id),
                ).fetchone()
                cells.append({
                    "row_id": row_id, "column_id": column_id,
                    "before": before_row["content_md"] if before_row is not None else None,
                    "after": content_md,
                })
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
                (row_ids[0],),
            )
            table_row = db.execute(
                "SELECT table_id FROM knowhow_rows WHERE id = ?", (row_ids[0],)
            ).fetchone()
            if table_row is not None:
                record_change(
                    db, new_id=self.new_id, now=self.now,
                    table_id=table_row["table_id"],
                    kind="cell_update", payload={"cells": cells},
                    actor=actor, origin=origin,
                )

    def update_knowhow_cells_bulk_guarded(
        self,
        notebook_id: str,
        updates: list[tuple[str, str, str, str, str]],
        actor: str = "",
        origin: str = "user",
    ) -> "dict[str, list[tuple[str, str]]]":
        """Compare-and-write bulk cell writer for
        ``scripts/backfill_knowhow_md.py``'s ``--apply`` paths (TOCTOU +
        membership fix). Each update is ``(table_id, row_id, column_id,
        expected_before, content_md)`` and ``notebook_id`` is the notebook the
        caller claims every entry belongs to. INSIDE ONE write transaction, for
        every update this:

        1. VALIDATES MEMBERSHIP (F2): joins the row and the column to their
           owning table and verifies ``row.table_id == column.table_id ==
           table_id`` (the id the caller claims for this entry) AND that table
           belongs to ``notebook_id``. An entry that fails -- ids belonging to
           another notebook/table, a cross-table (row, column) pair, or a
           nonexistent row/column/table -- is collected as REJECTED and left
           byte-for-byte untouched. This closes the hole where the method
           trusted caller-supplied ids: an entry whose (row, column) lived in a
           DIFFERENT notebook/table passed the ``before`` compare below and
           silently overwrote a foreign cell while bumping the CALLER's table.
           The membership read shares this write transaction, so it cannot race
           a concurrent table move.

        2. RE-READS the target cell's CURRENT ``content_md`` and writes
           ``content_md`` ONLY when the stored value still equals
           ``expected_before``; an update whose cell no longer matches (a live
           backend user edited it after the plan was reviewed, or the row was
           deleted so the current value is ``None``) is collected as SKIPPED and
           left untouched. Neither a skip nor a reject ever aborts the
           transaction.

        Doing the compare INSIDE the same write transaction that performs the
        write is the whole point. The CLI used to read the current cells, compare
        against each plan entry's ``before``, THEN call the plain bulk write in a
        SEPARATE step: a cell edited BETWEEN that read and the write passed the
        now-stale comparison and got overwritten on top of the live edit,
        violating the documented "post-review edits are skipped" guarantee.
        Re-reading under the write lock closes that window -- the compare and the
        write see the same committed state, so a cell that moved after the read
        is refused rather than clobbered.

        A written cell gets the exact per-row effect ``update_knowhow_cell``
        gives (cell upsert via the same
        ``ON CONFLICT(row_id, column_id)`` target + that row's ``updated_at`` /
        ``projection_status`` -> ``'pending'``), and every DISTINCT table with at
        least one actual write bumps its ``mutation_seq`` exactly once -- a table
        whose updates were ALL skipped/rejected does not bump (nothing in it
        changed). Empty ``updates`` is a no-op (no transaction opened).

        Returns ``{"written": [...], "skipped": [...], "already_applied": [...],
        "rejected": [...]}`` (each a list of ``(row_id, column_id)`` in first-seen
        order), so the CLI reports each outcome from THIS transaction's own result
        rather than a separate pre-read. F4: a cell whose CURRENT content already
        equals the update's ``content_md`` (the AFTER) -- a PRIOR apply committed
        the write but its separate reprojection step never completed (it threw, or
        the process exited), leaving the row 'pending'/'failed' -- is reported in
        the DISTINCT ``already_applied`` bucket, NOT lumped with genuine
        moved-target ``skipped`` (current != before AND != after). The CLI
        reprojects tables with written OR already_applied rows (reprojection is
        idempotent), so a rerun of the same plan recovers such stuck rows even
        though it writes nothing. Both buckets leave the cell untouched and bump
        no ``mutation_seq``.

        版本管理（spec §5）：collect-skips-and-press-on semantics carry over to
        the flow log — only entries that actually land in ``written`` go into
        a ``cell_update`` payload. A call whose ``updates`` span several tables
        records ONE entry PER DISTINCT written table — never one entry mixing
        rows from different tables — appended after every ``mutation_seq``
        bump. A table with zero actual writes (all skipped/rejected/already-
        applied) gets no entry at all, never one with an empty ``cells`` list.
        ``before`` reuses the already-verified ``expected_before`` from the
        phase-1 compare above — no second read."""
        if not updates:
            return {"written": [], "skipped": [], "already_applied": [], "rejected": []}
        now = self.now()
        written: list[tuple[str, str]] = []
        skipped: list[tuple[str, str]] = []
        already_applied: list[tuple[str, str]] = []
        rejected: list[tuple[str, str]] = []
        to_write: list[tuple[str, str, str, str, str]] = []
        written_table_ids: list[str] = []
        by_table: "dict[str, list[dict]]" = {}
        with self.database.write() as db:
            # F1 (cross-process atomicity): take a RESERVED lock BEFORE the
            # phase-1 re-reads. write()'s ``write_lock`` only serializes writers
            # in THIS process; pysqlite (isolation_level="") would otherwise open
            # the transaction lazily at the first DML below, leaving every
            # ``SELECT current`` above running in autocommit -- a second PROCESS
            # (a live backend beside the CLI, another worker) could commit an edit
            # between the compare and our write, and we would clobber it on top,
            # defeating the whole compare-and-write. BEGIN IMMEDIATE makes the
            # read-compare-write one cross-process-atomic critical section; a
            # writer already holding the lock surfaces as the connection's normal
            # busy handling (PRAGMA busy_timeout in _new_connection). The
            # enclosing ``with conn:`` still owns commit/rollback -- once
            # sqlite3_get_autocommit is 0 pysqlite issues no second implicit
            # BEGIN, so this integrates without a double-begin.
            db.execute("BEGIN IMMEDIATE")
            for table_id, row_id, column_id, expected_before, content_md in updates:
                # F2: membership -- row and column must both belong to the SAME
                # table the caller claims, and that table must belong to the
                # expected notebook. A missing row/column/table yields no join
                # row and is likewise rejected.
                membership = db.execute(
                    "SELECT r.table_id AS row_table, c.table_id AS col_table, "
                    "t.notebook_id AS tbl_notebook "
                    "FROM knowhow_rows r, knowhow_columns c, knowhow_tables t "
                    "WHERE r.id = ? AND c.id = ? AND t.id = ?",
                    (row_id, column_id, table_id),
                ).fetchone()
                if (
                    membership is None
                    or membership["row_table"] != table_id
                    or membership["col_table"] != table_id
                    or membership["tbl_notebook"] != notebook_id
                ):
                    rejected.append((row_id, column_id))
                    continue
                current_row = db.execute(
                    "SELECT content_md FROM knowhow_cells "
                    "WHERE row_id = ? AND column_id = ?",
                    (row_id, column_id),
                ).fetchone()
                current = current_row["content_md"] if current_row is not None else None
                if current != expected_before:
                    # F4: distinguish an ALREADY-APPLIED cell (current == the AFTER
                    # this update would write -- a prior committed apply, its
                    # reprojection never finished) from a GENUINE moved target
                    # (current != before AND != after -- a live edit after review).
                    # The CLI reprojects already_applied tables (idempotent) but
                    # not moved ones; both leave the cell untouched.
                    if current == content_md:
                        already_applied.append((row_id, column_id))
                    else:
                        skipped.append((row_id, column_id))
                    continue
                # 保留 current 的原值(未写过的格子是 None):历史流水的 before
                # 必须是 None 而非 ""(与 guarded_atomic 一致、见 update_knowhow_cells_
                # guarded_atomic 的 ``before`` 语义)。资产 diff 处再各自兜底成 ""。
                to_write.append(
                    (table_id, row_id, column_id, current, content_md)
                )

            self._require_assets_for_notebook(
                db,
                notebook_id,
                required_asset_ids(
                    (),
                    (
                        (old_content or "", content)
                        for _table, _row, _column, old_content, content in to_write
                    ),
                ),
            )
            for table_id, row_id, column_id, old_content, content_md in to_write:
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
                written.append((row_id, column_id))
                by_table.setdefault(table_id, []).append({
                    "row_id": row_id, "column_id": column_id,
                    "before": old_content, "after": content_md,
                })
                if table_id not in written_table_ids:
                    written_table_ids.append(table_id)
            for table_id in written_table_ids:
                db.execute(
                    "UPDATE knowhow_tables SET mutation_seq = mutation_seq + 1 WHERE id = ?",
                    (table_id,),
                )
            for written_table_id, cells in by_table.items():
                record_change(
                    db, new_id=self.new_id, now=self.now,
                    table_id=written_table_id,
                    kind="cell_update", payload={"cells": cells},
                    actor=actor, origin=origin,
                )
        return {
            "written": written,
            "skipped": skipped,
            "already_applied": already_applied,
            "rejected": rejected,
        }

    def update_knowhow_cells_guarded_atomic(
        self,
        notebook_id: str,
        updates: "list[tuple[str, str, str, str, str]]",
        require_assets: Sequence[str] = (),
        anchor_column_id: "str | None" = None,
        expected_anchor: "Sequence[str] | None" = None,
        actor: str = "",
        origin: str = "user",
    ) -> "dict[str, object]":
        """ALL-OR-NOTHING compare-and-write for the interactive editor's
        batch-reformat save (concurrency P1 fix b, HTTP 409 on a concurrent
        edit). Each update is ``(table_id, row_id, column_id, expected_before,
        content_md)`` and ``notebook_id`` is the notebook every entry must
        belong to. Same membership + compare guarantees as
        ``update_knowhow_cells_bulk_guarded``, but the FAILURE MODE is the
        opposite: where the bulk-guarded backfill collects skips/rejects and
        presses on (idempotent CLI re-run), THIS method refuses the WHOLE batch
        the moment any entry fails membership or its stored ``content_md`` no
        longer equals ``expected_before``.

        It does so in TWO PHASES inside ONE ``self.database.write()``
        transaction: phase 1 validates membership and re-reads+compares EVERY
        target's current content; only if EVERY entry clears does phase 2 write
        them all (same per-row effect as ``update_knowhow_cell`` — cell upsert +
        that row's ``updated_at``/``projection_status`` -> ``'pending'`` — plus
        one ``mutation_seq`` bump per distinct written table). A conflict in
        phase 1 returns BEFORE any write happens, so the transaction commits with
        nothing changed — all-or-nothing by construction, no rollback needed. The
        compare sharing the write transaction is the whole point: reading the
        cells in a SEPARATE step, comparing, then writing would reopen the TOCTOU
        window a live edit between the read and the write slips through.

        ``require_assets`` (F2) behaves exactly as in ``update_knowhow_cell``:
        asset ids the new content references that must still be live
        ``notebook_assets`` rows OF THIS notebook at COMMIT time. Checked INSIDE
        this same write transaction (after membership+stale clear, before any
        write), so the guarded save path gets the identical dangling-asset guard
        the legacy last-write-wins path already had — a missing/foreign asset
        raises ``ValueError`` and the transaction commits nothing (the caller maps
        it to the legacy 400 + CELL_ASSET_MISSING_MESSAGE, distinct from the 409).
        Sharing the write lock is the same anti-TOCTOU reasoning as the compare.

        ANCHOR BASELINE GUARD (round-4 P1, OPTIONAL): the editor's batch fan-out
        writes a merged/shared cell to every branch row of an anchor GROUP using
        row ids FROZEN when the modal opened. If another client moves a sibling
        row's ANCHOR out of that group but leaves the EDITED cell untouched,
        ``expected_before`` still matches and membership still holds — the
        candidate would silently land on a row that no longer belongs to the
        group. When the caller supplies BOTH ``anchor_column_id`` and
        ``expected_anchor`` (positionally parallel to ``updates`` —
        ``expected_anchor[i]`` is ``updates[i]``'s row's snapshot anchor value,
        exactly as ``expected_before`` parallels ``row_ids``), phase 1 also
        re-reads each target row's anchor-column cell UNDER THE SAME
        ``BEGIN IMMEDIATE`` and treats a mismatch as a conflict IDENTICAL to a
        stale ``expected_before``: the whole batch is refused before any write
        (all-or-nothing). Omitting either argument (both default ``None``) skips
        the anchor guard entirely — byte-identical to the pre-fix behavior, so
        the single-cell/manual-editor callers that never pass it are unaffected.

        ANCHOR STRUCTURAL GUARD (round-5 P1): the per-row baseline above catches a
        frozen target that LEFT the group, but not two drifts that leave EVERY
        frozen row still matching its snapshot: (a) the table's ANCHOR DESIGNATION
        moved to a different column (guarded cells all still equal their frozen
        values, but the grouping is now defined by another column); and (b) a NEW
        row JOINED the group (its anchor cell set to the group's value), making the
        fan-out cover only a SUBSET of the current logical group. So when the
        anchor guard is active, BEFORE any write and under the same
        ``BEGIN IMMEDIATE``, this method ALSO (1) verifies ``anchor_column_id`` is
        STILL the table's anchor column (``knowhow_columns.role == 'anchor'``), and
        (2) checks EXACT membership: for each distinct frozen anchor VALUE, the
        current member row-id set (rows whose anchor cell ``js_trim`` value equals
        it, mirroring the frontend ``groupRowsByAnchor`` and queried through the
        normalized expression index) must EQUAL the frozen target set. Any
        designation move, joiner, or vanished row refuses
        the whole batch (nothing written). The server derives membership itself, so
        no new wire is needed. (The per-row byte-exact baseline stays the value-edit
        guard; this pair is the structural guard.)

        Returns ``{"written": [(row_id, column_id), ...], "conflict": bool}``.
        ``conflict=True`` guarantees ``written == []`` (the caller maps it to a
        409 with a friendly "内容已被其他人修改，请刷新后重试"). Empty ``updates``
        is a no-op (``{"written": [], "conflict": False}``, no transaction).

        版本管理（spec §5）：a conflict returns before phase 2 ever runs, so it
        writes no flow entry (all-or-nothing extends to the log). On success,
        ``updates`` may span several tables (each entry carries its own
        ``table_id``), so this records ONE ``cell_update`` entry PER DISTINCT
        written table — never one entry mixing rows from different tables —
        appended after every ``mutation_seq`` bump. A never-written cell is
        presented on the table wire as ``""`` and therefore compares equal to
        ``expected_before=""``, while its history ``before`` remains ``None``
        so a revert deletes the newly inserted cell instead of materializing an
        empty one. Stored cells reuse the exact phase-1 value."""
        if not updates:
            return {"written": [], "conflict": False}
        now = self.now()
        written: list[tuple[str, str]] = []
        written_table_ids: list[str] = []
        by_table: "dict[str, list[dict]]" = {}
        before_values: "dict[tuple[str, str], str | None]" = {}
        with self.database.write() as db:
            # F1 (cross-process atomicity): reserve the write lock BEFORE phase 1
            # re-reads, for the same reason as update_knowhow_cells_bulk_guarded
            # -- write()'s ``write_lock`` is process-local, and pysqlite
            # (isolation_level="") would otherwise defer the transaction to the
            # first phase-2 DML, leaving the phase-1 compare running in
            # autocommit where a second PROCESS could slip a write in between.
            # BEGIN IMMEDIATE makes the read-compare-write cross-process atomic;
            # the enclosing ``with conn:`` still commits/rolls back (no double
            # BEGIN once autocommit is off), and a busy peer surfaces via
            # PRAGMA busy_timeout.
            db.execute("BEGIN IMMEDIATE")
            # Anchor baseline guard active only when BOTH inputs are supplied (the
            # editor's fan-out); omitted -> byte-identical legacy path. See the
            # docstring's ANCHOR BASELINE GUARD paragraph.
            anchor_guard = anchor_column_id is not None and expected_anchor is not None
            if anchor_guard:
                if len(expected_anchor) != len(updates):
                    return {"written": [], "conflict": True}
                # Round-5 P1: the per-row byte-exact anchor re-read in the phase-1
                # loop below catches a FROZEN target that LEFT the group, but two
                # STRUCTURAL drifts leave every per-row baseline still matching while
                # the fan-out has become a partial/misaimed group write. Both are
                # caught HERE, under the same BEGIN IMMEDIATE, before any write.
                #
                # (a) DESIGNATION: anchor_column_id must STILL be the table's anchor
                # column. The designation is stored as knowhow_columns.role ==
                # 'anchor' (moved only by set_knowhow_anchor_column); if another
                # client repointed it to a different column after the snapshot, the
                # guarded column's cells may all still equal their frozen values
                # while the logical grouping is now defined by another column
                # entirely -> refuse the whole batch (nothing written).
                anchor_col_row = db.execute(
                    "SELECT table_id, role FROM knowhow_columns WHERE id = ?",
                    (anchor_column_id,),
                ).fetchone()
                if (
                    anchor_col_row is None
                    or anchor_col_row["role"] != "anchor"
                    or any(
                        update_table_id != anchor_col_row["table_id"]
                        for update_table_id, _row, _column, _before, _content in updates
                    )
                ):
                    return {"written": [], "conflict": True}
                anchor_table_id = anchor_col_row["table_id"]
                # (b) EXACT MEMBERSHIP: for each DISTINCT frozen anchor VALUE among
                # the targets, the CURRENT member row-id set must EQUAL the frozen
                # target set for that value. "Member" MIRRORS the frontend
                # groupRowsByAnchor (frontend/app/knowhow-grouping-logic.ts): a row
                # whose anchor cell TRIM-equals the value (see js_trim, which
                # reproduces JS String.prototype.trim() exactly). A blank-after-trim
                # value is its OWN group there (never aggregated), so blank keys
                # carry no multi-row membership and are skipped — the fan-out only
                # ever targets non-empty groups. A row that JOINED (extra id) or a
                # frozen row that VANISHED (missing id) makes current != frozen ->
                # refuse the whole batch. The per-row byte-exact baseline stays the
                # VALUE-EDIT guard; this set check is the STRUCTURAL guard.
                frozen_by_key: "dict[str, set[str]]" = {}
                for (_ut, u_row, _uc, _ub, _um), snapshot in zip(updates, expected_anchor):
                    key = js_trim(snapshot)
                    if not key:
                        continue
                    frozen_by_key.setdefault(key, set()).add(u_row)
                if frozen_by_key:
                    # F2 (round-7 P2): DON'T rescan the table's ENTIRE anchor column
                    # per guarded call. A unique-anchor table makes every changed cell
                    # its own singleton coversAnchorGroup unit, so an R×C import fires
                    # R×C guarded calls, each O(R) -> O(R²C) inside the write lock.
                    # Instead, for each DISTINCT non-blank frozen key, fetch only
                    # exact members through the v21 normalized-anchor expression
                    # index. The SQL expression is shared with that migration and
                    # exactly matches js_trim, so a whitespace-padded joiner cannot
                    # hide and no Python post-filter is needed. Same BEGIN IMMEDIATE;
                    # each call now touches O(group) rows, not O(R). (Blank keys were
                    # already skipped when frozen_by_key was built, so blank-anchor
                    # rows never aggregate here -- each is its own group, mirroring
                    # the frontend.)
                    current_by_key: "dict[str, set[str]]" = {}
                    normalized_anchor = sqlite_js_trim_expression("c.content_md")
                    for key in frozen_by_key:
                        for cell in db.execute(
                            "SELECT c.row_id AS row_id "
                            "FROM knowhow_cells c JOIN knowhow_rows r ON r.id = c.row_id "
                            "WHERE r.table_id = ? AND c.column_id = ? "
                            f"AND {normalized_anchor} = ?",
                            (anchor_table_id, anchor_column_id, key),
                        ).fetchall():
                            current_by_key.setdefault(key, set()).add(cell["row_id"])
                    for key, frozen_ids in frozen_by_key.items():
                        if current_by_key.get(key, set()) != frozen_ids:
                            return {"written": [], "conflict": True}
            # Phase 1: membership + stale compare for ALL entries BEFORE writing.
            for idx, (table_id, row_id, column_id, expected_before, content_md) in enumerate(updates):
                membership = db.execute(
                    "SELECT r.table_id AS row_table, c.table_id AS col_table, "
                    "t.notebook_id AS tbl_notebook "
                    "FROM knowhow_rows r, knowhow_columns c, knowhow_tables t "
                    "WHERE r.id = ? AND c.id = ? AND t.id = ?",
                    (row_id, column_id, table_id),
                ).fetchone()
                if (
                    membership is None
                    or membership["row_table"] != table_id
                    or membership["col_table"] != table_id
                    or membership["tbl_notebook"] != notebook_id
                ):
                    return {"written": [], "conflict": True}
                current_row = db.execute(
                    "SELECT content_md FROM knowhow_cells "
                    "WHERE row_id = ? AND column_id = ?",
                    (row_id, column_id),
                ).fetchone()
                stored_before = (
                    current_row["content_md"] if current_row is not None else None
                )
                # get_knowhow_table omits a never-written cell from ``cells``;
                # every client reads that as "". Treat the same wire snapshot as
                # equal here, while retaining None separately for exact history
                # reversal (see phase 2 below).
                current_matches = (
                    expected_before in (None, "")
                    if stored_before is None
                    else stored_before == expected_before
                )
                if not current_matches:
                    return {"written": [], "conflict": True}
                before_values[(row_id, column_id)] = stored_before
                if anchor_guard:
                    # Re-read THIS row's anchor cell under the same lock and compare
                    # to the frozen snapshot value; a moved/cleared anchor (!=
                    # expected_anchor[idx]) is a conflict identical to a stale
                    # expected_before — the row left the logical group this fan-out
                    # targets. F1 (round-7 P2): an ABSENT anchor cell reads as ""
                    # (NOT None) to MIRROR get_knowhow_table, which represents a
                    # never-written cell as "" (the column key is simply missing from
                    # its `cells` dict) and the frontend's
                    # (row.cells[anchorColumnId] ?? "").trim(); the wire therefore
                    # sends expected_anchor="" for a blank-anchor row. Mapping the
                    # missing DB row to None instead made "" != None a PERMANENT 409 —
                    # blank-anchor rows could never be batch-saved even after a reload.
                    # (content_md is NOT NULL DEFAULT '', so a stored cell is never
                    # None; absent is the only source of the missing value.)
                    anchor_cell = db.execute(
                        "SELECT content_md FROM knowhow_cells "
                        "WHERE row_id = ? AND column_id = ?",
                        (row_id, anchor_column_id),
                    ).fetchone()
                    current_anchor = (
                        anchor_cell["content_md"] if anchor_cell is not None else ""
                    )
                    if current_anchor != expected_anchor[idx]:
                        return {"written": [], "conflict": True}
            # F2: every entry cleared membership+stale → the new content's newly
            # referenced assets must still exist in THIS notebook. All entries share
            # notebook_id (membership just proved it), so checking against any target
            # row resolves the same notebook (mirrors update_knowhow_cells checking
            # against row_ids[0]). A miss raises ValueError -> whole tx rolls back,
            # nothing written -> caller returns the legacy 400 (not a 409).
            self._require_assets_exist(
                db,
                updates[0][1],
                required_asset_ids(
                    require_assets,
                    (
                        (expected_before or "", content)
                        for _table, _row, _column, expected_before, content in updates
                    ),
                ),
            )
            # Phase 2: every entry cleared → write them all in this transaction.
            for table_id, row_id, column_id, expected_before, content_md in updates:
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
                written.append((row_id, column_id))
                by_table.setdefault(table_id, []).append({
                    "row_id": row_id, "column_id": column_id,
                    "before": before_values[(row_id, column_id)], "after": content_md,
                })
                if table_id not in written_table_ids:
                    written_table_ids.append(table_id)
            for table_id in written_table_ids:
                db.execute(
                    "UPDATE knowhow_tables SET mutation_seq = mutation_seq + 1 WHERE id = ?",
                    (table_id,),
                )
            for written_table_id, cells in by_table.items():
                record_change(
                    db, new_id=self.new_id, now=self.now,
                    table_id=written_table_id,
                    kind="cell_update", payload={"cells": cells},
                    actor=actor, origin=origin,
                )
        return {"written": written, "conflict": False}

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
        actor: str = "",
        origin: str = "user",
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
        guarantee both halves exist.

        版本管理（spec §5）：``before`` is the four value fields
        (``code_text``/``language``/``updated_by``/``cell_content_hash``) of
        whatever attachment already lived at ``(row_id, column_id)``, read
        via a SELECT preceding the UPSERT — ``None`` (not ``{}``) on a first
        write, mirroring ``update_knowhow_cell``'s "cell didn't exist yet"
        convention. ``table_id`` is resolved from ``row_id`` (this method's
        own signature carries none) the same way ``update_knowhow_cell``
        does — AFTER the UPSERT, so its ``FOREIGN KEY(row_id)`` has already
        confirmed the row is real; the ``is not None`` guard mirrors that
        call site's defensive style byte-for-byte."""
        now = self.now()
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            before_row = db.execute(
                "SELECT code_text, language, updated_by, cell_content_hash "
                "FROM knowhow_cell_code WHERE row_id = ? AND column_id = ?",
                (row_id, column_id),
            ).fetchone()
            before = dict(before_row) if before_row is not None else None
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
            table_row = db.execute(
                "SELECT table_id FROM knowhow_rows WHERE id = ?", (row_id,)
            ).fetchone()
            if table_row is not None:
                record_change(
                    db, new_id=self.new_id, now=self.now,
                    table_id=table_row["table_id"],
                    kind="cell_code_put",
                    payload={
                        "row_id": row_id, "column_id": column_id,
                        "before": before,
                        "after": {
                            "code_text": code_text,
                            "language": language or "",
                            "updated_by": updated_by or "",
                            "cell_content_hash": cell_content_hash,
                        },
                    },
                    actor=actor, origin=origin,
                )
        return row["id"]

    def get_knowhow_cell_code(self, row_id: str, column_id: str) -> "dict | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM knowhow_cell_code WHERE row_id = ? AND column_id = ?",
                (row_id, column_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def delete_knowhow_cell_code(
        self, row_id: str, column_id: str, actor: str = "", origin: str = "user"
    ) -> None:
        """Silent no-op when there is nothing to delete (zero-row DELETE
        convention).

        版本管理（spec §5）：``before`` is read (same four fields as
        ``upsert_knowhow_cell_code``) BEFORE the DELETE; nothing there means
        nothing to delete, so this returns immediately — no DML, no flow
        entry, the silent no-op preserved verbatim. ``table_id`` resolves
        from ``row_id`` the same way ``upsert_knowhow_cell_code`` does;
        ``after`` is always ``None`` (the attachment is gone)."""
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            before_row = db.execute(
                "SELECT code_text, language, updated_by, cell_content_hash "
                "FROM knowhow_cell_code WHERE row_id = ? AND column_id = ?",
                (row_id, column_id),
            ).fetchone()
            if before_row is None:
                return  # nothing to delete — nothing to record
            db.execute(
                "DELETE FROM knowhow_cell_code WHERE row_id = ? AND column_id = ?",
                (row_id, column_id),
            )
            table_row = db.execute(
                "SELECT table_id FROM knowhow_rows WHERE id = ?", (row_id,)
            ).fetchone()
            if table_row is not None:
                record_change(
                    db, new_id=self.new_id, now=self.now,
                    table_id=table_row["table_id"],
                    kind="cell_code_delete",
                    payload={
                        "row_id": row_id, "column_id": column_id,
                        "before": dict(before_row), "after": None,
                    },
                    actor=actor, origin=origin,
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

    def delete_source_asset_rows(self, source_id: str) -> list[dict]:
        """Delete every ``notebook_assets`` row linked to ``source_id`` and
        return the complete deleted rows so the caller can remove their
        on-disk files without an id-list plus per-row lookup N+1 (this store is
        rows-only, same division of labor as
        the notebook-delete/sweep_orphan_assets asset-GC paths in
        ``AssetService``/``maintenance``)."""
        with self.database.write() as db:
            rows = db.execute(
                "DELETE FROM notebook_assets WHERE source_id = ? RETURNING *",
                (source_id,),
            ).fetchall()
        return [dict(row) for row in rows]


__all__ = ["KnowhowStore", "VALID_KINDS"]
