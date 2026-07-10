from __future__ import annotations

import json
import sqlite3
from typing import Callable, List, Literal

from app.models.schemas import NotebookCreate, NotebookUpdate
from app.repositories.sqlite.database import SqliteDatabase

# Knowledge-object statuses that count as "usable" for retrieval and the
# NotebookSummary type counts.  Canonical definition lives here at the
# component layer; app.services.sqlite_repository re-exports it as the frozen
# compatibility name `USABLE_STATUSES`.
USABLE_STATUSES = ("approved", "reviewed", "project_specific", "conflict")


class NotebookStore:
    """SQLite notebooks-table row persistence: CRUD, tier transitions and row
    deletion (including the orphan knowledge-embedding cleanup that the
    schema's missing FK makes necessary).  Row-level only — summary projection
    and orchestration live in app.services.notebook_catalog."""

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

    def create_row(self, payload: NotebookCreate, created_by: str) -> str:
        """Minimal creation: only name + description (purpose). When the user
        leaves the description blank it is flagged auto (purpose_auto=1) and
        later derived from the first batch of uploaded sources."""
        notebook_id = self.new_id("nb")
        now = self.now()
        purpose = (payload.purpose or "").strip()
        purpose_auto = 0 if purpose else 1
        with self.database.write() as db:
            db.execute(
                """
                INSERT INTO notebooks
                (id, name, purpose, primary_domain, status, created_by, created_at, updated_at,
                 purpose_auto)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notebook_id,
                    payload.name,
                    purpose,
                    "Semiconductor",
                    "draft",
                    created_by,
                    now,
                    now,
                    purpose_auto,
                ),
            )
        return notebook_id

    def get_row(
        self, notebook_id: str, *, include_copying: bool = False
    ) -> sqlite3.Row:
        """Fetch one notebooks row; raises KeyError when absent.  By default
        status='copying' rows (copy_notebook's in-progress sentinel, P1-4) are
        treated as not-yet-existing; pass include_copying=True for the
        copy/sharing paths that must see them."""
        sql = "SELECT * FROM notebooks WHERE id = ?"
        if not include_copying:
            sql += " AND status != 'copying'"
        with self.database.connect() as db:
            row = db.execute(sql, (notebook_id,)).fetchone()
        if row is None:
            raise KeyError(notebook_id)
        return row

    def update_row(self, notebook_id: str, payload: NotebookUpdate) -> None:
        updates: List[str] = []
        values: List[str] = []
        if payload.name is not None:
            updates.append("name = ?")
            values.append(payload.name.strip() or "Untitled notebook")
        if payload.purpose is not None:
            updates.append("purpose = ?")
            values.append(payload.purpose.strip())
            # A user-edited description is manual; stop auto-regenerating it.
            updates.append("purpose_auto = ?")
            values.append(0)
        if payload.primary_domain is not None:
            updates.append("primary_domain = ?")
            values.append(payload.primary_domain.strip() or "Semiconductor")
        if payload.target_users is not None:
            updates.append("target_users = ?")
            values.append(payload.target_users.strip())
        if payload.access_scope is not None:
            updates.append("access_scope = ?")
            values.append(payload.access_scope.strip())
        for field in ("expected_questions", "source_types", "taxonomy"):
            value = getattr(payload, field)
            if value is not None:
                updates.append(f"{field} = ?")
                values.append(json.dumps(value, ensure_ascii=False))
        if updates:
            updates.append("updated_at = ?")
            values.append(self.now())
            values.append(notebook_id)
            with self.database.write() as db:
                db.execute(
                    f"UPDATE notebooks SET {', '.join(updates)} WHERE id = ?",
                    values,
                )

    def set_tier(
        self, notebook_id: str, tier: Literal["base", "personal"]
    ) -> None:
        """tier='base': mark THE single authoritative base KG — 基准库全局唯一,
        同一事务里先把其它 tier='base' 的 notebook 降级为 'personal', 再把目标设为
        'base'.  tier='personal': symmetric local reset.  Both idempotent."""
        now = self.now()
        with self.database.write() as db:
            if tier == "base":
                db.execute(
                    "UPDATE notebooks SET tier='personal', updated_at=? WHERE tier='base' AND id != ?",
                    (now, notebook_id),
                )
                db.execute(
                    "UPDATE notebooks SET tier='base', updated_at=? WHERE id=?",
                    (now, notebook_id),
                )
            else:
                db.execute(
                    "UPDATE notebooks SET tier='personal', updated_at=? WHERE id=?",
                    (now, notebook_id),
                )

    def delete_row_and_orphan_embeddings(self, notebook_id: str) -> list[str]:
        """Delete the notebooks row in ONE committed transaction and return the
        source file paths for the caller to remove AFTER the commit (DB first,
        files second — never the other way around)."""
        with self.database.write() as db:
            source_rows = db.execute(
                "SELECT file_path FROM sources WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
            # knowledge_embeddings has no FK to notebooks (see DDL), so
            # deleting the notebooks row does NOT cascade to it. Delete it here so
            # every public delete caller leaves zero orphan embedding rows.
            # (element_embeddings DOES cascade transitively via
            # source_elements -> sources -> notebooks, so it needs no explicit delete.)
            db.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id = ?",
                (notebook_id,),
            )
            db.execute("DELETE FROM kg_objects_fts WHERE notebook_id = ?", (notebook_id,))
            db.execute("DELETE FROM chunks_fts WHERE notebook_id = ?", (notebook_id,))
            db.execute("DELETE FROM notebooks WHERE id = ?", (notebook_id,))
        return [row["file_path"] for row in source_rows]
