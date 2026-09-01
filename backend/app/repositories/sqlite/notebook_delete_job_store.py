"""SQLite persistence for the delete-jobization job carrier (batch 3·W1
PR-3, design doc §T-3/§T-4). PostgreSQL twin:
``postgres/notebook_delete_job_store.py`` — see that module's docstring for
the full phase-ownership and lease-fencing rationale; this file mirrors it
statement-for-statement with SQLite's dialect (``?`` placeholders, no
``COLLATE "C"`` qualifiers to reason about, native ``executemany``, inline
``IN (?,?,...)`` lists in place of ``= ANY(%s)``, ``rowid`` in place of
``ctid``).
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Callable

from app.repositories.sqlite.access_sql import NOTEBOOK_LIVE_SQL
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.ports import NotebookAlreadyDeletingError

_log = logging.getLogger(__name__)

# §4.4/code-review P2-g: SQLite's two FTS5 shadow tables that mirror
# `knowledge_objects`/`chunks` but carry no FK to `notebooks` (virtual
# tables never do) -- phase 5's `delete_row_and_orphan_embeddings` used to
# delete them unconditionally as its own tail step; moved here so they clear
# alongside their real table's OWN phase-3 unit instead (§4.4's "两条 FTS
# 显式删除进相位 3 各自的批"). PostgreSQL has no FTS5 shadow tables for these
# (its full-text search rides GIN trgm indexes on the real columns), so the
# PostgreSQL twin's ``delete_fts_shadow`` is a no-op -- this map is the only
# backend-specific difference the runner ever has to be unaware of.
_FTS_SHADOW_TABLE = {
    "knowledge_objects": "kg_objects_fts",
    "chunks": "chunks_fts",
}


def _new_lease_token() -> str:
    return secrets.token_hex(16)


class NotebookDeleteJobStore:
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
    def _row(row) -> dict:
        return {
            "id": row["id"],
            "notebook_id": row["notebook_id"],
            "status": row["status"],
            "phase": row["phase"],
            "cursor_table": row["cursor_table"],
            "cursor_key": row["cursor_key"],
            "deleted_rows": int(row["deleted_rows"]),
            "lease_token": row["lease_token"],
            "attempts": int(row["attempts"]),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }

    def request(self, notebook_id: str, created_by: str) -> dict:
        """The tombstone CAS + same-transaction job-row insert (§T-2). See
        the PostgreSQL twin's docstring for the ``created_by``/404-vs-409
        rationale — identical here."""
        del created_by
        now = self.now()
        with self.database.write(operation="notebook_delete.request") as db:
            cas = db.execute(
                "UPDATE notebooks SET status='deleting',updated_at=? "
                f"WHERE id=? AND {NOTEBOOK_LIVE_SQL}",
                (now, notebook_id),
            )
            if cas.rowcount != 1:
                existing = db.execute(
                    "SELECT id FROM notebooks WHERE id=?", (notebook_id,)
                ).fetchone()
                if existing is None:
                    raise KeyError(notebook_id)
                raise NotebookAlreadyDeletingError(notebook_id)
            job_id = self.new_id("ndj")
            db.execute(
                "INSERT INTO notebook_delete_jobs"
                "(id,notebook_id,status,phase,cursor_table,cursor_key,"
                "deleted_rows,lease_token,attempts,error_code,error_message,"
                "created_at,updated_at,finished_at) "
                "VALUES (?,?,'queued','mark','','',0,'',0,'','',?,?,NULL)",
                (job_id, notebook_id, now, now),
            )
        return self.get(job_id)

    def purge_failed_jobs(self, notebook_id: str) -> None:
        """P1-E. PostgreSQL twin's docstring has the full rationale."""
        with self.database.write(
            operation="notebook_delete.purge_failed_jobs"
        ) as db:
            old_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM notebook_delete_jobs "
                    "WHERE notebook_id=? AND status='failed'",
                    (notebook_id,),
                ).fetchall()
            ]
            if not old_ids:
                return
            ph = ",".join("?" for _ in old_ids)
            db.execute(
                f"DELETE FROM notebook_delete_files WHERE job_id IN ({ph})",
                old_ids,
            )
            db.execute(
                f"DELETE FROM notebook_delete_jobs WHERE id IN ({ph})",
                old_ids,
            )

    def recreate_for_deleting_notebook(
        self, notebook_id: str, *, attempts: int = 0
    ) -> dict:
        """Sweep driver B (§T-4) + P1-E's carried-forward ``attempts``.
        PostgreSQL twin's docstring has the full rationale."""
        now = self.now()
        job_id = self.new_id("ndj")
        try:
            with self.database.write(
                operation="notebook_delete.recreate_missing_job"
            ) as db:
                db.execute(
                    "INSERT INTO notebook_delete_jobs"
                    "(id,notebook_id,status,phase,cursor_table,cursor_key,"
                    "deleted_rows,lease_token,attempts,error_code,"
                    "error_message,created_at,updated_at,finished_at) "
                    "VALUES (?,?,'queued','mark','','',0,'',?,'','',?,?,NULL)",
                    (job_id, notebook_id, attempts, now, now),
                )
        except sqlite3.IntegrityError:
            existing = self.latest_for_notebook(notebook_id)
            if existing is not None and existing["status"] in (
                "queued", "running", "waiting",
            ):
                return existing
            raise
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM notebook_delete_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row(row)

    def latest_for_notebook(self, notebook_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM notebook_delete_jobs WHERE notebook_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (notebook_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    def mark_running(self, job_id: str, *, stale_cutoff_seconds: float) -> str | None:
        """P2-a owner/lease CAS. PostgreSQL twin's docstring has the full
        rationale; SQLite's cutoff mirrors ``list_stale``'s own
        ``datetime.now() - timedelta(...)`` idiom."""
        token = _new_lease_token()
        cutoff = (
            datetime.now() - timedelta(seconds=max(1, stale_cutoff_seconds))
        ).replace(microsecond=0).isoformat()
        with self.database.write(operation="notebook_delete.mark_running") as db:
            cursor = db.execute(
                "UPDATE notebook_delete_jobs SET status='running',"
                "lease_token=?,updated_at=? WHERE id=? AND "
                "(status IN ('queued','waiting') "
                "OR (status='running' AND updated_at<?))",
                (token, self.now(), job_id, cutoff),
            )
        return token if cursor.rowcount == 1 else None

    def advance_phase(
        self, job_id: str, phase: str, *, lease_token: str,
        cursor_table: str = "", cursor_key: str = "", deleted_delta: int = 0,
    ) -> bool:
        with self.database.write(operation="notebook_delete.advance_phase") as db:
            cursor = db.execute(
                "UPDATE notebook_delete_jobs SET phase=?,cursor_table=?,"
                "cursor_key=?,deleted_rows=deleted_rows+?,updated_at=? "
                "WHERE id=? AND status='running' AND lease_token=?",
                (
                    phase, cursor_table, cursor_key, deleted_delta,
                    self.now(), job_id, lease_token,
                ),
            )
        return cursor.rowcount == 1

    def mark_waiting(self, job_id: str, *, lease_token: str, note: str = "") -> bool:
        """Phase 2 (quiesce) timeout ONLY -- see the PostgreSQL twin's
        docstring for why P2-c reserves this status to quiesce alone."""
        with self.database.write(operation="notebook_delete.mark_waiting") as db:
            cursor = db.execute(
                "UPDATE notebook_delete_jobs SET status='waiting',"
                "error_message=?,updated_at=? WHERE id=? AND "
                "status='running' AND lease_token=?",
                (note, self.now(), job_id, lease_token),
            )
        return cursor.rowcount == 1

    def mark_queued(self, job_id: str, *, lease_token: str, note: str = "") -> bool:
        """Phases 3/4/5's independent-claim-unavailable hand-back (P1-B/
        P2-c)."""
        with self.database.write(operation="notebook_delete.mark_queued") as db:
            cursor = db.execute(
                "UPDATE notebook_delete_jobs SET status='queued',"
                "error_message=?,updated_at=? WHERE id=? AND "
                "status='running' AND lease_token=?",
                (note, self.now(), job_id, lease_token),
            )
        return cursor.rowcount == 1

    def finish(
        self, job_id: str, status: str, *, lease_token: str,
        error_code: str = "", error_message: str = "",
    ) -> bool:
        """P1-E: increments ``attempts``. P2-b (codex PR#659 round 1):
        lease-fenced — see the PostgreSQL twin's docstring for the full
        rationale (a lost lease means a new owner is necessarily already in
        place; letting a stale worker settle the row is the actual bug, not
        fencing it out). ``rowcount==0`` is a normal, expected outcome (the
        row already moved on under a new lease, or under a different
        status), logged rather than raised."""
        now = self.now()
        with self.database.write(operation="notebook_delete.finish") as db:
            cursor = db.execute(
                "UPDATE notebook_delete_jobs SET status=?,error_code=?,"
                "error_message=?,attempts=attempts+1,updated_at=?,"
                "finished_at=? WHERE id=? AND lease_token=? "
                "AND status IN ('queued','running','waiting')",
                (status, error_code, error_message, now, now, job_id, lease_token),
            )
        if cursor.rowcount != 1:
            _log.info(
                "notebook delete job %s: finish(%s) fenced out (lease no "
                "longer held) — a new owner is settling this job instead",
                job_id, status,
            )
        return cursor.rowcount == 1

    def materialize_paths_page(
        self, job_id: str, notebook_id: str, after_id: str, limit: int
    ) -> tuple[int, str | None]:
        with self.database.write(operation="notebook_delete.materialize_paths") as db:
            start = db.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 AS next_ordinal "
                "FROM notebook_delete_files WHERE job_id=?",
                (job_id,),
            ).fetchone()
            next_ordinal = int(start["next_ordinal"])
            page = db.execute(
                "SELECT id,file_path FROM sources "
                "WHERE notebook_id=? AND id>? ORDER BY id LIMIT ?",
                (notebook_id, after_id, limit),
            ).fetchall()
            if not page:
                return 0, None
            db.executemany(
                "INSERT INTO notebook_delete_files(job_id,ordinal,file_path) "
                "VALUES (?,?,?)",
                [
                    (job_id, next_ordinal + offset, row["file_path"] or "")
                    for offset, row in enumerate(page)
                ],
            )
        return len(page), page[-1]["id"]

    def notebook_exists(self, notebook_id: str) -> bool:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone()
        return row is not None

    def ownership_snapshot(self, job_id: str) -> dict | None:
        """P1-A/P2-a. PostgreSQL twin's docstring has the full rationale."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT j.status AS job_status, j.lease_token AS lease_token, "
                "n.status AS notebook_status "
                "FROM notebook_delete_jobs j "
                "LEFT JOIN notebooks n ON n.id=j.notebook_id "
                "WHERE j.id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "status": row["job_status"],
            "lease_token": row["lease_token"],
            "notebook_status": row["notebook_status"],
        }

    def finish_residual(self, job_id: str, *, lease_token: str) -> bool:
        """§T-4 driver-A's out-of-band-delete special case (P1-A).
        PostgreSQL twin's docstring has the full P2-b lease-fencing
        rationale. Deliberately does NOT delegate to ``cleanup_job_on``
        (shared with the successful-finalize path, which has no lease token
        to fence with and is instead gated by the in-process claim's
        ``verify_held()`` immediately before it runs) — this fences its own
        ``notebook_delete_jobs`` DELETE first and only cascades to the
        ``notebook_delete_files`` side table if that row was actually this
        worker's to delete, so a fenced-out call leaves BOTH tables
        untouched rather than half-deleted."""
        with self.database.write(
            operation="notebook_delete.finish_residual"
        ) as db:
            cursor = db.execute(
                "DELETE FROM notebook_delete_jobs WHERE id=? AND lease_token=?",
                (job_id, lease_token),
            )
            if cursor.rowcount == 1:
                db.execute(
                    "DELETE FROM notebook_delete_files WHERE job_id=?", (job_id,)
                )
        if cursor.rowcount != 1:
            _log.info(
                "notebook delete job %s: finish_residual() fenced out (lease "
                "no longer held) — a new owner is settling this job instead",
                job_id,
            )
        return cursor.rowcount == 1

    def list_stale(self, older_than_seconds: float) -> list[dict]:
        """Sweep driver A (§T-4). PostgreSQL twin's docstring has the full
        rationale; SQLite's cutoff mirrors ``sharing_store.sweep_stale_
        copies``'s own ``datetime.now() - timedelta(...)`` idiom (a plain
        ISO string compared lexicographically, same as every other SQLite
        timestamp column in this schema)."""
        cutoff = (
            datetime.now() - timedelta(seconds=max(1, older_than_seconds))
        ).replace(microsecond=0).isoformat()
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM notebook_delete_jobs "
                "WHERE status IN ('queued','running','waiting') "
                "AND updated_at<?",
                (cutoff,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_notebooks_missing_job(self) -> list[dict]:
        """P1-E. PostgreSQL twin's docstring has the full rationale."""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT n.id AS notebook_id, "
                "(SELECT j2.attempts FROM notebook_delete_jobs j2 "
                "  WHERE j2.notebook_id=n.id AND j2.status='failed' "
                "  ORDER BY j2.finished_at DESC LIMIT 1) AS last_attempts, "
                "(SELECT j2.finished_at FROM notebook_delete_jobs j2 "
                "  WHERE j2.notebook_id=n.id AND j2.status='failed' "
                "  ORDER BY j2.finished_at DESC LIMIT 1) AS last_finished_at "
                "FROM notebooks n LEFT JOIN notebook_delete_jobs j "
                "ON j.notebook_id=n.id AND j.status IN ('queued','running','waiting') "
                "WHERE n.status='deleting' AND j.id IS NULL"
            ).fetchall()
        return [
            {
                "notebook_id": row["notebook_id"],
                "last_attempts": (
                    int(row["last_attempts"])
                    if row["last_attempts"] is not None else None
                ),
                "last_finished_at": row["last_finished_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Batch 3·W1 PR-3 Phase B: phase 3 (rows) batch-delete primitives.
    # PostgreSQL twin's docstring has the full design-doc rationale (§1.3/
    # §1.5); this mirrors it with SQLite's dialect -- `rowid` in place of
    # `ctid` (§1.5's "SQLite 对等形"), `?` placeholders, and an inline
    # `IN (?,?,...)` list in place of `= ANY(%s)` (same idiom every other
    # SQLite store in this repository already uses for a dynamic id list).
    # ------------------------------------------------------------------

    # P1-D: same sub-batch idiom as ``sqlite/chunk_store.py``'s
    # ``CHUNK_ELEMENT_LOOKUP_BATCH`` -- SQLite additionally has a hard bound
    # parameter ceiling per statement (SQLITE_MAX_VARIABLE_NUMBER, commonly
    # 999-32766 depending on build), which an unbounded ``IN (?,?,...)`` list
    # can exceed outright, not just run long.
    _CHILD_BATCH_SIZE = 500

    def delete_fts_shadow(self, table: str, notebook_id: str) -> None:
        """§4.4/P2-g: delete this notebook's rows from ``table``'s FTS5
        shadow, if it has one (``knowledge_objects``/``chunks`` only) --
        moved here from phase 5's tail so it clears alongside its real
        table's own phase-3 unit. A single unbatched ``DELETE ... WHERE
        notebook_id=?`` (same shape phase 5 always used) -- these are
        virtual tables with no FK/cascade of their own, but no evidence this
        notebook-scoped delete is ever large enough to need batching either
        (unlike the real table it shadows, whose OWN batched delete is what
        actually bounds this phase's per-transaction cost)."""
        shadow = _FTS_SHADOW_TABLE.get(table)
        if shadow is None:
            return
        with self.database.write(
            operation=f"notebook_delete.rows.{shadow}"
        ) as db:
            db.execute(f"DELETE FROM {shadow} WHERE notebook_id=?", (notebook_id,))

    def delete_direct_page_form_one(
        self, table: str, id_column: str, filter_column: str,
        filter_value: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        with self.database.write(
            operation=f"notebook_delete.rows.{table}"
        ) as db:
            page = db.execute(
                f"SELECT {id_column} FROM {table} "
                f"WHERE {filter_column}=? AND {id_column}>? "
                f"ORDER BY {id_column} LIMIT ?",
                (filter_value, cursor, limit),
            ).fetchall()
            if not page:
                return 0, None
            ids = [row[id_column] for row in page]
            placeholders = ",".join("?" for _ in ids)
            db.execute(
                f"DELETE FROM {table} WHERE {id_column} IN ({placeholders}) "
                f"AND {filter_column}=?",
                (*ids, filter_value),
            )
        return len(ids), ids[-1]

    def delete_direct_batch_form_two(
        self, table: str, filter_column: str, filter_value: str, limit: int,
    ) -> int:
        with self.database.write(
            operation=f"notebook_delete.rows.{table}"
        ) as db:
            cursor = db.execute(
                f"DELETE FROM {table} WHERE rowid IN ("
                f"SELECT rowid FROM {table} WHERE {filter_column}=? LIMIT ?)",
                (filter_value, limit),
            )
        return cursor.rowcount

    def delete_knowhow_rows_page(
        self, notebook_id: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        """P1-D: see the PostgreSQL twin's docstring for the fanout-bound
        rationale."""
        with self.database.write(
            operation="notebook_delete.rows.knowhow_rows"
        ) as db:
            page = db.execute(
                "SELECT kr.id AS id FROM knowhow_rows kr "
                "JOIN knowhow_tables kt ON kt.id=kr.table_id "
                "WHERE kt.notebook_id=? AND kr.id>? "
                "ORDER BY kr.id LIMIT ?",
                (notebook_id, cursor, limit),
            ).fetchall()
            if not page:
                return 0, None
            row_ids = [row["id"] for row in page]
            rph = ",".join("?" for _ in row_ids)
            db.execute(
                f"DELETE FROM knowhow_cells WHERE row_id IN ({rph})", row_ids,
            )
            db.execute(
                f"DELETE FROM knowhow_cell_code WHERE row_id IN ({rph})", row_ids,
            )
            db.execute(
                f"DELETE FROM knowhow_rows WHERE id IN ({rph})", row_ids,
            )
        return len(row_ids), row_ids[-1]

    def delete_knowhow_tables_page(
        self, notebook_id: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        """Runs after ``delete_knowhow_rows_page`` -- see the PostgreSQL
        twin's docstring."""
        with self.database.write(
            operation="notebook_delete.rows.knowhow_tables"
        ) as db:
            page = db.execute(
                "SELECT id FROM knowhow_tables WHERE notebook_id=? AND id>? "
                "ORDER BY id LIMIT ?",
                (notebook_id, cursor, limit),
            ).fetchall()
            if not page:
                return 0, None
            table_ids = [row["id"] for row in page]
            tph = ",".join("?" for _ in table_ids)
            db.execute(
                f"DELETE FROM knowhow_columns WHERE table_id IN ({tph})",
                table_ids,
            )
            db.execute(
                f"DELETE FROM knowhow_changes WHERE table_id IN ({tph})",
                table_ids,
            )
            db.execute(
                f"DELETE FROM knowhow_milestones WHERE table_id IN ({tph})",
                table_ids,
            )
            db.execute(
                f"DELETE FROM knowhow_tables WHERE id IN ({tph}) "
                "AND notebook_id=?",
                (*table_ids, notebook_id),
            )
        return len(table_ids), table_ids[-1]

    def delete_indexing_pipeline_stages_page(
        self, notebook_id: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        with self.database.write(
            operation="notebook_delete.rows.indexing_pipeline_stages"
        ) as db:
            page = db.execute(
                "SELECT job_id FROM indexing_pipeline_stages "
                "WHERE notebook_id=? AND job_id>? ORDER BY job_id LIMIT ?",
                (notebook_id, cursor, limit),
            ).fetchall()
            if not page:
                return 0, None
            job_ids = [row["job_id"] for row in page]
            jph = ",".join("?" for _ in job_ids)
            # P1-D: drain via rowid-bounded sub-batches, same rationale as
            # the PostgreSQL twin's ctid loop.
            while True:
                deleted = db.execute(
                    "DELETE FROM indexing_pipeline_stage_sources WHERE rowid IN ("
                    "SELECT rowid FROM indexing_pipeline_stage_sources "
                    f"WHERE job_id IN ({jph}) LIMIT ?)",
                    (*job_ids, self._CHILD_BATCH_SIZE),
                )
                if deleted.rowcount == 0:
                    break
            db.execute(
                f"DELETE FROM indexing_pipeline_stages WHERE job_id IN ({jph}) "
                "AND notebook_id=?",
                (*job_ids, notebook_id),
            )
        return len(job_ids), job_ids[-1]

    def delete_memory_items_page(
        self, notebook_id: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        with self.database.write(
            operation="notebook_delete.rows.memory_items"
        ) as db:
            page = db.execute(
                "SELECT id FROM memory_items WHERE notebook_id=? AND id>? "
                "ORDER BY id LIMIT ?",
                (notebook_id, cursor, limit),
            ).fetchall()
            if not page:
                return 0, None
            memory_ids = [row["id"] for row in page]
            mph = ",".join("?" for _ in memory_ids)
            db.execute(
                f"DELETE FROM memory_embeddings WHERE memory_id IN ({mph})",
                memory_ids,
            )
            db.execute(
                f"DELETE FROM memory_provenance WHERE memory_id IN ({mph})",
                memory_ids,
            )
            db.execute(
                f"DELETE FROM memory_revisions WHERE memory_id IN ({mph})",
                memory_ids,
            )
            db.execute(
                f"DELETE FROM memory_items WHERE id IN ({mph}) AND notebook_id=?",
                (*memory_ids, notebook_id),
            )
        return len(memory_ids), memory_ids[-1]

    def _drain_children_by_parent_ids(
        self, table: str, parent_column: str, parent_ids: list[str],
    ) -> None:
        """P1-D shared helper. PostgreSQL twin's docstring has the full
        rationale."""
        ph = ",".join("?" for _ in parent_ids)
        while True:
            with self.database.write(
                operation=f"notebook_delete.rows.{table}"
            ) as db:
                deleted = db.execute(
                    f"DELETE FROM {table} WHERE rowid IN ("
                    f"SELECT rowid FROM {table} WHERE {parent_column} IN ({ph}) "
                    f"LIMIT ?)",
                    (*parent_ids, self._CHILD_BATCH_SIZE),
                )
            if deleted.rowcount == 0:
                return

    def delete_source_elements_page(
        self, notebook_id: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        with self.database.connect() as db:
            page = db.execute(
                "SELECT id FROM sources WHERE notebook_id=? AND id>? "
                "ORDER BY id LIMIT ?",
                (notebook_id, cursor, limit),
            ).fetchall()
        if not page:
            return 0, None
        source_ids = [row["id"] for row in page]
        self._drain_children_by_parent_ids(
            "source_elements", "source_id", source_ids,
        )
        return len(source_ids), source_ids[-1]

    def delete_ask_trace_steps_page(
        self, notebook_id: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        with self.database.connect() as db:
            page = db.execute(
                "SELECT id FROM ask_jobs WHERE notebook_id=? AND id>? "
                "ORDER BY id LIMIT ?",
                (notebook_id, cursor, limit),
            ).fetchall()
        if not page:
            return 0, None
        job_ids = [row["id"] for row in page]
        self._drain_children_by_parent_ids(
            "ask_trace_steps", "job_id", job_ids,
        )
        return len(job_ids), job_ids[-1]

    def table_has_rows(
        self, table: str, filter_column: str, filter_value: str,
    ) -> bool:
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT 1 FROM {table} WHERE {filter_column}=? LIMIT 1",
                (filter_value,),
            ).fetchone()
        return row is not None

    def list_files_page(
        self, job_id: str, after_ordinal: int, limit: int,
    ) -> list[dict]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT ordinal,file_path FROM notebook_delete_files "
                "WHERE job_id=? AND ordinal>? ORDER BY ordinal LIMIT ?",
                (job_id, after_ordinal, limit),
            ).fetchall()
        return [
            {"ordinal": int(row["ordinal"]), "file_path": row["file_path"]}
            for row in rows
        ]

    @staticmethod
    def cleanup_job_on(db: sqlite3.Connection, job_id: str) -> None:
        """Delete this job's side-table rows and its own row -- called from
        WITHIN ``NotebookStore.delete_row_and_orphan_embeddings``'s finalize
        transaction OR ``finish_residual``'s own transaction above, never as
        an independent third transaction of its own (see the PostgreSQL
        twin's docstring)."""
        db.execute("DELETE FROM notebook_delete_files WHERE job_id=?", (job_id,))
        db.execute("DELETE FROM notebook_delete_jobs WHERE id=?", (job_id,))
