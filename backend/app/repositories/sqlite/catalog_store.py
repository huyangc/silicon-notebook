"""SQLite row persistence for command-catalog extraction (Plan C, stage C1b).

Row-level only: which sections exist, what a model answered and what survived
grounding all belong to ``app/services/command_catalog.py`` (pure) and
``app/services/catalog_job.py`` (orchestration). This module owns exactly two
things — the durable single-flight job row and the reviewable candidate rows —
and every read it exposes is bounded by construction.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from app.repositories.ports import (
    CATALOG_CANDIDATE_STATES,
    CATALOG_MAX_CANDIDATE_BATCH,
    CATALOG_MAX_CANDIDATE_PAGE,
    CATALOG_TERMINAL_STATUSES,
    CatalogJobAlreadyRunning,
)
from app.repositories.sqlite.database import SqliteDatabase


def _loads(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class CatalogStore:
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

    # ------------------------------------------------------------------ jobs
    @staticmethod
    def _job_row(row) -> dict:
        return {
            "id": row["id"],
            "notebook_id": row["notebook_id"],
            "source_id": row["source_id"],
            "created_by": row["created_by"],
            "status": row["status"],
            "sections_total": int(row["sections_total"]),
            "sections_done": int(row["sections_done"]),
            "entries": int(row["entries"]),
            "rejected": int(row["rejected"]),
            "uncovered": int(row["uncovered"]),
            "truncated_sections": int(row["truncated_sections"]),
            "failure_reason": row["failure_reason"],
            "diagnostic": row["diagnostic"],
            "applied_table_id": row["applied_table_id"],
            "source_generation": row["source_generation"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }

    def create_job(
        self,
        notebook_id: str,
        source_id: str,
        created_by: str,
        *,
        source_generation: str = "",
    ) -> dict:
        """Insert a ``queued`` job, or raise if this source already has one.

        The conditional unique index is the guard; catching its IntegrityError
        is the only race-free way to claim it (a SELECT-then-INSERT would let two
        concurrent requests both see "no active job").

        ``source_generation`` is the caller's snapshot of
        ``source_element_generation`` (see below): the element generation this
        run's candidates will be derived from. Stored, never interpreted here.
        """
        job_id = self.new_id("cjb")
        now = self.now()
        try:
            with self.database.write() as db:
                db.execute(
                    """
                    INSERT INTO catalog_jobs
                    (id, notebook_id, source_id, created_by, status,
                     sections_total, sections_done, entries, rejected, uncovered,
                     truncated_sections, failure_reason, diagnostic,
                     applied_table_id, source_generation,
                     created_at, updated_at, finished_at)
                    VALUES (?, ?, ?, ?, 'queued', 0, 0, 0, 0, 0, 0, '', '', '', ?, ?, ?, '')
                    """,
                    (
                        job_id,
                        notebook_id,
                        source_id,
                        created_by,
                        str(source_generation or ""),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "catalog_jobs.source_id" in str(exc):
                raise CatalogJobAlreadyRunning(source_id) from exc
            raise
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM catalog_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job_row(row)

    def latest_job(self, source_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM catalog_jobs WHERE source_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return self._job_row(row) if row is not None else None

    def active_job(self, source_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM catalog_jobs WHERE source_id=? "
                "AND status IN ('queued','running') LIMIT 1",
                (source_id,),
            ).fetchone()
        return self._job_row(row) if row is not None else None

    def latest_applied_table_id(self, source_id: str) -> str:
        """See ``CatalogStorePort.latest_applied_table_id``. Same index as
        ``latest_job`` (``idx_catalog_jobs_source_created``), narrowed to
        ``applied_table_id != ''`` — a bounded point lookup, never a scan of
        every job this source has ever had."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT applied_table_id FROM catalog_jobs WHERE source_id=? "
                "AND applied_table_id != '' "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return str(row["applied_table_id"]) if row is not None else ""

    def start_job(self, job_id: str, sections_total: int) -> bool:
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE catalog_jobs SET status='running', sections_total=?, "
                "updated_at=? WHERE id=? AND status='queued'",
                (max(0, int(sections_total)), self.now(), job_id),
            )
        return cursor.rowcount == 1

    def set_section_total(self, job_id: str, sections_total: int) -> bool:
        """Publish the section count once sectioning is done.

        Separate from ``start_job`` because the two answer different questions
        at different times: ``start_job`` claims the row before the expensive
        whole-source read (so a queued job that was cancelled costs nothing),
        and the total is only knowable after that read. ``running``-scoped, so a
        cancel that landed in between simply leaves the total at 0 rather than
        resurrecting a settled row.
        """
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE catalog_jobs SET sections_total=?, updated_at=? "
                "WHERE id=? AND status='running'",
                (max(0, int(sections_total)), self.now(), job_id),
            )
        return cursor.rowcount == 1

    def record_section(
        self,
        job_id: str,
        *,
        entries: int,
        rejected: int,
        uncovered: int,
        truncated: int = 0,
    ) -> bool:
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE catalog_jobs SET sections_done=sections_done+1, "
                "entries=entries+?, rejected=rejected+?, uncovered=uncovered+?, "
                "truncated_sections=truncated_sections+?, "
                "updated_at=? WHERE id=? AND status='running'",
                (
                    max(0, int(entries)),
                    max(0, int(rejected)),
                    max(0, int(uncovered)),
                    max(0, int(truncated)),
                    self.now(),
                    job_id,
                ),
            )
        return cursor.rowcount == 1

    def set_applied_table_id(self, job_id: str, table_id: str) -> bool:
        """Unconditional (no `status` filter): apply is legal on a job in ANY
        terminal run status, or even mid-run, since review/apply is decoupled
        from the extraction run itself."""
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE catalog_jobs SET applied_table_id=?, updated_at=? "
                "WHERE id=?",
                (str(table_id), self.now(), job_id),
            )
        return cursor.rowcount == 1

    def finish_job(
        self,
        job_id: str,
        status: str,
        *,
        failure_reason: str = "",
        diagnostic: str = "",
    ) -> bool:
        """Settle a job. ``WHERE status IN ('queued','running')`` makes this
        idempotent: a second settle attempt (a repeated interrupt, a cancel
        racing the worker's own finish) touches 0 rows and reports False rather
        than overwriting the outcome that already landed."""
        if status not in CATALOG_TERMINAL_STATUSES:
            raise ValueError("catalog job terminal status is not recognised")
        now = self.now()
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE catalog_jobs SET status=?, failure_reason=?, diagnostic=?, "
                "updated_at=?, finished_at=? "
                "WHERE id=? AND status IN ('queued','running')",
                (status, failure_reason, diagnostic, now, now, job_id),
            )
        return cursor.rowcount == 1

    # ------------------------------------------------------------ candidates
    @staticmethod
    def _candidate_row(row) -> dict:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "notebook_id": row["notebook_id"],
            "source_id": row["source_id"],
            "position": int(row["position"]),
            "section_path": row["section_path"],
            "command_name": row["command_name"],
            "payload": _loads(row["payload"]),
            "state": row["state"],
            "reject_info": _loads(row["reject_info"]),
            "created_at": row["created_at"],
        }

    def add_candidates(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Persist one section's candidate rows.

        The input is CHUNKED to `CATALOG_MAX_CANDIDATE_BATCH`, never truncated
        to it. The caller has already counted these rows into the job's
        `entries`/`rejected` progress, so a store that quietly dropped the tail
        would produce precisely the "registered under-report" this feature
        exists to eliminate — the progress row would claim N and the review
        queue would hold fewer.
        """
        batch = list(rows)
        if not batch:
            return
        now = self.now()
        values = [
            (
                self.new_id("cnd"),
                str(row["job_id"]),
                str(row["notebook_id"]),
                str(row["source_id"]),
                int(row.get("position") or 0),
                str(row.get("section_path") or ""),
                str(row.get("command_name") or ""),
                json.dumps(row.get("payload") or {}, ensure_ascii=False),
                str(row.get("state") or "candidate"),
                json.dumps(row.get("reject_info") or {}, ensure_ascii=False),
                now,
            )
            for row in batch
        ]
        statement = (
            "INSERT INTO catalog_candidates "
            "(id, job_id, notebook_id, source_id, position, section_path, "
            " command_name, payload, state, reject_info, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        )
        for start in range(0, len(values), CATALOG_MAX_CANDIDATE_BATCH):
            with self.database.write() as db:
                db.executemany(
                    statement, values[start:start + CATALOG_MAX_CANDIDATE_BATCH]
                )

    def update_candidate_payload(
        self,
        candidate_id: str,
        payload: Mapping[str, Any],
        reject_info: Mapping[str, Any],
    ) -> bool:
        """Revise one still-`candidate` row's payload — see the port for why
        this exists (v2's cross-window merge) and why it is this narrow.

        Two columns, by primary key, guarded on `state='candidate'`. No
        `updated_at`: `catalog_candidates` has no such column (only
        `created_at`), and adding one would be a migration this feature is
        explicitly not taking.
        """
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE catalog_candidates SET payload=?, reject_info=? "
                "WHERE id=? AND state='candidate'",
                (
                    json.dumps(dict(payload), ensure_ascii=False),
                    json.dumps(dict(reject_info), ensure_ascii=False),
                    str(candidate_id),
                ),
            )
        return int(cursor.rowcount or 0) == 1

    def list_candidates(
        self, job_id: str, *, state: str, cursor: int, limit: int
    ) -> list[dict]:
        """One keyset page of a job's candidates in insertion order.

        ``position`` is the cursor because it is the only total order this table
        has: ids are random and ``created_at`` repeats across a section's rows.
        """
        if state not in CATALOG_CANDIDATE_STATES:
            raise ValueError("unsupported catalog candidate state")
        bound = max(1, min(int(limit), CATALOG_MAX_CANDIDATE_PAGE))
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM catalog_candidates "
                "WHERE job_id=? AND state=? AND position>? "
                "ORDER BY position LIMIT ?",
                (job_id, state, max(0, int(cursor)), bound),
            ).fetchall()
        return [self._candidate_row(row) for row in rows]

    def candidate_counts(self, job_id: str) -> dict[str, int]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT state, COUNT(*) AS n FROM catalog_candidates "
                "WHERE job_id=? GROUP BY state",
                (job_id,),
            ).fetchall()
        counts = {state: 0 for state in sorted(CATALOG_CANDIDATE_STATES)}
        for row in rows:
            counts[str(row["state"])] = int(row["n"])
        return counts

    def candidates_by_ids(
        self, job_id: str, candidate_ids: Sequence[str], *, limit: int
    ) -> list[dict]:
        bound = max(1, min(int(limit), CATALOG_MAX_CANDIDATE_PAGE))
        # Sliced to `bound` BEFORE `dict.fromkeys`: the route already rejects
        # an explicit selection wider than `MAX_APPLY_CANDIDATES` with a 422,
        # but this store method has no way to see that its caller skipped the
        # check, so it must not itself materialize (str(), hash, dedupe) an
        # arbitrarily large `candidate_ids` just to throw most of it away
        # afterward.
        wanted = list(
            dict.fromkeys(str(value) for value in candidate_ids[:bound])
        )[:bound]
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        with self.database.connect() as db:
            rows = db.execute(
                f"SELECT * FROM catalog_candidates WHERE job_id=? AND id IN ({placeholders}) "
                "ORDER BY position",
                (job_id, *wanted),
            ).fetchall()
        return [self._candidate_row(row) for row in rows]

    def pending_candidates(self, job_id: str, *, limit: int) -> list[dict]:
        return self.list_candidates(
            job_id, state="candidate", cursor=0, limit=limit
        )

    def mark_candidates_applied(
        self, job_id: str, candidate_ids: Sequence[str]
    ) -> int:
        wanted = list(dict.fromkeys(str(value) for value in candidate_ids))[
            :CATALOG_MAX_CANDIDATE_PAGE
        ]
        if not wanted:
            return 0
        placeholders = ",".join("?" for _ in wanted)
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE catalog_candidates SET state='applied' "
                f"WHERE job_id=? AND state='candidate' AND id IN ({placeholders})",
                (job_id, *wanted),
            )
        return int(cursor.rowcount or 0)

    def mark_candidates_dismissed(
        self,
        job_id: str,
        candidate_ids: Sequence[str],
        *,
        reject_info: Mapping[str, Any],
    ) -> int:
        wanted = list(dict.fromkeys(str(value) for value in candidate_ids))[
            :CATALOG_MAX_CANDIDATE_PAGE
        ]
        if not wanted:
            return 0
        placeholders = ",".join("?" for _ in wanted)
        payload = json.dumps(dict(reject_info), ensure_ascii=False)
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE catalog_candidates SET state='dismissed', reject_info=? "
                f"WHERE job_id=? AND state='candidate' AND id IN ({placeholders})",
                (payload, job_id, *wanted),
            )
        return int(cursor.rowcount or 0)

    def expire_pending_candidates(
        self, job_id: str, *, reject_info: Mapping[str, Any]
    ) -> int:
        """Dismiss EVERY still-`candidate` row of one job, in one statement.

        Deliberately not expressible as ``mark_candidates_dismissed`` with a
        list: that method takes an explicit selection and caps it at one page,
        which is right for apply's conflict set and wrong here — this is the
        complete-set operation that releases the restart guard, and a job with
        more than ``CATALOG_MAX_CANDIDATE_PAGE`` candidates must not be left
        half-expired and still blocked. The write is bounded by the job's own
        candidate count and rides ``idx_catalog_candidates_job_state``.
        """
        payload = json.dumps(dict(reject_info), ensure_ascii=False)
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE catalog_candidates SET state='dismissed', reject_info=? "
                "WHERE job_id=? AND state='candidate'",
                (payload, job_id),
            )
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------ source generation
    def source_element_generation(self, source_id: str) -> str:
        """An opaque token that changes iff this source's ELEMENTS were swapped.

        ``replace_elements`` — the one writer that replaces a document source's
        elements — deletes every row and re-inserts the whole batch carrying a
        SINGLE ``created_at`` (the caller's microsecond-precision ``now()``),
        inside the same write transaction that advances ``sources.updated_at``.
        So ``MAX(created_at)`` over one source's elements IS that swap's stamp:
        equal token ⇒ same element generation, different token ⇒ the elements
        were replaced. A source with no elements yields ``""``, which compares
        equal to itself and therefore never produces a spurious mismatch.

        Deliberately NOT ``sources.updated_at`` (nor ``source_change_signal_rows``'
        token, which is built from it): that signal is intentionally coarse —
        it also moves on every lifecycle transition (``extracting``/``extracted``,
        a summary write, a re-extraction), none of which touch
        ``source_elements``. Its consumer is a COUNT CACHE, where a false
        invalidation costs a recount; this token's consumer refuses a confirm
        and tells the user their source was reparsed, so a false positive costs
        a whole paid re-extraction on top of a claim that is not true.

        One indexed aggregate on a human-paced path (start/apply/dismiss), never
        in a loop: ``idx_source_elements_source_type`` covers
        ``(source_id, element_type, created_at, id)``, so this is an index-only
        read of one source's entries.
        """
        with self.database.connect() as db:
            row = db.execute(
                "SELECT MAX(created_at) AS generation FROM source_elements "
                "WHERE source_id=?",
                (source_id,),
            ).fetchone()
        return str(row["generation"] or "") if row is not None else ""

    # --------------------------------------------------------------- preview
    def preview_elements(
        self, source_id: str, *, limit: int, text_chars: int
    ) -> tuple[list[dict], bool]:
        """A bounded prefix of one source's elements, each clipped in SQL.

        Returns ``(rows, clipped)``. The cost preview must never become the very
        scan it is estimating, so both dimensions are bounded here rather than
        in Python: ``LIMIT`` caps the rows and ``substr`` caps the bytes per
        row.

        ``clipped`` reports the SECOND bound, and it is not decorative. A
        caller can see the row cap for itself by counting what came back, but it
        cannot see per-row truncation at all — and that one bites harder: a
        clipped options table loses parameter names, which loses slices, which
        makes the estimate several times too low on exactly the documents this
        feature targets. ``length(text) > bound`` is evaluated in the same
        query, so it costs nothing extra.

        Each row also carries ``full_chars``: the element's WHOLE stripped
        length, not the clipped ``text``'s. Same normalisation and same reason
        as ``source_text_stats``, and it is what lets the caller subtract the
        prefix from that total exactly. Deriving it from the returned ``text``
        cannot work — that string has already lost everything past
        ``text_chars`` — and it is one more expression on a row the query is
        already producing, so it transmits an integer and reads nothing extra.
        """
        rows_bound = max(1, int(limit))
        chars_bound = max(1, int(text_chars))
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT id, element_type, substr(text,1,?) AS text, metadata, "
                "(length(text) > ?) AS clipped, "
                "length(trim(text, char(32)||char(9)||char(10)||char(13))) "
                "AS full_chars "
                "FROM source_elements WHERE source_id=? ORDER BY id LIMIT ?",
                (chars_bound, chars_bound, source_id, rows_bound),
            ).fetchall()
        out: list[dict] = []
        clipped = False
        for row in rows:
            metadata = _loads(row["metadata"])
            clipped = clipped or bool(row["clipped"])
            out.append(
                {
                    "id": row["id"],
                    "element_type": row["element_type"],
                    "text": row["text"] or "",
                    "section_path": str(metadata.get("section_path") or ""),
                    "full_chars": int(row["full_chars"] or 0),
                }
            )
        return out, clipped

    def source_text_stats(self, source_id: str) -> tuple[int, int]:
        """``(element_count, total_chars)`` over the SAME row universe
        ``preview_elements`` reads — ``WHERE source_id=?``, no other
        predicate — aggregated in SQL so no element text ever reaches Python.

        ``total_chars`` counts each element's text AFTER stripping leading and
        trailing whitespace, because that is what the packer counts
        (``command_catalog._window_elements`` strips every element before it
        packs anything). Summing raw ``LENGTH`` instead makes the caller's
        window arithmetic OVER-count, which is the one direction it may not
        err in: it is published as a lower bound, and a document of 2,001
        elements each holding one character and twenty trailing spaces would
        be quoted four windows when it really packs into one. The element JOIN
        separators the packer adds between elements are deliberately NOT
        counted — that omission can only make the total smaller, so it keeps
        the bound on the safe side.

        Feeds the v2 preview's ``estimated_windows`` (windows are packed by
        character count over the source's full text, not the bounded prefix
        ``preview_elements`` hydrates). One indexed, bounded scan: SQLite
        still has to visit every row of the source to sum ``LENGTH(text)``,
        but it is a single aggregate query with zero row/byte materialization
        back to the caller — the same "bounded scan, zero transmission"
        contract ``preview_elements`` documents for its own read.
        """
        with self.database.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS element_count, COALESCE(SUM(length("
                "trim(text, char(32)||char(9)||char(10)||char(13)))),0) "
                "AS total_chars "
                "FROM source_elements WHERE source_id=?",
                (source_id,),
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["element_count"] or 0), int(row["total_chars"] or 0)


__all__ = ["CatalogJobAlreadyRunning", "CatalogStore"]
