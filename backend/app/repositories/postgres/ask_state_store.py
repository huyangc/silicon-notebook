"""PostgreSQL Ask-domain durable state store."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from app.domain.retrieval_experience import project_run_step, project_trace_step
from app.models.ask import (
    ActiveAskJob,
    AskRequest,
    AskResponse,
    ConversationBulkDeleteResult,
    ConversationDetail,
    ConversationSummary,
    ConversationTurn,
    FeedbackRequest,
    FeedbackResponse,
)
from app.repositories.ports import (
    ConversationBusyError,
    ConversationHasNoShareableAnswer,
    ConversationShareWatermarkStale,
    PreparedAskTurn,
    project_ask_row,
    project_report_attempt,
    project_report_row,
    project_run_row,
)
from app.repositories.postgres._store_utils import (
    json_value,
    jsonb,
    iso_timestamp,
    normalize_timestamp,
)
from app.core.capability_tokens import new_capability_token
from app.core.internal_observability import (
    public_trace_steps,
    sanitize_answer_payload,
)
from app.repositories.postgres.database import PostgresDatabase
# Single source of truth for the public page's turn ceiling — see the SQLite
# adapter's import of the same constant for why the token-resolved query bounds
# its fetch to ``MAX_TURNS + 1`` (codex #522 R6 P2). Pure leaf module, no cycle.
from app.domain.conversation_public_view import MAX_TURNS
# Agentic Memory P3 (B-Profile, T7): the ONE place the question text coming
# back from ``recent_user_ask_languages`` is turned into a closed language
# bucket, before it leaves this store — see that method's own docstring, and
# the SQLite adapter's mirror import for the same reason.
from app.domain.search_profile import classify_ask_language


# Canonical oldest -> newest order for one conversation's answers. Mirrors
# the SQLite adapter's module constant of the same name — see its docstring
# for why `get_conversation` and the public-share snapshot query
# (`public_conversation_by_token` below) must stay byte-identical here.
# PostgreSQL's tie-break is `ordinal` (the BY DEFAULT identity column that
# already stands in for SQLite's rowid on this table — see
# `POSTGRES_ROWID_ORDINAL_TABLES`), not `id`; `created_at` is a native
# `timestamptz` here, so no `julianday()`-style text-comparison shim is
# needed the way SQLite's naive/offset-aware text column requires one.
CONVERSATION_ANSWERS_ORDER_ASC = "ORDER BY created_at ASC, ordinal ASC"
CONVERSATION_ANSWERS_ORDER_DESC = "ORDER BY created_at DESC, ordinal DESC"


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
                "SELECT id FROM conversations WHERE id = %s AND notebook_id = %s "
                "AND created_by = %s FOR UPDATE",
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

    def prepare_turn_for_job(
        self,
        job_id: str,
        notebook_id: str,
        conversation_id: "str | None",
        user_id: str,
    ) -> "PreparedAskTurn | None":
        """Prepare only the exact still-running durable job and its parent.

        The conversation row is the lifecycle lease.  Once it is locked, a
        non-locking job-status check is sufficient: cancellation may still win
        later, but this transaction can neither recreate a missing parent nor
        persist into a replacement conversation.  Avoiding a job-row lock here
        also preserves the canonical no-cycle order with final save
        (job→conversation).
        """
        if not conversation_id:
            return None
        with self.database.write() as db:
            parent = db.execute(
                "SELECT id FROM conversations WHERE id=%s AND notebook_id=%s "
                "AND created_by=%s FOR UPDATE",
                (conversation_id, notebook_id, user_id),
            ).fetchone()
            if parent is None:
                return None
            running = db.execute(
                "SELECT 1 FROM ask_jobs WHERE id=%s AND notebook_id=%s "
                "AND conversation_id=%s AND created_by=%s AND status='running'",
                (job_id, notebook_id, conversation_id, user_id),
            ).fetchone()
            if running is None:
                return None
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
                "asked_at,status,trace_json,answer_id,error,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s, 'running',%s,'','',%s,%s)",
                (job_id, notebook_id, conversation_id, user_id, mode,
                 question[:200], payload.asked_at, jsonb([]), now, now))
        return job_id, conversation_id

    def finish_job(
        self,
        job_id: str,
        status: str,
        *,
        answer_id: str = "",
        error: str = "",
    ) -> "str | None":
        """终态化仍在运行的 ask_job。已写入的终态不可被后来覆盖。返回该 job 的
        conversation_id(job 行不存在 → None);cancelled/failed 的空会话清理
        保持为**之后的另一个**事务(cleanup_empty_conversation),由 facade 编排。"""
        with self.database.write() as db:
            row = db.execute(
                "SELECT conversation_id,status FROM ask_jobs WHERE id=%s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row is not None and row["status"] == "running":
                db.execute(
                    "UPDATE ask_jobs SET status=%s, answer_id=%s, error=%s, updated_at=%s "
                    "WHERE id=%s AND status='running'",
                    (status, answer_id, error, normalize_timestamp(self.seams.now()), job_id),
                )
        return row["conversation_id"] if row is not None else None

    def cancel_running_job(self, job_id: str, user_id: str) -> dict:
        """Durably cancel a running owned job under its row lock."""
        with self.database.write() as db:
            row = db.execute(
                "SELECT conversation_id,status FROM ask_jobs "
                "WHERE id=%s AND created_by=%s FOR UPDATE",
                (job_id, user_id),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            cancelled = row["status"] == "running"
            if cancelled:
                db.execute(
                    "UPDATE ask_jobs SET status='cancelled',answer_id='',error='',updated_at=%s "
                    "WHERE id=%s AND status='running'",
                    (normalize_timestamp(self.seams.now()), job_id),
                )
        return {
            "job_id": job_id,
            "status": "cancelled" if cancelled else row["status"],
            "conversation_id": row["conversation_id"],
            "cancelled": cancelled,
        }

    def cleanup_empty_conversation(self, conversation_id: str) -> None:
        """Delete an answer-less conversation once no Ask worker can use it."""
        with self.database.write() as db:
            conversation = db.execute(
                "SELECT id FROM conversations WHERE id=%s FOR UPDATE",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                return
            in_use = db.execute(
                "SELECT 1 WHERE EXISTS "
                "(SELECT 1 FROM answers WHERE conversation_id=%s) OR EXISTS "
                "(SELECT 1 FROM ask_jobs WHERE conversation_id=%s AND status='running')",
                (conversation_id, conversation_id),
            ).fetchone()
            if in_use is not None:
                return
            db.execute(
                "DELETE FROM conversations WHERE id=%s",
                (conversation_id,),
            )

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
        return public_trace_steps(trace)

    def recent_user_ask_traces(
        self,
        notebook_id: str,
        user_id: str,
        *,
        job_limit: int,
        step_limit: int,
    ) -> list[dict]:
        """ONE member's own recent asks in ONE notebook, projected and bounded.

        Agentic Memory P1 (T5) — the PostgreSQL half of the SQLite method of
        the same name; see that docstring for the full contract. The two
        properties that must not diverge:

        * ``created_by = %s`` lives **in both SQL statements**, the trace one
          included even though its job ids came out of the first. This is a
          privacy boundary, not a filter: the result feeds one member's private
          overlay blocks, and a foreign row would be summarised into them
          silently. ``test_agent_profile_isolation_guard.py`` pins it here too.
        * The projection is the SHARED ``project_trace_step`` — this backend
          hands it a ``jsonb``-decoded dict where SQLite hands it TEXT, which
          is exactly the kind of difference a per-backend copy would resolve
          differently over time.
        * ``ORDER BY j.created_at DESC, j.id DESC, t.seq ASC`` before the
          ``step_limit`` cap — same fix as the SQLite side: the major sort key
          must be the job's own recency, not ``t.job_id`` (an opaque id with
          no relationship to when the job ran), or truncation drops whichever
          job's steps sort last lexicographically instead of the oldest one.
        """
        job_limit = max(1, int(job_limit))
        step_limit = max(1, int(step_limit))
        with self.database.connect() as db:
            job_rows = db.execute(
                "SELECT id, question, status, created_at FROM ask_jobs "
                "WHERE notebook_id = %s AND created_by = %s "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (notebook_id, user_id, job_limit),
            ).fetchall()
            asks = [
                project_ask_row(
                    row["id"],
                    row["question"],
                    row["status"],
                    iso_timestamp(row["created_at"]),
                )
                for row in job_rows
            ]
            if not asks:
                return []
            by_job = {ask["job_id"]: ask for ask in asks}
            placeholders = ",".join("%s" for _ in by_job)
            step_rows = db.execute(
                "SELECT t.job_id AS job_id, t.step_json AS step_json "
                "FROM ask_trace_steps t JOIN ask_jobs j ON j.id = t.job_id "
                f"WHERE j.notebook_id = %s AND j.created_by = %s AND t.job_id IN ({placeholders}) "
                "ORDER BY j.created_at DESC, j.id DESC, t.seq ASC LIMIT %s",
                (notebook_id, user_id, *by_job.keys(), step_limit),
            ).fetchall()
        for row in step_rows:
            step = project_trace_step(json_value(row["step_json"], None))
            target = by_job.get(str(row["job_id"]))
            if step is not None and target is not None:
                target["steps"].append(step)
        return asks

    def recent_completed_ask_runs(
        self, *, job_limit: int, step_limit: int
    ) -> list[dict]:
        """PostgreSQL mirror of the SQLite method — see that docstring for the
        contract (no user/notebook predicate BY DESIGN, safety carried by the
        projection, ``status = 'done'`` only, bounded twice).

        The one backend difference: ``step_json`` is ``jsonb`` here and TEXT on
        SQLite, so it goes through ``json_value`` before the shared
        ``project_run_step`` — which is exactly why that narrowing lives in
        ``ports.py`` once instead of being written twice.
        """
        job_limit = max(1, int(job_limit))
        step_limit = max(1, int(step_limit))
        with self.database.connect() as db:
            job_rows = db.execute(
                "SELECT id, mode FROM ask_jobs WHERE status = 'done' "
                # codex #524 R3 P2:只采样可注入的模式——经验只经
                # ReasoningRetriever(mode 恒 reasoning)注入,chunk/graph run
                # 学出的确定性行为翻成 reflect 建议既不可执行,还会在非
                # reasoning 流量占主导的部署里挤占全部 offered 席位。
                "AND mode = 'reasoning' "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (job_limit,),
            ).fetchall()
            runs = [project_run_row(row["id"], row["mode"]) for row in job_rows]
            if not runs:
                return []
            by_run = {run["run_id"]: run for run in runs}
            placeholders = ",".join("%s" for _ in by_run)
            step_rows = db.execute(
                "SELECT t.job_id AS job_id, t.step_json AS step_json "
                "FROM ask_trace_steps t JOIN ask_jobs j ON j.id = t.job_id "
                f"WHERE t.job_id IN ({placeholders}) "
                "ORDER BY j.created_at DESC, j.id DESC, t.seq ASC LIMIT %s",
                (*by_run.keys(), step_limit),
            ).fetchall()
        for row in step_rows:
            step = project_run_step(json_value(row["step_json"], None))
            target = by_run.get(str(row["job_id"]))
            if step is not None and target is not None:
                target["steps"].append(step)
        return runs

    def recent_user_report_traces(
        self,
        notebook_id: str,
        user_id: str,
        *,
        report_limit: int,
        attempt_limit: int,
    ) -> list[dict]:
        """ONE member's own recently completed deep reports, projected/bounded.

        Agentic Memory P2 (T4) — the PostgreSQL half of the SQLite method of
        the same name; see that docstring and
        ``AskStateStorePort.recent_user_report_traces`` for the full
        contract. The properties that must not diverge:

        * ``created_by = %s`` lives **in both SQL statements**, the attempt
          one included even though its ``report_id`` list came out of the
          first — same rule as ``recent_user_ask_traces``, pinned by the same
          guard (now over the two-element ``TRACE_READ_METHODS``).
        * ``sections_json`` (``jsonb``) is projected to ``$[*].attempted[*]``
          IN THE SQL via ``jsonb_array_elements``, guarded at each level with
          ``jsonb_typeof`` so a malformed/legacy row degrades to "no
          attempts" for that section rather than raising — never pulled whole
          into Python (one report's section markdown can run several hundred
          KB).
        * ``WITH ORDINALITY`` supplies the per-section/per-attempt array
          index (``s_ord``/``a_ord``) so the final ``ORDER BY`` can put the
          report's own recency first and still break ties deterministically,
          the same ``report → section → attempt`` order the SQLite side gets
          from ``json_each``'s ``key`` for free.
        * ``->>`` (text) extraction, not ``->`` — this store hands the two
          scalar columns to ``project_report_attempt`` as ``str | None``,
          where the SQLite side hands it whatever ``json_extract`` yields;
          that function is the one place both shapes are reconciled.

        ⚠ The per-attempt ``jsonb_typeof(a.value) = 'object'`` guard on the
        two extracted columns is BEHAVIOURAL PARITY, not a fix: ``jsonb`` is
        parsed at write time, so ``'"oops"'::jsonb ->> 'query'`` already
        returns NULL here rather than raising the way SQLite's
        ``json_extract`` on the same shape does (P2-T4 fix round, item 7).
        It is written out anyway so the two statements read as the same
        rule, and so a later edit on this side cannot quietly acquire the
        looser semantics the SQLite side is not allowed to have.
        """
        report_limit = max(1, int(report_limit))
        attempt_limit = max(1, int(attempt_limit))
        with self.database.connect() as db:
            report_rows = db.execute(
                "SELECT id, question, created_at FROM reports "
                "WHERE notebook_id = %s AND created_by = %s AND status = 'done' "
                "ORDER BY updated_at DESC, id DESC LIMIT %s",
                (notebook_id, user_id, report_limit),
            ).fetchall()
            reports = [
                project_report_row(
                    row["id"], row["question"], iso_timestamp(row["created_at"])
                )
                for row in report_rows
            ]
            if not reports:
                return []
            by_report = {report["report_id"]: report for report in reports}
            placeholders = ",".join("%s" for _ in by_report)
            attempt_rows = db.execute(
                "SELECT r.id AS report_id, "
                "CASE WHEN jsonb_typeof(a.value) = 'object' "
                "THEN (a.value ->> 'query') END AS query, "
                "CASE WHEN jsonb_typeof(a.value) = 'object' "
                "THEN (a.value ->> 'failed') END AS failed "
                "FROM reports r, "
                "jsonb_array_elements(CASE WHEN jsonb_typeof(r.sections_json) = 'array' "
                "THEN r.sections_json ELSE '[]'::jsonb END) "
                "WITH ORDINALITY AS s(value, s_ord), "
                "jsonb_array_elements(CASE WHEN jsonb_typeof(s.value) = 'object' "
                "AND jsonb_typeof(s.value -> 'attempted') = 'array' "
                "THEN s.value -> 'attempted' ELSE '[]'::jsonb END) "
                "WITH ORDINALITY AS a(value, a_ord) "
                f"WHERE r.notebook_id = %s AND r.created_by = %s AND r.status = 'done' "
                f"AND r.id IN ({placeholders}) "
                "ORDER BY r.updated_at DESC, r.id DESC, s_ord ASC, a_ord ASC "
                "LIMIT %s",
                (notebook_id, user_id, *by_report.keys(), attempt_limit),
            ).fetchall()
        for row in attempt_rows:
            target = by_report.get(str(row["report_id"]))
            if target is not None:
                target["attempts"].append(
                    project_report_attempt(row["query"], row["failed"])
                )
        return reports

    def recent_user_ask_languages(self, user_id: str, *, limit: int) -> list[dict]:
        """ONE person's recent asks, projected to nothing but a closed
        three-value language bucket (Agentic Memory P3, T7). Mirrors the
        SQLite adapter's method — see ``AskStateStorePort.
        recent_user_ask_languages`` for the full contract.
        """
        limit = max(1, int(limit))
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT question FROM ask_jobs "
                "WHERE created_by = %s AND status = 'done' "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (user_id, limit),
            ).fetchall()
        return [{"language": classify_ask_language(row["question"])} for row in rows]

    def ask_job_detail(self, job_id: str) -> dict:
        with self.database.connect() as db:
            def retained_detail() -> dict:
                retained = db.execute(
                    "SELECT record_id,notebook_id,actor_id,notebook_name,"
                    "conversation_id,mode,question,status,asked_at,deleted_at,"
                    "expires_at FROM retained_user_activity "
                    "WHERE activity_type='ask' AND record_id=%s "
                    "AND expires_at>CURRENT_TIMESTAMP "
                    "AND NOT EXISTS(SELECT 1 FROM notebooks live "
                    "WHERE live.id=retained_user_activity.notebook_id)",
                    (job_id,),
                ).fetchone()
                if retained is None:
                    raise KeyError(job_id)
                return {
                    "job_id": retained["record_id"],
                    "notebook_id": retained["notebook_id"],
                    "conversation_id": retained["conversation_id"],
                    "created_by": retained["actor_id"],
                    "mode": retained["mode"],
                    "question": retained["question"],
                    "status": retained["status"],
                    "trace": [],
                    "answer_id": "",
                    "error": "",
                    "asked_at": retained["asked_at"] or "",
                    "notebook_name": retained["notebook_name"],
                    "notebook_deleted_at": iso_timestamp(retained["deleted_at"]),
                    "retained_until": iso_timestamp(retained["expires_at"]),
                }

            row = db.execute(
                "SELECT id,notebook_id,conversation_id,created_by,mode,question,status,"
                "answer_id,error,asked_at FROM ask_jobs WHERE id=%s", (job_id,)).fetchone()
            if row is None:
                return retained_detail()
            trace = self.read_trace(db, job_id)
            if db.execute(
                "SELECT 1 FROM notebooks WHERE id=%s", (row["notebook_id"],)
            ).fetchone() is None:
                return retained_detail()
        return {"job_id": row["id"], "notebook_id": row["notebook_id"],
                "conversation_id": row["conversation_id"], "created_by": row["created_by"],
                "mode": row["mode"], "question": row["question"], "status": row["status"],
                "trace": trace, "answer_id": row["answer_id"], "error": row["error"],
                "asked_at": row["asked_at"] or ""}

    def ask_answer_detail(self, answer_id: str) -> "dict | None":
        """按 answer_id 直查单条答案(一条主键查询,不加载会话其余轮次)。

        取代旧路径「get_conversation(conversation_id) 再线性扫出匹配的
        turn」——那条路径的读取量随会话历史线性增长。``answered_at`` 的回填
        口径与 ``get_conversation`` 对单个 turn 的处理逐字一致：旧 payload
        缺 ``answered_at`` 时从 ``answers.created_at``(权威写入瞬间)回填。
        """
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id, question, payload, created_at FROM answers WHERE id=%s",
                (answer_id,),
            ).fetchone()
        if row is None:
            return None
        payload = sanitize_answer_payload(json_value(row["payload"], {}))
        payload["answered_at"] = str(
            payload.get("answered_at") or iso_timestamp(row["created_at"])
        )
        return {
            "answer_id": row["id"],
            "question": row["question"],
            "payload": payload,
            "created_at": iso_timestamp(row["created_at"]),
        }

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
        answers row in its own write transaction. A non-null conversation is
        owner/notebook checked under its row lock before insert because the
        legacy schema has no answers→conversations FK; ``None`` remains valid
        for server-owned answer snapshots used outside conversation history.
        The large-library ``index_required`` decoration stays a facade concern
        and must already be applied to ``response``."""
        answer_id = self.seams.new_id("ans")
        now = normalize_timestamp(self.seams.now())
        response.answered_at = iso_timestamp(now)
        payload = response.model_dump()
        payload["answer_id"] = answer_id
        with self.database.write() as db:
            self._lock_notebook_against_delete_on(db, notebook_id)
            self._lock_answer_conversation_on(
                db, notebook_id, conversation_id, user_id
            )
            self._insert_answer_on(
                db, answer_id, notebook_id, conversation_id, question, payload, now
            )
        return answer_id

    @staticmethod
    def _lock_notebook_against_delete_on(db: object, notebook_id: str) -> None:
        """Keep the root alive without conflicting with concurrent FK inserts."""
        row = db.execute(
            "SELECT id FROM notebooks WHERE id=%s FOR KEY SHARE", (notebook_id,)
        ).fetchone()
        if row is None:
            raise KeyError(notebook_id)

    @staticmethod
    def _lock_answer_conversation_on(
        db: object,
        notebook_id: str,
        conversation_id: Optional[str],
        user_id: str,
    ) -> None:
        if conversation_id is None:
            return
        row = db.execute(
            "SELECT id FROM conversations WHERE id=%s AND notebook_id=%s "
            "AND created_by=%s FOR UPDATE",
            (conversation_id, notebook_id, user_id),
        ).fetchone()
        if row is None:
            raise KeyError(conversation_id)

    @staticmethod
    def _insert_answer_on(
        db: object,
        answer_id: str,
        notebook_id: str,
        conversation_id: Optional[str],
        question: str,
        payload: dict,
        now: object,
    ) -> None:
        db.execute(
            "INSERT INTO answers (id, notebook_id, question, payload, created_at, conversation_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (answer_id, notebook_id, question, jsonb(payload), now, conversation_id),
        )

    def save_answer_for_job(
        self,
        job_id: str,
        notebook_id: str,
        conversation_id: Optional[str],
        question: str,
        response: AskResponse,
        user_id: str,
    ) -> "str | None":
        """Atomically save the final answer and move a running job to done."""
        answer_id = self.seams.new_id("ans")
        now = normalize_timestamp(self.seams.now())
        response.answered_at = iso_timestamp(now)
        payload = response.model_dump()
        payload["answer_id"] = answer_id
        with self.database.write() as db:
            self._lock_notebook_against_delete_on(db, notebook_id)
            row = db.execute(
                "SELECT status FROM ask_jobs WHERE id=%s AND notebook_id=%s "
                "AND conversation_id=%s AND created_by=%s FOR UPDATE",
                (job_id, notebook_id, conversation_id, user_id),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] != "running":
                return None
            self._lock_answer_conversation_on(
                db, notebook_id, conversation_id, user_id
            )
            self._insert_answer_on(
                db, answer_id, notebook_id, conversation_id, question, payload, now
            )
            db.execute(
                "UPDATE ask_jobs SET status='done',answer_id=%s,error='',updated_at=%s "
                "WHERE id=%s AND status='running'",
                (answer_id, now, job_id),
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
                "WHERE conversation_id = %s " + CONVERSATION_ANSWERS_ORDER_ASC,
                (conversation_id,),
            ).fetchall()
            job = db.execute(
                "SELECT id, question, asked_at, mode FROM ask_jobs "
                "WHERE conversation_id=%s AND status='running' "
                "ORDER BY created_at DESC, id COLLATE \"C\" DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            job_trace = self.read_trace(db, job["id"]) if job is not None else []
        turns = []
        for row in rows:
            payload = json_value(row["payload"], {})
            payload["answered_at"] = str(
                payload.get("answered_at") or iso_timestamp(row["created_at"])
            )
            turns.append(
                ConversationTurn(
                    answer_id=row["id"],
                    question=row["question"],
                    response=AskResponse(**payload),
                    asked_at=str(payload.get("asked_at") or ""),
                    created_at=iso_timestamp(row["created_at"]),
                )
            )
        used_reasoning = bool(turns[-1].response.reasoning_trace) if turns else False
        active_job = None
        if job is not None:
            active_job = ActiveAskJob(
                job_id=job["id"],
                question=job["question"] or "",
                asked_at=job["asked_at"] or "",
                mode=job["mode"] or "",
                trace=job_trace,
            )
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

    @staticmethod
    def _conversation_has_running_job_on(db: object, conversation_id: str) -> bool:
        return db.execute(
            "SELECT 1 FROM ask_jobs WHERE conversation_id=%s AND status='running'",
            (conversation_id,),
        ).fetchone() is not None

    @classmethod
    def _delete_idle_conversation_on(
        cls, db: object, conversation_id: str, *, refuse_running: bool
    ) -> bool:
        """Delete all private durable state only when no running job is visible.

        The caller already holds the conversation row lease.  This helper
        deliberately observes running jobs without locking them; final save
        may therefore retain job→conversation order without a deadlock cycle.
        Once no running row is visible, all remaining jobs are terminal and
        immutable, so answers, traces, jobs and parent can be removed atomically.
        """
        if cls._conversation_has_running_job_on(db, conversation_id):
            if refuse_running:
                raise ConversationBusyError()
            return False
        db.execute(
            "DELETE FROM answers WHERE conversation_id=%s", (conversation_id,)
        )
        db.execute(
            "DELETE FROM ask_trace_steps WHERE job_id IN "
            "(SELECT id FROM ask_jobs WHERE conversation_id=%s "
            "AND status<>'running')",
            (conversation_id,),
        )
        db.execute(
            "DELETE FROM ask_jobs WHERE conversation_id=%s AND status<>'running'",
            (conversation_id,),
        )
        parent = db.execute(
            "DELETE FROM conversations c WHERE c.id=%s "
            "AND NOT EXISTS (SELECT 1 FROM answers a "
            "WHERE a.conversation_id=c.id) "
            "AND NOT EXISTS (SELECT 1 FROM ask_jobs j "
            "WHERE j.conversation_id=c.id)",
            (conversation_id,),
        )
        return parent.rowcount == 1

    def delete_conversation(self, conversation_id: str) -> None:
        with self.database.write() as db:
            scope = db.execute(
                "SELECT notebook_id FROM conversations WHERE id=%s",
                (conversation_id,),
            ).fetchone()
            if scope is None:
                raise KeyError(conversation_id)
            self._lock_notebook_against_delete_on(db, scope["notebook_id"])
            parent = db.execute(
                "SELECT id FROM conversations WHERE id=%s AND notebook_id=%s "
                "FOR UPDATE",
                (conversation_id, scope["notebook_id"]),
            ).fetchone()
            if parent is None:
                raise KeyError(conversation_id)
            if not self._delete_idle_conversation_on(
                db, conversation_id, refuse_running=True
            ):
                raise KeyError(conversation_id)

    def bulk_delete_conversations(
        self, notebook_id: str, older_than_days: int, user_id: str
    ) -> ConversationBulkDeleteResult:
        """Delete inactive conversations under their parent-row leases.

        A root KEY SHARE lease first excludes whole-notebook deletion while
        remaining compatible with ordinary child inserts. Candidate parents
        are then locked in stable id order, then ownership,
        cutoff, child-answer completion and absence of a running durable job
        are revalidated in the same transaction.  Begin/prepare/raw save all
        need the same parent lock, so none can revive a snapshotted id between
        child and parent deletion.  Job rows are deliberately not locked while
        the parent is held, avoiding an inverse edge with final save's
        job→conversation order.
        """
        if older_than_days < 1:
            raise ValueError("older_than_days must be >= 1")
        # 用注入的仓库时钟(self.seams.now())而非真实 datetime.now(),与本 store 其余
        # 时间点一致(见 sqlite 侧同名方法的说明:固定注入时钟的用例才能确定性)。
        cutoff = normalize_timestamp(
            (
                datetime.fromisoformat(self.seams.now()) - timedelta(days=older_than_days)
            ).replace(microsecond=0)
        )
        with self.database.write() as db:
            self._lock_notebook_against_delete_on(db, notebook_id)
            candidates = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM conversations "
                    "WHERE notebook_id=%s AND created_by=%s AND updated_at<%s "
                    "ORDER BY id COLLATE \"C\" FOR UPDATE",
                    (notebook_id, user_id, cutoff),
                ).fetchall()
            ]
            deleted_ids: list[str] = []
            for conversation_id in candidates:
                eligible = db.execute(
                    "SELECT 1 FROM conversations c WHERE c.id=%s "
                    "AND c.notebook_id=%s AND c.created_by=%s AND c.updated_at<%s "
                    "AND NOT EXISTS (SELECT 1 FROM ask_jobs j "
                    "WHERE j.conversation_id=c.id AND j.status='running')",
                    (conversation_id, notebook_id, user_id, cutoff),
                ).fetchone()
                if eligible is None:
                    continue
                if self._delete_idle_conversation_on(
                    db, conversation_id, refuse_running=False
                ):
                    deleted_ids.append(conversation_id)
        return ConversationBulkDeleteResult(
            deleted=len(deleted_ids), deleted_ids=deleted_ids
        )

    # ------------------------------------------------------------------
    # public conversation sharing (T1: schema + store only, no caller yet —
    # see docs/superpowers/specs/2026-08-18-conversation-sharing-design_zh.md)
    # ------------------------------------------------------------------

    def share_conversation(
        self,
        notebook_id: str,
        conversation_id: str,
        expected_through_id: str | None = None,
    ) -> dict:
        """Issue (or reuse) the public token for one conversation and pin its
        read watermark to a specific answer.

        Idempotent on the TOKEN only: re-sharing keeps the existing link, so
        a URL already handed out never silently starts 404ing (mirrors
        ``report_store.share_report`` — same COALESCE-in-the-UPDATE shape,
        for the same reason: under READ COMMITTED, two concurrent shares
        would each observe a NULL token and unconditionally overwrite it
        without COALESCE). The WATERMARK is deliberately NOT idempotent —
        "share" and "update to latest" are the same call (see the design
        doc §四), so every invocation advances ``shared_through_at``/
        ``shared_through_id`` to the answer the boundary resolves to, even
        when the token itself does not change.

        ``expected_through_id`` closes the disclosure TOCTOU (codex #522 R2 P1)
        — see the SQLite ``share_conversation`` for the full rationale: it is
        the newest answer id the CLIENT saw in the same turns it disclosed, and
        the watermark is pinned to exactly that answer (so a newer answer that
        landed after the client's read is NOT published). When it no longer
        resolves (deleted) we ``raise ConversationShareWatermarkStale`` for a
        409, never silently fall back to latest. Omitted/empty falls back to
        the current latest answer (historical behaviour).

        The watermark is ADVANCE-ONLY (codex #522 R3) — see the SQLite
        ``share_conversation`` for the full rationale: a request whose boundary
        sorts BEFORE the already-published one is rejected with
        ``ConversationShareWatermarkStale`` (→ 409) rather than allowed to regress
        the link. The regression is measured in SQL against both answer rows with
        the SAME ``(created_at, ordinal)`` keyset the public snapshot uses; the
        ``FOR UPDATE`` above holds the row lock through the compare AND the
        UPDATE, so under READ COMMITTED the two are one atomic step. An equal
        boundary is an idempotent no-op; a strictly newer one advances.

        Raises ``KeyError`` when the conversation does not exist in this
        notebook, and ``ConversationHasNoShareableAnswer`` when it has no
        committed answer to bound the snapshot — the "at least one written
        answer" policy (design doc §七 item 5) is enforced HERE, atomically in
        the same ``FOR UPDATE`` transaction, so a zero-answer conversation never
        has a token minted (no more share-then-compensate rollback). The
        row-level ``created_by`` gate still belongs to the API/service layer that
        calls this method.
        """
        candidate = new_capability_token("cshr")
        expected = str(expected_through_id or "").strip()
        with self.database.write() as db:
            # ``FOR UPDATE`` serializes concurrent shares of the SAME
            # conversation (codex #522 R1 concurrency P2). Under READ COMMITTED
            # two shares could each read ``latest`` before either UPDATE, and a
            # stale one — reading an OLDER answer, or NULL before the first
            # answer commits — would then clobber a watermark the other already
            # returned live. Taking the row lock here, BEFORE reading ``latest``,
            # forces the second share to wait for the first to commit and then
            # re-read the newest answer, so the watermark only ever advances.
            # (The zero-answer atomic refuse below also depends on this ordering:
            # a share that parks on the lock re-reads ``latest`` after the first
            # answer commits, so it mints a real token instead of raising.) The
            # SQLite adapter needs no lock —
            # ``database.write()`` there is a process-level write lock that
            # already serializes these. This holds exactly one row lock (no other
            # row is locked in this method), so it cannot form a lock cycle;
            # ``ensure_conversation`` already locks the conversation row the same
            # way.
            conv = db.execute(
                "SELECT id, shared_through_id FROM conversations "
                "WHERE id=%s AND notebook_id=%s FOR UPDATE",
                (conversation_id, notebook_id),
            ).fetchone()
            if conv is None:
                raise KeyError(conversation_id)
            if expected:
                boundary = db.execute(
                    "SELECT id, created_at FROM answers "
                    "WHERE id=%s AND conversation_id=%s",
                    (expected, conversation_id),
                ).fetchone()
                if boundary is None:
                    # The disclosed boundary answer was deleted — fail loudly so
                    # the caller re-reviews, never publish "latest" behind the
                    # user's back.
                    raise ConversationShareWatermarkStale(expected)
                through_at = boundary["created_at"]
                through_id = boundary["id"]
            else:
                latest = db.execute(
                    "SELECT id, created_at FROM answers WHERE conversation_id=%s "
                    + CONVERSATION_ANSWERS_ORDER_DESC + " LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                through_at = latest["created_at"] if latest is not None else None
                through_id = latest["id"] if latest is not None else None
            # "At least one written answer" (design doc §七 item 5), enforced
            # atomically inside this ``FOR UPDATE`` transaction (codex #522 R5): a
            # zero-answer conversation resolves no boundary (``through_id is
            # None``), so we raise BEFORE the UPDATE and never mint a token —
            # rolling back this transaction leaves the row untouched. A stale
            # ``expected_through_id`` is already handled above by the Stale raise,
            # so ``through_id is None`` here means only the zero-answer fallback.
            # This replaces the old "mint a NULL-watermark token, then compensate
            # in the route" path (which left a permanent token-without-watermark
            # row if the process died between the steps).
            if through_id is None:
                raise ConversationHasNoShareableAnswer(conversation_id)
            # Advance-only (codex #522 R3): reject a request whose boundary sorts
            # BEFORE the already-published one, evaluated in SQL over both answer
            # rows with the SAME ``(created_at, ordinal)`` keyset the public
            # snapshot uses (never a pure timestamp). A request boundary that does
            # not resolve (zero-answer fallback) or a current boundary whose answer
            # was deleted skips the check and advances; an equal boundary is an
            # idempotent no-op (short-circuited by ``current_id != through_id``).
            current_id = conv["shared_through_id"]
            if through_id and current_id and current_id != through_id:
                regresses = db.execute(
                    "SELECT 1 FROM answers r, answers c "
                    "WHERE r.id=%s AND c.id=%s AND r.conversation_id=%s "
                    "AND c.conversation_id=%s AND ("
                    "r.created_at < c.created_at "
                    "OR (r.created_at = c.created_at AND r.ordinal < c.ordinal))",
                    (through_id, current_id, conversation_id, conversation_id),
                ).fetchone()
                if regresses is not None:
                    raise ConversationShareWatermarkStale(expected or through_id)
            issued = db.execute(
                "UPDATE conversations SET share_token=COALESCE(share_token,%s), "
                "shared_through_at=%s, shared_through_id=%s "
                "WHERE id=%s AND notebook_id=%s "
                "RETURNING share_token, shared_through_at, shared_through_id",
                (candidate, through_at, through_id, conversation_id, notebook_id),
            ).fetchone()
        return {
            "share_token": str(issued["share_token"]),
            "shared_through_at": iso_timestamp(issued["shared_through_at"]),
            "shared_through_id": str(issued["shared_through_id"] or ""),
        }

    def unshare_conversation(self, notebook_id: str, conversation_id: str) -> None:
        """Revoke the public link. The next public request 404s, same as an
        unknown token (mirrors ``report_store.unshare_report``)."""
        with self.database.write() as db:
            db.execute(
                "UPDATE conversations SET share_token=NULL, "
                "shared_through_at=NULL, shared_through_id=NULL "
                "WHERE id=%s AND notebook_id=%s",
                (conversation_id, notebook_id),
            )

    def conversation_share_state(self, notebook_id: str, conversation_id: str) -> dict:
        """The issued token + watermark, for the write-guarded read-back
        endpoint only (mirrors ``report_store.report_share_token``).

        Never fold this into ``get_conversation``'s projection: that method
        is reachable with read permission, and ``share_token`` is an
        anonymous access grant.
        """
        with self.database.connect() as db:
            row = db.execute(
                "SELECT share_token, shared_through_at, shared_through_id "
                "FROM conversations WHERE id=%s AND notebook_id=%s",
                (conversation_id, notebook_id),
            ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return {
            "share_token": str(row["share_token"] or ""),
            "shared_through_at": iso_timestamp(row["shared_through_at"]),
            "shared_through_id": str(row["shared_through_id"] or ""),
        }

    def conversation_creator(
        self, notebook_id: str, conversation_id: str
    ) -> "str | None":
        """The conversation's ``created_by``, read scoped by BOTH ids — the
        single notebook-scoped row read behind the authenticated share
        endpoints' ownership gate (design doc §四 / T2).

        Returns the creator id when the conversation exists IN THIS NOTEBOOK,
        else ``None``. The creator may be the legacy empty string —
        ``conversations.created_by`` is ``DEFAULT ''`` — so the three states
        stay distinct: ``None`` (not in this notebook) / ``""`` (creatorless
        legacy row) / a real id. Filtering on ``notebook_id`` is load-bearing:
        a conversation id from another notebook must not resolve here, or the
        api-layer gate would let a cross-notebook id slip through (mirrors
        ``report_store.get_report``'s notebook-scoped read that
        ``_own_report_or_404`` relies on).

        NOT an owner-scoped read: it binds no request user, so the api layer
        applies the creator-equality and empty-creator (fail-closed) checks on
        top. Deliberately separate from ``conversation_share_state``: the gate
        must run before ``share_conversation`` issues a token, and it has
        nothing to do with the read-back-only share state.
        """
        with self.database.connect() as db:
            row = db.execute(
                "SELECT created_by FROM conversations "
                "WHERE id=%s AND notebook_id=%s",
                (conversation_id, notebook_id),
            ).fetchone()
        if row is None:
            return None
        return str(row["created_by"] or "")

    def public_conversation_by_token(self, token: str) -> "dict | None":
        """Resolve one shared conversation by token alone — the only
        session-free read (mirrors ``report_store.public_report_by_token``;
        see its docstring for why this must take nothing but the token: the
        caller is an anonymous router that never binds a request user, and
        any other identifier would let this method run as whichever user the
        ContextVar happens to default to).

        Turns are watermark-bounded to a clean prefix of the SAME canonical
        order ``get_conversation`` uses (``CONVERSATION_ANSWERS_ORDER_ASC`` —
        design doc C-3). The boundary is a KEYSET on the watermark answer's
        ``(created_at, ordinal)`` tuple, not a pure ``created_at`` interval
        (codex #522 R2 P2): the canonical order tie-breaks two answers at the
        same instant by ``ordinal`` (the identity column standing in for
        SQLite's rowid), so ``created_at <= shared_through_at`` alone would also
        pull in the tie-break-LATER answer at the watermark's exact instant — an
        answer that sorts AFTER the watermark and was never meant to be
        published. We resolve the watermark answer's ``(created_at, ordinal)``
        from ``shared_through_id`` and include exactly the rows whose
        ``(created_at, ordinal)`` tuple is ``<=`` the watermark's. An in-flight
        turn (question submitted, no answer row written yet) is excluded by
        construction: the predicate only ever matches committed ``answers``
        rows, never a running ``ask_jobs`` entry.

        When ``shared_through_id`` no longer resolves (the watermark answer was
        deleted after the share), the keyset has no anchor, so we fall back to
        the pure ``created_at <= shared_through_at`` interval — slightly less
        precise on a same-instant tie, but the design edge case must NOT fail
        closed and 404 an already-shared conversation.

        Returns ``None`` for an unknown/revoked token, or for a conversation
        whose watermark is NULL (``share_conversation`` always sets the
        watermark together with the token, so NULL here means this row was
        never actually shared through the normal path — fail closed rather
        than serve an ungated conversation).

        Also returns ``notebook_id`` and ``created_by`` (GATE fields,
        mirroring ``report_store.GATE_FIELDS``) for the caller's live
        authorization re-check (design doc §七 item 3) — they are NOT part
        of any future public disclosure surface and must be popped before
        anything crosses to an anonymous reader.
        """
        clean = str(token or "").strip()
        if not clean:
            return None
        with self.database.connect() as db:
            conv = db.execute(
                "SELECT id, notebook_id, created_by, title, created_at, "
                "shared_through_at, shared_through_id "
                "FROM conversations WHERE share_token=%s",
                (clean,),
            ).fetchone()
            if conv is None or conv["shared_through_at"] is None:
                return None
            watermark = None
            through_id = conv["shared_through_id"]
            if through_id:
                watermark = db.execute(
                    "SELECT created_at, ordinal FROM answers "
                    "WHERE id=%s AND conversation_id=%s",
                    (through_id, conv["id"]),
                ).fetchone()
            # ``LIMIT MAX_TURNS + 1`` (cap + 1) after the watermark keyset and
            # canonical ORDER BY — mirrors the SQLite adapter. It bounds the fetch
            # to exactly the projected prefix plus the one extra row the projection
            # needs to set ``truncated_turns``; without it a >MAX_TURNS conversation
            # forces every anonymous page read (and every image request) to load and
            # deserialize the whole conversation (codex #522 R6 P2). Never touches
            # the keyset/order, only caps the row count.
            if watermark is not None:
                # Keyset on (created_at, ordinal) <= the watermark row's — the
                # exact prefix ending at the watermark answer.
                rows = db.execute(
                    "SELECT id, question, payload, created_at FROM answers "
                    "WHERE conversation_id=%s AND ("
                    "created_at < %s OR (created_at = %s AND ordinal <= %s)"
                    ") " + CONVERSATION_ANSWERS_ORDER_ASC + " LIMIT %s",
                    (conv["id"], watermark["created_at"],
                     watermark["created_at"], watermark["ordinal"],
                     MAX_TURNS + 1),
                ).fetchall()
            else:
                # Watermark answer deleted: fall back to the pure created_at
                # interval rather than fail closed on the already-shared link.
                rows = db.execute(
                    "SELECT id, question, payload, created_at FROM answers "
                    "WHERE conversation_id=%s AND created_at <= %s "
                    + CONVERSATION_ANSWERS_ORDER_ASC + " LIMIT %s",
                    (conv["id"], conv["shared_through_at"], MAX_TURNS + 1),
                ).fetchall()
        turns = [
            {
                "answer_id": row["id"],
                "question": row["question"],
                "payload": json_value(row["payload"], {}),
                "created_at": iso_timestamp(row["created_at"]),
            }
            for row in rows
        ]
        return {
            "id": conv["id"],
            "notebook_id": conv["notebook_id"],
            "created_by": conv["created_by"],
            "title": conv["title"] or "",
            "created_at": iso_timestamp(conv["created_at"]),
            "shared_through_at": iso_timestamp(conv["shared_through_at"]),
            "shared_through_id": conv["shared_through_id"] or "",
            "turns": turns,
        }

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
