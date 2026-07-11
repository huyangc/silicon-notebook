from __future__ import annotations

import json
import sqlite3
from typing import Callable, List, Literal, Sequence

from app.models.schemas import NotebookCreate, NotebookUpdate
from app.repositories.sqlite.database import SqliteDatabase

# Knowledge-object statuses that count as "usable" for retrieval and the
# NotebookSummary type counts.  Task 13 moved the canonical definition to
# app.services.knowledge_contracts; this re-export keeps the Task-8 import
# sites (facade / notebook_catalog) pointing at the SAME tuple.
from app.services.knowledge_contracts import USABLE_STATUSES  # noqa: F401


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

    def tier_map(self, notebook_ids: Sequence[str]) -> dict[str, str]:
        ids = list(dict.fromkeys(notebook_id for notebook_id in notebook_ids if notebook_id))
        if not ids:
            return {}
        out: dict[str, str] = {}
        with self.database.connect() as db:
            for offset in range(0, len(ids), 900):
                batch = ids[offset:offset + 900]
                placeholders = ",".join("?" for _ in batch)
                for row in db.execute(
                    f"SELECT id, tier FROM notebooks WHERE id IN ({placeholders})",
                    batch,
                ):
                    out[row["id"]] = row["tier"] or "personal"
        return out

    def participant_notebook_ids(self, active_notebook_id: str) -> list[str]:
        with self.database.connect() as db:
            return self.participant_ids(db, active_notebook_id)

    @staticmethod
    def participant_ids(db: sqlite3.Connection, active_notebook_id: str) -> list[str]:
        rows = db.execute(
            "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
            (active_notebook_id,),
        ).fetchall()
        return [active_notebook_id] + [row["id"] for row in rows]

    @staticmethod
    def participant_rows(db: sqlite3.Connection, active_notebook_id: str):
        base_rows = db.execute(
            "SELECT id, tier FROM notebooks WHERE tier='base' AND id != ?",
            (active_notebook_id,),
        ).fetchall()
        active_row = db.execute(
            "SELECT id, tier FROM notebooks WHERE id=?", (active_notebook_id,),
        ).fetchone()
        return active_row, base_rows

    @staticmethod
    def participant_tiers(db: sqlite3.Connection, active_notebook_id: str):
        rows = db.execute(
            "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
            (active_notebook_id,),
        ).fetchall()
        ids = [active_notebook_id] + [row["id"] for row in rows]
        tiers = {}
        for notebook_id in ids:
            row = db.execute(
                "SELECT tier FROM notebooks WHERE id=?", (notebook_id,),
            ).fetchone()
            tiers[notebook_id] = row["tier"] if row else "personal"
        return ids, tiers

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

    # ------------------------------------------------- Task 26 primitives
    @staticmethod
    def meta_row(db: sqlite3.Connection, notebook_id: str) -> "dict | None":
        """Name + purpose_auto flag for the metadata-augmentation guard
        (moved verbatim from the facade's `_notebook_meta_row`)."""
        row = db.execute(
            "SELECT name, purpose_auto FROM notebooks WHERE id = ?", (notebook_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "name": row["name"],
            "purpose_auto": ("purpose_auto" in row.keys() and row["purpose_auto"] == 1),
        }

    def apply_meta(
        self, db: sqlite3.Connection, notebook_id: str, *,
        guard_name: str, name: str, purpose: str,
    ) -> None:
        """Optimistically apply auto-derived notebook metadata: the name only
        overwrites the placeholder we read (no clobber of a concurrent
        rename); the purpose only lands while purpose_auto=1. The caller owns
        the ONE write transaction; the clock rides the compatibility seam."""
        if name:
            db.execute(
                "UPDATE notebooks SET name = ?, updated_at = ? WHERE id = ? AND name = ?",
                (name, self.now(), notebook_id, guard_name),
            )
        if purpose:
            db.execute(
                "UPDATE notebooks SET purpose = ?, updated_at = ? "
                "WHERE id = ? AND purpose_auto = 1",
                (purpose, self.now(), notebook_id),
            )

    @staticmethod
    def tier_on(db: sqlite3.Connection, notebook_id: str) -> str:
        row = db.execute(
            "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
        ).fetchone()
        return str(row["tier"]) if row is not None and row["tier"] else ""

    def meta_for_notebook(self, notebook_id: str) -> "dict | None":
        with self.database.connect() as db:
            return self.meta_row(db, notebook_id)

    def apply_meta_for_notebook(
        self, notebook_id: str, *, guard_name: str, name: str, purpose: str
    ) -> None:
        with self.database.write() as db:
            self.apply_meta(
                db, notebook_id, guard_name=guard_name, name=name, purpose=purpose
            )

    def tier(self, notebook_id: str) -> str:
        with self.database.connect() as db:
            return self.tier_on(db, notebook_id)

    def participant_notebook_ids(self, notebook_id: str) -> List[str]:
        with self.database.connect() as db:
            return self.participant_ids(db, notebook_id)
