"""PostgreSQL persistence for auto user-memory suggestions (0087).

Canonical twin of ``sqlite/memory_suggest_store.py``; the port contract is
``MemorySuggestStorePort`` in ``app/repositories/ports.py``. Every read binds
its owner in SQL (design §6.1): ask jobs by ``created_by``, answers through
``conversations.created_by``, feedback only through answers that owner owns,
memories by ``created_by`` -- ``tests/test_memory_suggest_privacy_guard.py``
pins each read statement to its predicate.

Instants are ``timestamptz`` here; every instant parameter goes through
``normalize_timestamp`` (a naive string means system-local wall time, the
facade clock's convention) and every instant read back is an ISO string.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from app.domain.memory_suggest_contracts import (
    FAILURE_REASON_INTERRUPTED,
    FINGERPRINT_DECLINED,
    FINGERPRINT_KINDS,
    FINGERPRINT_PROPOSED,
    JOB_STATUS_IDLE,
    JOB_STATUS_RUNNING,
    MEMORY_MODE_MANUAL,
    MEMORY_MODES,
    NEIGHBOR_VECTOR_SCAN_MAX,
    OUTCOME_FAILED,
    REVISION_REASON_EXPIRED,
)
from app.repositories.memory_mode_epoch import (
    invalidate_mode_epoch,
    memory_mode_write,
    stable_mode_epoch,
)
from app.repositories.memory_suggest_rows import (
    checked_statuses,
    clarification_rows,
    cosine_top,
    dedupe_neighbors,
    json_list_value,
    ordered_job_delete_sql,
)
from app.repositories.postgres._store_utils import (
    execute_many,
    iso_timestamp,
    jsonb,
    normalize_timestamp,
)
from app.repositories.postgres.access_sql import NOTEBOOK_LIVE_SQL, read_access_clause
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.id_binding import bind_ids, execute_ids, member_of
from app.repositories.postgres.memory_suggest_read_period import ask_after_read_loss_sql, lock_mode_read_period_on
from app.repositories.postgres.read_authority_lock import lock_live_reader_on, lock_read_changes_on
from app.repositories.postgres.search import MemoryCandidateScope, memory_candidate_ids

_SAMPLE_WHERE = (
    "a.created_by = %s AND a.notebook_id = %s AND a.status = 'done' "
    "AND a.submitted_via = 'web' AND a.output = 'answer' AND a.report_id = ''"
)
_ASK_COLUMNS = (
    "a.id, a.created_at, a.question, a.answer_id, a.conversation_id, a.mode"
)
_JOB_COLUMNS = (
    "notebook_id, owner_id, status, pending_signal, cursor_created_at, cursor_id, "
    "cursor_inclusive, window_started_at, consecutive_failures, claim_token, runs, "
    "candidates_written, last_outcome, failure_reason, started_at, finished_at, "
    "created_at, updated_at"
)


#: Write one ``proposed`` fingerprint ``(notebook_id, owner_id, fingerprint,
#: created_at)``: upgrades a ``declined`` row and never expires, leaves a
#: ``proposed`` row alone (design §5.1). The one text both
#: ``write_fingerprints`` and the suggestion write transaction
#: (``memory_store.create_suggestions_with_initial_revision``) execute.
PROPOSED_FINGERPRINT_UPSERT_SQL = (
    "INSERT INTO memory_suggest_fingerprints "
    "(notebook_id, owner_id, fingerprint, kind, expires_at, created_at) "
    "VALUES (%s, %s, %s, 'proposed', NULL, %s) "
    "ON CONFLICT (notebook_id, owner_id, fingerprint) DO UPDATE SET "
    "kind = 'proposed', expires_at = NULL "
    "WHERE memory_suggest_fingerprints.kind = 'declined'"
)


#: Write one ``declined`` fingerprint ``(notebook_id, owner_id, fingerprint,
#: expires_at, created_at)``: never downgrades a ``proposed`` row and only
#: extends an existing ``declined`` expiry (design §5.1). The one text both
#: ``write_fingerprints`` and the suggestion write transaction
#: (``memory_store.create_suggestions_with_initial_revision``, review P3-2)
#: execute.
DECLINED_FINGERPRINT_UPSERT_SQL = (
    "INSERT INTO memory_suggest_fingerprints "
    "(notebook_id, owner_id, fingerprint, kind, expires_at, created_at) "
    "VALUES (%s, %s, %s, 'declined', %s, %s) "
    "ON CONFLICT (notebook_id, owner_id, fingerprint) DO UPDATE SET "
    "expires_at = EXCLUDED.expires_at "
    "WHERE memory_suggest_fingerprints.kind = 'declined' "
    "AND EXCLUDED.expires_at > memory_suggest_fingerprints.expires_at"
)


def _instant(value: Any):
    return normalize_timestamp(value)


def _ask_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "created_at": iso_timestamp(row["created_at"]),
        "question": str(row["question"] or ""),
        "answer_id": str(row["answer_id"] or ""),
        "conversation_id": str(row["conversation_id"] or ""),
        "mode": str(row["mode"] or ""),
    }


def _job_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "notebook_id": str(row["notebook_id"]),
        "owner_id": str(row["owner_id"]),
        "status": str(row["status"]),
        "pending_signal": int(row["pending_signal"]),
        "cursor_created_at": iso_timestamp(row["cursor_created_at"]),
        "cursor_id": str(row["cursor_id"]),
        "cursor_inclusive": bool(row["cursor_inclusive"]),
        "window_started_at": iso_timestamp(row["window_started_at"]),
        "consecutive_failures": int(row["consecutive_failures"]),
        "claim_token": str(row["claim_token"]),
        "runs": int(row["runs"]),
        "candidates_written": int(row["candidates_written"]),
        "last_outcome": str(row["last_outcome"]),
        "failure_reason": str(row["failure_reason"]),
        "started_at": iso_timestamp(row["started_at"]),
        "finished_at": iso_timestamp(row["finished_at"]),
        "created_at": iso_timestamp(row["created_at"]),
        "updated_at": iso_timestamp(row["updated_at"]),
    }


class MemorySuggestStore:
    """Row persistence for the switch, the single-flight job rows, the
    fingerprints and the bounded sampling reads of auto memory suggestions."""

    def __init__(self, database: PostgresDatabase, *, new_id: Callable[[str], str]) -> None:
        self.database = database
        self.new_id = new_id
        invalidate_mode_epoch()

    # ----------------------------------------------------------- switch
    def mode_epoch(self) -> int | None:
        return stable_mode_epoch()

    def user_state(self, owner_id: str) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT owner_id, enabled, enabled_at, quota_day, quota_used, updated_at "
                "FROM memory_suggest_users WHERE owner_id = %s",
                (owner_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "owner_id": str(row["owner_id"]),
            "enabled": bool(row["enabled"]),
            "enabled_at": iso_timestamp(row["enabled_at"]),
            "quota_day": str(row["quota_day"]),
            "quota_used": int(row["quota_used"]),
            "updated_at": iso_timestamp(row["updated_at"]),
        }

    def set_memory_mode(self, user_id: str, mode: str, *, now: str | Callable[[], str]) -> None:
        if mode not in MEMORY_MODES:
            raise ValueError("invalid memory_mode")
        enabled = 0 if mode == MEMORY_MODE_MANUAL else 1
        with memory_mode_write(), self.database.write() as db:
            lock_mode_read_period_on(db, exclusive=True)
            instant = _instant(now() if callable(now) else now)
            if db.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
                raise KeyError(user_id)
            updated = db.execute(
                "UPDATE user_profiles SET memory_mode = %s, updated_at = %s "
                "WHERE user_id = %s RETURNING id",
                (mode, instant, user_id),
            ).fetchone()
            if updated is None:
                db.execute(
                    "INSERT INTO user_profiles "
                    "(id, user_id, memory_mode, domain_focus, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET memory_mode = EXCLUDED.memory_mode, "
                    "updated_at = EXCLUDED.updated_at",
                    (f"profile-{user_id}", user_id, mode, jsonb([]), instant, instant),
                )
            db.execute(
                "INSERT INTO memory_suggest_users (owner_id, enabled, enabled_at, updated_at) "
                "VALUES (%s, %s, CASE WHEN %s = 1 THEN %s::timestamptz END, %s) "
                "ON CONFLICT (owner_id) DO UPDATE SET "
                "enabled = EXCLUDED.enabled, "
                "enabled_at = CASE WHEN EXCLUDED.enabled = 1 AND "
                "(memory_suggest_users.enabled = 0 OR memory_suggest_users.enabled_at IS NULL) "
                "THEN EXCLUDED.enabled_at "
                "ELSE memory_suggest_users.enabled_at END, "
                "updated_at = EXCLUDED.updated_at",
                (user_id, enabled, enabled, instant, instant),
            )
            if not enabled:
                db.execute(
                    ordered_job_delete_sql("memory_suggest_jobs.owner_id = %s"), (user_id,)
                )

    # ----------------------------------------------------------- job rows
    def note_signal(
        self, notebook_id: str, owner_id: str, *, ask_job_id: str, now: str
    ) -> int:
        """One write transaction: create the row at the triggering ask
        (inclusive) or, when it already exists, only count the signal -- and
        only while the owner's switch is on AND the owner can still read this
        live notebook. Returns 0 when nothing was counted (switch off, no
        switch row, notebook gone or not live, no read access).

        Read access (plan B9) is locked first, in the same transaction, by the
        shared ``lock_live_reader_on``: the notebooks row ``FOR SHARE`` with
        the live predicate, then the member row, or the direct/``everyone``
        grant edge, or the group grant edge plus its ``group_members`` row,
        each ``FOR SHARE``. A ``delete_grant`` / member or group removal that
        commits first is seen and nothing is written; one that arrives after
        waits on those row locks and, once this commits, its own job-row
        cleanup (``_clear_suggest_jobs_without_read_on``) deletes the row
        written here. The upsert then repeats the read predicate as an EXISTS
        (shared ``read_access_clause``, the switch row's owner as the user) so
        the statement alone never counts for a non-reader.

        The switch row is read ``FOR SHARE`` in the upsert itself: a
        ``set_memory_mode(manual)`` that commits first makes it see
        ``enabled = 0`` and write nothing; one that starts after waits for it
        and then deletes the row it wrote. Lock order is access rows →
        ``memory_suggest_users`` → job row, the order every writer here keeps
        (plan B11)."""
        instant = _instant(now)
        readable = read_access_clause("nb", "nm", user_ref="u.owner_id")
        with self.database.write() as db:
            if not lock_live_reader_on(db, notebook_id, owner_id):
                return 0
            row = db.execute(
                "INSERT INTO memory_suggest_jobs (notebook_id, owner_id, status, "
                "pending_signal, cursor_created_at, cursor_id, cursor_inclusive, "
                "window_started_at, created_at, updated_at) "
                "SELECT %s, u.owner_id, 'idle', 1, "
                "GREATEST(COALESCE(a.created_at, %s), u.enabled_at, f.lost_at), "
                "CASE WHEN a.created_at >= GREATEST(u.enabled_at, f.lost_at) THEN a.id ELSE '' END, 1, "
                "GREATEST(COALESCE(a.created_at, %s), u.enabled_at, f.lost_at), %s, %s "
                "FROM memory_suggest_users u LEFT JOIN ask_jobs a ON a.id = %s "
                "AND a.created_by = u.owner_id AND a.notebook_id = %s "
                "LEFT JOIN memory_suggest_read_fences f ON f.owner_id=u.owner_id AND f.notebook_id=%s "
                "WHERE u.owner_id = %s AND u.enabled = 1 "
                "AND EXISTS (SELECT 1 FROM notebooks nb WHERE nb.id = %s "
                f"AND nb.{NOTEBOOK_LIVE_SQL} AND {readable}) "
                "FOR SHARE OF u "
                "ON CONFLICT (notebook_id, owner_id) DO UPDATE SET "
                "pending_signal = memory_suggest_jobs.pending_signal + 1, "
                "updated_at = EXCLUDED.updated_at "
                "RETURNING pending_signal",
                (
                    notebook_id, instant, instant, instant, instant,
                    ask_job_id, notebook_id, notebook_id, owner_id, notebook_id,
                ),
            ).fetchone()
        return 0 if row is None else int(row["pending_signal"])

    def claim(
        self, notebook_id: str, owner_id: str, *, claim_token: str, now: str
    ) -> dict[str, Any] | None:
        if not claim_token:
            raise ValueError("claim_token must be non-empty")
        instant = _instant(now)
        with self.database.write() as db:
            row = db.execute(
                "UPDATE memory_suggest_jobs SET status = %s, claim_token = %s, "
                "started_at = %s, updated_at = %s "
                "WHERE notebook_id = %s AND owner_id = %s AND status = %s "
                f"RETURNING {_JOB_COLUMNS}",
                (
                    JOB_STATUS_RUNNING, claim_token, instant, instant,
                    notebook_id, owner_id, JOB_STATUS_IDLE,
                ),
            ).fetchone()
        return None if row is None else _job_row(row)

    def settle(
        self,
        notebook_id: str,
        owner_id: str,
        *,
        claim_token: str,
        outcome: str,
        failure_reason: str,
        cursor: tuple[str, str, bool] | None,
        consecutive_failures: int,
        consumed_pending: int,
        candidates_written: int,
        now: str,
    ) -> bool:
        cursor_sql = ""
        cursor_params: tuple[Any, ...] = ()
        if cursor is not None:
            cursor_sql = "cursor_created_at = %s, cursor_id = %s, cursor_inclusive = %s, "
            cursor_params = (_instant(cursor[0]), str(cursor[1]), 1 if cursor[2] else 0)
        instant = _instant(now)
        with self.database.write() as db:
            changed = db.execute(
                "UPDATE memory_suggest_jobs SET status = %s, claim_token = '', "
                f"{cursor_sql}"
                "consecutive_failures = %s, last_outcome = %s, failure_reason = %s, "
                "runs = runs + 1, candidates_written = candidates_written + %s, "
                "pending_signal = GREATEST(pending_signal - %s, 0), "
                "finished_at = %s, updated_at = %s "
                "WHERE notebook_id = %s AND owner_id = %s AND status = %s AND claim_token = %s",
                (
                    JOB_STATUS_IDLE, *cursor_params,
                    int(consecutive_failures), outcome, failure_reason,
                    int(candidates_written), max(0, int(consumed_pending)),
                    instant, instant, notebook_id, owner_id, JOB_STATUS_RUNNING, claim_token,
                ),
            ).rowcount
        return changed == 1

    def sweep_stale_on_start(self, *, now: str) -> int:
        """Startup recovery: a ``running`` row left by a previous process is a
        run that died. It settles as one ``failed`` run of the same batch
        (see ``FAILURE_REASON_INTERRUPTED``): cursor unchanged, ``runs`` and
        ``consecutive_failures`` + 1, so T7b's give-up rule covers a batch
        that keeps killing the process."""
        instant = _instant(now)
        with self.database.write() as db:
            return int(db.execute(
                "UPDATE memory_suggest_jobs SET status = %s, claim_token = '', "
                "last_outcome = %s, failure_reason = %s, runs = runs + 1, "
                "consecutive_failures = consecutive_failures + 1, "
                "finished_at = %s, updated_at = %s WHERE status = %s",
                (
                    JOB_STATUS_IDLE, OUTCOME_FAILED, FAILURE_REASON_INTERRUPTED,
                    instant, instant, JOB_STATUS_RUNNING,
                ),
            ).rowcount)

    def clear_job_rows(self, notebook_id: str, owner_ids: Sequence[str]) -> int:
        """Post-commit hygiene must neither clear surviving access nor a new
        window created after regrant. Recheck authority inside the write."""
        owners = list(dict.fromkeys(str(owner) for owner in owner_ids))
        if not owners:
            return 0
        bound = bind_ids(owners)
        readable = read_access_clause("nb", "nm", user_ref="memory_suggest_jobs.owner_id")
        with self.database.write() as db:
            lock_read_changes_on(db, [notebook_id])
            return int(execute_ids(
                db,
                ordered_job_delete_sql(
                    "memory_suggest_jobs.notebook_id = %s "
                    f"AND {member_of('memory_suggest_jobs.owner_id', bound)} "
                    "AND NOT EXISTS (SELECT 1 FROM notebooks nb "
                    "WHERE nb.id=memory_suggest_jobs.notebook_id "
                    f"AND {readable})"
                ),
                (notebook_id, bound.param),
            ).rowcount)

    def reserve_daily_quota(
        self, owner_id: str, *, today_utc: str, cap: int, now: str
    ) -> bool:
        with self.database.write() as db:
            changed = db.execute(
                "UPDATE memory_suggest_users SET "
                "quota_used = CASE WHEN quota_day = %s THEN quota_used + 1 ELSE 1 END, "
                "quota_day = %s, updated_at = %s "
                "WHERE owner_id = %s AND (quota_day <> %s OR quota_used < %s)",
                (today_utc, today_utc, _instant(now), owner_id, today_utc, int(cap)),
            ).rowcount
        return changed == 1

    def unadopted_candidate_count(self, owner_id: str, notebook_id: str) -> int:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM memory_items m "
                "WHERE m.created_by = %s AND m.notebook_id = %s AND m.status = 'candidate' "
                "AND m.suggested_signal IS NOT NULL",
                (owner_id, notebook_id),
            ).fetchone()
        return int(row["n"])

    # ----------------------------------------------------------- sampling
    def clarified_scan(
        self,
        owner_id: str,
        notebook_id: str,
        *,
        cursor_created_at: str,
        cursor_id: str,
        inclusive: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        compare = ">=" if inclusive else ">"
        with self.database.connect() as db:
            rows = db.execute(
                f"SELECT {_ASK_COLUMNS} FROM ask_jobs a WHERE {_SAMPLE_WHERE} "
                f"AND {ask_after_read_loss_sql()} "
                f"AND (a.created_at, a.id) {compare} (%s, %s) "
                "ORDER BY a.created_at, a.id LIMIT %s",
                (
                    owner_id, notebook_id, _instant(cursor_created_at), cursor_id,
                    max(1, int(limit)),
                ),
            ).fetchall()
        return [_ask_row(row) for row in rows]

    def corrected_scan(
        self, owner_id: str, notebook_id: str, *, cursor_created_at: str,
        cursor_id: str, inclusive: bool, window_started_at: str, limit: int,
    ) -> list[dict[str, Any]]:
        """The raw immediately preceding turn, never a filtered predecessor.

        Invalid predecessors remain NULL, so the ascending scan still covers
        its full eligible prefix and cannot jump over a truncated signal.
        Materializing that prefix also bounds correlated predecessor reads
        before resolving the raw adjacent turn (mirrored by SQLite).
        Both turns must follow the owner's last read loss; predecessor
        period/eligibility is checked after resolving the raw adjacent turn.
        """
        compare = ">=" if inclusive else ">"
        with self.database.connect() as db:
            rows = db.execute(
                f"WITH prefix AS MATERIALIZED (SELECT {_ASK_COLUMNS} "
                "FROM ask_jobs a JOIN conversations c ON c.id=a.conversation_id "
                "AND c.created_by=%s AND c.notebook_id=a.notebook_id "
                f"WHERE {_SAMPLE_WHERE} "
                f"AND {ask_after_read_loss_sql('a')} "
                f"AND (a.created_at,a.id) {compare} (%s,%s) "
                "ORDER BY a.created_at,a.id LIMIT %s) "
                f"SELECT {_ASK_COLUMNS}, p.answer_id AS previous_answer_id "
                "FROM prefix a LEFT JOIN ask_jobs p ON p.id=("
                "SELECT previous.id FROM ask_jobs previous "
                "WHERE previous.conversation_id=a.conversation_id "
                "AND (previous.created_at,previous.id)<(a.created_at,a.id) "
                "ORDER BY previous.created_at DESC,previous.id DESC LIMIT 1) "
                "AND p.created_by=%s AND p.notebook_id=%s AND p.status='done' "
                "AND p.submitted_via='web' AND p.output='answer' AND p.report_id='' "
                "AND p.created_at >= %s "
                f"AND {ask_after_read_loss_sql('p')} "
                "ORDER BY a.created_at,a.id",
                (owner_id, owner_id, notebook_id, _instant(cursor_created_at), cursor_id,
                 max(1, int(limit)), owner_id, notebook_id, _instant(window_started_at)),
            ).fetchall()
        return [{**_ask_row(row), "previous_answer_id": str(row["previous_answer_id"] or "")} for row in rows]

    def earliest_unfinished_ask(
        self, owner_id: str, notebook_id: str, *,
        cursor_created_at: str, cursor_id: str, inclusive: bool,
    ) -> dict[str, Any] | None:
        """``{id, created_at}`` of the owner's earliest ask here that the
        scans could still pick up once it finishes (the sampling predicate
        with ``status = 'running'``), or ``None``. A run never moves its
        cursor past it. Filter by the current cursor before LIMIT so an older
        running ask outside the window cannot hide an eligible one."""
        compare = ">=" if inclusive else ">"
        with self.database.connect() as db:
            row = db.execute(
                "SELECT a.id, a.created_at FROM ask_jobs a "
                "WHERE a.created_by = %s AND a.notebook_id = %s AND a.status = 'running' "
                "AND a.submitted_via = 'web' AND a.output = 'answer' AND a.report_id = '' "
                f"AND {ask_after_read_loss_sql()} "
                f"AND (a.created_at, a.id) {compare} (%s, %s) "
                "ORDER BY a.created_at, a.id LIMIT 1",
                (owner_id, notebook_id, _instant(cursor_created_at), cursor_id),
            ).fetchone()
        return None if row is None else {
            "id": str(row["id"]), "created_at": iso_timestamp(row["created_at"]),
        }

    def repeated_window(
        self, owner_id: str, notebook_id: str, *, lower: str, limit: int
    ) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            rows = db.execute(
                f"SELECT {_ASK_COLUMNS} FROM ask_jobs a WHERE {_SAMPLE_WHERE} "
                f"AND {ask_after_read_loss_sql()} "
                "AND a.created_at >= %s "
                "ORDER BY a.created_at DESC, a.id DESC LIMIT %s",
                (owner_id, notebook_id, _instant(lower), max(1, int(limit))),
            ).fetchall()
        return [_ask_row(row) for row in rows]

    def answer_projections(
        self,
        owner_id: str,
        answer_ids: Sequence[str],
        *,
        excerpt_chars: int,
        clarification_chars: int,
        with_sources: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """The owner's answers only. ``clarification_answers`` is what
        ``query_intent.finalize_query_intent`` persists -- ``{id, question,
        answer}`` rows, the question cut to ``clarification_chars``; a
        finalized intent carries no ambiguity rows (they are emptied on
        confirmation) and no options, so neither is read. The answer excerpt is
        cut in SQL to ``excerpt_chars``.

        ``with_sources`` adds the answer's ``citations`` and ``anchors`` (a
        repeated-class draft's provenance, G13; plan appendix B14) in the same
        statement -- same join, same owner predicate -- shaped like
        ``ask_state.answer_memory_source`` (anything but a list reads ``[]``).
        The run asks for them only for the few drafts it writes."""
        ids = list(dict.fromkeys(str(value) for value in answer_ids if value))
        if not ids:
            return {}
        bound = bind_ids(ids)
        sources = (
            "an.payload->'citations' AS citations, an.payload->'anchors' AS anchors, "
            if with_sources else ""
        )
        with self.database.connect() as db:
            rows = execute_ids(
                db,
                f"SELECT an.id, an.conversation_id, an.created_at, {sources}"
                "an.payload->'intent'->'clarification_answers' AS clarifications, "
                "an.payload->>'evidence_level' AS evidence_level, "
                "left(COALESCE(an.payload->>'answer', ''), %s) AS excerpt "
                "FROM answers an JOIN conversations c ON c.id = an.conversation_id "
                f"WHERE c.created_by = %s AND {member_of('an.id', bound)}",
                (max(1, int(excerpt_chars)), owner_id, bound.param),
            ).fetchall()
        return {
            str(row["id"]): {
                "answer_id": str(row["id"]),
                "conversation_id": str(row["conversation_id"] or ""),
                "created_at": iso_timestamp(row["created_at"]),
                "clarification_answers": clarification_rows(
                    row["clarifications"], question_chars=clarification_chars
                ),
                "evidence_level": str(row["evidence_level"] or ""),
                "excerpt": str(row["excerpt"] or ""),
                **(
                    {
                        "citations": json_list_value(row["citations"]),
                        "anchors": json_list_value(row["anchors"]),
                    }
                    if with_sources else {}
                ),
            }
            for row in rows
        }

    def latest_feedback(
        self, owner_id: str, answer_ids: Sequence[str]
    ) -> dict[str, str]:
        ids = list(dict.fromkeys(str(value) for value in answer_ids if value))
        if not ids:
            return {}
        bound = bind_ids(ids)
        with self.database.connect() as db:
            rows = execute_ids(
                db,
                "SELECT DISTINCT ON (f.answer_id) f.answer_id, f.rating FROM feedback f "
                "JOIN answers an ON an.id = f.answer_id "
                "JOIN conversations c ON c.id = an.conversation_id "
                f"WHERE c.created_by = %s AND {member_of('f.answer_id', bound)} "
                "ORDER BY f.answer_id, f.created_at DESC, f.id DESC",
                (owner_id, bound.param),
            ).fetchall()
        return {str(row["answer_id"]): str(row["rating"]) for row in rows}

    def answers_with_memory(
        self, owner_id: str, answer_ids: Sequence[str]
    ) -> set[str]:
        ids = list(dict.fromkeys(str(value) for value in answer_ids if value))
        if not ids:
            return set()
        bound = bind_ids(ids)
        with self.database.connect() as db:
            rows = execute_ids(
                db,
                "SELECT m.source_answer_id FROM memory_items m "
                f"WHERE m.created_by = %s AND {member_of('m.source_answer_id', bound)}",
                (owner_id, bound.param),
            ).fetchall()
        return {str(row["source_answer_id"]) for row in rows}

    # ----------------------------------------------------------- fingerprints
    def fingerprints_present(
        self,
        owner_id: str,
        notebook_id: str,
        fingerprints: Sequence[str],
        *,
        now: str,
    ) -> set[str]:
        values = list(dict.fromkeys(str(value) for value in fingerprints if value))
        if not values:
            return set()
        bound = bind_ids(values)
        with self.database.connect() as db:
            rows = execute_ids(
                db,
                "SELECT fp.fingerprint FROM memory_suggest_fingerprints fp "
                "WHERE fp.notebook_id = %s AND fp.owner_id = %s "
                f"AND {member_of('fp.fingerprint', bound)} "
                "AND (fp.kind = %s OR fp.expires_at > %s)",
                (notebook_id, owner_id, bound.param, FINGERPRINT_PROPOSED, _instant(now)),
            ).fetchall()
        return {str(row["fingerprint"]) for row in rows}

    # 测试观察点:指纹优先级的直写构造点(生产只经 memory_store 的建议写事务写指纹)
    def write_fingerprints(
        self,
        owner_id: str,
        notebook_id: str,
        fingerprints: Sequence[str],
        *,
        kind: str,
        expires_at: str | None,
        now: str,
    ) -> None:
        """``proposed`` upgrades a ``declined`` row and never expires;
        ``declined`` never downgrades a ``proposed`` row and only extends an
        existing ``declined`` expiry (design §5.1)."""
        if kind not in FINGERPRINT_KINDS:
            raise ValueError(f"unknown fingerprint kind: {kind!r}")
        if kind == FINGERPRINT_DECLINED and not expires_at:
            raise ValueError("a declined fingerprint needs an expiry")
        values = list(dict.fromkeys(str(value) for value in fingerprints if value))
        if not values:
            return
        instant = _instant(now)
        if kind == FINGERPRINT_PROPOSED:
            statement = PROPOSED_FINGERPRINT_UPSERT_SQL
            rows = [(notebook_id, owner_id, value, instant) for value in values]
        else:
            statement = DECLINED_FINGERPRINT_UPSERT_SQL
            expiry = _instant(expires_at)
            rows = [(notebook_id, owner_id, value, expiry, instant) for value in values]
        with self.database.write() as db:
            execute_many(db, statement, rows)

    # ----------------------------------------------------------- neighbours
    def suggest_neighbors(
        self,
        owner_id: str,
        notebook_id: str,
        *,
        query_vectors: Sequence[Sequence[float]],
        lexical_queries: Sequence[str],
        statuses: Sequence[str],
        k: int,
    ) -> list[list[dict[str, Any]]]:
        """Per signal item: its raw-cosine top ``k`` among the owner's
        vector-bearing memories here (the most recently updated
        ``NEIGHBOR_VECTOR_SCAN_MAX``), then its lexical top ``k`` in every
        given status (the shared Memory matcher). ``query_vectors`` may be
        empty (no embedding service): lexical only."""
        allowed = checked_statuses(statuses)
        queries = [str(query or "").strip() for query in lexical_queries]
        if query_vectors and len(query_vectors) != len(queries):
            raise ValueError("query_vectors and lexical_queries must align")
        if not allowed or not queries:
            return [[] for _ in queries]
        placeholders = ",".join("%s" for _ in allowed)
        limit = max(1, int(k))
        result: list[list[dict[str, Any]]] = []
        with self.database.connect() as db:
            vector_rows: list[tuple[str, str, str, Any]] = []
            if query_vectors:
                vector_rows = [
                    (
                        str(row["id"]), str(row["title"]), str(row["status"]),
                        bytes(row["vector"]) if row["vector"] is not None else None,
                    )
                    for row in db.execute(
                        "SELECT m.id, m.title, m.status, me.vector FROM memory_items m "
                        "JOIN memory_embeddings me ON me.memory_id = m.id "
                        "WHERE m.created_by = %s AND m.notebook_id = %s "
                        f"AND m.status IN ({placeholders}) "
                        "ORDER BY m.updated_at DESC, m.id COLLATE \"C\" LIMIT %s",
                        (owner_id, notebook_id, *allowed, NEIGHBOR_VECTOR_SCAN_MAX),
                    ).fetchall()
                ]
            for index, query in enumerate(queries):
                found = (
                    cosine_top(query_vectors[index], vector_rows, limit)
                    if query_vectors else []
                )
                if query:
                    ranked = memory_candidate_ids(
                        db,
                        query,
                        limit,
                        scope=MemoryCandidateScope(
                            owner_id=owner_id,
                            viewer_id=owner_id,
                            notebook_id=notebook_id,
                            statuses=allowed,
                        ),
                    )[:limit]
                    if ranked:
                        bound = bind_ids(ranked)
                        titles = {
                            str(row["id"]): row
                            for row in execute_ids(
                                db,
                                "SELECT m.id, m.title, m.status FROM memory_items m "
                                f"WHERE m.created_by = %s AND {member_of('m.id', bound)}",
                                (owner_id, bound.param),
                            ).fetchall()
                        }
                        for memory_id in ranked:
                            row = titles.get(memory_id)
                            if row is not None:
                                found.append({
                                    "id": memory_id, "title": str(row["title"]),
                                    "status": str(row["status"]), "side": "lexical",
                                    "score": 0.0,
                                })
                result.append(dedupe_neighbors(found))
        return result

    # ----------------------------------------------------------- expiry
    def expire_overdue(
        self,
        *,
        now: str,
        cutoff: str,
        limit: int,
        owner_id: str | None = None,
        notebook_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Move open suggestions created before ``cutoff`` to ``rejected`` with
        one ``expired`` revision each (changed by their owner). Only rows with a
        ``suggested_signal`` and still ``candidate`` move; at most ``limit``,
        oldest first; rows another transaction holds are skipped, not waited
        for. Two set statements in one write transaction: the move
        (``UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED)
        RETURNING`` over a materialized pick) and the revisions (``INSERT ... SELECT`` over the moved
        ids paired with fresh revision ids)."""
        clauses = [
            "m.suggested_signal IS NOT NULL", "m.status = 'candidate'", "m.created_at < %s",
        ]
        params: list[Any] = [_instant(cutoff)]
        if owner_id is not None:
            clauses.append("m.created_by = %s")
            params.append(owner_id)
        if notebook_id is not None:
            clauses.append("m.notebook_id = %s")
            params.append(notebook_id)
        instant = _instant(now)
        with self.database.write() as db:
            # A MATERIALIZED CTE, not ``id IN (SELECT ... LIMIT n FOR UPDATE
            # SKIP LOCKED)``: the planner may re-run an IN sub-select, and with
            # SKIP LOCKED each re-run skips the rows the first run just locked
            # and hands back the next ones -- the LIMIT would not hold.
            moved = db.execute(
                "WITH picked AS MATERIALIZED ("
                f"SELECT m.id FROM memory_items m WHERE {' AND '.join(clauses)} "
                "ORDER BY m.created_at, m.id COLLATE \"C\" LIMIT %s "
                "FOR UPDATE SKIP LOCKED) "
                "UPDATE memory_items SET status = 'rejected', updated_at = %s "
                "FROM picked WHERE memory_items.id = picked.id "
                "AND memory_items.status = 'candidate' "
                "RETURNING memory_items.id, memory_items.created_by, "
                "memory_items.notebook_id, memory_items.suggested_signal, "
                "memory_items.created_at",
                (*params, max(1, int(limit)), instant),
            ).fetchall()
            if moved:
                revision_ids = [self.new_id("memrev") for _ in moved]
                memory_ids = [str(row["id"]) for row in moved]
                db.execute(
                    "INSERT INTO memory_revisions (id, memory_id, revision, title, "
                    "content_md, tags_json, status, promotion_state, changed_by, "
                    "change_reason, created_at) "
                    "SELECT p.revision_id, m.id, "
                    "(SELECT COALESCE(MAX(r.revision), 0) + 1 FROM memory_revisions r "
                    "WHERE r.memory_id = m.id), m.title, m.content_md, m.tags_json, "
                    "m.status, m.promotion_state, m.created_by, %s, %s "
                    "FROM unnest(%s::text[], %s::text[]) AS p(revision_id, memory_id) "
                    "JOIN memory_items m ON m.id = p.memory_id",
                    (REVISION_REASON_EXPIRED, instant, revision_ids, memory_ids),
                )
        moved = sorted(moved, key=lambda row: (row["created_at"], str(row["id"])))
        return [
            {
                "id": str(row["id"]),
                "owner_id": str(row["created_by"]),
                "notebook_id": str(row["notebook_id"]),
                "suggested_signal": str(row["suggested_signal"]),
            }
            for row in moved
        ]
