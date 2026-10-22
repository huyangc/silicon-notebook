"""Durable global conversations and jobs on the shared database boundary."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.core.capability_tokens import (
    GLOBAL_CONVERSATION_SHARE_PREFIX,
    new_capability_token,
)
# Safety ceiling on how many turns one public page renders; the same name the
# notebook-scoped public read caps its fetch by. Pure leaf module, no cycle.
from app.domain.conversation_public_view import MAX_TURNS
from app.domain.global_ask_attribution import job_attribution
from app.domain.global_ask_retention import (
    auto_title_on_expired_first_turn_sql,
    conversation_live_sql,
    shown_title_sql,
    live_turn_sql,
    participant_bind_count,
    participant_sql,
)
from app.domain.share_counts import ShareRanks, share_prefixes, window_size
from app.models.ask import CONVERSATION_TITLE_MAX_CHARS
from app.models.global_ask import (
    GLOBAL_JOB_READ_TIME_FIELDS,
    GlobalAskJob,
    GlobalConversationSummary,
    global_job_notebook_ids,
)
from app.repositories.global_ask_ports import ReplacedJobUnavailable
from app.repositories.ports import (
    ConversationHasNoShareableAnswer,
    ConversationShareWatermarkStale,
)

# The canonical order of one global conversation's jobs, oldest first, and its
# DESC twin. Every existing read of ``global_ask_jobs`` already sorts by this
# pair, so the share watermark's keyset is written against the SAME tuple and
# cannot drift from the order the public page renders in.
GLOBAL_JOBS_ORDER_ASC = "ORDER BY created_at ASC,id ASC"
GLOBAL_JOBS_ORDER_DESC = "ORDER BY created_at DESC,id DESC"

# "Sorts at or before this job" on the canonical pair: the keyset every
# published prefix is cut by (params: the job's created_at, then its id). A
# row-value comparison -- the same lexicographic order as "created_at < t OR
# (created_at = t AND id <= i)" -- so both backends use it as an INDEX
# condition on ``(conversation_id, created_at, id)``: a window read backwards
# from it starts at the bound instead of walking every newer job first.
_KEYSET_THROUGH = "(created_at, id) <= (?, ?)"

# The three job lookups every share read resolves, written once and spliced
# into each statement that needs them, so the share, the public read and the
# dialog's counts cannot resolve a job differently (``{live}`` is the dialect's
# retention gate, ``live_turn_sql``):
#
# * the BOUNDARY a share pins to: a live done job of this owner's conversation
#   (``_share_boundary``; params id, conversation, user);
# * the NEWEST such job, the boundary when none is asked (params conversation,
#   user);
# * the WATERMARK the public read anchors its keyset on: any done job of the
#   conversation, live or not (``_watermark_anchor``; params id, conversation).
#
# ``{by_conversation}`` pins SQLite to ``idx_global_jobs_conversation`` where a
# statement walks one conversation's jobs: without statistics (production
# never runs ANALYZE) its planner otherwise picks the ``status`` or
# ``user_id`` index and reads every done job of the deployment, or of the
# user, to answer for one conversation. PostgreSQL plans these on its own
# statistics and gets an empty string.
_BOUNDARY_JOB_SQL = (
    "SELECT id, created_at FROM global_ask_jobs "
    "WHERE id=? AND conversation_id=? AND user_id=? AND status='done' AND {live}"
)
_NEWEST_JOB_SQL = (
    "SELECT id, created_at FROM global_ask_jobs{by_conversation} WHERE conversation_id=? "
    "AND user_id=? AND status='done' AND {live} " + GLOBAL_JOBS_ORDER_DESC + " LIMIT 1"
)
_WATERMARK_JOB_SQL = (
    "SELECT id, created_at FROM global_ask_jobs "
    "WHERE id=? AND conversation_id=? AND status='done'"
)

# The share dialog's prefix sizes in ONE aggregate over the owner's live done
# jobs of one conversation (``GlobalAskStore.share_counts``): the boundary job
# the dialog asks about (``a``), the watermark job (``w``), the newest job
# (``n``: the bound every window is read under) -- the three lookups above,
# verbatim -- and per job whether it sorts at/before ``a``, at/before ``w``,
# or at/before the watermark instant (the page's fallback when ``w`` is gone).
# Placeholders, in order: a(id, conversation, user), w(id, conversation),
# n(conversation, user), the watermark instant, then the scan's (conversation,
# user).
_SHARE_RANKS_SQL = (
    "WITH a AS (" + _BOUNDARY_JOB_SQL + "), "
    "w AS (" + _WATERMARK_JOB_SQL + "), "
    "n AS (" + _NEWEST_JOB_SQL + ") "
    "SELECT COUNT(j.id) AS total, "
    "MAX(a.id) AS a_id, MAX(a.created_at) AS a_created, "
    "SUM(CASE WHEN (j.created_at, j.id) <= (a.created_at, a.id) THEN 1 ELSE 0 END) AS a_rank, "
    "MAX(w.id) AS w_id, MAX(w.created_at) AS w_created, "
    "SUM(CASE WHEN (j.created_at, j.id) <= (w.created_at, w.id) THEN 1 ELSE 0 END) AS w_rank, "
    "SUM(CASE WHEN j.created_at <= ? THEN 1 ELSE 0 END) AS t_rank, "
    "MAX(n.id) AS n_id, MAX(n.created_at) AS n_created "
    "FROM global_ask_jobs j{by_conversation} LEFT JOIN a ON 1=1 LEFT JOIN w ON 1=1 LEFT JOIN n ON 1=1 "
    "WHERE j.conversation_id=? AND j.user_id=? AND j.status='done' AND {live_j}"
)


def _transition_now() -> str:
    """The ``updated_at`` stamp of one row transition.

    Same clock and same spelling as the service's ``created_at`` stamp (an
    aware UTC ISO instant), so the two columns of one row compare under the
    same text order and the admin readers cast both the same way.
    """
    return datetime.now(timezone.utc).isoformat()


def _payload_json(job) -> str:
    """The stored payload of ``job``: everything but the read-time facts."""
    return job.model_dump_json(exclude=set(GLOBAL_JOB_READ_TIME_FIELDS))


def _aware_iso(value) -> str:
    """An aware UTC ISO-8601 instant, the spelling every stamp here is written in."""
    if isinstance(value, datetime):
        instant = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return instant.astimezone(timezone.utc).isoformat()
    return str(value)


def _dialect(marker) -> str:
    return "postgres" if marker == "%s" else "sqlite"


# How many ids one purge statement names: far below SQLite's bound-parameter
# ceiling and PostgreSQL's comfortable IN-list size.
_PURGE_BATCH = 500

# The attribution schema (``global_ask_job_notebooks`` + ``attributed``).
ATTRIBUTION_SQLITE_VERSION = 98
ATTRIBUTION_POSTGRES_VERSION = 78
# Attribution rows per INSERT statement (two bound values each).
_ATTRIBUTION_INSERT_BATCH = 500
# Whole-page attempts when PostgreSQL reports a deadlock anyway.
_DEADLOCK_RETRIES = 3

# ``job_scope_rows``' projection: the six notebook-id lists and the four
# anchor/citation arrays ``global_ask._snapshot_notebook_ids`` reads, as
# ``(column alias, answer shape or None for the top level, key)``.
_SCOPE_LISTS = (
    ("r0", None, "resolved_notebook_ids"), ("c0", None, "cited_notebook_ids"),
    ("r1", "answer", "resolved_notebook_ids"), ("c1", "answer", "cited_notebook_ids"),
    ("r2", "response", "resolved_notebook_ids"), ("c2", "response", "cited_notebook_ids"),
)
_SCOPE_ITEMS = (
    ("a1", "answer", "anchors"), ("z1", "answer", "citations"),
    ("a2", "response", "anchors"), ("z2", "response", "citations"),
)
# How many job ids one ``job_scope_rows(job_ids=...)`` statement names: a
# batched key probe (the ids are the primary keys of the rows it reads) kept
# below both backends' batch rule (SQLite <= 900 placeholders, PostgreSQL <=
# 1024 ids; ``docs/development.md``, "Binding id lists in SQL"), the size the
# purge pages by.
_SCOPE_PROBE_BATCH = 500


def _scope_projection_sqlite(document):
    """``(alias, expression)`` per projected path over the JSON text ``document``.

    A list is returned only when the stored value is an array (else NULL); an
    anchor/citation array becomes the array of its elements' ``notebook_id``
    (NULL for an element that is not an object or has none), ``'[]'`` when the
    path is not an array.

    Known difference, unreachable: on an object with a DUPLICATED key SQLite's
    ``json_extract`` reads the FIRST value while Python's ``json.loads`` and
    PostgreSQL's ``jsonb`` keep the LAST, so here (and only here) the narrowed
    lists could name other libraries than the stored job's model does. On the
    list that direction is fail-open. It cannot happen: every writer of
    ``payload_json`` serializes through ``json.dumps`` / ``model_dump_json``
    (and the feedback patch through ``json_patch`` / ``jsonb ||``), none of
    which emits a duplicate key. And a turn is never READ on this projection:
    ``agent_conversation_turn`` re-checks the job it reads through the
    validated model (``global_job_notebook_ids``), which sees the last value.
    """
    def path(shape, key):
        return f"$.{shape}.{key}" if shape else f"$.{key}"

    projection = [
        (alias, f"CASE json_type({document},'{path(shape, key)}') WHEN 'array' "
                f"THEN json_extract({document},'{path(shape, key)}') END")
        for alias, shape, key in _SCOPE_LISTS
    ]
    projection.extend(
        (alias, f"CASE json_type({document},'{path(shape, key)}') WHEN 'array' THEN "
                "(SELECT json_group_array(CASE e.type WHEN 'object' "
                "THEN json_extract(e.value,'$.notebook_id') END) "
                f"FROM json_each({document},'{path(shape, key)}') e) ELSE '[]' END")
        for alias, shape, key in _SCOPE_ITEMS
    )
    return projection


def _scope_projection_postgres(document):
    """The PostgreSQL twin of ``_scope_projection_sqlite`` over the jsonb
    expression ``document`` (already parsed once by the caller)."""
    def node(shape, key):
        return f"{document}->'{shape}'->'{key}'" if shape else f"{document}->'{key}'"

    projection = [
        (alias, f"CASE WHEN jsonb_typeof({node(shape, key)})='array' THEN {node(shape, key)} END")
        for alias, shape, key in _SCOPE_LISTS
    ]
    projection.extend(
        (alias, "(SELECT COALESCE(jsonb_agg(CASE WHEN jsonb_typeof(e)='object' "
                "THEN e->'notebook_id' END),'[]'::jsonb) FROM jsonb_array_elements("
                f"CASE WHEN jsonb_typeof({node(shape, key)})='array' THEN {node(shape, key)} "
                "ELSE '[]'::jsonb END) e)")
        for alias, shape, key in _SCOPE_ITEMS
    )
    return projection


def _projected_list(value):
    """One projected column as a Python list, or ``None``: SQLite hands back
    JSON text, psycopg an already-decoded value."""
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    return value if isinstance(value, list) else None


def _narrowed_scope_payload(row):
    """The narrowed payload ``job_scope_rows`` documents, rebuilt from one row
    of the projection above. Elements are passed on exactly as stored."""
    payload = {}
    shapes = {"answer": {}, "response": {}}
    for alias, shape, key in _SCOPE_LISTS:
        (payload if shape is None else shapes[shape])[key] = _projected_list(row[alias])
    for alias, shape, key in _SCOPE_ITEMS:
        shapes[shape][key] = [
            {"notebook_id": value} for value in (_projected_list(row[alias]) or [])
        ]
    payload.update(shapes)
    return payload


class GlobalAskStore:
    def __init__(self, database, *, marker="?", access_sql=None, read_authority_lock=None):
        self.database = database
        self.marker = marker
        # The backend's read-authority SQL module (``read_access_clause`` /
        # ``read_access_params`` / ``NOTEBOOK_LIVE_SQL``; placeholder styles
        # mirror ``marker``) and, on PostgreSQL, the ``lock_reader_access_on``
        # helper that takes ``FOR SHARE`` on one reader's whole authority chain.
        # Only ``guarded_admin_job_record`` consumes them.
        self._access_sql = access_sql
        self._read_authority_lock = read_authority_lock
        # The retention read gate (``app/domain/global_ask_retention.py``):
        # every read of a job's content skips a turn past its stamp.
        self._live = live_turn_sql(dialect=_dialect(marker))
        self._live_j = live_turn_sql(dialect=_dialect(marker), alias="j")
        self._by_conversation = (
            " INDEXED BY idx_global_jobs_conversation" if _dialect(marker) == "sqlite" else ""
        )
        # ...and a conversation whose every turn did (``conversation_live_sql``).
        self._conversation_live = conversation_live_sql(dialect=_dialect(marker), alias="c")
        # An automatic title naming an expired first turn reads as the
        # earliest live question (``shown_title_sql``), on every surface that
        # shows a title: the list, the conversation, the public page.
        self._shown_title = shown_title_sql(
            dialect=_dialect(marker), conversation="c", title_chars=CONVERSATION_TITLE_MAX_CHARS,
        )
        self._history_projection = (
            "payload_json::jsonb->>'question' AS question, "
            "COALESCE(payload_json::jsonb->'answer'->>'answer', "
            "payload_json::jsonb->'response'->>'answer') AS answer, "
            "payload_json::jsonb->'resolved_notebook_ids' AS notebook_ids"
            if marker == "%s" else
            "json_extract(payload_json,'$.question') AS question, "
            "COALESCE(json_extract(payload_json,'$.answer.answer'), "
            "json_extract(payload_json,'$.response.answer')) AS answer, "
            "json_extract(payload_json,'$.resolved_notebook_ids') AS notebook_ids"
        )

    def _sql(self, text):
        return text.replace("?", self.marker)

    @staticmethod
    def _conversation(row):
        if row is None:
            return None
        try:
            title = row["shown_title"]
        except (KeyError, IndexError):
            title = row["title"]
        return GlobalConversationSummary(
            id=row["id"], title=title, created_at=row["created_at"],
            updated_at=row["updated_at"], notebook_scope=json.loads(row["scope_json"]),
            submitted_via=row["submitted_via"],
        )

    def conversation(self, conversation_id, user_id):
        with self.database.connect() as db:
            row = db.execute(self._sql(
                f"SELECT c.*, {self._shown_title} AS shown_title "
                "FROM global_ask_conversations c WHERE c.id=? AND c.user_id=? "
                f"AND {self._conversation_live}"
            ), (conversation_id, user_id)).fetchone()
        return self._conversation(row)

    def _owner_conversations_sql(self):
        """One owner's live conversations, most recently updated first -- the
        statement behind both the paged list and its unpaged twin, so the two
        cannot disagree on title, liveness or order."""
        return (
            f"SELECT c.*, {self._shown_title} AS shown_title "
            "FROM global_ask_conversations c WHERE c.user_id=? "
            f"AND {self._conversation_live} "
            "ORDER BY c.updated_at DESC,c.id DESC"
        )

    def list_conversations(self, user_id, limit, offset):
        with self.database.connect() as db:
            rows = db.execute(self._sql(
                self._owner_conversations_sql() + " LIMIT ? OFFSET ?"
            ), (user_id, limit, offset)).fetchall()
        return [self._conversation(row) for row in rows]

    def all_conversations(self, user_id):
        """``list_conversations`` without the page: every live conversation of
        ``user_id`` in the same order with the same shown title.

        The Agent history list (``GlobalAskService.agent_conversations``) has
        to judge each conversation on ALL of its turns before it can page, so
        it reads the owner's whole list and pages after filtering.
        """
        with self.database.connect() as db:
            rows = db.execute(self._sql(
                self._owner_conversations_sql()
            ), (user_id,)).fetchall()
        return [self._conversation(row) for row in rows]

    @staticmethod
    def _job(row):
        if row is None:
            return None
        result = GlobalAskJob.model_validate_json(row["payload_json"])
        result.status = row["status"]
        # The submission's idempotency id and the two record-parity instants
        # live in their own columns, not in the payload; a narrower SELECT
        # simply does not carry them.
        for field in (
            "client_request_id", "asked_at", "updated_at", "submitted_via", "retained_until",
        ):
            try:
                setattr(result, field, row[field] or "")
            except (KeyError, IndexError):
                pass
        if result.status == "interrupted":
            result.error = "服务已重启，请重新提交问题。"
        return result

    def job(self, job_id, user_id):
        with self.database.connect() as db:
            row = db.execute(self._sql(
                f"SELECT * FROM global_ask_jobs WHERE id=? AND user_id=? AND {self._live}"
            ), (job_id, user_id)).fetchone()
        return self._job(row)

    def request_job(self, user_id, request_id):
        if not request_id:
            return None
        with self.database.connect() as db:
            row = db.execute(self._sql(
                "SELECT * FROM global_ask_jobs WHERE user_id=? AND client_request_id=?"
            ), (user_id, request_id)).fetchone()
        return None if row is None else (self._job(row), row["request_json"])

    def create(self, job, user_id, request_id, request_json, submitted_via, *,
               new_conversation, replaces_job_id=None):
        """Insert one running job, stamped so that insertion order IS keyset order.

        ⚠ ``job.created_at`` is REWRITTEN here when it would not sort strictly
        after every job this conversation already holds. The public share
        snapshot is a keyset prefix over ``(created_at, id)``, and "a job that
        finishes after the share stays outside it" only holds if a later-inserted
        job can never carry an earlier stamp. It could: the service stamps a job
        before its reasoning preflight (a model call), so a submission that
        stalled there was inserted AFTER a faster one had been submitted,
        answered and shared -- with a timestamp from before that share's
        watermark, which published it without the owner ever re-sharing. The
        one-running-job index does not prevent this (the faster job is already
        done when the slow one is inserted).

        Clamping inside the insert transaction closes it for every writer, the
        service's own re-stamp after preflight included: whatever the caller's
        clock said, this row sorts after its conversation's newest job. With the
        one-running-job index that gives the ordering the snapshot needs -- a job
        still running at share time was inserted after the watermark job, and a
        job inserted later still sorts later.

        ``replaces_job_id`` is "edit and re-send": the stopped job the user is
        re-asking is deleted in THIS transaction, so the conversation never holds
        both the abandoned attempt and its replacement, and never loses the old
        one without gaining the new one. See ``_discard_replaced`` for what may
        be named. A cancelled job is never inside a share snapshot (the public
        projection reads ``done`` jobs only), so deleting it cannot move one.
        """
        if replaces_job_id and new_conversation:
            raise ReplacedJobUnavailable(replaces_job_id)
        # The row's own column is the authority for the idempotency id and ``_job``
        # echoes it on every read; the object handed in (and the payload stored from
        # it) says the same, so what ``create`` was given equals what is read back.
        job.client_request_id = request_id or ""
        with self.database.write() as db:
            if new_conversation:
                db.execute(self._sql(
                    "INSERT INTO global_ask_conversations"
                    "(id,user_id,title,scope_json,submitted_via,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)"
                ), (job.conversation_id, user_id,
                    job.question[:CONVERSATION_TITLE_MAX_CHARS],
                    job.notebook_scope.model_dump_json(), submitted_via,
                    job.created_at, job.created_at))
            else:
                # ⛔ LOCK FIRST, then read the newest stamp. PostgreSQL runs this
                # transaction at READ COMMITTED with no writer serialization: a
                # submission that read ``MAX(created_at)`` and then paused could
                # resume after a sibling had been inserted, answered and shared,
                # and insert itself carrying a stamp from before that watermark.
                # ``share_conversation`` takes this same row lock, so the two
                # serialize on it: whichever goes second sees the other's commit.
                # (SQLite's ``write()`` is a process-level write lock already.)
                row = db.execute(self._sql(
                    "SELECT id FROM global_ask_conversations WHERE id=? AND user_id=?"
                    + self._row_lock
                ), (job.conversation_id, user_id)).fetchone()
                if row is None:
                    raise KeyError(job.conversation_id)
                # Clamp BEFORE the delete: stamps stay monotonic over every job
                # this conversation has ever held, replaced ones included.
                job.created_at = self._after_latest_job(db, job.conversation_id, job.created_at)
                if replaces_job_id:
                    self._discard_replaced(db, job, user_id, replaces_job_id)
            # ``updated_at`` starts equal to ``created_at``: a running job's last
            # transition IS its admission, exactly as ``ask_jobs`` stamps both
            # columns with the same ``now`` on insert.
            job.updated_at = job.created_at
            db.execute(self._sql(
                "INSERT INTO global_ask_jobs"
                "(id,conversation_id,user_id,client_request_id,request_json,status,"
                "payload_json,created_at,submitted_via,asked_at,updated_at,mode) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
            ), (job.job_id, job.conversation_id, user_id, request_id or None,
                request_json, job.status, _payload_json(job), job.created_at,
                submitted_via, job.asked_at, job.updated_at, job.mode))
            db.execute(self._sql(
                "UPDATE global_ask_conversations SET scope_json=?,updated_at=? WHERE id=? AND user_id=?"
            ), (job.notebook_scope.model_dump_json(), job.created_at, job.conversation_id, user_id))

    def _discard_replaced(self, db, job, user_id, replaces_job_id):
        """Delete the stopped job ``job`` re-asks. Caller holds the conversation row lock.

        ⛔ Only the conversation's NEWEST job, and only while ``cancelled``. An
        older turn has later turns that were asked with it on screen; a ``done``
        turn is an answer (and may be inside a share); a ``running`` one still
        has a worker. Anything else raises and the whole insert rolls back.

        A conversation that still carries its automatic title (the first
        question) and is left with no other job takes the new question as its
        title: the history list would otherwise keep naming a question that no
        longer exists anywhere in it. A title the user typed is left alone.
        """
        newest = db.execute(self._sql(
            "SELECT id,status,payload_json FROM global_ask_jobs "
            f"WHERE conversation_id=? AND user_id=? {GLOBAL_JOBS_ORDER_DESC} LIMIT 1"
        ), (job.conversation_id, user_id)).fetchone()
        if newest is None or newest["id"] != replaces_job_id or newest["status"] != "cancelled":
            raise ReplacedJobUnavailable(replaces_job_id)
        old_title = self._job(newest).question[:CONVERSATION_TITLE_MAX_CHARS]
        db.execute(self._sql(
            "DELETE FROM global_ask_jobs WHERE id=? AND user_id=? AND status='cancelled'"
        ), (replaces_job_id, user_id))
        db.execute(self._sql(
            "UPDATE global_ask_conversations SET title=? WHERE id=? AND user_id=? AND title=? "
            "AND NOT EXISTS (SELECT 1 FROM global_ask_jobs WHERE conversation_id=?)"
        ), (job.question[:CONVERSATION_TITLE_MAX_CHARS], job.conversation_id, user_id,
            old_title, job.conversation_id))

    def discard_cancelled(self, job_id, user_id):
        """Drop a job the user stopped before it showed anything. True when dropped.

        "Stopped before any output" means the question went back to the input
        box as if it had never been sent, so nothing of the attempt stays: the
        row goes, and a conversation this question had just opened goes with it
        (the same rule the notebook Ask applies through
        ``cleanup_empty_conversation``). Same gate as ``_discard_replaced`` --
        only the conversation's NEWEST job, only while ``cancelled`` -- and the
        same row lock, so it serializes with a concurrent insert or share.
        """
        with self.database.write() as db:
            row = db.execute(self._sql(
                "SELECT conversation_id FROM global_ask_jobs WHERE id=? AND user_id=?"
            ), (job_id, user_id)).fetchone()
            if row is None:
                return False
            conversation_id = row["conversation_id"]
            locked = db.execute(self._sql(
                "SELECT id FROM global_ask_conversations WHERE id=? AND user_id=?" + self._row_lock
            ), (conversation_id, user_id)).fetchone()
            if locked is None:
                return False
            newest = db.execute(self._sql(
                "SELECT id,status FROM global_ask_jobs "
                f"WHERE conversation_id=? AND user_id=? {GLOBAL_JOBS_ORDER_DESC} LIMIT 1"
            ), (conversation_id, user_id)).fetchone()
            if newest is None or newest["id"] != job_id or newest["status"] != "cancelled":
                return False
            db.execute(self._sql(
                "DELETE FROM global_ask_jobs WHERE id=? AND user_id=? AND status='cancelled'"
            ), (job_id, user_id))
            db.execute(self._sql(
                "DELETE FROM global_ask_conversations WHERE id=? AND user_id=? "
                "AND NOT EXISTS (SELECT 1 FROM global_ask_jobs WHERE conversation_id=?)"
            ), (conversation_id, user_id, conversation_id))
        return True

    def _after_latest_job(self, db, conversation_id, stamp):
        """``stamp``, or one microsecond past the conversation's newest job when
        ``stamp`` would not sort strictly after it.

        The comparison is the same TEXT comparison the keyset makes, so "sorts
        after" here means exactly what the snapshot predicate will later see. An
        unparsable stored stamp is left alone rather than guessed at.
        """
        # PostgreSQL: pin the aggregate to the byte order the Python comparison
        # below (and the share keyset) uses; a linguistic default collation
        # weighs the punctuation in an ISO stamp differently.
        collate = ' COLLATE "C"' if self.marker == "%s" else ""
        row = db.execute(self._sql(
            f"SELECT MAX(created_at{collate}) AS latest FROM global_ask_jobs WHERE conversation_id=?"
        ), (conversation_id,)).fetchone()
        latest = row["latest"] if row is not None else None
        if not latest or str(stamp) > str(latest):
            return stamp
        try:
            bumped = datetime.fromisoformat(str(latest)) + timedelta(microseconds=1)
        except ValueError:
            return stamp
        return bumped.isoformat()

    def save(self, job, user_id, *, error_detail=""):
        """Move a running job to its terminal state.

        ``updated_at`` is stamped here, in the same statement: it is the finish
        instant of the row, the one ``ask_jobs.updated_at`` has always recorded.
        ``error_detail`` is the raw ``ExceptionType: message`` of a failed run
        -- administrator diagnostics only, so it goes into its own column and
        never into ``payload_json`` (the owner-facing body, whose ``error`` stays
        the fixed Chinese sentence). Stamped onto the object too, so the value
        the caller keeps publishing equals what the row now says.
        """
        # Stamped onto the object only when THIS write won: the loser of a
        # cancel/finish race must not carry an instant the row never had.
        stamp = _transition_now()
        payload = _payload_json(job.model_copy(update={"updated_at": stamp}))
        # A ``done`` transition records the job's attribution in the same
        # transaction (``attributed = 1`` plus its ``global_ask_job_notebooks``
        # rows, SQLite v98 / PostgreSQL 0078), so no reader ever sees a done
        # job flagged attributed without its rows. ``done`` is final (only a
        # ``running`` row is updated here), so the rows never go stale. A
        # failed, cancelled or interrupted job gets none.
        done = job.status == "done"
        with self.database.write() as db:
            cursor = db.execute(self._sql(
                "UPDATE global_ask_jobs SET status=?,payload_json=?,updated_at=?,"
                "error_detail=?,attributed=? WHERE id=? AND user_id=? AND status='running'"
            ), (job.status, payload, stamp, error_detail or "", 1 if done else 0, job.job_id, user_id))
            if cursor.rowcount == 1 and done:
                self._insert_attribution(db, job.job_id, job_attribution(job))
        if cursor.rowcount == 1:
            job.updated_at = stamp
        return cursor.rowcount == 1

    def _insert_attribution(self, db, job_id, notebook_ids):
        """One multi-row INSERT of a done job's attributed libraries
        (``job_attribution`` output: already de-duplicated, participant order)."""
        if not notebook_ids:
            return
        values = ",".join("(?,?)" for _ in notebook_ids)
        params = [value for notebook_id in notebook_ids for value in (job_id, notebook_id)]
        db.execute(self._sql(
            f"INSERT INTO global_ask_job_notebooks (job_id,notebook_id) VALUES {values}"
        ), params)

    # ------------------------------------------- attribution backfill
    # The reads/writes of the attribution backfill
    # (``app/services/global_ask_attribution_backfill.py``: the post-readiness
    # background pass and ``scripts/backfill_global_ask_attribution.py``):
    # done jobs written before SQLite v98 / PostgreSQL 0078 carry
    # ``attributed = 0`` and are counted through the payload predicate until
    # this pass gives them rows.

    def attribution_schema_ready(self):
        """Whether the database carries the attribution schema (SQLite v98 /
        PostgreSQL 0078 or later). A process that does not own the schema (the
        backfill CLI composes with ``migrate=False``) checks this instead of
        migrating."""
        with self.database.connect() as db:
            if self.marker == "%s":
                row = db.execute(
                    "SELECT COALESCE(MAX(version), 0) AS v FROM silicon_schema_migrations"
                ).fetchone()
                return int(row["v"]) >= ATTRIBUTION_POSTGRES_VERSION
            row = db.execute("PRAGMA user_version").fetchone()
            return int(row[0]) >= ATTRIBUTION_SQLITE_VERSION

    def unattributed_done_count(self):
        """How many done jobs still wait for their attribution rows."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM global_ask_jobs WHERE status='done' AND attributed=0"
            ).fetchone()
        return int(row["c"])

    def unattributed_done_page(self, after_id, limit):
        """One keyset page (by id) of done jobs with ``attributed = 0``: ``[(id,
        payload_json)]``, read from the partial ``idx_global_ask_jobs_unattributed``
        (the WHERE repeats its own predicate), already in id order. A plain
        read: no row lock is taken here."""
        # SQLite would rather walk the status index and sort; pin the partial
        # index there (PostgreSQL proves the index predicate on its own).
        # ``INDEXED BY`` naming an index that does not exist is a hard error
        # ("no such index"), never a silent table scan -- the schema check
        # above runs first.
        pinned = "" if self.marker == "%s" else " INDEXED BY idx_global_ask_jobs_unattributed"
        with self.database.connect() as db:
            rows = db.execute(self._sql(
                f"SELECT id,payload_json FROM global_ask_jobs{pinned} "
                "WHERE status='done' AND attributed=0 AND id > ? ORDER BY id LIMIT ?"
            ), (after_id, int(limit))).fetchall()
        return [(row["id"], row["payload_json"]) for row in rows]

    def write_backfilled_attributions(self, attributions):
        """Flag the ``(job_id, notebook_ids)`` done jobs attributed and insert
        their rows -- one transaction for the page. Returns how many jobs this
        call attributed; the rest stay at ``attributed = 0`` for a later pass.

        Never waits on a row lock, so it cannot join a lock cycle with a
        serving writer (a notebook deletion's ``stamp_retained_turns`` locks
        the participating jobs in heap order, the opposite of this page's id
        order; #931 was such a cycle). On PostgreSQL the page's rows are
        locked first with ``FOR UPDATE SKIP LOCKED`` in id order: a row
        another transaction holds is skipped, not waited for, and the single
        UPDATE then touches only what this transaction locked. SQLite has one
        writer, so its single guarded UPDATE cannot wait on another row.
        Either way the UPDATE re-checks ``status='done' AND attributed=0``: a
        row the completion path or an earlier pass attributed, or one purged
        meanwhile, is left alone and not counted -- the pass is idempotent.
        A deadlock report (``40P01``) all the same retries the whole page."""
        by_id = {job_id: list(notebook_ids) for job_id, notebook_ids in attributions}
        if not by_id:
            return 0
        ids = sorted(by_id)
        for attempt in range(_DEADLOCK_RETRIES):
            try:
                with self.database.write() as db:
                    updated = self._attribute_page(db, ids)
                    self._insert_attribution_rows(
                        db, [(job_id, nb) for job_id in updated for nb in by_id[job_id]]
                    )
                return len(updated)
            except Exception as exc:  # noqa: BLE001 - only a deadlock is retried
                if getattr(exc, "sqlstate", None) != "40P01" or attempt + 1 >= _DEADLOCK_RETRIES:
                    raise
        return 0  # pragma: no cover - the loop returns or raises

    def _attribute_page(self, db, ids):
        if self.marker == "%s":
            locked = [row["id"] for row in db.execute(
                "SELECT id FROM global_ask_jobs WHERE id = ANY(%s) AND status='done' "
                "AND attributed=0 ORDER BY id FOR UPDATE SKIP LOCKED", (ids,),
            ).fetchall()]
            if not locked:
                return []
            rows = db.execute(
                "UPDATE global_ask_jobs SET attributed=1 WHERE id = ANY(%s) "
                "AND status='done' AND attributed=0 RETURNING id", (locked,),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in ids)
            rows = db.execute(
                f"UPDATE global_ask_jobs SET attributed=1 WHERE id IN ({placeholders}) "
                "AND status='done' AND attributed=0 RETURNING id", tuple(ids),
            ).fetchall()
        return sorted(row["id"] for row in rows)

    def _insert_attribution_rows(self, db, pairs):
        for offset in range(0, len(pairs), _ATTRIBUTION_INSERT_BATCH):
            chunk = pairs[offset:offset + _ATTRIBUTION_INSERT_BATCH]
            values = ",".join("(?,?)" for _ in chunk)
            db.execute(self._sql(
                f"INSERT INTO global_ask_job_notebooks (job_id,notebook_id) VALUES {values}"
            ), [value for pair in chunk for value in pair])

    def save_progress(self, job, user_id):
        """Patch coverage and trace only; never resend the question or overwrite
        a terminal job.

        ``trace`` joins the patch because it is the OTHER thing a poller watches
        while the answer is still being written -- a reasoning run publishes its
        steps as they happen, and a progress save that carried the coverage
        lists but not the steps would leave the trace panel empty until the run
        finished, which is exactly when it stops being useful. It is still a
        bounded, transient field: the finished job clears it because
        ``answer.reasoning_trace`` is the authority.
        """
        patch = job.model_dump(include={
            "searched_notebook_ids", "skipped_notebooks", "degraded_notebook_ids",
            "trace",
        }, mode="json")
        expression = "(payload_json::jsonb || ?::jsonb)::text" if self.marker == "%s" else "json_patch(payload_json, ?)"
        # A progress save is a transition too: ``ask_jobs.updated_at`` moves on
        # every trace append, so the global row's last-touch instant does as well.
        stamp = _transition_now()
        with self.database.write() as db:
            cursor = db.execute(self._sql(
                f"UPDATE global_ask_jobs SET payload_json={expression},updated_at=? "
                "WHERE id=? AND user_id=? AND status='running'"
            ), (json.dumps(patch, ensure_ascii=False), stamp, job.job_id, user_id))
        if cursor.rowcount == 1:
            job.updated_at = stamp
        return cursor.rowcount == 1

    def set_feedback(self, job_id, user_id, rating):
        """First write wins: a job that already carries a non-empty feedback
        keeps it, matching the disabled-once-clicked button in the interface.

        The empty-feedback check rides in the UPDATE's own WHERE clause
        (rather than a read-then-write from Python) so two concurrent
        submissions cannot both "win" -- only one UPDATE can match the row
        while it still has no feedback; the other sees it already gone from
        the WHERE and reports 0 rows, exactly the same as arriving after the
        first request's response landed.

        Returns ``None`` only when the job does not belong to this user or is
        not yet ``done`` -- the two conditions the caller must reject with a
        fresh error. Any other case (written now, or already carrying a prior
        rating) returns the current row.
        """
        # ``feedback_at`` rides in the same patch: the notebook ``feedback`` row
        # records when the rating was given, so the global rating does too.
        patch = json.dumps(
            {"feedback": rating, "feedback_at": _transition_now()}, ensure_ascii=False,
        )
        expression = "(payload_json::jsonb || ?::jsonb)::text" if self.marker == "%s" else "json_patch(payload_json, ?)"
        empty_feedback = (
            "(payload_json::jsonb->>'feedback' IS NULL OR payload_json::jsonb->>'feedback' = '')"
            if self.marker == "%s" else
            "(json_extract(payload_json,'$.feedback') IS NULL OR json_extract(payload_json,'$.feedback') = '')"
        )
        with self.database.write() as db:
            cursor = db.execute(self._sql(
                f"UPDATE global_ask_jobs SET payload_json={expression} "
                f"WHERE id=? AND user_id=? AND status='done' AND {empty_feedback} "
                f"AND {self._live}"
            ), (patch, job_id, user_id))
            if cursor.rowcount != 1:
                eligible = db.execute(self._sql(
                    "SELECT 1 FROM global_ask_jobs WHERE id=? AND user_id=? AND status='done' "
                    f"AND {self._live}"
                ), (job_id, user_id)).fetchone()
                if eligible is None:
                    return None
        return self.job(job_id, user_id)

    def jobs(self, conversation_id, user_id, limit, offset):
        with self.database.connect() as db:
            rows = db.execute(self._sql(
                "SELECT * FROM global_ask_jobs WHERE conversation_id=? AND user_id=? "
                f"AND {self._live} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?"
            ), (conversation_id, user_id, limit, offset)).fetchall()
        return [self._job(row) for row in rows]

    def completed_history(self, conversation_id, user_id, limit):
        """Only successful dialogue turns; avoid constructing answer/citation models."""
        with self.database.connect() as db:
            rows = db.execute(self._sql(
                f"SELECT {self._history_projection} FROM global_ask_jobs WHERE conversation_id=? AND user_id=? "
                f"AND status='done' AND {self._live} ORDER BY created_at DESC,id DESC LIMIT ?"
            ), (conversation_id, user_id, limit)).fetchall()
        turns = []
        for row in rows:
            if row["answer"] is not None:
                ids = row["notebook_ids"]
                turns.append({"question": row["question"], "answer": row["answer"],
                              "notebook_ids": json.loads(ids) if isinstance(ids, str) else ids})
        return turns

    def running_job_ids(self, conversation_id, user_id):
        with self.database.connect() as db:
            rows = db.execute(self._sql(
                "SELECT id FROM global_ask_jobs WHERE conversation_id=? AND user_id=? AND status='running'"
            ), (conversation_id, user_id)).fetchall()
        return [row["id"] for row in rows]

    # ------------------------------------------------------------------
    # Agent history reads (``GlobalAskService.agent_conversations`` /
    # ``agent_conversation_turn``): which libraries every live turn names, and
    # the n-th finished turn.
    # ------------------------------------------------------------------

    @staticmethod
    def _scope_where(user_id, conversation_id):
        if conversation_id is None:
            return "user_id=?", (user_id,)
        return "user_id=? AND conversation_id=?", (user_id, conversation_id)

    def job_scope_heads(self, user_id, conversation_id=None):
        """Every live job of ``user_id`` (optionally of one conversation) without
        its payload: ``{"conversation_id", "job_id", "status", "updated_at"}``,
        ordered by conversation, then the canonical ``(created_at, id)``. The
        jobs ``job_scope_rows`` would return, for a caller that already knows
        the libraries of some of them (a finished job's never change) and only
        projects the rest."""
        where, params = self._scope_where(user_id, conversation_id)
        with self.database.connect() as db:
            rows = db.execute(self._sql(
                "SELECT conversation_id, id, status, updated_at FROM global_ask_jobs "
                f"WHERE {where} AND {self._live} "
                "ORDER BY conversation_id,created_at ASC,id ASC"
            ), params).fetchall()
        return [
            {"conversation_id": row["conversation_id"], "job_id": row["id"],
             "status": row["status"], "updated_at": row["updated_at"] or ""}
            for row in rows
        ]

    def job_scope_rows(self, user_id, conversation_id=None, *, job_ids=None):
        """One row per live job of ``user_id`` (optionally of one conversation):
        ``{"conversation_id", "job_id", "status", "updated_at", "payload"}``,
        ordered by conversation, then the canonical ``(created_at, id)``.

        ``job_ids`` restricts the read to those jobs (still only this owner's
        live ones), read in batched primary-key probes of
        ``_SCOPE_PROBE_BATCH``; the rows then come batch by batch, and the
        caller keys them by ``job_id``. ``status`` and ``updated_at`` are read
        in the same row as ``payload``, so they describe that very payload.

        ``payload`` is the NARROWED input of ``global_ask._snapshot_notebook_ids``
        -- exactly the keys that function reads, nothing else::

            {"resolved_notebook_ids": r0, "cited_notebook_ids": c0,
             "answer":   {"resolved_notebook_ids": r1, "cited_notebook_ids": c1,
                          "anchors": a1, "citations": z1},
             "response": {...the same four keys...}}

        A notebook-id list comes back only when the stored value IS an array
        (elements untouched), otherwise ``None``, so the function's "empty top
        level falls back to the nested shapes" behaves as on the whole payload.
        Each anchor / citation is rebuilt as ``{"notebook_id": x}`` with ``x``
        exactly as stored (``None`` when absent or the element is not an
        object); the function filters those itself, and this store adds no rule
        of its own. ``tests/global_ask_history_store_cases.py`` pins the parity
        against the whole payload on both backends.

        Why not the whole payload: the visibility judgement runs over EVERY live
        turn of the owner on each list call, and the payload carries the answer
        body, its excerpts and the reasoning trace -- none of which decides
        anything here. Only the ten JSON paths cross the boundary.
        """
        where, params = self._scope_where(user_id, conversation_id)
        if job_ids is None:
            batches = [((), "")]
        else:
            ids = list(dict.fromkeys(job_ids))
            batches = [
                (tuple(page), " AND id IN (" + ",".join("?" for _ in page) + ")")
                for page in (ids[start:start + _SCOPE_PROBE_BATCH]
                             for start in range(0, len(ids), _SCOPE_PROBE_BATCH))
            ]
        rows = []
        with self.database.connect() as db:
            for page, probe in batches:
                rows.extend(db.execute(
                    self._sql(self._scope_statement(where + probe)), params + page,
                ).fetchall())
        return [
            {
                "conversation_id": row["conversation_id"],
                "job_id": row["id"],
                "status": row["status"],
                "updated_at": row["updated_at"] or "",
                "payload": _narrowed_scope_payload(row),
            }
            for row in rows
        ]

    def _scope_statement(self, where):
        """``job_scope_rows``' statement over the jobs ``where`` (plus the live
        gate) selects."""
        if self.marker == "%s":
            # The payload is parsed ONCE per row (``payload_json::jsonb AS p``).
            # ``OFFSET 0`` is an optimization fence, nothing else: an offset of
            # zero drops no row, it only keeps the planner from flattening the
            # subquery back into one ``::jsonb`` parse per path read (the same
            # fence ``attribution_sql(document="p")``'s callers use). Measured
            # on 2,000 turns of ~63 KB each: 163 ms with it, 1,356 ms without.
            # No plan pin holds it (such pins flip with the visibility map);
            # this note is what keeps it.
            return (
                "SELECT j.conversation_id, j.id, j.status, j.updated_at, "
                + ", ".join(
                    f"{expression} AS {alias}"
                    for alias, expression in _scope_projection_postgres("j.p")
                )
                + " FROM (SELECT conversation_id, id, status, created_at, updated_at, "
                "payload_json::jsonb AS p FROM global_ask_jobs "
                f"WHERE {where} AND {self._live} OFFSET 0) j "
                "ORDER BY j.conversation_id,j.created_at ASC,j.id ASC"
            )
        return (
            "SELECT conversation_id, id, status, updated_at, "
            + ", ".join(
                f"{expression} AS {alias}"
                for alias, expression in _scope_projection_sqlite("payload_json")
            )
            + f" FROM global_ask_jobs WHERE {where} AND {self._live} "
            "ORDER BY conversation_id,created_at ASC,id ASC"
        )

    def finished_job_at(self, conversation_id, user_id, index):
        """The ``index``-th (0-based, canonical ``(created_at, id)`` order)
        live job of this owner's conversation that is no longer ``running``,
        or ``None`` past the end. A running turn is not a turn yet; once it
        finishes it sorts after every turn already counted (it is the
        conversation's newest job), so earlier indexes do not move. ``index``
        is never negative (the caller guarantees it)."""
        with self.database.connect() as db:
            row = db.execute(self._sql(
                "SELECT * FROM global_ask_jobs WHERE conversation_id=? AND user_id=? "
                f"AND status<>'running' AND {self._live} {GLOBAL_JOBS_ORDER_ASC} "
                "LIMIT 1 OFFSET ?"
            ), (conversation_id, user_id, int(index))).fetchone()
        return self._job(row)

    def rename(self, conversation_id, user_id, title):
        with self.database.write() as db:
            return db.execute(self._sql(
                "UPDATE global_ask_conversations SET title=? WHERE id=? AND user_id=?"
            ), (title, conversation_id, user_id)).rowcount == 1

    def delete(self, conversation_id, user_id):
        with self.database.write() as db:
            return db.execute(self._sql(
                "DELETE FROM global_ask_conversations WHERE id=? AND user_id=?"
            ), (conversation_id, user_id)).rowcount == 1

    def recover(self):
        # The settlement is a transition like any other: ``ask_jobs`` stamps
        # ``updated_at`` when startup recovery marks a row interrupted, so the
        # global row records when it was settled too.
        with self.database.write() as db:
            db.execute(self._sql(
                "UPDATE global_ask_jobs SET status='interrupted',updated_at=? "
                "WHERE status='running'"
            ), (_transition_now(),))

    @contextmanager
    def guarded_admin_job_record(self, job_id, owner_id, *, reader_id):
        """``admin_job_record`` under the activity-detail locking invariant.

        The notebook twin (``guarded_ask_detail``) leases the notebook root
        ``FOR KEY SHARE`` and the reader's authority chain ``FOR SHARE`` and
        keeps both until the API has assembled its response, so a revocation
        or deletion that lands mid-request cannot leave an answer in flight
        that the caller was no longer entitled to. This does the same for a
        global job: one transaction leases every live participant notebook
        (``FOR KEY SHARE``, which conflicts with whole-notebook deletion's
        ``FOR UPDATE``), then reads the job row (``FOR SHARE``), resolves the names the
        OWNER can still read, and -- for the owner's self-service read -- locks
        each participant's authority chain and refuses unless every
        participant is still readable. On SQLite ``database.write()`` is the
        process-level write lock, so the same statements serialize without
        row locks.

        ⛔ LOCK ORDER: notebooks, THEN the job row -- the order the notebook
        delete's finalize transaction takes them in (``notebooks FOR UPDATE``
        first, the retention stamp's ``UPDATE global_ask_jobs`` later). Taking
        the job row first let a finalize that already held the notebook wait on
        this reader's job lock while this reader waited on the notebook: a
        deadlock that failed the detail read or the deletion. The participant
        set is read once without a lock to know which notebooks to lease; it is
        frozen at the job's creation, and the locked re-read below is the one
        the record is built from.

        ``reader_id`` is ``None`` for an administrator, who keeps the audit
        record after the owner lost a participant; otherwise it is the owner.
        """
        share = " FOR SHARE" if self.marker == "%s" else ""
        key_share = " FOR KEY SHARE" if self.marker == "%s" else ""
        access = self._access_sql
        with self.database.write() as db:
            unlocked = db.execute(self._sql(
                f"SELECT * FROM global_ask_jobs WHERE id=? AND user_id=? AND {self._live}"
            ), (job_id, owner_id)).fetchone()
            if unlocked is None:
                yield None
                return
            ids = list(dict.fromkeys(self._job(unlocked).resolved_notebook_ids))
            live = {}
            if ids and access is not None:
                placeholders = ",".join("?" for _ in ids)
                live = {
                    r["id"]: r for r in db.execute(self._sql(
                        f"SELECT nb.id,nb.name,nb.created_by FROM notebooks nb "
                        f"WHERE nb.id IN ({placeholders}) AND nb.{access.NOTEBOOK_LIVE_SQL}"
                        + key_share
                    ), tuple(ids)).fetchall()
                }
            row = db.execute(self._sql(
                f"SELECT * FROM global_ask_jobs WHERE id=? AND user_id=? AND {self._live}" + share
            ), (job_id, owner_id)).fetchone()
            if row is None:
                yield None
                return
            job = self._job(row)
            names = {}
            readable = set()
            if ids and access is not None:
                placeholders = ",".join("?" for _ in ids)
                readable = {
                    r["id"] for r in db.execute(self._sql(
                        f"SELECT nb.id FROM notebooks nb WHERE nb.id IN ({placeholders}) AND "
                        + access.read_access_clause()
                        + f" AND nb.{access.NOTEBOOK_LIVE_SQL}"
                    ), (*ids, *access.read_access_params(owner_id))).fetchall()
                }
                names = {nb_id: live[nb_id]["name"] for nb_id in ids if nb_id in readable}
                if reader_id is not None and self._read_authority_lock is not None:
                    for nb_id in ids:
                        if nb_id in readable and not self._read_authority_lock(
                            db, nb_id, reader_id,
                            notebook_owner_id=live[nb_id]["created_by"],
                        ):
                            readable.discard(nb_id)
            # The owner's retained turn (``app/domain/global_ask_retention.py``):
            # a participant that no longer exists at all no longer takes part
            # in the rule -- the same tolerance ``GlobalAskService._check_turns``
            # applies to the conversation read. A participant that still exists
            # but is unreadable keeps refusing the whole record.
            named = global_job_notebook_ids(job) if job.retained_until else []
            gone = self._missing_on(db, named) if named else set()
            if reader_id is not None and not set(ids) - gone <= readable:
                yield None
                return
            # Read-time, like the conversation read's: which of the libraries
            # this turn names are gone, so the detail pane degrades their
            # citations instead of requesting anything from them.
            job.deleted_notebook_ids = [nb_id for nb_id in named if nb_id in gone]
            yield {
                "job": job,
                "error_detail": row["error_detail"] or "",
                "submitted_via": row["submitted_via"] or "",
                "notebook_names": names,
            }

    def admin_job_record(self, job_id, user_id):
        """One job as the administrator activity detail reads it: the owner's
        job plus the two per-row facts the owner-facing ``GlobalAskJob`` never
        carries -- the raw failure text and the submission surface.

        ``user_id`` is the OWNER (the viewed user), not the reader: the caller
        decides who may read (admin, or the owner under a live read re-check)
        exactly as ``guarded_ask_detail`` does for a notebook job. Returns
        ``None`` when the row does not belong to that owner, so a foreign job
        is indistinguishable from a missing one.
        """
        with self.database.connect() as db:
            row = db.execute(self._sql(
                f"SELECT * FROM global_ask_jobs WHERE id=? AND user_id=? AND {self._live}"
            ), (job_id, user_id)).fetchone()
        if row is None:
            return None
        return {
            "job": self._job(row),
            "error_detail": row["error_detail"] or "",
            "submitted_via": row["submitted_via"] or "",
        }

    # ------------------------------------------------------------------
    # retention after a participant library is deleted
    # (``app/domain/global_ask_retention.py``)
    # ------------------------------------------------------------------

    def _missing_on(self, db, notebook_ids):
        """The ids among ``notebook_ids`` with no ``notebooks`` row at all.

        "No row" rather than "not live": a library still being deleted has its
        row until the deletion's final transaction, and until then it is an
        existing library this reader cannot read, not a deleted one.
        """
        ids = sorted({str(value) for value in notebook_ids if value})
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        present = {
            row["id"] for row in db.execute(self._sql(
                f"SELECT id FROM notebooks WHERE id IN ({placeholders})"
            ), tuple(ids)).fetchall()
        }
        return set(ids) - present

    def missing_notebook_ids(self, notebook_ids):
        """``_missing_on`` in its own read."""
        with self.database.connect() as db:
            return self._missing_on(db, notebook_ids)

    @staticmethod
    def stamp_retained_turns(db, notebook_id, expires_at, *, marker):
        """Stamp every job that ran over ``notebook_id`` with ``expires_at``.

        Runs inside the notebook delete transaction, beside the
        ``retained_user_activity`` projection whose window ``expires_at`` is,
        before the notebook row goes. A turn that already carries an EARLIER
        stamp (an earlier participant deletion) keeps it: a later deletion can
        never extend what an earlier one started. Returns the stamped count.
        """
        dialect = _dialect(marker)
        stamp = _aware_iso(expires_at)
        participant = participant_sql(dialect=dialect, marker=marker)
        binds = (notebook_id,) * participant_bind_count(dialect=dialect)
        earlier = (
            "retained_until::timestamptz <= %s::timestamptz" if dialect == "postgres"
            else "julianday(retained_until) <= julianday(?)"
        )
        cursor = db.execute(
            f"UPDATE global_ask_jobs SET retained_until={marker} WHERE {participant} "
            f"AND NOT (retained_until IS NOT NULL AND {earlier})",
            (stamp, *binds, stamp),
        )
        return cursor.rowcount

    @classmethod
    def purge_expired_turns(cls, db, now, *, marker):
        """Physically delete every turn whose stamp is at or before ``now``.

        The retention twin of ``DELETE FROM retained_user_activity WHERE
        expires_at<=now``, called at the same two points: startup recovery and
        the next notebook deletion (inside its transaction). The read gate
        already hides these rows; this only reclaims them.

        A conversation left with no turn is deleted with them (its share token
        dies with it). A conversation that keeps other turns keeps its user
        title, but an AUTOMATIC title -- still the first question, as
        ``create`` set it -- that names a purged first turn moves on to the
        first remaining question, so the history list does not keep showing a
        question that no longer exists anywhere (the rule ``_discard_replaced``
        applies when it removes a turn). Returns the purged turn count.
        """
        dialect = _dialect(marker)
        expired_sql = (
            "retained_until IS NOT NULL AND retained_until::timestamptz <= %s::timestamptz"
            if dialect == "postgres" else
            "retained_until IS NOT NULL AND julianday(retained_until) <= julianday(?)"
        )
        when = _aware_iso(now)
        rows = db.execute(
            f"SELECT id, conversation_id FROM global_ask_jobs WHERE {expired_sql}", (when,),
        ).fetchall()
        if not rows:
            return 0
        conversations = sorted({row["conversation_id"] for row in rows})
        if dialect == "postgres":
            # The same row lock ``create`` takes before inserting a turn: a
            # submission into one of these conversations either commits first
            # (and the empty-conversation delete below then sees its job) or
            # waits and finds the conversation gone. SQLite's ``write()`` is a
            # process-level write lock already.
            for start in range(0, len(conversations), _PURGE_BATCH):
                page = conversations[start:start + _PURGE_BATCH]
                db.execute(
                    "SELECT id FROM global_ask_conversations WHERE id IN ("
                    + ",".join(marker for _ in page) + ") ORDER BY id FOR UPDATE",
                    tuple(page),
                ).fetchall()
        # ONE set-based UPDATE per page of conversations, inside whichever
        # transaction called the purge (a notebook delete's finalize): an
        # automatic title naming the expiring first turn moves to the earliest
        # remaining question -- the very title readers were already being shown
        # (``shown_title_sql``) -- and a user-typed title is left alone.
        at = "%s::timestamptz" if dialect == "postgres" else "julianday(?)"
        column = (
            (lambda alias: f"{alias}.retained_until::timestamptz") if dialect == "postgres"
            else (lambda alias: f"julianday({alias}.retained_until)")
        )
        condition, replacement = auto_title_on_expired_first_turn_sql(
            dialect=dialect, conversation="global_ask_conversations",
            title_chars=CONVERSATION_TITLE_MAX_CHARS,
            expired=lambda alias: (
                f"({alias}.retained_until IS NOT NULL AND {column(alias)} <= {at})"
            ),
        )
        for start in range(0, len(conversations), _PURGE_BATCH):
            page = conversations[start:start + _PURGE_BATCH]
            db.execute(
                f"UPDATE global_ask_conversations SET title=COALESCE({replacement}, title) "
                "WHERE id IN (" + ",".join(marker for _ in page) + f") AND {condition}",
                (when, *page, when),
            )
        ids = [row["id"] for row in rows]
        for start in range(0, len(ids), _PURGE_BATCH):
            page = ids[start:start + _PURGE_BATCH]
            db.execute(
                "DELETE FROM global_ask_jobs WHERE id IN ("
                + ",".join(marker for _ in page) + ")", tuple(page),
            )
        for start in range(0, len(conversations), _PURGE_BATCH):
            page = conversations[start:start + _PURGE_BATCH]
            db.execute(
                "DELETE FROM global_ask_conversations WHERE id IN ("
                + ",".join(marker for _ in page) + ") AND NOT EXISTS("
                "SELECT 1 FROM global_ask_jobs j WHERE j.conversation_id="
                "global_ask_conversations.id)", tuple(page),
            )
        return len(ids)

    # ------------------------------------------------------------------
    # public conversation sharing -- the global twin of
    # ask_state_store.share_conversation / unshare_conversation /
    # conversation_share_state / public_conversation_by_token. Read those
    # docstrings for the full design rationale; every decision recorded there
    # (token idempotent but watermark advance-only, expected_through_id pinning
    # the disclosed boundary, atomic refusal of a zero-answer conversation,
    # keyset rather than timestamp-interval snapshot) holds here verbatim. The
    # differences are mechanical: the shareable unit is a DONE job rather than
    # an answer row, the keyset tie-break is the job id rather than
    # rowid/ordinal, and ownership is a ``user_id`` column on the row instead
    # of an api-layer ``created_by`` gate -- a global conversation belongs to
    # exactly one user and to no notebook, so every method here scopes by it
    # and a non-owner is indistinguishable from a missing row.
    # ------------------------------------------------------------------

    @property
    def _row_lock(self):
        """``FOR UPDATE`` on PostgreSQL, nothing on SQLite.

        It serializes concurrent shares of the SAME conversation so the
        watermark can only advance: under READ COMMITTED two shares could each
        read the latest done job before either UPDATE, and the stale one would
        clobber a watermark the other already returned live. SQLite needs no
        lock -- ``database.write()`` there is a process-level write lock that
        already serializes these.
        """
        return " FOR UPDATE" if self.marker == "%s" else ""

    def share_conversation(self, conversation_id, user_id, *, expected_through_id=None):
        """Issue (or reuse) this conversation's public token and pin its read
        watermark to one completed job.

        Idempotent on the TOKEN only: re-sharing keeps the existing link so a
        URL already handed out never starts 404ing. The WATERMARK always
        advances -- "share" and "update to latest" are the same call.

        ``expected_through_id`` is a JOB id (the newest completed job the client
        saw in the turns it computed its disclosure from). When it resolves to a
        done job of this conversation the watermark is pinned to exactly that
        job, even if a newer one has since finished. When it does not resolve --
        deleted, still running, failed, cancelled, or another conversation's --
        we raise ``ConversationShareWatermarkStale`` rather than silently
        publishing "latest", which would bypass the user's consent.

        Raises ``KeyError`` when the conversation does not exist OR is not this
        user's, and ``ConversationHasNoShareableAnswer`` when it holds no
        completed job to bound the snapshot -- enforced inside the same write
        transaction, so a never-answered conversation never has a token minted.

        ⚠ THE SNAPSHOT RESTS ON TWO FACTS OWNED ELSEWHERE, and needs both. The
        keyset is over ``(created_at, id)``, so "a job that finishes after this
        share stays outside it" requires that such a job always sorts AFTER the
        watermark job:

        * ``create`` stamps every job to sort strictly after its conversation's
          newest one, inside the insert transaction -- so keyset order is
          insertion order, whatever instant the caller's clock offered (a
          submission that stalled in its reasoning preflight used to be inserted
          late carrying an early stamp);
        * ``idx_global_ask_running`` allows one running job per conversation --
          so a job still running when the share is taken was inserted after the
          watermark job finished being the running one.

        Drop either and an answer nobody reviewed can appear on a published link.
        """
        candidate = new_capability_token(GLOBAL_CONVERSATION_SHARE_PREFIX)
        expected = str(expected_through_id or "").strip()
        with self.database.write() as db:
            conv = db.execute(self._sql(
                "SELECT id, shared_through_id FROM global_ask_conversations "
                "WHERE id=? AND user_id=?" + self._row_lock
            ), (conversation_id, user_id)).fetchone()
            if conv is None:
                raise KeyError(conversation_id)
            through_at, through_id = self._share_boundary(
                db, conversation_id, user_id, expected
            )
            if through_id is None:
                raise ConversationHasNoShareableAnswer(conversation_id)
            self._refuse_regression(
                db, conversation_id, through_id, conv["shared_through_id"], expected
            )
            issued = db.execute(self._sql(
                "UPDATE global_ask_conversations "
                "SET share_token=COALESCE(share_token,?), shared_through_at=?, "
                "shared_through_id=? WHERE id=? AND user_id=? "
                "RETURNING share_token, shared_through_at, shared_through_id"
            ), (candidate, through_at, through_id, conversation_id, user_id)).fetchone()
        return self._share_state(issued)

    def _refuse_regression(self, db, conversation_id, through_id, current_id, expected):
        """Advance-only: raise ``ConversationShareWatermarkStale`` when the
        boundary sorts BEFORE the published watermark ``current_id``, evaluated
        in SQL over both job rows with the SAME (created_at, id) keyset the
        public snapshot uses. An equal boundary is an idempotent no-op; a
        current boundary whose job was deleted resolves nothing here and
        advances, matching the public read's deleted-watermark fallback. One
        check for the share and its read-only window, so both refuse alike."""
        if not current_id or current_id == through_id:
            return
        regresses = db.execute(self._sql(
            "SELECT 1 FROM global_ask_jobs r, global_ask_jobs c "
            "WHERE r.id=? AND c.id=? AND r.conversation_id=? "
            "AND c.conversation_id=? AND (r.created_at < c.created_at "
            "OR (r.created_at = c.created_at AND r.id < c.id))"
        ), (through_id, current_id, conversation_id, conversation_id)).fetchone()
        if regresses is not None:
            raise ConversationShareWatermarkStale(expected or through_id)

    def _share_boundary(self, db, conversation_id, user_id, expected):
        """Resolve ``(created_at, id)`` of the job the watermark pins to.

        Only a ``done`` job can bound a snapshot: a running/failed/cancelled one
        has nothing publishable, so it must not resolve even when its id is
        passed explicitly.
        """
        if expected:
            boundary = db.execute(self._sql(_BOUNDARY_JOB_SQL.format(live=self._live)), (
                expected, conversation_id, user_id)).fetchone()
            if boundary is None:
                raise ConversationShareWatermarkStale(expected)
            return boundary["created_at"], boundary["id"]
        latest = db.execute(self._sql(_NEWEST_JOB_SQL.format(
            live=self._live, by_conversation=self._by_conversation)), (
            conversation_id, user_id)).fetchone()
        if latest is None:
            return None, None
        return latest["created_at"], latest["id"]

    @staticmethod
    def _share_state(row):
        return {
            "share_token": str(row["share_token"] or ""),
            "shared_through_at": str(row["shared_through_at"] or ""),
            "shared_through_id": str(row["shared_through_id"] or ""),
        }

    def unshare_conversation(self, conversation_id, user_id):
        """Revoke the public link; idempotent. The next public request 404s,
        same as an unknown token. A later share mints a NEW token rather than
        resurrecting the revoked one (the COALESCE above sees NULL)."""
        with self.database.write() as db:
            db.execute(self._sql(
                "UPDATE global_ask_conversations SET share_token=NULL, "
                "shared_through_at=NULL, shared_through_id=NULL "
                "WHERE id=? AND user_id=?"
            ), (conversation_id, user_id))

    def conversation_share_state(self, conversation_id, user_id):
        """The issued token + watermark, for the owner's read-back only.

        Never fold this into ``conversation``'s projection: that one is the
        ordinary session read, and ``share_token`` is an anonymous access grant.
        Raises ``KeyError`` when the conversation is missing or not this user's.
        """
        with self.database.connect() as db:
            row = db.execute(self._sql(
                "SELECT share_token, shared_through_at, shared_through_id "
                "FROM global_ask_conversations WHERE id=? AND user_id=?"
            ), (conversation_id, user_id)).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return self._share_state(row)

    def public_conversation_by_token(self, token):
        """Resolve one shared global conversation by token alone -- the only
        session-free read here, so it takes nothing but the token (any other
        identifier would let it run as whichever user the ambient context
        happens to default to).

        Returns ``None`` for an unknown/revoked token, and also for a row whose
        watermark is NULL: ``share_conversation`` always writes both together,
        so NULL means the row was never shared through the normal path -- fail
        closed rather than serve an ungated conversation.

        Jobs are bounded to a clean prefix of the canonical ``(created_at, id)``
        order, as a KEYSET on the watermark job's own tuple rather than a
        ``created_at <=`` interval: two jobs can share an instant, and the
        interval would also pull in the one that sorts AFTER the watermark. Only
        ``done`` jobs are eligible, so a running/failed/cancelled turn is
        excluded by construction. When ``shared_through_id`` no longer resolves
        (that job was deleted after the share) the keyset has no anchor and we
        fall back to the plain interval -- slightly less precise on a
        same-instant tie, but an already-shared link must not fail closed.

        Of that prefix only its newest ``MAX_TURNS`` jobs come back (the page's
        ``public_window``, oldest first), so the shared turn is always on the
        page; ``earlier_turns`` is how many older ones the prefix holds beyond
        them, counted only when there are any.

        ``user_id`` comes back as the sharer's identity for the caller's live
        authorization re-check; it is NOT part of any public disclosure surface
        and must be dropped before anything crosses to an anonymous reader. Job
        payloads are handed back exactly as stored -- the public whitelist
        projection belongs to the service layer, and re-validating through a
        model here would quietly drop the legacy ``response``-shaped payloads.
        """
        clean = str(token or "").strip()
        if not clean:
            return None
        with self.database.connect() as db:
            conv = db.execute(self._sql(
                f"SELECT c.id, c.user_id, {self._shown_title} AS title, c.created_at, c.shared_through_at, "
                "c.shared_through_id FROM global_ask_conversations c WHERE c.share_token=? "
                f"AND {self._conversation_live}"
            ), (clean,)).fetchone()
            if conv is None or not conv["shared_through_at"]:
                return None
            rows, earlier = self._public_jobs(db, conv)
        return {
            "id": conv["id"],
            "user_id": conv["user_id"],
            "title": conv["title"] or "",
            "created_at": conv["created_at"],
            "shared_through_at": conv["shared_through_at"],
            "shared_through_id": conv["shared_through_id"] or "",
            "jobs": self._window_jobs(rows),
            "earlier_turns": earlier,
        }

    def public_job(self, job_id, user_id):
        """One finished MCP job of this owner, in the shape of a
        ``public_conversation_by_token`` ``jobs[]`` element, or ``None``.

        Only ``status='done'`` and ``submitted_via='mcp'`` rows qualify: a
        browser-asked, running, failed or cancelled job is not something an
        answer link may publish, and a foreign owner's id resolves to nothing
        (``user_id`` is part of the predicate, never a post-filter). The payload
        crosses exactly as stored, like the share read.
        """
        clean = str(job_id or "").strip()
        owner = str(user_id or "").strip()
        if not clean or not owner:
            return None
        with self.database.connect() as db:
            row = db.execute(self._sql(
                "SELECT id, payload_json, created_at FROM global_ask_jobs "
                f"WHERE id=? AND user_id=? AND status='done' AND submitted_via='mcp' AND {self._live}"
            ), (clean, owner)).fetchone()
        if row is None:
            return None
        return {
            "job_id": row["id"],
            "payload": self._payload(row["payload_json"]),
            "created_at": row["created_at"],
        }

    def share_window(self, conversation_id, user_id, *, expected_through_id=None):
        """The done jobs a share pinned at ``expected_through_id`` would
        publish, read only: ``{"through_id": <boundary job id>, "jobs": [...]}``
        with the jobs in the shape ``public_conversation_by_token`` returns.

        The boundary resolves exactly as ``share_conversation`` resolves it
        (``_share_boundary``: that done job, or the newest done job when empty)
        and the jobs are the same keyset prefix ``_public_jobs`` serves for a
        watermark on that job -- the snapshot the share would publish, so the
        disclosure is counted over exactly it (M4).  Refuses as the share does:
        ``KeyError`` for a missing or foreign conversation,
        ``ConversationShareWatermarkStale`` for a boundary that does not
        resolve or that sorts before the published watermark
        (``_refuse_regression``), ``ConversationHasNoShareableAnswer`` without
        a done job.

        Like the public read it carries the page's window (``jobs``, the
        newest ``MAX_TURNS`` of the prefix) and ``earlier_turns``.
        """
        expected = str(expected_through_id or "").strip()
        with self.database.connect() as db:
            conv = db.execute(self._sql(
                "SELECT id, shared_through_id FROM global_ask_conversations "
                "WHERE id=? AND user_id=?"
            ), (conversation_id, user_id)).fetchone()
            if conv is None:
                raise KeyError(conversation_id)
            through_at, through_id = self._share_boundary(
                db, conversation_id, user_id, expected
            )
            if through_id is None:
                raise ConversationHasNoShareableAnswer(conversation_id)
            self._refuse_regression(
                db, conversation_id, through_id, conv["shared_through_id"], expected
            )
            rows, earlier = self._jobs_through(
                db, conversation_id, {"id": through_id, "created_at": through_at}
            )
        return {
            "through_id": through_id,
            "jobs": self._window_jobs(rows),
            "earlier_turns": earlier,
        }

    def _window_jobs(self, rows):
        return [
            {
                "job_id": row["id"],
                "payload": self._payload(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _watermark_anchor(self, db, conversation_id, watermark_id):
        """The watermark job's ``(id, created_at)`` -- any done job of the
        conversation, live or not, exactly as the public read anchors its
        keyset -- or ``None``."""
        if not watermark_id:
            return None
        return db.execute(self._sql(_WATERMARK_JOB_SQL), (watermark_id, conversation_id)).fetchone()

    def _public_jobs(self, db, conv):
        """The page's window of the watermark-bounded prefix of this
        conversation's done jobs, and how many older ones it leaves out
        (``_prefix_window``). A watermark job that no longer resolves falls
        back to the ``created_at <=`` interval."""
        watermark = self._watermark_anchor(db, conv["id"], conv["shared_through_id"])
        if watermark is not None:
            return self._jobs_through(db, conv["id"], watermark)
        return self._prefix_window(
            db, conv["id"], "created_at <= ?", (conv["shared_through_at"],)
        )

    def _jobs_through(self, db, conversation_id, boundary):
        """The page's window of the done jobs up to and including ``boundary``
        (a job row's ``id`` and ``created_at``) in the canonical ``(created_at,
        id)`` order, as a keyset -- the one statement behind both the published
        snapshot and the share window."""
        return self._prefix_window(
            db, conversation_id, _KEYSET_THROUGH,
            (boundary["created_at"], boundary["id"]),
        )

    def _prefix_window(self, db, conversation_id, bound_sql, bound_params, *, size=None):
        """``(rows, earlier)`` for one prefix of the conversation's live done
        jobs (``bound_sql`` bounds it): its newest ``MAX_TURNS`` rows, oldest
        first (``public_window``), and how many older ones the prefix holds.

        The rows come off the ``(conversation_id, created_at, id)`` index read
        BACKWARDS from the bound, ``LIMIT MAX_TURNS + 1``: a pathological
        conversation costs one window per anonymous page read, never every
        payload it ever produced. The extra row only says whether older jobs
        exist; then, and only then, one ``COUNT`` over the same prefix gives
        their number -- an ordinary conversation reads exactly one statement,
        as before. ``size``: the prefix's length when the caller already
        counted it (the dialog's aggregate); then the read is ``LIMIT`` the
        window and no ``COUNT`` runs.
        """
        where = (
            "FROM global_ask_jobs WHERE conversation_id=? AND status='done' "
            f"AND {self._live} AND ({bound_sql})"
        )
        params = (conversation_id, *bound_params)
        rows = db.execute(self._sql(
            f"SELECT id, payload_json, created_at {where} "
            + GLOBAL_JOBS_ORDER_DESC + " LIMIT ?"
        ), (*params, MAX_TURNS + 1 if size is None else window_size(size))).fetchall()
        if size is not None:
            return list(reversed(rows)), max(0, size - len(rows))
        earlier = 0
        if len(rows) > MAX_TURNS:
            total = db.execute(self._sql(f"SELECT COUNT(*) AS n {where}"), params).fetchone()
            earlier = max(0, int(total["n"]) - MAX_TURNS)
            rows = rows[:MAX_TURNS]
        return list(reversed(rows)), earlier

    def share_counts(self, conversation_id, user_id, *, through_id="",
                     watermark_id=None, watermark_at=None):
        """The facts behind the share dialog's numbers, read only: the prefix
        sizes of ``app.domain.share_counts.ShareRanks`` from ONE aggregate over
        this owner's conversation, then the page's window of each prefix the
        dialog describes (at most two).

        ``through_id`` is the boundary the dialog asks about (empty = the newest
        completed job); ``watermark_id``/``watermark_at`` are the link's
        watermark as the dialog holds it (empty = not shared; ``None`` = the
        watermark stored now, read with the owner gate in one statement). The watermark
        resolves exactly as the public read resolves it (``_watermark_anchor``:
        a keyset on that job, else the ``created_at <=`` interval), so every
        prefix here is one the page itself could serve. All of them are
        bounded by the newest completed job the aggregate saw, so a job
        finishing in between cannot slip into a window the sizes did not count.

        Returns ``{"ranks": ShareRanks, "through_id": str, "windows": {size:
        [jobs]}, "watermark_id": str, "watermark_at": str}`` (the watermark
        counted against); ``KeyError`` for a missing or foreign conversation
        (or one whose every turn is past its stamp, as the owner read says);
        ``ConversationShareWatermarkStale`` when the conversation changed
        between the aggregate and a window read (the dialog then says it could
        not count).
        """
        asked = str(through_id or "").strip()
        with self.database.connect() as db:
            conv = db.execute(self._sql(
                "SELECT c.share_token, c.shared_through_id, c.shared_through_at "
                "FROM global_ask_conversations c WHERE c.id=? AND c.user_id=? "
                f"AND {self._conversation_live}"
            ), (conversation_id, user_id)).fetchone()
            if conv is None:
                raise KeyError(conversation_id)
            if watermark_id is None:
                state = self._share_state(conv)
                shared = bool(state["share_token"])
                watermark_id = state["shared_through_id"] if shared else ""
                watermark_at = state["shared_through_at"] if shared else ""
            mark_id = str(watermark_id or "").strip()
            mark_at = str(watermark_at or "")
            facts = db.execute(self._sql(
                _SHARE_RANKS_SQL.format(live=self._live, live_j=self._live_j,
                                        by_conversation=self._by_conversation)
            ), (asked, conversation_id, user_id, mark_id, conversation_id,
                conversation_id, user_id, mark_at,
                conversation_id, user_id)).fetchone()
            ranks, bounds = self._share_ranks(facts, asked, mark_id, mark_at)
            prefixes = share_prefixes(ranks)
            windows = {}
            if prefixes is not None:
                # The longer prefix is read first; when its whole window fits
                # the page (<= MAX_TURNS) the shorter one's window is its head,
                # so it is sliced out instead of read and decoded again.
                for size in sorted({prefixes.current, prefixes.scope}, reverse=True):
                    longest = max(windows, default=None)
                    if longest is not None and longest <= MAX_TURNS:
                        windows[size] = windows[longest][:size]
                        continue
                    bound_sql, bound_params = bounds[size]
                    rows, _earlier = self._prefix_window(
                        db, conversation_id, bound_sql, bound_params, size=size
                    )
                    if len(rows) != window_size(size):
                        raise ConversationShareWatermarkStale(asked or mark_id)
                    windows[size] = self._window_jobs(rows)
        newest = facts["n_id"] or ""
        boundary_id = asked if asked else newest
        return {
            "ranks": ranks,
            "through_id": boundary_id if ranks.boundary is not None else "",
            "windows": windows,
            "watermark_id": mark_id,
            "watermark_at": mark_at,
        }

    @staticmethod
    def _share_ranks(facts, asked, mark_id, mark_at):
        """``ShareRanks`` off the aggregate row, and for every prefix size the
        bound that selects exactly that prefix (``{size: (sql, params)}``).
        Every bound is ANDed with the newest job the aggregate counted, so two
        prefixes of one size are the same prefix."""
        newest = (facts["n_created"], facts["n_id"])
        total = int(facts["total"] or 0)

        def through(anchor_sql, anchor_params):
            return (f"({_KEYSET_THROUGH}) AND ({anchor_sql})", (*newest, *anchor_params))

        bounds = {total: (_KEYSET_THROUGH, newest)}
        boundary = total
        if asked:
            boundary = None
            if facts["a_id"] is not None:
                boundary = int(facts["a_rank"] or 0)
                bounds.setdefault(boundary, through(
                    _KEYSET_THROUGH, (facts["a_created"], facts["a_id"]),
                ))
        shared = mark = None
        if mark_id and facts["w_id"] is not None:
            shared = mark = int(facts["w_rank"] or 0)
            bounds.setdefault(shared, through(
                _KEYSET_THROUGH, (facts["w_created"], facts["w_id"]),
            ))
        elif mark_id:
            # The watermark job is gone: the page falls back to the instant.
            shared = int(facts["t_rank"] or 0)
            bounds.setdefault(shared, through("created_at <= ?", (mark_at,)))
        return ShareRanks(
            total=total, boundary=boundary, bounded=bool(asked),
            shared=shared, mark=mark,
        ), bounds

    @staticmethod
    def _payload(raw):
        try:
            return json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
