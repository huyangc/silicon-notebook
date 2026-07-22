"""PostgreSQL Ask-domain durable state store."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from app.models.ask import (
    ActiveAskJob,
    AskRequest,
    AskResponse,
    ConversationDetail,
    ConversationSummary,
    ConversationTurn,
    FeedbackRequest,
    FeedbackResponse,
)
from app.repositories.ports import PreparedAskTurn
from app.repositories.postgres._store_utils import (
    json_value,
    jsonb,
    iso_timestamp,
    normalize_timestamp,
)
from app.repositories.postgres.database import PostgresDatabase


class AskStateStore:
    def __init__(self, database: PostgresDatabase, seams) -> None:
        self.database = database
        self.seams = seams

    # ------------------------------------------------------------------
    # turn preparation
    # ------------------------------------------------------------------

    def ensure_conversation(
        self,
        db: object,
        notebook_id: str,
        conversation_id: Optional[str],
        question: str,
        user_id: str,
    ) -> str:
        """Return the conversation id for this turn: append to an existing
        conversation in this notebook (touching `updated_at`), or create a new
        one (id `conv-<hex>`, title from the first question)."""
        now = normalize_timestamp(self.seams.now())
        if conversation_id:
            # 只接续**调用者自己**的对话:共享库里成员传入 owner/他人的 conv-id 不命中,
            # 落到下面新建一条归自己的对话,杜绝跨用户注入回合(read-only 成员经 ask 触达)。
            row = db.execute(
                "SELECT id FROM conversations WHERE id = %s AND notebook_id = %s AND created_by = %s",
                (conversation_id, notebook_id, user_id),
            ).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE conversations SET updated_at = %s WHERE id = %s",
                    (now, conversation_id),
                )
                return conversation_id
        new_id = self.seams.new_id("conv")
        db.execute(
            "INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (new_id, notebook_id, question[:60], user_id, now, now),
        )
        return new_id

    def conversation_history(
        self, db: object, conversation_id: str, limit: int = 5
    ) -> str:
        """Build the prior-turns history block (oldest->newest, last `limit`
        turns) from stored answer payloads. Uses each turn's `conclusion`
        (provenance markers already stripped). Returns "" when no prior turns."""
        rows = db.execute(
            "SELECT question, payload FROM answers WHERE conversation_id = %s "
            "ORDER BY created_at ASC, ordinal ASC",
            (conversation_id,),
        ).fetchall()
        rows = rows[-limit:]
        lines = []
        for row in rows:
            payload = json_value(row["payload"], {})
            conclusion = str(payload.get("conclusion", "")).strip()
            lines.append(f"User: {row['question']}\nAssistant: {conclusion}")
        return "\n".join(lines)

    def prepare_turn(
        self,
        notebook_id: str,
        requested_conversation_id: "str | None",
        question: str,
        user_id: str,
    ) -> PreparedAskTurn:
        """Create/continue the conversation and read back its history in ONE
        write transaction — the exact read-modify block every ask mode engine
        opens today (Task 24 moves the engines onto this port)."""
        with self.database.write() as db:
            conversation_id = self.ensure_conversation(
                db, notebook_id, requested_conversation_id, question, user_id
            )
            history = self.conversation_history(db, conversation_id)
        return PreparedAskTurn(conversation_id=conversation_id, history=history)

    # ------------------------------------------------------------------
    # durable job state machine (running → done/failed/cancelled)
    # ------------------------------------------------------------------

    def begin_durable_job(
        self,
        notebook_id: str,
        payload: AskRequest,
        mode: str,
        user_id: str,
    ) -> tuple[str, str]:
        """建/接续会话 + 插入 running 的 ask_jobs 行,一个写事务原子提交。
        就地把解析出的 conversation_id 写回 payload(与基线同一时点——在事务内、
        插 job 行之前,故即便 job 插入失败回滚,payload 仍保留生成的 id),
        使随后的 handler(_ensure_conversation)接续同一会话、不另建。
        返回 (job_id, conversation_id)。cancel-event 注册留在 facade 编排。"""
        question = payload.question.strip()
        now = normalize_timestamp(self.seams.now())
        job_id = self.seams.new_id("askjob")
        with self.database.write() as db:
            conversation_id = self.ensure_conversation(
                db, notebook_id, payload.conversation_id, question, user_id)
            payload.conversation_id = conversation_id
            db.execute(
                "INSERT INTO ask_jobs (id,notebook_id,conversation_id,created_by,mode,question,"
                "status,trace_json,answer_id,error,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s, 'running',%s,'','',%s,%s)",
                (job_id, notebook_id, conversation_id, user_id, mode,
                 question[:200], jsonb([]), now, now))
        return job_id, conversation_id

    def finish_job(
        self,
        job_id: str,
        status: str,
        *,
        answer_id: str = "",
        error: str = "",
    ) -> "str | None":
        """终态化 ask_job(仅这一个终态 job 行事务)。返回该 job 的
        conversation_id(job 行不存在 → None);cancelled/failed 的空会话清理
        保持为**之后的另一个**事务(cleanup_empty_conversation),由 facade 编排。"""
        with self.database.write() as db:
            row = db.execute(
                "SELECT conversation_id,status FROM ask_jobs WHERE id=%s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row is not None and (status == "cancelled" or row["status"] != "cancelled"):
                db.execute(
                    "UPDATE ask_jobs SET status=%s, answer_id=%s, error=%s, updated_at=%s WHERE id=%s",
                    (status, answer_id, error, normalize_timestamp(self.seams.now()), job_id))
        return row["conversation_id"] if row is not None else None

    def cleanup_empty_conversation(self, conversation_id: str) -> None:
        """删掉没有任何 answer 的会话(取消首轮留下的空壳);有答案则保留。"""
        with self.database.write() as db:
            db.execute(
                "DELETE FROM conversations WHERE id=%s AND NOT EXISTS "
                "(SELECT 1 FROM answers WHERE conversation_id=%s)",
                (conversation_id, conversation_id))

    def ask_job_status(self, job_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id,notebook_id,conversation_id,created_by,mode,status,answer_id,error "
                "FROM ask_jobs WHERE id=%s", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return {"job_id": row["id"], "notebook_id": row["notebook_id"],
                "conversation_id": row["conversation_id"], "created_by": row["created_by"],
                "mode": row["mode"], "status": row["status"], "answer_id": row["answer_id"],
                "error": row["error"]}

    # ------------------------------------------------------------------
    # M1 trace: append-only sub-table
    # ------------------------------------------------------------------

    def append_trace(
        self, notebook_id: str, job_id: str, step: dict, user_id: str
    ) -> None:
        """把一个 trace step 追加进 ask_trace_steps 子表(append-only,O(1) 单行
        INSERT)。seq 用 `SELECT COALESCE(MAX(seq),-1)+1 WHERE job_id=%s` 在同一个
        写事务里取号+插入,避免与自己的下一次 append 竞态(虽单 worker 写单个
        job、无写写竞态,取号+插同事务仍是稳妥做法)。job 行不存在 → no-op(基线守卫)。

        raw store 语义:持久化失败**上抛**;fail-open(记日志吞掉、绝不拖垮 ask)
        是 facade 协调层 append_ask_trace 的既有契约,不在本层。notebook_id /
        user_id 是冻结 port 签名的一部分(Task 24 的引擎调用会带真实值),本层
        SQL 只按 job_id 落行——与基线一致,不多查一行。"""
        with self.database.write() as db:
            exists = db.execute(
                "SELECT 1 FROM ask_jobs WHERE id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if exists is None:
                return
            next_seq = db.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM ask_trace_steps WHERE job_id=%s",
                (job_id,),
            ).fetchone()["n"]
            db.execute(
                "INSERT INTO ask_trace_steps (job_id, seq, step_json, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (job_id, next_seq, jsonb(step), normalize_timestamp(self.seams.now())),
            )

    @staticmethod
    def read_trace(db: object, job_id: str) -> list:
        """从 ask_trace_steps 子表按 seq 顺序读回一个 job 的完整轨迹,拼成 list。
        单行解析失败(损坏的 step_json)容错跳过而非整体失败——与旧版
        trace_json 列「解析失败即空列表」的粗粒度容错相比更细,但不改变
        「解析失败不抛」这条既有契约。取代直读 ask_jobs.trace_json 列
        (该列已停止写入,只为兼容旧行保留,见 append_trace)。"""
        rows = db.execute(
            "SELECT step_json FROM ask_trace_steps WHERE job_id=%s ORDER BY seq ASC",
            (job_id,),
        ).fetchall()
        trace = []
        for r in rows:
            try:
                value = json_value(r["step_json"], None)
                if isinstance(value, dict):
                    trace.append(value)
            except (TypeError, ValueError):
                continue
        return trace

    def ask_job_detail(self, job_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id,notebook_id,conversation_id,created_by,mode,question,status,"
                "answer_id,error FROM ask_jobs WHERE id=%s", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            trace = self.read_trace(db, job_id)
        return {"job_id": row["id"], "notebook_id": row["notebook_id"],
                "conversation_id": row["conversation_id"], "created_by": row["created_by"],
                "mode": row["mode"], "question": row["question"], "status": row["status"],
                "trace": trace, "answer_id": row["answer_id"], "error": row["error"]}

    # ------------------------------------------------------------------
    # answers
    # ------------------------------------------------------------------

    def answer_notebook_id(self, answer_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT notebook_id FROM answers WHERE id=%s", (answer_id,)
            ).fetchone()
        return row["notebook_id"] if row is not None else None

    def answer_memory_source(self, answer_id: str) -> dict:
        """Return the durable, server-owned Ask fields used by Memory capture."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT notebook_id,question,payload,conversation_id "
                "FROM answers WHERE id=%s",
                (answer_id,),
            ).fetchone()
        if row is None:
            raise KeyError(answer_id)
        payload = json_value(row["payload"], {})
        return {
            "answer_id": answer_id,
            "notebook_id": row["notebook_id"],
            "question": row["question"] or "",
            "answer": str(payload.get("answer") or payload.get("conclusion") or ""),
            "conversation_id": row["conversation_id"],
            "mode": str(payload.get("mode") or ""),
            "model": str(payload.get("llm_mode") or ""),
            "evidence_level": str(payload.get("evidence_level") or "inferred"),
            "anchors": payload.get("anchors") if isinstance(payload.get("anchors"), list) else [],
            "citations": payload.get("citations") if isinstance(payload.get("citations"), list) else [],
        }

    def save_answer(
        self,
        notebook_id: str,
        conversation_id: Optional[str],
        question: str,
        response: AskResponse,
        user_id: str,
    ) -> str:
        """Mint the answer id, stamp it into the payload JSON and commit the
        answers row in its own write transaction.  The large-library
        ``index_required`` decoration stays a facade concern (scale-index
        domain) and must already be applied to ``response``."""
        answer_id = self.seams.new_id("ans")
        now = normalize_timestamp(self.seams.now())
        payload = response.model_dump()
        payload["answer_id"] = answer_id
        with self.database.write() as db:
            db.execute(
                "INSERT INTO answers (id, notebook_id, question, payload, created_at, conversation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    answer_id,
                    notebook_id,
                    question,
                    jsonb(payload),
                    now,
                    conversation_id,
                ),
            )
        return answer_id

    # ------------------------------------------------------------------
    # conversation projections / CRUD
    # ------------------------------------------------------------------

    def get_conversation(self, conversation_id: str) -> ConversationDetail:
        """Rebuild a ConversationDetail from the conversations row + its answer
        turns. Raises KeyError if the conversation does not exist."""
        with self.database.connect() as db:
            conv = db.execute(
                "SELECT id, notebook_id, title, updated_at FROM conversations WHERE id = %s",
                (conversation_id,),
            ).fetchone()
            if conv is None:
                raise KeyError(conversation_id)
            rows = db.execute(
                "SELECT id, question, payload, created_at FROM answers "
                "WHERE conversation_id = %s ORDER BY created_at ASC, ordinal ASC",
                (conversation_id,),
            ).fetchall()
            job = db.execute(
                "SELECT id, question, mode FROM ask_jobs "
                "WHERE conversation_id=%s AND status='running' "
                "ORDER BY created_at DESC, id COLLATE \"C\" DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            job_trace = self.read_trace(db, job["id"]) if job is not None else []
        turns = []
        for row in rows:
            payload = json_value(row["payload"], {})
            turns.append(
                ConversationTurn(
                    answer_id=row["id"],
                    question=row["question"],
                    response=AskResponse(**payload),
                    created_at=iso_timestamp(row["created_at"]),
                )
            )
        used_reasoning = bool(turns[-1].response.reasoning_trace) if turns else False
        active_job = None
        if job is not None:
            active_job = ActiveAskJob(job_id=job["id"], question=job["question"] or "",
                                      mode=job["mode"] or "", trace=job_trace)
        return ConversationDetail(
            id=conv["id"],
            notebook_id=conv["notebook_id"],
            title=conv["title"] or "",
            updated_at=iso_timestamp(conv["updated_at"]),
            turn_count=len(turns),
            used_reasoning=used_reasoning,
            turns=turns,
            active_job=active_job,
        )

    def list_conversations(
        self, notebook_id: str, user_id: str
    ) -> List[ConversationSummary]:
        """List the given user's conversations for a notebook (most-recently-
        updated first) with a per-conversation turn count.  The notebook-
        existence KeyError guard stays with the facade adapter."""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT c.id, c.notebook_id, c.title, c.updated_at, "
                "(SELECT COUNT(*) FROM answers a WHERE a.conversation_id = c.id) AS turn_count, "
                "(SELECT COALESCE(jsonb_array_length(CASE "
                "WHEN jsonb_typeof(a.payload->'reasoning_trace')='array' "
                "THEN a.payload->'reasoning_trace' ELSE '[]'::jsonb END), 0) > 0 "
                "   FROM answers a WHERE a.conversation_id = c.id "
                "  ORDER BY a.ordinal DESC LIMIT 1) AS used_reasoning "
                "FROM conversations c WHERE c.notebook_id = %s AND c.created_by = %s "
                "ORDER BY c.updated_at DESC, c.id COLLATE \"C\"",
                (notebook_id, user_id),
            ).fetchall()
        return [
            ConversationSummary(
                id=row["id"],
                notebook_id=row["notebook_id"],
                title=row["title"] or "",
                updated_at=iso_timestamp(row["updated_at"]),
                turn_count=row["turn_count"],
                used_reasoning=bool(row["used_reasoning"]),
            )
            for row in rows
        ]

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        with self.database.write() as db:
            cur = db.execute(
                "UPDATE conversations SET title=%s, updated_at=%s WHERE id=%s",
                (title, normalize_timestamp(self.seams.now()), conversation_id),
            )
            if cur.rowcount == 0:
                raise KeyError(conversation_id)

    def delete_conversation(self, conversation_id: str) -> None:
        with self.database.write() as db:
            cur = db.execute("DELETE FROM conversations WHERE id=%s", (conversation_id,))
            if cur.rowcount == 0:
                raise KeyError(conversation_id)
            db.execute("DELETE FROM answers WHERE conversation_id=%s", (conversation_id,))

    def bulk_delete_conversations(
        self, notebook_id: str, older_than_days: int, user_id: str
    ) -> int:
        """Delete the given user's conversations in `notebook_id` whose last
        activity (`updated_at`) is strictly older than `older_than_days` days,
        cascading to their answers. Returns the number deleted.  The notebook-
        existence KeyError guard stays with the facade adapter."""
        if older_than_days < 1:
            raise ValueError("older_than_days must be >= 1")
        cutoff = normalize_timestamp(
            (datetime.now() - timedelta(days=older_than_days)).replace(microsecond=0)
        )
        with self.database.write() as db:
            ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM conversations "
                    "WHERE notebook_id = %s AND created_by = %s AND updated_at < %s",
                    (notebook_id, user_id, cutoff),
                ).fetchall()
            ]
            db.executemany("DELETE FROM answers WHERE conversation_id = %s", [(cid,) for cid in ids])
            db.executemany("DELETE FROM conversations WHERE id = %s", [(cid,) for cid in ids])
        return len(ids)

    # ------------------------------------------------------------------
    # feedback
    # ------------------------------------------------------------------

    def submit_feedback(
        self, answer_id: str, payload: FeedbackRequest
    ) -> FeedbackResponse:
        if payload.rating not in {"useful", "not_useful"}:
            raise ValueError("rating must be useful or not_useful")
        now = normalize_timestamp(self.seams.now())
        feedback_id = self.seams.new_id("fb")
        with self.database.write() as db:
            answer = db.execute(
                "SELECT notebook_id FROM answers WHERE id = %s",
                (answer_id,),
            ).fetchone()
            if answer is None:
                raise KeyError(answer_id)
            db.execute(
                "INSERT INTO feedback (id, answer_id, notebook_id, rating, comment, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (feedback_id, answer_id, answer["notebook_id"], payload.rating, payload.comment, now),
            )
        return FeedbackResponse(
            id=feedback_id,
            answer_id=answer_id,
            rating=payload.rating,
            comment=payload.comment,
        )
