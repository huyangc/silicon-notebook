"""SQLite persistence for auto user-memory suggestions (v106).

Mirror of ``postgres/memory_suggest_store.py``; the port contract is
``MemorySuggestStorePort`` in ``app/repositories/ports.py``. Every read binds
its owner in SQL (design §6.1): ask jobs by ``created_by``, answers through
``conversations.created_by``, feedback only through answers that owner owns,
memories by ``created_by`` -- ``tests/test_memory_suggest_privacy_guard.py``
pins each read statement to its predicate.

Sampling/cursor keys use ``(sn_utc_us(created_at), id)``: the same exact UTC
microseconds as Python's SignalCursor. Expiry cutoffs still use julianday;
sampling must not use its submillisecond rounding or original text as a tie.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Sequence

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
)
from app.repositories.sqlite.access_sql import NOTEBOOK_LIVE_SQL, read_access_clause
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.id_binding import bind_ids, drive_by
from app.repositories.sqlite.memory_suggest_read_period import ask_after_read_loss_sql

# The sampling predicate shared by both reads (design §4.2 step 3 / §6.1):
# the owner's own finished web answers in this notebook, not a report follow-up.
_SAMPLE_WHERE = (
    "a.created_by = ? AND a.notebook_id = ? AND a.status = 'done' "
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
#: Mirror of the PostgreSQL constant: one ``proposed`` fingerprint
#: ``(notebook_id, owner_id, fingerprint, created_at)``, upgrading a
#: ``declined`` row and leaving a ``proposed`` one alone (design §5.1). Shared
#: by ``write_fingerprints`` and
#: ``memory_store.create_suggestions_with_initial_revision``.
PROPOSED_FINGERPRINT_UPSERT_SQL = (
    "INSERT INTO memory_suggest_fingerprints "
    "(notebook_id, owner_id, fingerprint, kind, expires_at, created_at) "
    "VALUES (?, ?, ?, 'proposed', NULL, ?) "
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
    "VALUES (?, ?, ?, 'declined', ?, ?) "
    "ON CONFLICT (notebook_id, owner_id, fingerprint) DO UPDATE SET "
    "expires_at = excluded.expires_at "
    "WHERE memory_suggest_fingerprints.kind = 'declined' "
    "AND julianday(excluded.expires_at) > julianday(memory_suggest_fingerprints.expires_at)"
)


def _ask_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "created_at": str(row["created_at"] or ""),
        "question": str(row["question"] or ""),
        "answer_id": str(row["answer_id"] or ""),
        "conversation_id": str(row["conversation_id"] or ""),
        "mode": str(row["mode"] or ""),
    }


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "notebook_id": str(row["notebook_id"]),
        "owner_id": str(row["owner_id"]),
        "status": str(row["status"]),
        "pending_signal": int(row["pending_signal"]),
        "cursor_created_at": str(row["cursor_created_at"]),
        "cursor_id": str(row["cursor_id"]),
        "cursor_inclusive": bool(row["cursor_inclusive"]),
        "window_started_at": str(row["window_started_at"]),
        "consecutive_failures": int(row["consecutive_failures"]),
        "claim_token": str(row["claim_token"]),
        "runs": int(row["runs"]),
        "candidates_written": int(row["candidates_written"]),
        "last_outcome": str(row["last_outcome"]),
        "failure_reason": str(row["failure_reason"]),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


class MemorySuggestStore:
    """Row persistence for the switch, the single-flight job rows, the
    fingerprints and the bounded sampling reads of auto memory suggestions."""

    def __init__(self, database: SqliteDatabase, *, new_id: Callable[[str], str]) -> None:
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
                "FROM memory_suggest_users WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "owner_id": str(row["owner_id"]),
            "enabled": bool(row["enabled"]),
            "enabled_at": str(row["enabled_at"] or ""),
            "quota_day": str(row["quota_day"]),
            "quota_used": int(row["quota_used"]),
            "updated_at": str(row["updated_at"]),
        }

    def set_memory_mode(self, user_id: str, mode: str, *, now: str | Callable[[], str]) -> None:
        if mode not in MEMORY_MODES:
            raise ValueError("invalid memory_mode")
        enabled = 0 if mode == MEMORY_MODE_MANUAL else 1
        with memory_mode_write(), self.database.write() as db:
            now = now() if callable(now) else now
            if db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                raise KeyError(user_id)
            cursor = db.execute(
                "UPDATE user_profiles SET memory_mode = ?, updated_at = ? WHERE user_id = ?",
                (mode, now, user_id),
            )
            if cursor.rowcount == 0:
                db.execute(
                    "INSERT INTO user_profiles "
                    "(id, user_id, memory_mode, domain_focus, created_at, updated_at) "
                    "VALUES (?, ?, ?, '[]', ?, ?)",
                    (f"profile-{user_id}", user_id, mode, now, now),
                )
            db.execute(
                "INSERT INTO memory_suggest_users (owner_id, enabled, enabled_at, updated_at) "
                "VALUES (?, ?, CASE WHEN ? = 1 THEN ? END, ?) "
                "ON CONFLICT (owner_id) DO UPDATE SET "
                "enabled = excluded.enabled, "
                "enabled_at = CASE WHEN excluded.enabled = 1 AND "
                "(memory_suggest_users.enabled = 0 OR memory_suggest_users.enabled_at IS NULL) "
                "THEN excluded.enabled_at "
                "ELSE memory_suggest_users.enabled_at END, "
                "updated_at = excluded.updated_at",
                (user_id, enabled, enabled, now, now),
            )
            if not enabled:
                db.execute("DELETE FROM memory_suggest_jobs WHERE owner_id = ?", (user_id,))

    # ----------------------------------------------------------- job rows
    def note_signal(
        self, notebook_id: str, owner_id: str, *, ask_job_id: str, now: str
    ) -> int:
        """One statement: create the row at the triggering ask (inclusive) or,
        when it already exists, only count the signal -- and only while the
        owner's switch is on AND the owner can still read this live notebook
        (plan B9: the shared ``read_access_clause`` with the switch row's
        owner as the user, plus ``NOTEBOOK_LIVE_SQL``). Both are checked in the
        same statement; SQLite writers are serialised, so a
        ``set_memory_mode(manual)`` or any access loss (with its job-row
        cleanup) is wholly before or after it. Returns 0 when nothing was
        counted."""
        readable = read_access_clause("nb", "nm", user_ref="u.owner_id")
        floor = ("CASE WHEN sn_utc_us(f.lost_at) > sn_utc_us(u.enabled_at) "
                 "THEN f.lost_at ELSE u.enabled_at END")
        with self.database.write() as db:
            row = db.execute(
                "INSERT INTO memory_suggest_jobs (notebook_id, owner_id, status, "
                "pending_signal, cursor_created_at, cursor_id, cursor_inclusive, "
                "window_started_at, created_at, updated_at) "
                "SELECT ?, u.owner_id, 'idle', 1, "
                f"CASE WHEN sn_utc_us(COALESCE(NULLIF(a.created_at, ''), ?)) < sn_utc_us({floor}) "
                f"THEN {floor} ELSE COALESCE(NULLIF(a.created_at, ''), ?) END, "
                f"CASE WHEN sn_utc_us(a.created_at) >= sn_utc_us({floor}) "
                "THEN a.id ELSE '' END, 1, "
                f"CASE WHEN sn_utc_us(COALESCE(NULLIF(a.created_at, ''), ?)) < sn_utc_us({floor}) "
                f"THEN {floor} ELSE COALESCE(NULLIF(a.created_at, ''), ?) END, ?, ? "
                "FROM memory_suggest_users u LEFT JOIN ask_jobs a ON a.id = ? "
                "AND a.created_by = u.owner_id AND a.notebook_id = ? "
                "LEFT JOIN memory_suggest_read_fences f ON f.owner_id=u.owner_id AND f.notebook_id=? "
                "WHERE u.owner_id = ? AND u.enabled = 1 "
                "AND EXISTS (SELECT 1 FROM notebooks nb WHERE nb.id = ? "
                f"AND nb.{NOTEBOOK_LIVE_SQL} AND {readable}) "
                "ON CONFLICT (notebook_id, owner_id) DO UPDATE SET "
                "pending_signal = memory_suggest_jobs.pending_signal + 1, "
                "updated_at = excluded.updated_at "
                "RETURNING pending_signal",
                (
                    notebook_id, now, now, now, now, now, now,
                    ask_job_id, notebook_id, notebook_id, owner_id, notebook_id,
                ),
            ).fetchone()
        return 0 if row is None else int(row["pending_signal"])

    def claim(
        self, notebook_id: str, owner_id: str, *, claim_token: str, now: str
    ) -> dict[str, Any] | None:
        if not claim_token:
            raise ValueError("claim_token must be non-empty")
        with self.database.write() as db:
            row = db.execute(
                "UPDATE memory_suggest_jobs SET status = ?, claim_token = ?, "
                "started_at = ?, updated_at = ? "
                "WHERE notebook_id = ? AND owner_id = ? AND status = ? "
                f"RETURNING {_JOB_COLUMNS}",
                (
                    JOB_STATUS_RUNNING, claim_token, now, now,
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
            cursor_sql = "cursor_created_at = ?, cursor_id = ?, cursor_inclusive = ?, "
            cursor_params = (str(cursor[0]), str(cursor[1]), 1 if cursor[2] else 0)
        with self.database.write() as db:
            changed = db.execute(
                "UPDATE memory_suggest_jobs SET status = ?, claim_token = '', "
                f"{cursor_sql}"
                "consecutive_failures = ?, last_outcome = ?, failure_reason = ?, "
                "runs = runs + 1, candidates_written = candidates_written + ?, "
                "pending_signal = MAX(pending_signal - ?, 0), "
                "finished_at = ?, updated_at = ? "
                "WHERE notebook_id = ? AND owner_id = ? AND status = ? AND claim_token = ?",
                (
                    JOB_STATUS_IDLE, *cursor_params,
                    int(consecutive_failures), outcome, failure_reason,
                    int(candidates_written), max(0, int(consumed_pending)),
                    now, now, notebook_id, owner_id, JOB_STATUS_RUNNING, claim_token,
                ),
            ).rowcount
        return changed == 1

    def sweep_stale_on_start(self, *, now: str) -> int:
        """Startup recovery: a ``running`` row left by a previous process is a
        run that died. It settles as one ``failed`` run of the same batch
        (see ``FAILURE_REASON_INTERRUPTED``): cursor unchanged, ``runs`` and
        ``consecutive_failures`` + 1, so T7b's give-up rule covers a batch
        that keeps killing the process."""
        with self.database.write() as db:
            return int(db.execute(
                "UPDATE memory_suggest_jobs SET status = ?, claim_token = '', "
                "last_outcome = ?, failure_reason = ?, runs = runs + 1, "
                "consecutive_failures = consecutive_failures + 1, "
                "finished_at = ?, updated_at = ? WHERE status = ?",
                (
                    JOB_STATUS_IDLE, OUTCOME_FAILED, FAILURE_REASON_INTERRUPTED,
                    now, now, JOB_STATUS_RUNNING,
                ),
            ).rowcount)

    def clear_job_rows(self, notebook_id: str, owner_ids: Sequence[str]) -> int:
        """Clear lost-read windows only; preserve surviving/regranted readers."""
        owners = list(dict.fromkeys(str(owner) for owner in owner_ids))
        if not owners:
            return 0
        bound = bind_ids(owners)
        readable = read_access_clause("nb", "nm", user_ref="memory_suggest_jobs.owner_id")
        with self.database.write() as db:
            return int(db.execute(
                "DELETE FROM memory_suggest_jobs WHERE notebook_id = ? "
                f"AND {drive_by('owner_id', bound)} "
                "AND NOT EXISTS (SELECT 1 FROM notebooks nb "
                "WHERE nb.id=memory_suggest_jobs.notebook_id "
                f"AND {readable})",
                (notebook_id, bound.param),
            ).rowcount)

    def reserve_daily_quota(
        self, owner_id: str, *, today_utc: str, cap: int, now: str
    ) -> bool:
        with self.database.write() as db:
            changed = db.execute(
                "UPDATE memory_suggest_users SET "
                "quota_used = CASE WHEN quota_day = ? THEN quota_used + 1 ELSE 1 END, "
                "quota_day = ?, updated_at = ? "
                "WHERE owner_id = ? AND (quota_day <> ? OR quota_used < ?)",
                (today_utc, today_utc, now, owner_id, today_utc, int(cap)),
            ).rowcount
        return changed == 1

    def unadopted_candidate_count(self, owner_id: str, notebook_id: str) -> int:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM memory_items m "
                "WHERE m.created_by = ? AND m.notebook_id = ? AND m.status = 'candidate' "
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
                # SQLite does not turn an expression-key row comparison into
                # an index range. This redundant scalar bound supplies the
                # seek; the tuple still owns exact timestamp/id semantics.
                "AND sn_utc_us(a.created_at) >= sn_utc_us(?) "
                f"AND (sn_utc_us(a.created_at), a.id) {compare} (sn_utc_us(?), ?) "
                "ORDER BY sn_utc_us(a.created_at), a.id LIMIT ?",
                (
                    owner_id, notebook_id, cursor_created_at, cursor_created_at,
                    cursor_id, max(1, int(limit)),
                ),
            ).fetchall()
        return [_ask_row(row) for row in rows]

    def corrected_scan(
        self, owner_id: str, notebook_id: str, *, cursor_created_at: str,
        cursor_id: str, inclusive: bool, window_started_at: str, limit: int,
    ) -> list[dict[str, Any]]:
        """One ascending prefix plus the raw immediately preceding turn.

        Eligibility is checked after finding that predecessor, so a skipped
        MCP/report/foreign turn never joins two nonadjacent turns together.
        Materialize the bounded prefix before correlated predecessor reads;
        the exact-UTC conversation index then seeks each of at most ``limit``
        raw predecessors, rather than walking its conversation's whole history.
        Both turns must follow the owner's last read loss. Check predecessor
        period/eligibility only after finding the raw adjacent turn.
        """
        compare = ">=" if inclusive else ">"
        with self.database.connect() as db:
            rows = db.execute(
                f"WITH prefix AS MATERIALIZED (SELECT {_ASK_COLUMNS} "
                "FROM ask_jobs a JOIN conversations c ON c.id=a.conversation_id "
                "AND c.created_by=? AND c.notebook_id=a.notebook_id "
                f"WHERE {_SAMPLE_WHERE} "
                f"AND {ask_after_read_loss_sql('a')} "
                "AND sn_utc_us(a.created_at) >= sn_utc_us(?) "
                f"AND (sn_utc_us(a.created_at),a.id) {compare} (sn_utc_us(?),?) "
                "ORDER BY sn_utc_us(a.created_at),a.id LIMIT ?) "
                f"SELECT {_ASK_COLUMNS}, p.answer_id AS previous_answer_id "
                "FROM prefix a LEFT JOIN ask_jobs p ON p.id=("
                "SELECT previous.id FROM ask_jobs previous "
                "WHERE previous.conversation_id=a.conversation_id "
                "AND sn_utc_us(previous.created_at) <= sn_utc_us(a.created_at) "
                "AND (sn_utc_us(previous.created_at),previous.id) "
                "<(sn_utc_us(a.created_at),a.id) "
                "ORDER BY sn_utc_us(previous.created_at) DESC,previous.id DESC LIMIT 1) "
                "AND p.created_by=? AND p.notebook_id=? AND p.status='done' "
                "AND p.submitted_via='web' AND p.output='answer' AND p.report_id='' "
                "AND sn_utc_us(p.created_at) >= sn_utc_us(?) "
                f"AND {ask_after_read_loss_sql('p')} "
                "ORDER BY sn_utc_us(a.created_at),a.id",
                (owner_id, owner_id, notebook_id, cursor_created_at, cursor_created_at,
                 cursor_id, max(1, int(limit)), owner_id, notebook_id, window_started_at),
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
                "WHERE a.created_by = ? AND a.notebook_id = ? AND a.status = 'running' "
                "AND a.submitted_via = 'web' AND a.output = 'answer' AND a.report_id = '' "
                f"AND {ask_after_read_loss_sql()} "
                "AND sn_utc_us(a.created_at) >= sn_utc_us(?) "
                f"AND (sn_utc_us(a.created_at), a.id) {compare} (sn_utc_us(?), ?) "
                "ORDER BY sn_utc_us(a.created_at), a.id LIMIT 1",
                (owner_id, notebook_id, cursor_created_at, cursor_created_at, cursor_id),
            ).fetchone()
        return None if row is None else {
            "id": str(row["id"]), "created_at": str(row["created_at"] or ""),
        }

    def repeated_window(
        self, owner_id: str, notebook_id: str, *, lower: str, limit: int
    ) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            rows = db.execute(
                f"SELECT {_ASK_COLUMNS} FROM ask_jobs a WHERE {_SAMPLE_WHERE} "
                f"AND {ask_after_read_loss_sql()} "
                "AND sn_utc_us(a.created_at) >= sn_utc_us(?) "
                "ORDER BY sn_utc_us(a.created_at) DESC, a.id DESC LIMIT ?",
                (owner_id, notebook_id, lower, max(1, int(limit))),
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
            "json_extract(an.payload, '$.citations') AS citations, "
            "json_extract(an.payload, '$.anchors') AS anchors, "
            if with_sources else ""
        )
        with self.database.connect() as db:
            rows = db.execute(
                f"SELECT an.id, an.conversation_id, an.created_at, {sources}"
                "json_extract(an.payload, '$.intent.clarification_answers') AS clarifications, "
                "json_extract(an.payload, '$.evidence_level') AS evidence_level, "
                "substr(COALESCE(json_extract(an.payload, '$.answer'), ''), 1, ?) AS excerpt "
                "FROM answers an JOIN conversations c ON c.id = an.conversation_id "
                f"WHERE c.created_by = ? AND {drive_by('an.id', bound)}",
                (max(1, int(excerpt_chars)), owner_id, bound.param),
            ).fetchall()
        return {
            str(row["id"]): {
                "answer_id": str(row["id"]),
                "conversation_id": str(row["conversation_id"] or ""),
                "created_at": str(row["created_at"] or ""),
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
            rows = db.execute(
                "SELECT f.answer_id, f.rating FROM feedback f "
                "JOIN answers an ON an.id = f.answer_id "
                "JOIN conversations c ON c.id = an.conversation_id "
                f"WHERE c.created_by = ? AND {drive_by('f.answer_id', bound)} "
                "ORDER BY f.answer_id, julianday(f.created_at) DESC, f.id DESC",
                (owner_id, bound.param),
            ).fetchall()
        latest: dict[str, str] = {}
        for row in rows:
            latest.setdefault(str(row["answer_id"]), str(row["rating"]))
        return latest

    def answers_with_memory(
        self, owner_id: str, answer_ids: Sequence[str]
    ) -> set[str]:
        ids = list(dict.fromkeys(str(value) for value in answer_ids if value))
        if not ids:
            return set()
        bound = bind_ids(ids)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT m.source_answer_id FROM memory_items m "
                f"WHERE m.created_by = ? AND {drive_by('m.source_answer_id', bound)}",
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
            rows = db.execute(
                "SELECT fp.fingerprint FROM memory_suggest_fingerprints fp "
                "WHERE fp.notebook_id = ? AND fp.owner_id = ? "
                f"AND {drive_by('fp.fingerprint', bound)} "
                "AND (fp.kind = ? OR julianday(fp.expires_at) > julianday(?))",
                (notebook_id, owner_id, bound.param, FINGERPRINT_PROPOSED, now),
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
        if kind == FINGERPRINT_PROPOSED:
            statement = PROPOSED_FINGERPRINT_UPSERT_SQL
            rows = [(notebook_id, owner_id, value, now) for value in values]
        else:
            statement = DECLINED_FINGERPRINT_UPSERT_SQL
            rows = [(notebook_id, owner_id, value, expires_at, now) for value in values]
        with self.database.write() as db:
            db.executemany(statement, rows)

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
        given status. ``query_vectors`` may be empty (no embedding service):
        lexical only."""
        allowed = checked_statuses(statuses)
        queries = [str(query or "").strip() for query in lexical_queries]
        if query_vectors and len(query_vectors) != len(queries):
            raise ValueError("query_vectors and lexical_queries must align")
        if not allowed or not queries:
            return [[] for _ in queries]
        placeholders = ",".join("?" for _ in allowed)
        limit = max(1, int(k))
        result: list[list[dict[str, Any]]] = []
        with self.database.connect() as db:
            vector_rows: list[tuple[str, str, str, Any]] = []
            if query_vectors:
                vector_rows = [
                    (str(row["id"]), str(row["title"]), str(row["status"]), row["vector"])
                    for row in db.execute(
                        "SELECT m.id, m.title, m.status, me.vector FROM memory_items m "
                        "JOIN memory_embeddings me ON me.memory_id = m.id "
                        "WHERE m.created_by = ? AND m.notebook_id = ? "
                        f"AND m.status IN ({placeholders}) "
                        "ORDER BY m.updated_at DESC, m.id LIMIT ?",
                        (owner_id, notebook_id, *allowed, NEIGHBOR_VECTOR_SCAN_MAX),
                    ).fetchall()
                ]
            for index, query in enumerate(queries):
                found = (
                    cosine_top(query_vectors[index], vector_rows, limit)
                    if query_vectors else []
                )
                if query:
                    for row in db.execute(
                        "SELECT m.id, m.title, m.status FROM memory_items m "
                        "JOIN memory_items_fts f ON f.rowid = m.rowid "
                        "WHERE m.created_by = ? AND m.notebook_id = ? "
                        f"AND m.status IN ({placeholders}) AND memory_items_fts MATCH ? "
                        "ORDER BY bm25(memory_items_fts), m.updated_at DESC LIMIT ?",
                        (
                            owner_id, notebook_id, *allowed,
                            '"' + query.replace('"', '""') + '"', limit,
                        ),
                    ).fetchall():
                        found.append({
                            "id": str(row["id"]), "title": str(row["title"]),
                            "status": str(row["status"]), "side": "lexical", "score": 0.0,
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
        oldest first. Two set statements in one write transaction: the move
        (``UPDATE ... RETURNING``) and the revisions (``INSERT ... SELECT`` over
        the moved ids paired with fresh revision ids)."""
        clauses = [
            "m.suggested_signal IS NOT NULL", "m.status = 'candidate'",
            "julianday(m.created_at) < julianday(?)",
        ]
        params: list[Any] = [cutoff]
        if owner_id is not None:
            clauses.append("m.created_by = ?")
            params.append(owner_id)
        if notebook_id is not None:
            clauses.append("m.notebook_id = ?")
            params.append(notebook_id)
        with self.database.write() as db:
            moved = db.execute(
                "UPDATE memory_items SET status = 'rejected', updated_at = ? "
                "WHERE status = 'candidate' AND id IN ("
                f"SELECT m.id FROM memory_items m WHERE {' AND '.join(clauses)} "
                "ORDER BY julianday(m.created_at), m.created_at, m.id LIMIT ?) "
                "RETURNING id, created_by, notebook_id, suggested_signal, created_at, "
                "julianday(created_at) AS created_jd",
                (now, *params, max(1, int(limit))),
            ).fetchall()
            if moved:
                pairs = json.dumps(
                    [[self.new_id("memrev"), str(row["id"])] for row in moved]
                )
                db.execute(
                    "INSERT INTO memory_revisions (id, memory_id, revision, title, "
                    "content_md, tags_json, status, promotion_state, changed_by, "
                    "change_reason, created_at) "
                    "SELECT json_extract(p.value, '$[0]'), m.id, "
                    "(SELECT COALESCE(MAX(r.revision), 0) + 1 FROM memory_revisions r "
                    "WHERE r.memory_id = m.id), m.title, m.content_md, m.tags_json, "
                    "m.status, m.promotion_state, m.created_by, ?, ? "
                    "FROM json_each(?) p JOIN memory_items m "
                    "ON m.id = json_extract(p.value, '$[1]')",
                    (REVISION_REASON_EXPIRED, now, pairs),
                )
        # The pick's own key (julianday, then text, then id): the rows come
        # back in the order they were chosen, whatever their offset spelling.
        moved = sorted(
            moved,
            key=lambda row: (row["created_jd"], str(row["created_at"]), str(row["id"])),
        )
        return [
            {
                "id": str(row["id"]),
                "owner_id": str(row["created_by"]),
                "notebook_id": str(row["notebook_id"]),
                "suggested_signal": str(row["suggested_signal"]),
            }
            for row in moved
        ]
