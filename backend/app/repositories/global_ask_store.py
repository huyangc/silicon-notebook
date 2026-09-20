"""Durable global conversations and jobs on the shared database boundary."""
from __future__ import annotations

import json

from app.models.ask import CONVERSATION_TITLE_MAX_CHARS
from app.models.global_ask import GlobalAskJob, GlobalConversationSummary


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

    def create(self, job, user_id, request_id, request_json, submitted_via, *, new_conversation):
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
                row = db.execute(self._sql(
                    "SELECT id FROM global_ask_conversations WHERE id=? AND user_id=?"
                ), (job.conversation_id, user_id)).fetchone()
                if row is None:
                    raise KeyError(job.conversation_id)
            db.execute(self._sql(
                "INSERT INTO global_ask_jobs"
                "(id,conversation_id,user_id,client_request_id,request_json,status,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)"
            ), (job.job_id, job.conversation_id, user_id, request_id or None,
                request_json, job.status, job.model_dump_json(), job.created_at))
            db.execute(self._sql(
                "UPDATE global_ask_conversations SET scope_json=?,updated_at=? WHERE id=? AND user_id=?"
            ), (job.notebook_scope.model_dump_json(), job.created_at, job.conversation_id, user_id))

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
