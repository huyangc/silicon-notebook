"""PostgreSQL persistence for the delete-jobization job carrier (batch 3·W1
PR-3, design doc §T-3/§T-4).

Shape mirrors ``KgBuildJobStore`` deliberately — same durable-job idiom this
schema already uses for background work — but with no per-source staging
methods: the delete job has six coarse phases, not per-source publish steps.

Phase 5's actual finalize transaction (the single-transaction fence+archive+
delete) lives in ``NotebookStore.delete_row_and_orphan_embeddings``'s
``job_id``-bearing extension, NOT here — that transaction owns both the
``notebooks`` row and the two tables this store owns, so its cleanup of
``notebook_delete_files``/``notebook_delete_jobs`` has to run inside
NotebookStore's own transaction (see that method's docstring for why: the
exact same rows this store would otherwise delete in a SEPARATE transaction
after the fact). ``finish_residual`` below is the one exception: the driver-A
"job row present, notebooks row absent" special case (§T-4, code-review P1-A)
has no ``notebooks`` row left to fence, so it cleans up in its OWN small
transaction instead.

**Ownership/lease fencing (code-review P2-a).** Every successful
``mark_running`` mints a fresh ``lease_token``; every subsequent write this
job's ``run()`` invocation issues carries that same token in its ``WHERE``
clause. A worker that has been superseded — its job row "stolen" by a second
sweep-driven resubmission because the first looked stale (dead, not merely
slow) — writes nothing further even if it keeps executing after losing
ownership: every write matches zero rows and the caller's own liveness check
(``ownership_snapshot``) also independently catches it before the next batch.
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Callable

from app.repositories.postgres._store_utils import (
    TimestampInput,
    execute_many,
    normalized_clock,
    utc_now,
)
from app.repositories.postgres.access_sql import NOTEBOOK_LIVE_SQL
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.ports import NotebookAlreadyDeletingError

_log = logging.getLogger(__name__)

# Sec T-3/T-4: the job's own defense-in-depth single-flight states. 'queued'
# just after request()/a sweep requeue, 'running' while a background worker
# owns it, 'waiting' when phase 2 (quiesce) times out and hands the job back
# to the sweep (Sec T-3.3) -- distinct from 'queued' so an operator can tell
# "waiting for a slot" from "waiting for an in-flight KG rebuild to stop"
# apart in the same status column. Code review P2-c: 'queued' is ALSO where
# phases 3/4/5's independent-lock-claim-unavailable cases land (P1-B) --
# 'waiting' is reserved for quiesce alone from this point on.
_ACTIVE_STATUSES = ("queued", "running", "waiting")


def _new_lease_token() -> str:
    return secrets.token_hex(16)


class NotebookDeleteJobStore:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], TimestampInput],
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = normalized_clock(now)

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
        """The tombstone CAS + same-transaction job-row insert (§T-2).

        ``created_by`` is accepted for the caller's audit trail even though
        this table has no column for it today (the job row carries no actor
        column -- see the migration's own header comment on what the columns
        are for); kept as a parameter now so a future audit column does not
        have to change this method's call sites.

        Raises ``KeyError`` when the notebook does not exist at all (404),
        ``NotebookAlreadyDeletingError`` when it exists but is already
        'copying' or 'deleting' (409) -- the CAS predicate cannot distinguish
        these by rowcount alone, so a rowcount-0 CAS falls through to a
        second read that can.
        """
        del created_by  # see docstring: accepted, not yet persisted
        now = self.now()
        with self.database.write() as connection:
            cas = connection.execute(
                "UPDATE notebooks SET status='deleting',updated_at=%s "
                f"WHERE id=%s AND {NOTEBOOK_LIVE_SQL}",
                (now, notebook_id),
            )
            if cas.rowcount != 1:
                existing = connection.execute(
                    "SELECT id FROM notebooks WHERE id=%s", (notebook_id,)
                ).fetchone()
                if existing is None:
                    raise KeyError(notebook_id)
                raise NotebookAlreadyDeletingError(notebook_id)
            job_id = self.new_id("ndj")
            connection.execute(
                "INSERT INTO notebook_delete_jobs"
                "(id,notebook_id,status,phase,cursor_table,cursor_key,"
                "deleted_rows,lease_token,attempts,error_code,error_message,"
                "created_at,updated_at,finished_at) "
                "VALUES (%s,%s,'queued','mark','','',0,'',0,'','',%s,%s,NULL)",
                (job_id, notebook_id, now, now),
            )
        return self.get(job_id)


    def recreate_for_deleting_notebook(
        self, notebook_id: str, *, attempts: int = 0
    ) -> dict:
        """Sweep driver B (§T-4): ``notebooks.status`` is ALREADY 'deleting'
        (that is exactly the condition ``list_notebooks_missing_job``
        selects on) but no active job row owns it -- the CAS+insert in
        ``request`` cannot be reused here because its CAS predicate would
        reject a row that is already 'deleting'. Starts fresh at phase
        'mark' -- phase 5's finalize step (``NotebookStore.delete_row_and_
        orphan_embeddings``) tolerates re-running the whole pipeline from
        scratch on an already-'deleting' notebook with no observable
        difference (the CAS already happened; every other phase is
        idempotent by construction -- see design doc §T-4).

        ``attempts`` (P1-E) is the CONTINUATION of this notebook's failure
        count, not this new row's own -- the caller (``NotebookDeleteJob
        Runner.sweep_once``) has already read it off the most recent failed
        row and decided this notebook is still under the retry ceiling.
        Carrying it forward into the new row means a single point read of
        the LATEST row always tells the whole story; ``finish(...,
        'failed', ...)`` increments it further on this new row's own
        failure.

        A concurrent sweep tick racing this same notebook loses to the
        partial unique index (``idx_notebook_delete_jobs_one_active``) --
        caught here and treated as "someone else already recreated it",
        returning THEIR job row instead of raising."""
        from psycopg import errors

        now = self.now()
        job_id = self.new_id("ndj")
        try:
            with self.database.write() as connection:
                # P1-E + codex #659 R4: purge this notebook's accumulated
                # 'failed' rows (and their notebook_delete_files leftovers)
                # in the SAME transaction that inserts the replacement — a
                # crash between a separate purge and this insert would erase
                # the only row carrying attempts/finished_at, resetting the
                # bounded-retry policy to attempt one forever.
                old_ids = [
                    row["id"] for row in connection.execute(
                        "SELECT id FROM notebook_delete_jobs "
                        "WHERE notebook_id=%s AND status='failed'",
                        (notebook_id,),
                    ).fetchall()
                ]
                if old_ids:
                    connection.execute(
                        "DELETE FROM notebook_delete_files "
                        "WHERE job_id = ANY(%s)",
                        (old_ids,),
                    )
                    connection.execute(
                        "DELETE FROM notebook_delete_jobs WHERE id = ANY(%s)",
                        (old_ids,),
                    )
                connection.execute(
                    "INSERT INTO notebook_delete_jobs"
                    "(id,notebook_id,status,phase,cursor_table,cursor_key,"
                    "deleted_rows,lease_token,attempts,error_code,"
                    "error_message,created_at,updated_at,finished_at) "
                    "VALUES (%s,%s,'queued','mark','','',0,'',%s,'','',%s,%s,NULL)",
                    (job_id, notebook_id, attempts, now, now),
                )
        except errors.UniqueViolation:
            existing = self.latest_for_notebook(notebook_id)
            if existing is not None and existing["status"] in _ACTIVE_STATUSES:
                return existing
            raise
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notebook_delete_jobs WHERE id=%s", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row(row)

    def latest_for_notebook(self, notebook_id: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notebook_delete_jobs WHERE notebook_id=%s "
                "ORDER BY created_at DESC LIMIT 1",
                (notebook_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    def mark_running(self, job_id: str, *, stale_cutoff_seconds: float) -> str | None:
        """CAS ``'queued'``/``'waiting'`` -> ``'running'``, OR steal a
        ``'running'``-but-stale row (P2-a: sweep driver A resubmitting a job
        whose ``updated_at`` has not moved in ``stale_cutoff_seconds`` --
        genuinely dead, not merely slow; a live worker's own heartbeat
        ``advance_phase`` calls keep ``updated_at`` fresh, so this branch
        never fires against it). Mints and returns a fresh ``lease_token`` on
        success (``None`` on failure) -- every write this run() invocation
        issues from here on must carry it."""
        token = _new_lease_token()
        cutoff = utc_now() - timedelta(seconds=max(1, stale_cutoff_seconds))
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE notebook_delete_jobs SET status='running',"
                "lease_token=%s,updated_at=%s WHERE id=%s AND "
                "(status IN ('queued','waiting') "
                "OR (status='running' AND updated_at<%s))",
                (token, self.now(), job_id, cutoff),
            )
        return token if cursor.rowcount == 1 else None

    def advance_phase(
        self, job_id: str, phase: str, *, lease_token: str,
        cursor_table: str = "", cursor_key: str = "", deleted_delta: int = 0,
    ) -> bool:
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE notebook_delete_jobs SET phase=%s,cursor_table=%s,"
                "cursor_key=%s,deleted_rows=deleted_rows+%s,updated_at=%s "
                "WHERE id=%s AND status='running' AND lease_token=%s",
                (
                    phase, cursor_table, cursor_key, deleted_delta,
                    self.now(), job_id, lease_token,
                ),
            )
        return cursor.rowcount == 1

    def mark_waiting(self, job_id: str, *, lease_token: str, note: str = "") -> bool:
        """Phase 2 (quiesce) timeout hand-back (§T-3.3) ONLY -- 'running' ->
        'waiting', never straight into phase 'rows'. Code review P2-c:
        phases 3/4/5's independent-lock-claim-unavailable cases use
        ``mark_queued`` instead, not this -- 'waiting' means "an in-flight
        KG rebuild has not stopped yet", a different operator-facing fact
        than "the exclusive claim is busy"."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE notebook_delete_jobs SET status='waiting',"
                "error_message=%s,updated_at=%s WHERE id=%s AND "
                "status='running' AND lease_token=%s",
                (note, self.now(), job_id, lease_token),
            )
        return cursor.rowcount == 1

    def mark_queued(self, job_id: str, *, lease_token: str, note: str = "") -> bool:
        """Phases 3/4/5's independent-claim-unavailable hand-back (P1-B/
        P2-c): 'running' -> 'queued', same status the sweep already resumes
        without any special-casing (``_ACTIVE_STATUSES``/``mark_running``'s
        CAS both already include it)."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE notebook_delete_jobs SET status='queued',"
                "error_message=%s,updated_at=%s WHERE id=%s AND "
                "status='running' AND lease_token=%s",
                (note, self.now(), job_id, lease_token),
            )
        return cursor.rowcount == 1

    def finish(
        self, job_id: str, status: str, *, lease_token: str,
        error_code: str = "", error_message: str = "",
    ) -> bool:
        """Terminal-failure settle only -- a SUCCESSFUL finalize deletes this
        row itself (see this module's docstring), so ``status`` here is only
        ever 'failed' in practice. P1-E: increments ``attempts`` so
        ``recreate_for_deleting_notebook``'s caller can apply a retry
        ceiling/backoff -- unconditional (not gated on ``status=='failed'``)
        since that is the only value any caller ever actually passes; kept
        general to mirror ``KgBuildJobStore.finish``'s shape.

        P2-b (codex PR#659 round 1): **lease-fenced**, reversing the
        original "deliberately unfenced" design. That original rationale
        ("a worker settling a job it is no longer sure it owns must not be
        blocked, or a genuinely-failed job wedges forever") does not survive
        scrutiny: a lease is lost ONLY when ``mark_running``'s CAS actually
        succeeds for a different worker, and CAS success means that new
        worker is NECESSARILY alive and already progressing the same
        job_id. So a fenced-out ``finish`` from the OLD worker is not a job
        wedging with no one left to settle it -- it is exactly the case
        where the row is not this caller's to settle at all; the new owner
        will settle it (via its own success or its own eventual failure).
        Without this fence, a slow-but-not-dead worker's late exception
        could stamp 'failed' + increment ``attempts`` on the NEW owner's
        still-live row out from under it. ``rowcount==0`` is therefore a
        normal, expected outcome -- logged, not raised (the caller is
        already inside ``run()``'s top-level except handler; there is
        nothing above it to usefully re-raise into)."""
        now = self.now()
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE notebook_delete_jobs SET status=%s,error_code=%s,"
                "error_message=%s,attempts=attempts+1,updated_at=%s,"
                "finished_at=%s WHERE id=%s AND lease_token=%s "
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
        """Phase 1 ('paths'): copy one keyset page of ``sources.file_path``
        into ``notebook_delete_files``, ordinal-numbered by resuming from
        this job's current ``MAX(ordinal)`` so a crash-and-resume never
        reuses an ordinal.

        Returns ``(rows_copied, last_source_id_or_None)`` -- the caller loops
        until ``rows_copied == 0``, storing ``last_source_id`` as the next
        page's ``after_id`` (and as the job's ``cursor_key``) between calls.
        """
        with self.database.write() as connection:
            start = connection.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 AS next_ordinal "
                "FROM notebook_delete_files WHERE job_id=%s",
                (job_id,),
            ).fetchone()
            next_ordinal = int(start["next_ordinal"])
            page = connection.execute(
                "SELECT id,file_path FROM sources "
                "WHERE notebook_id=%s AND id>%s ORDER BY id LIMIT %s",
                (notebook_id, after_id, limit),
            ).fetchall()
            if not page:
                return 0, None
            execute_many(
                connection,
                "INSERT INTO notebook_delete_files(job_id,ordinal,file_path) "
                "VALUES (%s,%s,%s)",
                [
                    (job_id, next_ordinal + offset, row["file_path"] or "")
                    for offset, row in enumerate(page)
                ],
            )
        return len(page), page[-1]["id"]

    def notebook_exists(self, notebook_id: str) -> bool:
        """Raw existence check (no live-status filter) -- consumed by
        ``NotebookDeleteJobRunner.run()`` (P1-A) to decide, ONCE at the top
        of a run, whether this is an ordinary delete or the sweep driver-A
        "job row present, notebooks row absent" residual-cleanup special
        case (§T-4)."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM notebooks WHERE id=%s", (notebook_id,)
            ).fetchone()
        return row is not None

    def ownership_snapshot(self, job_id: str) -> dict | None:
        """P1-A/P2-a: one query combining this job's own status/lease_token
        with its target notebook's CURRENT status (``None`` if that row no
        longer exists -- the residual-cleanup case) -- replaces the former
        two-point-query ``_still_deleting`` (one SELECT on this table, one on
        ``notebooks``) with a single LEFT JOIN. Returns ``None`` only when
        the JOB row itself is gone (already finalized, or cleaned up out of
        band)."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT j.status AS job_status, j.lease_token AS lease_token, "
                "n.status AS notebook_status "
                "FROM notebook_delete_jobs j "
                "LEFT JOIN notebooks n ON n.id=j.notebook_id "
                "WHERE j.id=%s",
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
        """§T-4 driver-A's out-of-band-delete special case (P1-A): NO
        notebooks row is left to fence, and the archive projections' source
        tables may already be gone via cascade, so this NEVER attempts
        phase 5's fence+archive -- it only deletes this job's own two
        side-table footprints, in its own transaction (unlike a SUCCESSFUL
        finalize, which piggybacks the exact same cleanup onto
        ``delete_row_and_orphan_embeddings``'s transaction because that one
        also owns the ``notebooks`` row this one does not have).

        P2-b: **lease-fenced**, same argument as ``finish`` above -- only
        the worker that currently holds this job_id's lease may execute the
        terminal residual cleanup; a fenced-out caller has already lost
        ownership to a new worker that is (or will) settle it instead.
        Deliberately does NOT delegate to ``cleanup_job_on`` (shared with
        the successful-finalize path, which has no lease token to fence
        with -- it is instead gated by the in-process claim's
        ``verify_held()`` immediately before it runs): this fences the
        ``notebook_delete_jobs`` DELETE itself first and only cascades to
        the ``notebook_delete_files`` side table when that row was actually
        this worker's to delete, so a fenced-out call leaves BOTH tables
        untouched rather than half-deleted."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "DELETE FROM notebook_delete_jobs WHERE id=%s AND lease_token=%s",
                (job_id, lease_token),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    "DELETE FROM notebook_delete_files WHERE job_id=%s", (job_id,)
                )
        if cursor.rowcount != 1:
            _log.info(
                "notebook delete job %s: finish_residual() fenced out "
                "(lease no longer held) — a new owner is settling this job "
                "instead",
                job_id,
            )
        return cursor.rowcount == 1

    def list_stale(self, older_than_seconds: float) -> list[dict]:
        """Sweep driver A (§T-4): active job rows whose ``updated_at`` has
        not moved in at least ``older_than_seconds`` -- a worker died
        mid-phase, or a phase-2 quiesce timeout left the row 'waiting'. Same
        cutoff-computed-in-the-store idiom as
        ``sharing_store.sweep_stale_copies`` (``utc_now() - timedelta(...)``,
        a real ``datetime`` bound against the ``timestamptz`` column) rather
        than accepting a precomputed value from a backend-neutral caller."""
        cutoff = utc_now() - timedelta(seconds=max(1, older_than_seconds))
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM notebook_delete_jobs "
                "WHERE status IN ('queued','running','waiting') "
                "AND updated_at<%s",
                (cutoff,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_notebooks_missing_job(self) -> list[dict]:
        """Sweep driver B (§T-4): ``notebooks.status='deleting'`` rows with no
        active job row -- recovers from "the CAS committed but the job-row
        INSERT failed" and from an active job row being deleted out of band.

        P1-E: each item also carries this notebook's MOST RECENT failed
        job's ``(attempts, finished_at)`` (both ``None`` if it has never
        failed) -- the correlated subqueries below, not a second round trip,
        since driver B ticks frequently and this table stays small. The
        caller (``NotebookDeleteJobRunner.sweep_once``) uses these to apply
        an exponential backoff window and an attempt ceiling in Python (kept
        backend-neutral, not duplicated as time arithmetic in two SQL
        dialects)."""
        with self.database.connect() as connection:
            rows = connection.execute(
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
    # Design doc §1.5 (form-one/form-two) and §1.3 (B-class chains). Table/
    # column names below are ALWAYS code constants (never caller/user input
    # — see ``services/notebook_delete_tables.py``'s static registry), so an
    # f-string identifier substitution here is exactly as safe as the
    # existing ``materialize_paths_page``'s ``notebook_id``/``file_path``
    # literals above.
    # ------------------------------------------------------------------

    # P1-D: a fixed sub-batch size for expanding a page's parent-id list into
    # child DELETEs, and for the ctid-bounded child-draining loops below --
    # same value/idiom as the repository's existing convention
    # (``sqlite/chunk_store.py``'s ``CHUNK_ELEMENT_LOOKUP_BATCH``).
    _CHILD_BATCH_SIZE = 500

    def delete_fts_shadow_page(
        self, table: str, notebook_id: str, cursor_rowid: int, limit: int,
    ) -> tuple[int, int]:
        """§4.4/P2-g + codex #659 R5: no-op on PostgreSQL -- this backend has
        no FTS5 shadow tables (full-text search rides GIN trgm indexes on the
        real columns). SQLite twin deletes ONE bounded rowid-keyset page from
        ``kg_objects_fts``/``chunks_fts`` per call. Present here so the
        backend-neutral runner can loop it unconditionally without asking
        which backend it is on."""
        del table, notebook_id, limit
        return 0, cursor_rowid

    def delete_direct_page_form_one(
        self, table: str, id_column: str, filter_column: str,
        filter_value: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        with self.database.write() as connection:
            page = connection.execute(
                f"SELECT {id_column} FROM {table} "
                f"WHERE {filter_column}=%s AND {id_column}>%s "
                f"ORDER BY {id_column} LIMIT %s",
                (filter_value, cursor, limit),
            ).fetchall()
            if not page:
                return 0, None
            ids = [row[id_column] for row in page]
            connection.execute(
                f"DELETE FROM {table} WHERE {id_column} = ANY(%s) "
                f"AND {filter_column}=%s",
                (ids, filter_value),
            )
        return len(ids), ids[-1]

    def delete_direct_batch_form_two(
        self, table: str, filter_column: str, filter_value: str, limit: int,
    ) -> int:
        with self.database.write() as connection:
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE ctid = ANY(ARRAY("
                f"SELECT ctid FROM {table} WHERE {filter_column}=%s LIMIT %s))",
                (filter_value, limit),
            )
        return cursor.rowcount

    def delete_knowhow_rows_page(
        self, notebook_id: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        """P1-D: the row/cell dimension, split out of the table-paged chain
        so one page's fanout is bounded by "N rows × that table's own column
        count" rather than "N tables × however many rows each happens to
        have"."""
        with self.database.write() as connection:
            page = connection.execute(
                "SELECT kr.id AS id FROM knowhow_rows kr "
                "JOIN knowhow_tables kt ON kt.id=kr.table_id "
                "WHERE kt.notebook_id=%s AND kr.id>%s "
                "ORDER BY kr.id LIMIT %s",
                (notebook_id, cursor, limit),
            ).fetchall()
            if not page:
                return 0, None
            row_ids = [row["id"] for row in page]
            # A cell's row_id uniquely ties it to one table, so filtering by
            # row_id alone already captures every cell/cell-code row for
            # this page -- no column_id join needed.
            connection.execute(
                "DELETE FROM knowhow_cells WHERE row_id = ANY(%s)",
                (row_ids,),
            )
            connection.execute(
                "DELETE FROM knowhow_cell_code WHERE row_id = ANY(%s)",
                (row_ids,),
            )
            connection.execute(
                "DELETE FROM knowhow_rows WHERE id = ANY(%s)",
                (row_ids,),
            )
        return len(row_ids), row_ids[-1]

    def delete_knowhow_tables_page(
        self, notebook_id: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        """Runs after ``delete_knowhow_rows_page`` has drained rows/cells for
        this notebook -- this page's fanout is now bounded by "N tables ×
        each one's own column/change/milestone count", the ordinary small
        scale the original (pre-P1-D) design already assumed."""
        with self.database.write() as connection:
            page = connection.execute(
                "SELECT id FROM knowhow_tables WHERE notebook_id=%s AND id>%s "
                "ORDER BY id LIMIT %s",
                (notebook_id, cursor, limit),
            ).fetchall()
            if not page:
                return 0, None
            table_ids = [row["id"] for row in page]
            connection.execute(
                "DELETE FROM knowhow_columns WHERE table_id = ANY(%s)",
                (table_ids,),
            )
            connection.execute(
                "DELETE FROM knowhow_changes WHERE table_id = ANY(%s)",
                (table_ids,),
            )
            connection.execute(
                "DELETE FROM knowhow_milestones WHERE table_id = ANY(%s)",
                (table_ids,),
            )
            connection.execute(
                "DELETE FROM knowhow_tables WHERE id = ANY(%s) AND notebook_id=%s",
                (table_ids, notebook_id),
            )
        return len(table_ids), table_ids[-1]

    def delete_indexing_pipeline_stages_page(
        self, notebook_id: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        with self.database.write() as connection:
            page = connection.execute(
                "SELECT job_id FROM indexing_pipeline_stages "
                "WHERE notebook_id=%s AND job_id>%s ORDER BY job_id LIMIT %s",
                (notebook_id, cursor, limit),
            ).fetchall()
            if not page:
                return 0, None
            job_ids = [row["job_id"] for row in page]
            # §1.3: this child also has a source_id parent, so it MUST be
            # cleared before `sources` is ever deleted (phase 5) -- clearing
            # it here, driven by the job_id page, satisfies that regardless
            # of when phase 5 runs relative to this chain.
            #
            # P1-D: a single unbounded `job_id = ANY(job_ids)` DELETE could
            # match far more rows than one page's worth if a handful of
            # jobs each touched many sources -- drain it via the SAME
            # ctid-bounded loop form-two uses, still inside this one
            # transaction (the per-job_id fanout here is modest enough that
            # keeping it in-transaction, unlike the read-only-parent chains
            # below, does not risk the same unbounded-total-work shape).
            while True:
                deleted = connection.execute(
                    "DELETE FROM indexing_pipeline_stage_sources WHERE ctid = ANY(ARRAY("
                    "SELECT ctid FROM indexing_pipeline_stage_sources "
                    "WHERE job_id = ANY(%s) LIMIT %s))",
                    (job_ids, self._CHILD_BATCH_SIZE),
                )
                if deleted.rowcount == 0:
                    break
            connection.execute(
                "DELETE FROM indexing_pipeline_stages "
                "WHERE job_id = ANY(%s) AND notebook_id=%s",
                (job_ids, notebook_id),
            )
        return len(job_ids), job_ids[-1]

    def delete_memory_items_page(
        self, notebook_id: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        with self.database.write() as connection:
            page = connection.execute(
                "SELECT id FROM memory_items WHERE notebook_id=%s AND id>%s "
                "ORDER BY id LIMIT %s",
                (notebook_id, cursor, limit),
            ).fetchall()
            if not page:
                return 0, None
            memory_ids = [row["id"] for row in page]
            connection.execute(
                "DELETE FROM memory_embeddings WHERE memory_id = ANY(%s)",
                (memory_ids,),
            )
            connection.execute(
                "DELETE FROM memory_provenance WHERE memory_id = ANY(%s)",
                (memory_ids,),
            )
            connection.execute(
                "DELETE FROM memory_revisions WHERE memory_id = ANY(%s)",
                (memory_ids,),
            )
            connection.execute(
                "DELETE FROM memory_items WHERE id = ANY(%s) AND notebook_id=%s",
                (memory_ids, notebook_id),
            )
        return len(memory_ids), memory_ids[-1]

    def _drain_children_by_parent_ids(
        self, table: str, parent_column: str, parent_ids: list[str],
    ) -> None:
        """P1-D shared helper: drain every ``table`` row referencing any of
        ``parent_ids``, via ctid-bounded batches EACH IN ITS OWN
        transaction (never one unbounded statement, never one long
        transaction) -- used by the two read-only-parent chains, where a
        page of 500 parents can plausibly carry far more total children than
        one batch's worth."""
        while True:
            with self.database.write() as connection:
                deleted = connection.execute(
                    f"DELETE FROM {table} WHERE ctid = ANY(ARRAY("
                    f"SELECT ctid FROM {table} WHERE {parent_column} = ANY(%s) "
                    f"LIMIT %s))",
                    (parent_ids, self._CHILD_BATCH_SIZE),
                )
            if deleted.rowcount == 0:
                return

    def delete_source_elements_page(
        self, notebook_id: str, cursor: str, limit: int,
    ) -> tuple[int, str | None]:
        with self.database.connect() as connection:
            # Read-only: `sources` is an archive-input table (§T-3.2 step 4
            # owns its rows in phase 5), so this page is NEVER deleted here.
            page = connection.execute(
                "SELECT id FROM sources WHERE notebook_id=%s AND id>%s "
                "ORDER BY id LIMIT %s",
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
        with self.database.connect() as connection:
            # Read-only: `ask_jobs` is an archive-input table, same rationale
            # as delete_source_elements_page above.
            page = connection.execute(
                "SELECT id FROM ask_jobs WHERE notebook_id=%s AND id>%s "
                "ORDER BY id LIMIT %s",
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
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT 1 FROM {table} WHERE {filter_column}=%s LIMIT 1",
                (filter_value,),
            ).fetchone()
        return row is not None

    def list_files_page(
        self, job_id: str, after_ordinal: int, limit: int,
    ) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT ordinal,file_path FROM notebook_delete_files "
                "WHERE job_id=%s AND ordinal>%s ORDER BY ordinal LIMIT %s",
                (job_id, after_ordinal, limit),
            ).fetchall()
        return [
            {"ordinal": int(row["ordinal"]), "file_path": row["file_path"]}
            for row in rows
        ]

    @staticmethod
    def cleanup_job_on(connection, job_id: str) -> None:
        """Delete this job's side-table rows and its own row -- called from
        WITHIN ``NotebookStore.delete_row_and_orphan_embeddings``'s finalize
        transaction (Sec T-3.2 step 7) OR ``finish_residual``'s own
        transaction above (Sec T-4 driver-A special case), never as an
        independent third transaction of its own."""
        connection.execute(
            "DELETE FROM notebook_delete_files WHERE job_id=%s", (job_id,)
        )
        connection.execute(
            "DELETE FROM notebook_delete_jobs WHERE id=%s", (job_id,)
        )
