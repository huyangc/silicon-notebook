from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Callable, Sequence

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase

# Deep-copy row snapshot: table -> the exact SELECT the former mixin issued.
# "notebooks" carries the single source row that the copy service rewrites
# into the destination's hidden 'copying' sentinel.  Order matters only for
# readability; the INSERT order (FK-safe) is owned by NotebookCopyService.
_COPY_SNAPSHOT_QUERIES: tuple[tuple[str, str], ...] = (
    ("notebooks", "SELECT * FROM notebooks WHERE id = ?"),
    ("sources", "SELECT * FROM sources WHERE notebook_id = ?"),
    (
        "source_elements",
        "SELECT se.* FROM source_elements se JOIN sources s ON s.id = se.source_id "
        "WHERE s.notebook_id = ?",
    ),
    ("chunks", "SELECT * FROM chunks WHERE notebook_id = ?"),
    ("knowledge_objects", "SELECT * FROM knowledge_objects WHERE notebook_id = ?"),
    ("knowledge_relations", "SELECT * FROM knowledge_relations WHERE notebook_id = ?"),
    ("chunk_embeddings", "SELECT * FROM chunk_embeddings WHERE notebook_id = ?"),
    (
        "element_embeddings",
        "SELECT ee.* FROM element_embeddings ee JOIN sources s ON s.id = ee.source_id "
        "WHERE s.notebook_id = ?",
    ),
    ("knowledge_embeddings", "SELECT * FROM knowledge_embeddings WHERE notebook_id = ?"),
    ("relation_embeddings", "SELECT * FROM relation_embeddings WHERE notebook_id = ?"),
    ("concept_clusters", "SELECT * FROM concept_clusters WHERE notebook_id = ?"),
)

# Tables copy_notebook validates row-parity on after the copy completes.
_COPY_VALIDATED_TABLES = (
    "sources",
    "chunks",
    "knowledge_objects",
    "knowledge_relations",
    "concept_clusters",
)


