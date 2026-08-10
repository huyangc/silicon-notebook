"""PostgreSQL row persistence for command-catalog extraction (Plan C, C1b).

A byte-for-byte behavioural mirror of ``app/repositories/sqlite/catalog_store.py``:
same method names, same bounds, same return shapes. The only differences are the
ones the backends genuinely disagree about — ``jsonb`` instead of JSON text,
``timestamptz`` instead of ISO strings (with the historical empty-string
sentinel restored on read), and ``COLLATE "C"`` on the ordering keys so a
non-C-collated database still pages in the same order SQLite does.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from psycopg import errors

from app.repositories.postgres._store_utils import (
    TimestampInput,
    execute_many,
    iso_timestamp,
    json_value,
    jsonb,
    normalized_clock,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.ports import (
    CATALOG_CANDIDATE_STATES,
    CATALOG_MAX_CANDIDATE_BATCH,
    CATALOG_MAX_CANDIDATE_PAGE,
    CATALOG_TERMINAL_STATUSES,
    CatalogJobAlreadyRunning,
)


class CatalogStore:
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
            "applied_table_id": row["applied_table_id"] or "",
            "source_generation": row["source_generation"] or "",
            "created_at": iso_timestamp(row["created_at"]),
            "updated_at": iso_timestamp(row["updated_at"]),
            "finished_at": iso_timestamp(row["finished_at"]),
        }

    def create_job(
        self,
        notebook_id: str,
        source_id: str,
        created_by: str,
        *,
        source_generation: str = "",
    ) -> dict:
        job_id = self.new_id("cjb")
        now = self.now()
        try:
            with self.database.write() as connection:
                connection.execute(
                    "INSERT INTO catalog_jobs"
                    "(id,notebook_id,source_id,created_by,status,sections_total,"
                    "sections_done,entries,rejected,uncovered,truncated_sections,"
                    "failure_reason,diagnostic,applied_table_id,source_generation,"
                    "created_at,updated_at,finished_at) "
                    "VALUES (%s,%s,%s,%s,'queued',0,0,0,0,0,0,'','','',%s,%s,%s,NULL)",
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
        except errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "idx_catalog_jobs_one_active":
                raise CatalogJobAlreadyRunning(source_id) from exc
            raise
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM catalog_jobs WHERE id=%s", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job_row(row)

    def latest_job(self, source_id: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM catalog_jobs WHERE source_id=%s "
                'ORDER BY created_at DESC,id COLLATE "C" DESC LIMIT 1',
                (source_id,),
            ).fetchone()
        return self._job_row(row) if row is not None else None

    def active_job(self, source_id: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM catalog_jobs WHERE source_id=%s "
                "AND status IN ('queued','running') LIMIT 1",
                (source_id,),
            ).fetchone()
        return self._job_row(row) if row is not None else None

    def latest_applied_table_id(self, source_id: str) -> str:
        """See the SQLite mirror. Same ``COLLATE "C"`` tie-break on ``id`` as
        ``latest_job`` so a non-C-collated database still resolves ties the
        same way SQLite's default binary collation does."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT applied_table_id FROM catalog_jobs WHERE source_id=%s "
                "AND applied_table_id != '' "
                'ORDER BY created_at DESC,id COLLATE "C" DESC LIMIT 1',
                (source_id,),
            ).fetchone()
        return str(row["applied_table_id"]) if row is not None else ""

    def start_job(self, job_id: str, sections_total: int) -> bool:
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE catalog_jobs SET status='running',sections_total=%s,"
                "updated_at=%s WHERE id=%s AND status='queued'",
                (max(0, int(sections_total)), self.now(), job_id),
            )
        return cursor.rowcount == 1

    def set_section_total(self, job_id: str, sections_total: int) -> bool:
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE catalog_jobs SET sections_total=%s,updated_at=%s "
                "WHERE id=%s AND status='running'",
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
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE catalog_jobs SET sections_done=sections_done+1,"
                "entries=entries+%s,rejected=rejected+%s,uncovered=uncovered+%s,"
                "truncated_sections=truncated_sections+%s,"
                "updated_at=%s WHERE id=%s AND status='running'",
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
        """Unconditional (no `status` filter) — see the SQLite mirror."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE catalog_jobs SET applied_table_id=%s,updated_at=%s "
                "WHERE id=%s",
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
        if status not in CATALOG_TERMINAL_STATUSES:
            raise ValueError("catalog job terminal status is not recognised")
        now = self.now()
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE catalog_jobs SET status=%s,failure_reason=%s,diagnostic=%s,"
                "updated_at=%s,finished_at=%s "
                "WHERE id=%s AND status IN ('queued','running')",
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
            "payload": json_value(row["payload"], {}) or {},
            "state": row["state"],
            "reject_info": json_value(row["reject_info"], {}) or {},
            "created_at": iso_timestamp(row["created_at"]),
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
                jsonb(row.get("payload") or {}),
                str(row.get("state") or "candidate"),
                jsonb(row.get("reject_info") or {}),
                now,
            )
            for row in batch
        ]
        statement = (
            "INSERT INTO catalog_candidates"
            "(id,job_id,notebook_id,source_id,position,section_path,"
            "command_name,payload,state,reject_info,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        for start in range(0, len(values), CATALOG_MAX_CANDIDATE_BATCH):
            with self.database.write() as connection:
                execute_many(
                    connection,
                    statement,
                    values[start:start + CATALOG_MAX_CANDIDATE_BATCH],
                )

    def update_candidate_payload(
        self,
        candidate_id: str,
        payload: Mapping[str, Any],
        reject_info: Mapping[str, Any],
    ) -> bool:
        """Revise one still-`candidate` row's payload — see the SQLite mirror
        (and the port) for why this exists and why it touches two columns only.
        """
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE catalog_candidates SET payload=%s,reject_info=%s "
                "WHERE id=%s AND state='candidate'",
                (jsonb(dict(payload)), jsonb(dict(reject_info)), str(candidate_id)),
            )
        return int(cursor.rowcount or 0) == 1

    def list_candidates(
        self, job_id: str, *, state: str, cursor: int, limit: int
    ) -> list[dict]:
        if state not in CATALOG_CANDIDATE_STATES:
            raise ValueError("unsupported catalog candidate state")
        bound = max(1, min(int(limit), CATALOG_MAX_CANDIDATE_PAGE))
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM catalog_candidates "
                "WHERE job_id=%s AND state=%s AND position>%s "
                "ORDER BY position LIMIT %s",
                (job_id, state, max(0, int(cursor)), bound),
            ).fetchall()
        return [self._candidate_row(row) for row in rows]

    def candidate_counts(self, job_id: str) -> dict[str, int]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT state,COUNT(*) AS n FROM catalog_candidates "
                "WHERE job_id=%s GROUP BY state",
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
        # Sliced to `bound` BEFORE `dict.fromkeys` — see the SQLite mirror for
        # why: this store method cannot see whether its caller already
        # rejected an oversized selection, so it must not itself materialize
        # an arbitrarily large `candidate_ids` just to discard most of it.
        wanted = list(
            dict.fromkeys(str(value) for value in candidate_ids[:bound])
        )[:bound]
        if not wanted:
            return []
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM catalog_candidates WHERE job_id=%s AND id=ANY(%s) "
                "ORDER BY position",
                (job_id, wanted),
            ).fetchall()
        return [self._candidate_row(row) for row in rows]

    def pending_candidates(self, job_id: str, *, limit: int) -> list[dict]:
        return self.list_candidates(job_id, state="candidate", cursor=0, limit=limit)

    def mark_candidates_applied(
        self, job_id: str, candidate_ids: Sequence[str]
    ) -> int:
        wanted = list(dict.fromkeys(str(value) for value in candidate_ids))[
            :CATALOG_MAX_CANDIDATE_PAGE
        ]
        if not wanted:
            return 0
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE catalog_candidates SET state='applied' "
                "WHERE job_id=%s AND state='candidate' AND id=ANY(%s)",
                (job_id, wanted),
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
        payload = jsonb(dict(reject_info))
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE catalog_candidates SET state='dismissed', reject_info=%s "
                "WHERE job_id=%s AND state='candidate' AND id=ANY(%s)",
                (payload, job_id, wanted),
            )
        return int(cursor.rowcount or 0)

    def expire_pending_candidates(
        self, job_id: str, *, reject_info: Mapping[str, Any]
    ) -> int:
        """Complete-set dismissal of one job's remaining candidates — see the
        SQLite mirror for why this is not `mark_candidates_dismissed` with a
        list."""
        payload = jsonb(dict(reject_info))
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE catalog_candidates SET state='dismissed', reject_info=%s "
                "WHERE job_id=%s AND state='candidate'",
                (payload, job_id),
            )
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------ source generation
    def source_element_generation(self, source_id: str) -> str:
        """Element-generation token — see the SQLite mirror for the full
        rationale (why it is the element stamp and not ``sources.updated_at``).

        ``created_at`` is ``timestamptz`` here rather than ISO text, so the
        aggregate goes through ``iso_timestamp`` for a deterministic string.
        The value is only ever compared against another token taken from THIS
        backend (it is stored in ``catalog_jobs.source_generation`` and read
        back), so the rendering only has to be stable, not cross-backend.
        """
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT MAX(created_at) AS generation FROM source_elements "
                "WHERE source_id=%s",
                (source_id,),
            ).fetchone()
        if row is None or row["generation"] is None:
            return ""
        return iso_timestamp(row["generation"])

    # --------------------------------------------------------------- preview
    def preview_elements(
        self, source_id: str, *, limit: int, text_chars: int
    ) -> tuple[list[dict], bool]:
        rows_bound = max(1, int(limit))
        chars_bound = max(1, int(text_chars))
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,element_type,substr(text,1,%s) AS text,metadata,"
                "(length(text) > %s) AS clipped "
                'FROM source_elements WHERE source_id=%s ORDER BY id COLLATE "C" '
                "LIMIT %s",
                (chars_bound, chars_bound, source_id, rows_bound),
            ).fetchall()
        output: list[dict] = []
        clipped = False
        for row in rows:
            metadata = json_value(row["metadata"], {})
            section_path = (
                str(metadata.get("section_path") or "")
                if isinstance(metadata, dict)
                else ""
            )
            clipped = clipped or bool(row["clipped"])
            output.append(
                {
                    "id": row["id"],
                    "element_type": row["element_type"],
                    "text": row["text"] or "",
                    "section_path": section_path,
                }
            )
        return output, clipped

    def source_text_stats(self, source_id: str) -> tuple[int, int]:
        """``(element_count, total_chars)`` — see the SQLite mirror for the
        full rationale (same row universe as ``preview_elements``, i.e.
        ``WHERE source_id=%s`` with no other predicate, aggregated entirely
        in SQL)."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS element_count, "
                "COALESCE(SUM(LENGTH(text)),0) AS total_chars "
                "FROM source_elements WHERE source_id=%s",
                (source_id,),
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["element_count"] or 0), int(row["total_chars"] or 0)


__all__ = ["CatalogJobAlreadyRunning", "CatalogStore"]
