"""Durable global conversations and jobs on the shared database boundary."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.core.capability_tokens import (
    GLOBAL_CONVERSATION_SHARE_PREFIX,
    new_capability_token,
)
# Safety ceiling on how many turns one public page renders; the same name the
# notebook-scoped public read caps its fetch by. Pure leaf module, no cycle.
from app.domain.conversation_public_view import MAX_TURNS
from app.models.ask import CONVERSATION_TITLE_MAX_CHARS
from app.models.global_ask import GlobalAskJob, GlobalConversationSummary
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


class GlobalAskStore:
    def __init__(self, database, *, marker="?"):
        self.database = database
        self.marker = marker
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
        return GlobalConversationSummary(
            id=row["id"], title=row["title"], created_at=row["created_at"],
            updated_at=row["updated_at"], notebook_scope=json.loads(row["scope_json"]),
            submitted_via=row["submitted_via"],
        )

    def conversation(self, conversation_id, user_id):
        with self.database.connect() as db:
            row = db.execute(self._sql(
                "SELECT * FROM global_ask_conversations WHERE id=? AND user_id=?"
            ), (conversation_id, user_id)).fetchone()
        return self._conversation(row)

    def list_conversations(self, user_id, limit, offset):
        with self.database.connect() as db:
            rows = db.execute(self._sql(
                "SELECT * FROM global_ask_conversations WHERE user_id=? "
                "ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?"
            ), (user_id, limit, offset)).fetchall()
        return [self._conversation(row) for row in rows]

    @staticmethod
    def _job(row):
        if row is None:
            return None
        result = GlobalAskJob.model_validate_json(row["payload_json"])
        result.status = row["status"]
        if result.status == "interrupted":
            result.error = "服务已重启，请重新提交问题。"
        return result

    def job(self, job_id, user_id):
        with self.database.connect() as db:
            row = db.execute(self._sql(
                "SELECT * FROM global_ask_jobs WHERE id=? AND user_id=?"
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
            db.execute(self._sql(
                "INSERT INTO global_ask_jobs"
                "(id,conversation_id,user_id,client_request_id,request_json,status,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)"
            ), (job.job_id, job.conversation_id, user_id, request_id or None,
                request_json, job.status, job.model_dump_json(), job.created_at))
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

    def save(self, job, user_id):
        with self.database.write() as db:
            cursor = db.execute(self._sql(
                "UPDATE global_ask_jobs SET status=?,payload_json=? "
                "WHERE id=? AND user_id=? AND status='running'"
            ), (job.status, job.model_dump_json(), job.job_id, user_id))
        return cursor.rowcount == 1

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
        with self.database.write() as db:
            cursor = db.execute(self._sql(
                f"UPDATE global_ask_jobs SET payload_json={expression} "
                "WHERE id=? AND user_id=? AND status='running'"
            ), (json.dumps(patch, ensure_ascii=False), job.job_id, user_id))
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
        patch = json.dumps({"feedback": rating}, ensure_ascii=False)
        expression = "(payload_json::jsonb || ?::jsonb)::text" if self.marker == "%s" else "json_patch(payload_json, ?)"
        empty_feedback = (
            "(payload_json::jsonb->>'feedback' IS NULL OR payload_json::jsonb->>'feedback' = '')"
            if self.marker == "%s" else
            "(json_extract(payload_json,'$.feedback') IS NULL OR json_extract(payload_json,'$.feedback') = '')"
        )
        with self.database.write() as db:
            cursor = db.execute(self._sql(
                f"UPDATE global_ask_jobs SET payload_json={expression} "
                f"WHERE id=? AND user_id=? AND status='done' AND {empty_feedback}"
            ), (patch, job_id, user_id))
            if cursor.rowcount != 1:
                eligible = db.execute(self._sql(
                    "SELECT 1 FROM global_ask_jobs WHERE id=? AND user_id=? AND status='done'"
                ), (job_id, user_id)).fetchone()
                if eligible is None:
                    return None
        return self.job(job_id, user_id)

    def jobs(self, conversation_id, user_id, limit, offset):
        with self.database.connect() as db:
            rows = db.execute(self._sql(
                "SELECT * FROM global_ask_jobs WHERE conversation_id=? AND user_id=? "
                "ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?"
            ), (conversation_id, user_id, limit, offset)).fetchall()
        return [self._job(row) for row in rows]

    def completed_history(self, conversation_id, user_id, limit):
        """Only successful dialogue turns; avoid constructing answer/citation models."""
        with self.database.connect() as db:
            rows = db.execute(self._sql(
                f"SELECT {self._history_projection} FROM global_ask_jobs WHERE conversation_id=? AND user_id=? "
                "AND status='done' ORDER BY created_at DESC,id DESC LIMIT ?"
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
        with self.database.write() as db:
            db.execute("UPDATE global_ask_jobs SET status='interrupted' WHERE status='running'")

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
            current_id = conv["shared_through_id"]
            if current_id and current_id != through_id:
                # Advance-only: reject a request whose boundary sorts BEFORE the
                # published one, evaluated in SQL over both job rows with the
                # SAME (created_at, id) keyset the public snapshot uses. An equal
                # boundary short-circuits above as an idempotent no-op; a current
                # boundary whose job was deleted resolves nothing here and
                # advances, matching the public read's deleted-watermark
                # fallback.
                regresses = db.execute(self._sql(
                    "SELECT 1 FROM global_ask_jobs r, global_ask_jobs c "
                    "WHERE r.id=? AND c.id=? AND r.conversation_id=? "
                    "AND c.conversation_id=? AND (r.created_at < c.created_at "
                    "OR (r.created_at = c.created_at AND r.id < c.id))"
                ), (through_id, current_id, conversation_id, conversation_id)).fetchone()
                if regresses is not None:
                    raise ConversationShareWatermarkStale(expected or through_id)
            issued = db.execute(self._sql(
                "UPDATE global_ask_conversations "
                "SET share_token=COALESCE(share_token,?), shared_through_at=?, "
                "shared_through_id=? WHERE id=? AND user_id=? "
                "RETURNING share_token, shared_through_at, shared_through_id"
            ), (candidate, through_at, through_id, conversation_id, user_id)).fetchone()
        return self._share_state(issued)

    def _share_boundary(self, db, conversation_id, user_id, expected):
        """Resolve ``(created_at, id)`` of the job the watermark pins to.

        Only a ``done`` job can bound a snapshot: a running/failed/cancelled one
        has nothing publishable, so it must not resolve even when its id is
        passed explicitly.
        """
        if expected:
            boundary = db.execute(self._sql(
                "SELECT id, created_at FROM global_ask_jobs "
                "WHERE id=? AND conversation_id=? AND user_id=? AND status='done'"
            ), (expected, conversation_id, user_id)).fetchone()
            if boundary is None:
                raise ConversationShareWatermarkStale(expected)
            return boundary["created_at"], boundary["id"]
        latest = db.execute(self._sql(
            "SELECT id, created_at FROM global_ask_jobs WHERE conversation_id=? "
            "AND user_id=? AND status='done' " + GLOBAL_JOBS_ORDER_DESC + " LIMIT 1"
        ), (conversation_id, user_id)).fetchone()
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
                "SELECT id, user_id, title, created_at, shared_through_at, "
                "shared_through_id FROM global_ask_conversations WHERE share_token=?"
            ), (clean,)).fetchone()
            if conv is None or not conv["shared_through_at"]:
                return None
            rows = self._public_jobs(db, conv)
        return {
            "id": conv["id"],
            "user_id": conv["user_id"],
            "title": conv["title"] or "",
            "created_at": conv["created_at"],
            "shared_through_at": conv["shared_through_at"],
            "shared_through_id": conv["shared_through_id"] or "",
            "jobs": [
                {
                    "job_id": row["id"],
                    "payload": self._payload(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    def _public_jobs(self, db, conv):
        """The watermark-bounded prefix of this conversation's done jobs.

        ``LIMIT MAX_TURNS + 1`` (cap + 1) is applied AFTER the keyset predicate
        and the canonical ORDER BY: it bounds the fetch to exactly what the
        projection renders plus the one extra row it needs to disclose
        truncation. Without it a pathological conversation makes every anonymous
        page read deserialize every payload it ever produced.
        """
        watermark = None
        if conv["shared_through_id"]:
            watermark = db.execute(self._sql(
                "SELECT id, created_at FROM global_ask_jobs "
                "WHERE id=? AND conversation_id=? AND status='done'"
            ), (conv["shared_through_id"], conv["id"])).fetchone()
        if watermark is not None:
            return db.execute(self._sql(
                "SELECT id, payload_json, created_at FROM global_ask_jobs "
                "WHERE conversation_id=? AND status='done' AND ("
                "created_at < ? OR (created_at = ? AND id <= ?)) "
                + GLOBAL_JOBS_ORDER_ASC + " LIMIT ?"
            ), (conv["id"], watermark["created_at"], watermark["created_at"],
                watermark["id"], MAX_TURNS + 1)).fetchall()
        return db.execute(self._sql(
            "SELECT id, payload_json, created_at FROM global_ask_jobs "
            "WHERE conversation_id=? AND status='done' AND created_at <= ? "
            + GLOBAL_JOBS_ORDER_ASC + " LIMIT ?"
        ), (conv["id"], conv["shared_through_at"], MAX_TURNS + 1)).fetchall()

    @staticmethod
    def _payload(raw):
        try:
            return json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