class SharingStore:
    """SQLite sharing/membership rows plus the connection-taking deep-copy
    primitives (snapshot / chunked insert / compensate / sweep / validate).

    Row-level only — share-token generation policy, ID remapping, filesystem
    copy and compensation ORDERING live in app.services.notebook_sharing.
    ``insert_row`` is the facade's ``_insert_row`` compatibility seat injected
    late so per-instance monkeypatches keep observing every copied row.
    """

    def __init__(
        self,
        database: SqliteDatabase,
        settings: Settings,
        *,
        now: Callable[[], str],
        insert_row: Callable[[sqlite3.Connection, str, dict], None],
    ) -> None:
        self.database = database
        self.settings = settings
        self.now = now
        self.insert_row = insert_row

    # ------------------------------------------------------------ share rows
    def set_share_token(self, notebook_id: str, token: str) -> str:
        """Mark shared.  ``token`` is the candidate for a first-time share; an
        already-shared notebook keeps its existing token (idempotent re-share)
        — the read-choose-write stays inside ONE write transaction exactly as
        the former mixin did."""
        with self.database.write() as db:
            row = db.execute(
                "SELECT is_shared, share_token FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
            chosen = (
                row["share_token"]
                if row["is_shared"] and row["share_token"]
                else token
            )
            db.execute(
                "UPDATE notebooks SET is_shared = 1, share_token = ?, updated_at = ? WHERE id = ?",
                (chosen, self.now(), notebook_id),
            )
        return str(chosen)

    def clear_share(self, notebook_id: str) -> None:
        """Unshare + kick every member in ONE write transaction."""
        with self.database.write() as db:
            db.execute(
                "UPDATE notebooks SET is_shared = 0, share_token = NULL, updated_at = ? WHERE id = ?",
                (self.now(), notebook_id),
            )
            db.execute("DELETE FROM notebook_members WHERE notebook_id = ?", (notebook_id,))

    def find_by_token(self, token: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id FROM notebooks WHERE share_token = ? AND is_shared = 1", (token,)
            ).fetchone()
        return row["id"] if row else None

    def list_shared_by_owner(self, user_id: str) -> list[sqlite3.Row]:
        with self.database.connect() as db:
            return db.execute(
                "SELECT id, name, share_token FROM notebooks "
                "WHERE created_by = ? AND is_shared = 1 ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()

    def notebook_row(self, notebook_id: str) -> "sqlite3.Row | None":
        with self.database.connect() as db:
            return db.execute(
                "SELECT * FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()

    @staticmethod
    def notebook_row_on(
        db: sqlite3.Connection, notebook_id: str
    ) -> "sqlite3.Row | None":
        """Read a notebook on the caller's summary-hydration snapshot."""
        return db.execute(
            "SELECT * FROM notebooks WHERE id = ?", (notebook_id,)
        ).fetchone()

    def shared_preview_rows(self, notebook_id: str) -> "tuple[str, list[str]]":
        """(owner_display, first-50 source titles) for the share preview.

        Excludes Memory-derived synthetic sources (source_type='memory'): this
        title list is shown to a prospective copier/joiner in the /shared/{token}
        modal — a user-facing surface, hidden the same as list_sources. (The
        preview's numeric source_count comes from notebook_copy_stats, the
        copy-materialization true set, deliberately unfiltered.)"""
        with self.database.connect() as db:
            owner = db.execute(
                "SELECT u.username FROM notebooks nb LEFT JOIN users u ON u.id = nb.created_by "
                "WHERE nb.id = ?",
                (notebook_id,),
            ).fetchone()
            titles = [
                row["title"]
                for row in db.execute(
                    "SELECT title FROM sources WHERE notebook_id = ? AND source_type != 'memory' "
                    "ORDER BY created_at LIMIT 50",
                    (notebook_id,),
                ).fetchall()
            ]
        return (owner["username"] if owner and owner["username"] else "", titles)

    # ------------------------------------------------------- access & members
    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT created_by FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
        return bool(row) and row["created_by"] == user_id

    def is_member(self, notebook_id: str, user_id: str) -> bool:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 FROM notebook_members WHERE notebook_id = ? AND user_id = ?",
                (notebook_id, user_id),
            ).fetchone()
        return row is not None

    def add_member(self, notebook_id: str, user_id: str) -> None:
        with self.database.write() as db:
            db.execute(
                "INSERT OR IGNORE INTO notebook_members (notebook_id, user_id, role, added_at) "
                "VALUES (?, ?, 'reader', ?)",
                (notebook_id, user_id, self.now()),
            )

    def remove_member(self, notebook_id: str, user_id: str) -> None:
        with self.database.write() as db:
            db.execute(
                "DELETE FROM notebook_members WHERE notebook_id = ? AND user_id = ?",
                (notebook_id, user_id),
            )

    def kick_all_members(self, notebook_id: str) -> None:
        with self.database.write() as db:
            db.execute("DELETE FROM notebook_members WHERE notebook_id = ?", (notebook_id,))

    def list_members(self, notebook_id: str) -> list:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT u.username AS username, m.added_at AS added_at "
                "FROM notebook_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.notebook_id = ? ORDER BY m.added_at ASC",
                (notebook_id,),
            ).fetchall()
        return [
            {"username": row["username"], "added_at": row["added_at"]} for row in rows
        ]

    # ------------------------------------------------------------- ownership
    def source_owner(self, source_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT nb.created_by AS owner FROM sources s "
                "JOIN notebooks nb ON nb.id = s.notebook_id WHERE s.id = ?",
                (source_id,),
            ).fetchone()
        return row["owner"] if row else None

    def conversation_owner(self, conversation_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT created_by AS owner FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        return row["owner"] if row else None

    def answer_owner(self, answer_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT nb.created_by AS owner FROM answers a "
                "JOIN notebooks nb ON nb.id = a.notebook_id WHERE a.id = ?",
                (answer_id,),
            ).fetchone()
        return row["owner"] if row else None

    def source_notebook_id(self, source_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT notebook_id FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        return row["notebook_id"] if row else None

    def answer_notebook_id(self, answer_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT notebook_id FROM answers WHERE id = ?", (answer_id,)
            ).fetchone()
        return row["notebook_id"] if row else None

    # ------------------------------------------------------- copy primitives
    def snapshot_copy_rows(self, notebook_id: str) -> dict[str, list[dict]]:
        """Read every copyable table's rows for the source notebook (the exact
        SELECTs the former mixin issued, one read connection)."""
        snapshot: dict[str, list[dict]] = {}
        with self.database.connect() as db:
            for table, sql in _COPY_SNAPSHOT_QUERIES:
                snapshot[table] = [
                    dict(row) for row in db.execute(sql, (notebook_id,)).fetchall()
                ]
        return snapshot

    def insert_copy_rows(
        self,
        table: str,
        rows: Sequence[dict],
        *,
        chunk_size: int,
    ) -> None:
        """Chunked insert: one write transaction per chunk (the write lock is
        released between chunks, P1-4), every row through the facade's
        ``_insert_row`` seat."""
        for index in range(0, len(rows), chunk_size):
            with self.database.write() as db:
                for data in rows[index:index + chunk_size]:
                    self.insert_row(db, table, data)

    def insert_fts_rows(
        self,
        sql: str,
        rows: Sequence[tuple],
        *,
        chunk_size: int,
    ) -> None:
        """Chunked executemany for the FTS mirror tables (same statement shape
        as the former mixin — NOT routed through the per-row insert seat)."""
        for index in range(0, len(rows), chunk_size):
            with self.database.write() as db:
                db.executemany(sql, rows[index:index + chunk_size])

    def validate_copy(self, source_notebook_id: str, new_id: str) -> None:
        """Post-copy integrity self-check: row parity per table + no dangling
        relation endpoints.  Raises RuntimeError (copy is then compensated)."""
        with self.database.connect() as db:
            for table in _COPY_VALIDATED_TABLES:
                copied_count = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE notebook_id = ?", (new_id,)
                ).fetchone()[0]
                source_count = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE notebook_id = ?",
                    (source_notebook_id,),
                ).fetchone()[0]
                if copied_count != source_count:
                    raise RuntimeError(
                        f"copy_notebook: {table} 行数不一致 {copied_count}!={source_count}"
                    )
            dangling = db.execute(
                "SELECT COUNT(*) FROM knowledge_relations r WHERE r.notebook_id = ? AND ("
                "r.source_object_id NOT IN "
                "(SELECT id FROM knowledge_objects WHERE notebook_id = ?) OR "
                "r.target_object_id NOT IN "
                "(SELECT id FROM knowledge_objects WHERE notebook_id = ?))",
                (new_id, new_id, new_id),
            ).fetchone()[0]
            if dangling:
                raise RuntimeError("copy_notebook: 关系存在悬空引用")

    def publish_copy(self, notebook_id: str, status: str) -> None:
        """Flip the hidden 'copying' sentinel to the source's original status."""
        with self.database.write() as db:
            db.execute(
                "UPDATE notebooks SET status = ?, updated_at = ? WHERE id = ?",
                (status, self.now(), notebook_id),
            )

    def compensate_copy(self, notebook_id: str) -> None:
        """Failure compensation: remove the destination's FTS mirrors, the
        no-FK knowledge_embeddings rows, and the 'copying' sentinel (children
        cascade off the notebooks row).  Never touches the source."""
        with self.database.write() as db:
            db.execute("DELETE FROM kg_objects_fts WHERE notebook_id = ?", (notebook_id,))
            db.execute("DELETE FROM chunks_fts WHERE notebook_id = ?", (notebook_id,))
            db.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id = ?", (notebook_id,)
            )
            db.execute(
                "DELETE FROM notebooks WHERE id = ? AND status = 'copying'", (notebook_id,)
            )

    def sweep_stale_copies(self, *, created_by: "str | None" = None) -> int:
        """Delete expired copy sentinels without touching concurrent copies."""
        cutoff = (
            datetime.now()
            - timedelta(seconds=max(1, self.settings.notebook_copy_stale_seconds))
        ).replace(microsecond=0).isoformat()
        with self.database.write() as db:
            if created_by is not None:
                rows = db.execute(
                    "SELECT id FROM notebooks WHERE status = 'copying' AND created_by = ? "
                    "AND created_at < ?",
                    (created_by, cutoff),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id FROM notebooks WHERE status = 'copying' AND created_at < ?",
                    (cutoff,),
                ).fetchall()
            stuck_ids = [row["id"] for row in rows]
            if not stuck_ids:
                return 0
            placeholders = ",".join("?" for _ in stuck_ids)
            db.execute(
                f"DELETE FROM kg_objects_fts WHERE notebook_id IN ({placeholders})", stuck_ids
            )
            db.execute(
                f"DELETE FROM chunks_fts WHERE notebook_id IN ({placeholders})", stuck_ids
            )
            db.execute(
                f"DELETE FROM knowledge_embeddings WHERE notebook_id IN ({placeholders})",
                stuck_ids,
            )
            db.execute(f"DELETE FROM notebooks WHERE id IN ({placeholders})", stuck_ids)
        return len(stuck_ids)

    @staticmethod
    def insert_row_values(db: sqlite3.Connection, table: str, data: dict) -> None:
        """Insert one dict-shaped row (Task 26: the facade `_insert_row`
        compatibility seat's SQL body, moved verbatim — the seat itself stays
        the injectable per-row boundary for copy failure injection)."""
        columns = list(data.keys())
        db.execute(
            f"INSERT INTO {table} ({','.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))})",
            [data[column] for column in columns],
        )
