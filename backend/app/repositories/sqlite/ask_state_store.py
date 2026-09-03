"""Ask-domain durable state store (Task 22).

Owns the answers / conversations / ask_jobs / ask_trace_steps / feedback SQL:
the prepared-turn read-modify transaction, the durable job rows of the
running → done/failed/cancelled state machine (startup "interrupted" recovery
stays with the migrator), the append-only M1 trace sub-table, the answer
payload JSON and the conversation CRUD/history projections.

Composition rules (Gate 8):

* Identity is EXPLICIT — every owner-scoped method takes ``user_id`` from the
  caller; the store never reads the request ContextVar.  The
  ``ensure_conversation`` created-by semantics (a member passing someone
  else's conversation id gets a NEW conversation of their own, never an
  injected turn) are moved verbatim.
* ``ensure_conversation`` / ``conversation_history`` / ``read_trace`` take the
  CALLER's connection — the ask mode engines keep owning their write
  transaction on the facade until Task 24 moves them, so the facade ``_write``
  begin/commit traces stay byte-identical.  The self-transaction methods ride
  the ONE shared :class:`SqliteDatabase` boundary (the same object the facade
  ``_write``/``_connect`` compatibility seams delegate to), preserving the
  frozen streaming-ask commit boundaries: begin, trace, answer and finish are
  independent short transactions and an answer may exist before job finish
  after a crash (transaction_phases.json: streaming_ask).
* SQL text is moved verbatim — statement-matching failure injections keep
  binding.
* The raw store RAISES on trace persistence failure; the facade coordinator
  keeps the fail-open log-and-continue policy (error_policies.json:
  append_ask_trace).  Report persistence is deliberately NOT part of this
  store (it is its own Gate-8 domain).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator, List, Optional

from app.core.capability_tokens import new_capability_token
from app.core.internal_observability import (
    public_trace_steps,
    sanitize_answer_payload,
)
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
    AskRequestKeyConflict,
    ConversationBusyError,
    ConversationHasNoShareableAnswer,
    ConversationShareWatermarkStale,
    PreparedAskTurn,
    project_ask_row,
    project_report_attempt,
    project_report_row,
    project_run_row,
)
from app.repositories.sqlite.access_sql import (
    NOTEBOOK_LIVE_SQL,
    NOTEBOOK_READ_SQL,
    read_access_params,
)
from app.repositories.sqlite.database import SqliteDatabase
# Agentic Memory P3 (B-Profile, T7): the ONE place the question text coming
# back from ``recent_user_ask_languages`` is turned into a closed language
# bucket, before it leaves this store — see that method's own docstring.
from app.domain.search_profile import classify_ask_language
# The public projection's turn ceiling is the single source of truth for how many
# turns one anonymous page renders; the token-resolved query below bounds its
# fetch to ``MAX_TURNS + 1`` (cap + 1) so a >MAX_TURNS conversation cannot force
# every anonymous read to load and deserialize the WHOLE conversation's payloads
# (codex #522 R6 P2). The projection is a pure leaf module (stdlib-only imports),
# so importing this constant here creates no cycle and keeps the two in lockstep.
from app.domain.conversation_public_view import MAX_TURNS


# Canonical oldest -> newest order for one conversation's answers.
# `AskStateStore.get_conversation` and the public-share snapshot query
# (`public_conversation_by_token` below) MUST stay byte-identical here — the
# question-answer session sharing design doc's C-3 decision is that the
# public page's turn order can never diverge from what the author sees, and
# two independently-typed copies of this ORDER BY are exactly how that would
# silently drift. `julianday()` leads because it compares the absolute
# instant across legacy naive-UTC and newer offset-aware `created_at` text
# (same reasoning as the "Ask 会话即时入历史" SQLite offset-comparison red
# line); `rowid` — not `id` — is the final tie-break because within-tick
# answer order is insertion order, and rowid is SQLite's insertion-order
# column for this table.
CONVERSATION_ANSWERS_ORDER_ASC = (
    "ORDER BY julianday(created_at) ASC, created_at ASC, rowid ASC"
)
CONVERSATION_ANSWERS_ORDER_DESC = (
    "ORDER BY julianday(created_at) DESC, created_at DESC, rowid DESC"
)


class AskStateStore:
    def __init__(self, database: SqliteDatabase, seams) -> None:
        self.database = database
        self.seams = seams

    # ------------------------------------------------------------------
    # turn preparation
    # ------------------------------------------------------------------

    def ensure_conversation(
        self,
        db: sqlite3.Connection,
        notebook_id: str,
        conversation_id: Optional[str],
        question: str,
        user_id: str,
    ) -> str:
        """Return the conversation id for this turn: append to an existing
        conversation in this notebook (touching `updated_at`), or create a new
        one (id `conv-<hex>`, title from the first question).

        Raises ``KeyError(notebook_id)`` when the notebook is not live
        (``status IN ('copying','deleting')``) or has already been deleted —
        codex #659 R6 P2: ``conversations`` has no FK to ``notebooks`` (a
        deliberate closure-external table, see ``notebook_delete_tables.py``)
        and this write transaction never re-checked the notebook's lifecycle
        before this fix. Phase 3's one-time closure-external sweep only
        passes through ``conversations`` ONCE; a NEW row inserted after that
        sweep ran (Ask having already passed its route's capability guard
        before a delete job's tombstone landed) would survive phase 3 AND
        phase 5's finalize (which cascades only FK-having tables) — a
        permanent orphan with no future cleanup path. The INSERT below is
        therefore an ``INSERT ... SELECT ... WHERE EXISTS`` guarded by the
        SAME ``NOTEBOOK_LIVE_SQL`` every other lifecycle check in this
        codebase uses (single point of definition, both backends) — 0 rows
        inserted when the notebook is not live, detected via
        ``cursor.rowcount`` and mapped to the same ``KeyError`` shape every
        other "notebook not found" call site in this repo raises."""
        now = self.seams.now()
        if conversation_id:
            # 只接续**调用者自己**的对话:共享库里成员传入 owner/他人的 conv-id 不命中,
            # 落到下面新建一条归自己的对话,杜绝跨用户注入回合(read-only 成员经 ask 触达)。
            row = db.execute(
                "SELECT id FROM conversations WHERE id = ? AND notebook_id = ? AND created_by = ?",
                (conversation_id, notebook_id, user_id),
            ).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
                return conversation_id
        new_id = self.seams.new_id("conv")
        cursor = db.execute(
            "INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
            "SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS ("
            f"SELECT 1 FROM notebooks WHERE id = ? AND {NOTEBOOK_LIVE_SQL})",
            (new_id, notebook_id, question[:60], user_id, now, now, notebook_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(notebook_id)
        return new_id

    def conversation_history(
        self, db: sqlite3.Connection, conversation_id: str, limit: int = 5
    ) -> str:
        """Build the prior-turns history block (oldest->newest, last `limit`
        turns) from stored answer payloads. Uses each turn's `conclusion`
        (provenance markers already stripped). Returns "" when no prior turns."""
        rows = db.execute(
            "SELECT question, payload FROM answers WHERE conversation_id = ? "
            "ORDER BY julianday(created_at) ASC, created_at ASC, rowid ASC",
            (conversation_id,),
        ).fetchall()
        rows = rows[-limit:]
        lines = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
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
            self.database.begin_guarded_write(db)
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

        ``BEGIN IMMEDIATE`` makes the parent/status validation one guarded
        state transition.  Missing or terminal state returns ``None`` and can
        never fall through to ``ensure_conversation``'s compatibility create.
        """
        if not conversation_id:
            return None
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            parent = db.execute(
                "SELECT id FROM conversations WHERE id=? AND notebook_id=? "
                "AND created_by=?",
                (conversation_id, notebook_id, user_id),
            ).fetchone()
            if parent is None:
                return None
            running = db.execute(
                "SELECT 1 FROM ask_jobs WHERE id=? AND notebook_id=? "
                "AND conversation_id=? AND created_by=? AND status='running'",
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
        返回 (job_id, conversation_id)。cancel-event 注册留在 facade 编排。

        ALWAYS creates. ``payload.client_request_id`` is deliberately NOT
        persisted here (the row's key stays NULL): this is the non-idempotent
        begin the synchronous ``/ask`` route and compatibility callers use, and
        writing the key would make a repeated keyed call trip the unique index
        instead of keeping its always-create semantics. Only
        ``begin_or_attach_durable_job`` honours (and stores) the key."""
        question = payload.question.strip()
        now = self.seams.now()
        job_id = self.seams.new_id("askjob")
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            conversation_id = self.ensure_conversation(
                db, notebook_id, payload.conversation_id, question, user_id)
            payload.conversation_id = conversation_id
            self._insert_job_row(
                db, job_id, notebook_id, conversation_id, user_id, mode, payload, now,
                client_request_id=None)
        return job_id, conversation_id

    @staticmethod
    def _insert_job_row(
        db: sqlite3.Connection,
        job_id: str,
        notebook_id: str,
        conversation_id: str,
        user_id: str,
        mode: str,
        payload: AskRequest,
        now: str,
        *,
        client_request_id: "str | None",
    ) -> None:
        db.execute(
            "INSERT INTO ask_jobs (id,notebook_id,conversation_id,created_by,mode,question,"
            "asked_at,client_request_id,status,trace_json,answer_id,error,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?, 'running','','','',?,?)",
            (job_id, notebook_id, conversation_id, user_id, mode,
             payload.question.strip(), payload.asked_at, client_request_id,
             now, now))

    def find_job_for_client_request(
        self, user_id: str, client_request_id: str,
    ) -> "dict | None":
        """The job this user already created under ``client_request_id`` (any
        notebook), or ``None``. A read-only probe for the streaming route so a
        keyed retry can attach BEFORE any request-dependent validation runs;
        the authoritative lookup+insert stays in ``begin_or_attach_durable_job``."""
        with self.database.connect() as db:
            row = self._job_for_client_request(db, user_id, client_request_id)
        if row is None:
            return None
        return {"job_id": row["id"], "notebook_id": row["notebook_id"],
                "conversation_id": row["conversation_id"]}

    def begin_or_attach_durable_job(
        self,
        notebook_id: str,
        payload: AskRequest,
        mode: str,
        user_id: str,
    ) -> tuple[str, str, bool]:
        """``begin_durable_job`` with the submission's idempotency key honoured.

        ``payload.client_request_id`` names ONE browser submission. If this user
        already has a job under that key, no second job (and no second
        conversation) is created: the existing job's ids are returned with
        ``attached=True`` and ``payload.conversation_id`` is rewritten to its
        conversation, exactly as a fresh begin would. The lookup and the insert
        share one guarded write transaction: ``begin_guarded_write`` takes
        ``BEGIN IMMEDIATE`` (the cross-process writer fence) BEFORE the lookup,
        so no other process can land the same key between the two statements
        — unlike PostgreSQL, whose store needs a ``UniqueViolation`` fallback.
        The partial unique index ``idx_ask_jobs_client_request`` is parity with
        that backend and defense in depth, not the mechanism.

        A key already spent in ANOTHER notebook raises
        :class:`AskRequestKeyConflict` — see that class. Without a key this is
        ``begin_durable_job`` verbatim (``attached=False``)."""
        key = payload.client_request_id
        if not key:
            job_id, conversation_id = self.begin_durable_job(
                notebook_id, payload, mode, user_id)
            return job_id, conversation_id, False
        question = payload.question.strip()
        now = self.seams.now()
        job_id = self.seams.new_id("askjob")
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            existing = self._job_for_client_request(db, user_id, key)
            if existing is not None:
                return self._attach_existing(existing, notebook_id, payload, key)
            conversation_id = self.ensure_conversation(
                db, notebook_id, payload.conversation_id, question, user_id)
            payload.conversation_id = conversation_id
            self._insert_job_row(
                db, job_id, notebook_id, conversation_id, user_id, mode, payload, now,
                client_request_id=key)
        return job_id, conversation_id, False

    @staticmethod
    def _job_for_client_request(
        db: sqlite3.Connection, user_id: str, client_request_id: str,
    ) -> "sqlite3.Row | None":
        return db.execute(
            "SELECT id, notebook_id, conversation_id FROM ask_jobs "
            "WHERE created_by=? AND client_request_id=?",
            (user_id, client_request_id),
        ).fetchone()

    @staticmethod
    def _attach_existing(
        existing, notebook_id: str, payload: AskRequest, client_request_id: str,
    ) -> tuple[str, str, bool]:
        if existing["notebook_id"] != notebook_id:
            raise AskRequestKeyConflict(client_request_id)
        payload.conversation_id = existing["conversation_id"]
        return existing["id"], existing["conversation_id"], True

    def update_job_mode(self, job_id: str, mode: str) -> None:
        """Record the engine an automatic-mode job resolved to. The job row is
        begun under the request-only ``auto`` id so ``started`` can be delivered
        before engine selection; once selected, the resolved id replaces it so
        per-mode consumers (e.g. the reasoning-run experience sample) see the
        engine that actually answered. Only a still-running row is touched."""
        with self.database.write() as db:
            db.execute(
                "UPDATE ask_jobs SET mode=?, updated_at=? WHERE id=? AND status='running'",
                (mode, self.seams.now(), job_id),
            )

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
                "SELECT conversation_id,status FROM ask_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is not None and row["status"] == "running":
                db.execute(
                    "UPDATE ask_jobs SET status=?, answer_id=?, error=?, updated_at=? "
                    "WHERE id=? AND status='running'",
                    (status, answer_id, error, self.seams.now(), job_id),
                )
        return row["conversation_id"] if row is not None else None

    def cancel_running_job(self, job_id: str, user_id: str) -> dict:
        """Durably cancel a running owned job in one write transaction."""
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            row = db.execute(
                "SELECT conversation_id,status FROM ask_jobs "
                "WHERE id=? AND created_by=?",
                (job_id, user_id),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            cancelled = row["status"] == "running"
            if cancelled:
                db.execute(
                    "UPDATE ask_jobs SET status='cancelled',answer_id='',error='',updated_at=? "
                    "WHERE id=? AND status='running'",
                    (self.seams.now(), job_id),
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
            self.database.begin_guarded_write(db)
            conversation = db.execute(
                "SELECT id FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if conversation is None:
                return
            in_use = db.execute(
                "SELECT 1 WHERE EXISTS "
                "(SELECT 1 FROM answers WHERE conversation_id=?) OR EXISTS "
                "(SELECT 1 FROM ask_jobs WHERE conversation_id=? AND status='running')",
                (conversation_id, conversation_id),
            ).fetchone()
            if in_use is not None:
                return
            db.execute(
                "DELETE FROM conversations WHERE id=?", (conversation_id,)
            )

    def ask_job_status(self, job_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id,notebook_id,conversation_id,created_by,mode,status,answer_id,error "
                "FROM ask_jobs WHERE id=?", (job_id,)).fetchone()
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
        INSERT)。seq 用 `SELECT COALESCE(MAX(seq),-1)+1 WHERE job_id=?` 在同一个
        写事务里取号+插入,避免与自己的下一次 append 竞态(虽单 worker 写单个
        job、无写写竞态,取号+插同事务仍是稳妥做法)。job 行不存在 → no-op(基线守卫)。

        raw store 语义:持久化失败**上抛**;fail-open(记日志吞掉、绝不拖垮 ask)
        是 facade 协调层 append_ask_trace 的既有契约,不在本层。notebook_id /
        user_id 是冻结 port 签名的一部分(Task 24 的引擎调用会带真实值),本层
        SQL 只按 job_id 落行——与基线一致,不多查一行。"""
        with self.database.write() as db:
            exists = db.execute("SELECT 1 FROM ask_jobs WHERE id=?", (job_id,)).fetchone()
            if exists is None:
                return
            next_seq = db.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM ask_trace_steps WHERE job_id=?",
                (job_id,),
            ).fetchone()["n"]
            db.execute(
                "INSERT INTO ask_trace_steps (job_id, seq, step_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (job_id, next_seq, json.dumps(step, ensure_ascii=False), self.seams.now()),
            )

    @staticmethod
    def read_trace(db: sqlite3.Connection, job_id: str) -> list:
        """从 ask_trace_steps 子表按 seq 顺序读回一个 job 的完整轨迹,拼成 list。
        单行解析失败(损坏的 step_json)容错跳过而非整体失败——与旧版
        trace_json 列「解析失败即空列表」的粗粒度容错相比更细,但不改变
        「解析失败不抛」这条既有契约。取代直读 ask_jobs.trace_json 列
        (该列已停止写入,只为兼容旧行保留,见 append_trace)。"""
        rows = db.execute(
            "SELECT step_json FROM ask_trace_steps WHERE job_id=? ORDER BY seq ASC",
            (job_id,),
        ).fetchall()
        trace = []
        for r in rows:
            try:
                trace.append(json.loads(r["step_json"]))
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

        Agentic Memory P1 (T5). This is the only read in the repository whose
        ``user_id`` argument is a **privacy boundary** rather than an audit
        attribution: its result feeds that member's private overlay blocks, and
        a row belonging to someone else would put A's questions into a block
        the consolidation prompt then summarises — a leak with no error and no
        failing test anywhere.

        ⚠ Both statements carry ``created_by = ?`` **in the SQL text**, the
        trace statement included even though its ``job_id`` list already came
        out of the first one. That second predicate is redundant TODAY and
        deliberately kept: it costs nothing (``ask_jobs.id`` is the primary
        key), and it makes "these steps belong to this user" a property of the
        statement rather than of the two statements' relationship — which is
        what survives someone later changing how the job ids are chosen. The
        same rule, for the same reason, as ``memory_items.created_by``.
        ``backend/tests/test_agent_profile_isolation_guard.py`` pins it
        statically in both backends; a Python-side filter fails that guard on
        purpose.

        Bounded twice and independently: ``job_limit`` most-recent asks, and
        ``step_limit`` trace rows across all of them. One exhaustive reasoning
        ask can carry a hundred steps, so "N asks" alone is not a bound.
        ``ORDER BY j.created_at DESC, j.id DESC, t.seq ASC`` before the row cap
        makes the truncation deterministic AND biased the right way: it groups
        every job's steps together (major key is the job's own recency, not
        ``t.job_id`` — a job id is an opaque string with no relationship to
        when the job ran, so sorting by it directly discarded steps from
        whichever job happened to sort last lexicographically, not the oldest
        one), newest job first, each job's own steps still in ``seq`` order.
        When the 600-row ceiling bites, what falls off the LIMIT is therefore
        the OLDEST job's tail steps — never a step belonging to the ask the
        member just finished.
        """
        job_limit = max(1, int(job_limit))
        step_limit = max(1, int(step_limit))
        with self.database.connect() as db:
            job_rows = db.execute(
                "SELECT id, question, status, created_at FROM ask_jobs "
                "WHERE notebook_id = ? AND created_by = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (notebook_id, user_id, job_limit),
            ).fetchall()
            asks = [
                project_ask_row(
                    row["id"], row["question"], row["status"], row["created_at"]
                )
                for row in job_rows
            ]
            if not asks:
                return []
            by_job = {ask["job_id"]: ask for ask in asks}
            placeholders = ",".join("?" for _ in by_job)
            step_rows = db.execute(
                "SELECT t.job_id AS job_id, t.step_json AS step_json "
                "FROM ask_trace_steps t JOIN ask_jobs j ON j.id = t.job_id "
                f"WHERE j.notebook_id = ? AND j.created_by = ? AND t.job_id IN ({placeholders}) "
                "ORDER BY j.created_at DESC, j.id DESC, t.seq ASC LIMIT ?",
                (notebook_id, user_id, *by_job.keys(), step_limit),
            ).fetchall()
        for row in step_rows:
            step = project_trace_step(row["step_json"])
            target = by_job.get(str(row["job_id"]))
            if step is not None and target is not None:
                target["steps"].append(step)
        return asks

    def recent_completed_ask_runs(
        self, *, job_limit: int, step_limit: int
    ) -> list[dict]:
        """The deployment's most recent COMPLETED asks, projected and bounded —
        across every notebook and every user.

        Agentic Memory P2 (T5). See ``AskStateStorePort`` for the full
        contract; the two properties that must not drift are here:

        ⚠ There is NO ``created_by`` predicate and NO ``notebook_id``
        predicate, and neither is missing by accident. This read feeds the
        deployment-global retrieval-experience library, whose entries are
        statements about retrieval TACTICS ("in this shape of question, this
        action pays off") rather than about anyone's material. Narrowing it to
        one person would not make it safer, it would make it useless — and it
        is not what makes it safe. What makes it safe is
        ``project_run_row``/``project_run_step``: an opaque run id, a closed
        engine mode, and per step an action type, one count and one duration,
        plus a bools-and-small-ints situation from the ``intent`` step. The
        member's question, the step summaries, the notebook and the user id
        never leave this method.

        ⚠ It reads ONLY ``status = 'done'`` rows. A failed or cancelled ask
        stopped somewhere in the middle of its retrieval, so its action
        sequence is a record of an interruption rather than of a strategy, and
        counting its truncated tail as "these actions came back empty" would
        teach the library the opposite of what happened. Same gate, same
        reason, as the report sample's ``status = 'done'``.

        Bounded twice and independently, exactly like
        ``recent_user_ask_traces``: ``job_limit`` most-recent asks and
        ``step_limit`` trace rows across all of them, with the same
        ``ORDER BY j.created_at DESC, j.id DESC, t.seq ASC`` so the row cap
        drops the OLDEST run's tail steps rather than an arbitrary job's.
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
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (job_limit,),
            ).fetchall()
            runs = [project_run_row(row["id"], row["mode"]) for row in job_rows]
            if not runs:
                return []
            by_run = {run["run_id"]: run for run in runs}
            placeholders = ",".join("?" for _ in by_run)
            step_rows = db.execute(
                "SELECT t.job_id AS job_id, t.step_json AS step_json "
                "FROM ask_trace_steps t JOIN ask_jobs j ON j.id = t.job_id "
                f"WHERE t.job_id IN ({placeholders}) "
                "ORDER BY j.created_at DESC, j.id DESC, t.seq ASC LIMIT ?",
                (*by_run.keys(), step_limit),
            ).fetchall()
        for row in step_rows:
            step = project_run_step(row["step_json"])
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

        Agentic Memory P2 (T4). See ``AskStateStorePort.recent_user_report_
        traces`` for the full contract (attribution asymmetry, ``status``
        gate, the ``new == 0`` zero-hit reading). This method's SQL is the
        two properties that must not diverge from ``recent_user_ask_traces``:

        ⚠ Both statements carry ``created_by = ?`` **in the SQL text**, the
        attempt statement included even though its ``report_id`` list already
        came out of the first — same redundant-predicate rule, same reason
        (``backend/tests/test_agent_profile_isolation_guard.py`` pins it here
        too, via the now two-element ``TRACE_READ_METHODS``).

        ⚠ ``sections_json`` is projected IN THE SQL, not pulled into Python —
        one deep report's ``sections_json`` carries full section markdown and
        can run several hundred KB. The nested ``json_each`` walk extracts
        only ``$[*].attempted[*].{query,failed}``, defensively, so a
        malformed or legacy (pre-T4) row degrades to "no attempts" for that
        section rather than raising and losing the whole sample — the same
        tolerance ``read_trace``/``project_trace_step`` already have for a
        corrupt row shape.

        ⚠ The guards are ``json_each``'s own ``type`` COLUMN and nested
        ``CASE``, NOT ``json_type(x)`` on the element value, and not two
        predicates joined by ``AND``. Both alternatives raise (verified,
        SQLite 3.x): ``json_type('hello')`` on the string element of
        ``["hello"]`` is ``malformed JSON`` because ``json_type`` PARSES its
        argument, and ``json_type(a.value, '$.new')`` does the same for the
        string element of ``{"attempted": ["oops"]}``. ``AND`` does not save
        it either — SQL has no guaranteed evaluation order, so a guard
        written as ``json_valid(x) AND json_type(x) = 'array'`` can still
        evaluate the parsing half first. ``CASE`` does have a defined order
        (``WHEN``s in sequence, only the matching ``THEN``), and ``s.type`` /
        ``a.type`` are pre-computed columns that parse nothing at all. This
        matters because one malformed report poisons the WHOLE sample, not
        just its own row: the statement raises and this member's overlay
        refresh loses every report they have.

        ``ORDER BY r.updated_at DESC, r.id DESC, s.key ASC, a.key ASC`` before
        the ``attempt_limit`` cap mirrors the ask side's job-then-step
        ordering: the major key is the report's own recency (not
        ``s.key``/``a.key`` — pure array-index ordinals, meaningless across
        reports), so when the ceiling bites, what falls off is the OLDEST
        report's tail attempts, never a direction from the report the member
        just finished.

        ⚠ Recency is COMPLETION order — ``updated_at``, the terminal write of
        a ``done`` report — not ``created_at`` (codex #524 R15 P2): a report
        retried or slow-finished after ``report_limit`` newer ones were
        CREATED falls outside a creation-ordered window, and it is precisely
        the report whose completion just triggered this refresh. The
        PostgreSQL mirror orders the same way.
        """
        report_limit = max(1, int(report_limit))
        attempt_limit = max(1, int(attempt_limit))
        with self.database.connect() as db:
            report_rows = db.execute(
                "SELECT id, question, created_at FROM reports "
                "WHERE notebook_id = ? AND created_by = ? AND status = 'done' "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (notebook_id, user_id, report_limit),
            ).fetchall()
            reports = [
                project_report_row(row["id"], row["question"], row["created_at"])
                for row in report_rows
            ]
            if not reports:
                return []
            by_report = {report["report_id"]: report for report in reports}
            placeholders = ",".join("?" for _ in by_report)
            attempt_rows = db.execute(
                "SELECT r.id AS report_id, "
                "CASE WHEN a.type = 'object' "
                "THEN json_extract(a.value, '$.query') END AS query, "
                "CASE WHEN a.type = 'object' "
                "THEN json_extract(a.value, '$.failed') END AS failed "
                "FROM reports r "
                "JOIN json_each(CASE WHEN json_valid(r.sections_json) "
                "THEN (CASE WHEN json_type(r.sections_json) = 'array' "
                "THEN r.sections_json ELSE '[]' END) ELSE '[]' END) AS s "
                "JOIN json_each(CASE WHEN s.type = 'object' "
                "THEN (CASE WHEN json_type(s.value, '$.attempted') = 'array' "
                "THEN json_extract(s.value, '$.attempted') ELSE '[]' END) "
                "ELSE '[]' END) AS a "
                f"WHERE r.notebook_id = ? AND r.created_by = ? AND r.status = 'done' "
                f"AND r.id IN ({placeholders}) "
                "ORDER BY r.updated_at DESC, r.id DESC, s.key ASC, a.key ASC "
                "LIMIT ?",
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
        three-value language bucket (Agentic Memory P3, T7). See
        ``AskStateStorePort.recent_user_ask_languages`` for the full
        contract; the two properties that must not drift from it:

        ⚠ The question text NEVER leaves this method — it is read, classified
        by ``classify_ask_language`` and discarded in the same statement's
        result-row loop, so no caller outside this file can ever hold it.

        ⚠ ``created_by = ?`` is in the SQL text, unscoped by notebook (see the
        port docstring for why), and ``status = 'done'`` mirrors the two
        sibling reads above.
        """
        limit = max(1, int(limit))
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT question FROM ask_jobs "
                "WHERE created_by = ? AND status = 'done' "
                # codex #535 R10 P2:julianday 先行——created_at 文本序在 offset
                # 混排(DST/合库)下不是时间序,采样会漏掉更新的问题;与 PG 的
                # timestamptz 序对齐(「Ask 会话即时入历史」同款红线)。
                "ORDER BY julianday(created_at) DESC, id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [{"language": classify_ask_language(row["question"])} for row in rows]

    def ask_job_detail(self, job_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id,notebook_id,conversation_id,created_by,mode,question,status,"
                "answer_id,error,asked_at FROM ask_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            trace = self.read_trace(db, job_id)
            if db.execute(
                "SELECT 1 FROM notebooks WHERE id=?", (row["notebook_id"],)
            ).fetchone() is None:
                raise KeyError(job_id)
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
                "SELECT id, question, payload, created_at FROM answers WHERE id=?",
                (answer_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = sanitize_answer_payload(json.loads(row["payload"] or "{}"))
        except (TypeError, ValueError):
            payload = {}
        payload["answered_at"] = str(payload.get("answered_at") or row["created_at"] or "")
        return {
            "answer_id": row["id"],
            "question": row["question"],
            "payload": payload,
            "created_at": row["created_at"],
        }

    @contextmanager
    def guarded_ask_detail(
        self,
        job_id: str,
        *,
        actor_id: str,
        reader_id: str | None,
    ) -> Iterator[dict]:
        """Yield a detail snapshot while notebook deletion is excluded.

        ``BEGIN IMMEDIATE`` extends the database's cross-process writer fence
        to this otherwise read-only projection; the process-local write lock
        supplies the same ordering for sibling repository calls. The fence is
        held until the API has assembled its response object.
        """
        with self.database.write(operation="admin.ask_detail") as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,notebook_id,conversation_id,created_by,mode,question,status,"
                "answer_id,error,asked_at FROM ask_jobs WHERE id=? AND created_by=?",
                (job_id, actor_id),
            ).fetchone()
            if row is not None and db.execute(
                "SELECT 1 FROM notebooks WHERE id=?", (row["notebook_id"],)
            ).fetchone() is not None:
                if reader_id is not None and db.execute(
                    NOTEBOOK_READ_SQL,
                    (row["notebook_id"], *read_access_params(reader_id)),
                ).fetchone() is None:
                    raise KeyError(job_id)
                job = {
                    "job_id": row["id"],
                    "notebook_id": row["notebook_id"],
                    "conversation_id": row["conversation_id"],
                    "created_by": row["created_by"],
                    "mode": row["mode"],
                    "question": row["question"],
                    "status": row["status"],
                    "trace": self.read_trace(db, job_id),
                    "answer_id": row["answer_id"],
                    "error": row["error"],
                    "asked_at": row["asked_at"] or "",
                }
                answer_detail = None
                if row["answer_id"]:
                    answer_row = db.execute(
                        "SELECT id, question, payload, created_at FROM answers "
                        "WHERE id=?",
                        (row["answer_id"],),
                    ).fetchone()
                    if answer_row is not None:
                        try:
                            payload = sanitize_answer_payload(
                                json.loads(answer_row["payload"] or "{}")
                            )
                        except (TypeError, ValueError):
                            payload = {}
                        payload["answered_at"] = str(
                            payload.get("answered_at")
                            or answer_row["created_at"]
                            or ""
                        )
                        answer_detail = {
                            "answer_id": answer_row["id"],
                            "question": answer_row["question"],
                            "payload": payload,
                            "created_at": answer_row["created_at"],
                        }
                yield {"job": job, "answer_detail": answer_detail}
                return

            retained = db.execute(
                "SELECT record_id,notebook_id,actor_id,notebook_name,"
                "conversation_id,mode,question,status,asked_at,deleted_at,"
                "expires_at FROM retained_user_activity "
                "WHERE activity_type='ask' AND record_id=? "
                "AND actor_id=? "
                "AND julianday(expires_at)>julianday('now') "
                "AND NOT EXISTS(SELECT 1 FROM notebooks live "
                "WHERE live.id=retained_user_activity.notebook_id)",
                (job_id, actor_id),
            ).fetchone()
            if retained is None or reader_id is not None:
                raise KeyError(job_id)
            yield {
                "job": {
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
                    "notebook_deleted_at": retained["deleted_at"],
                    "retained_until": retained["expires_at"],
                },
                "answer_detail": None,
            }

    # ------------------------------------------------------------------
    # answers
    # ------------------------------------------------------------------

    def answer_notebook_id(self, answer_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT notebook_id FROM answers WHERE id=?", (answer_id,)
            ).fetchone()
        return row["notebook_id"] if row is not None else None

    def answer_memory_source(self, answer_id: str) -> dict:
        """Return the durable, server-owned Ask fields used by Memory capture."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT notebook_id,question,payload,conversation_id "
                "FROM answers WHERE id=?",
                (answer_id,),
            ).fetchone()
        if row is None:
            raise KeyError(answer_id)
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
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
        answers row in its own guarded write transaction. A non-null
        conversation is owner/notebook checked before insert because the
        legacy schema has no answers→conversations FK; ``None`` remains valid
        for server-owned answer snapshots used outside conversation history.
        The large-library ``index_required`` decoration stays a facade concern
        and must already be applied to ``response``."""
        answer_id = self.seams.new_id("ans")
        now = self.seams.now()
        response.answered_at = now
        payload = response.model_dump()
        payload["answer_id"] = answer_id
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            self._lock_answer_conversation_on(
                db, notebook_id, conversation_id, user_id
            )
            self._insert_answer_on(
                db, answer_id, notebook_id, conversation_id, question, payload, now
            )
        return answer_id

    @staticmethod
    def _lock_answer_conversation_on(
        db: sqlite3.Connection,
        notebook_id: str,
        conversation_id: Optional[str],
        user_id: str,
    ) -> None:
        if conversation_id is None:
            return
        row = db.execute(
            "SELECT id FROM conversations WHERE id=? AND notebook_id=? "
            "AND created_by=?",
            (conversation_id, notebook_id, user_id),
        ).fetchone()
        if row is None:
            raise KeyError(conversation_id)

    @staticmethod
    def _insert_answer_on(
        db: sqlite3.Connection,
        answer_id: str,
        notebook_id: str,
        conversation_id: Optional[str],
        question: str,
        payload: dict,
        now: str,
    ) -> None:
        db.execute(
            "INSERT INTO answers (id, notebook_id, question, payload, created_at, conversation_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                answer_id,
                notebook_id,
                question,
                json.dumps(payload, ensure_ascii=False),
                now,
                conversation_id,
            ),
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
        """Atomically save the final answer and move a running job to done.

        A durable cancel that wins the transaction ordering returns ``None``
        and leaves no answer row.
        """
        answer_id = self.seams.new_id("ans")
        now = self.seams.now()
        response.answered_at = now
        payload = response.model_dump()
        payload["answer_id"] = answer_id
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            row = db.execute(
                "SELECT status FROM ask_jobs WHERE id=? AND notebook_id=? "
                "AND conversation_id=? AND created_by=?",
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
                "UPDATE ask_jobs SET status='done',answer_id=?,error='',updated_at=? "
                "WHERE id=? AND status='running'",
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
                "SELECT id, notebook_id, title, updated_at FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conv is None:
                raise KeyError(conversation_id)
            rows = db.execute(
                "SELECT id, question, payload, created_at FROM answers "
                "WHERE conversation_id = ? " + CONVERSATION_ANSWERS_ORDER_ASC,
                (conversation_id,),
            ).fetchall()
            job = db.execute(
                "SELECT id, question, asked_at, mode FROM ask_jobs "
                "WHERE conversation_id=? AND status='running' "
                "ORDER BY julianday(created_at) DESC, created_at DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            job_trace = self.read_trace(db, job["id"]) if job is not None else []
        turns = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            # Pre-answered_at rows retain their authoritative completion time
            # in the answers table.  Project it into the response so old and
            # new conversations render identically without a migration.
            payload["answered_at"] = str(
                payload.get("answered_at") or row["created_at"] or ""
            )
            turns.append(
                ConversationTurn(
                    answer_id=row["id"],
                    question=row["question"],
                    response=AskResponse(**payload),
                    asked_at=str(payload.get("asked_at") or ""),
                    created_at=row["created_at"],
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
            updated_at=conv["updated_at"] or "",
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
                "(SELECT COALESCE(json_array_length(json_extract(a.payload, '$.reasoning_trace')), 0) > 0 "
                "   FROM answers a WHERE a.conversation_id = c.id "
                "  ORDER BY a.rowid DESC LIMIT 1) AS used_reasoning "
                "FROM conversations c WHERE c.notebook_id = ? AND c.created_by = ? "
                # julianday compares the absolute instant across legacy/local
                # UTC offsets; the raw ISO value preserves microsecond order
                # when SQLite's date function rounds two values to one tick.
                "ORDER BY julianday(c.updated_at) DESC, c.updated_at DESC, c.id DESC",
                (notebook_id, user_id),
            ).fetchall()
        return [
            ConversationSummary(
                id=row["id"],
                notebook_id=row["notebook_id"],
                title=row["title"] or "",
                updated_at=row["updated_at"] or "",
                turn_count=row["turn_count"],
                used_reasoning=bool(row["used_reasoning"]),
            )
            for row in rows
        ]

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        with self.database.write() as db:
            cur = db.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                (title, self.seams.now(), conversation_id),
            )
            if cur.rowcount == 0:
                raise KeyError(conversation_id)

    @staticmethod
    def _conversation_has_running_job_on(
        db: sqlite3.Connection, conversation_id: str
    ) -> bool:
        return db.execute(
            "SELECT 1 FROM ask_jobs WHERE conversation_id=? AND status='running'",
            (conversation_id,),
        ).fetchone() is not None

    @classmethod
    def _delete_idle_conversation_on(
        cls,
        db: sqlite3.Connection,
        conversation_id: str,
        *,
        refuse_running: bool,
    ) -> bool:
        """Guard and purge one conversation under SQLite's writer lease."""
        if cls._conversation_has_running_job_on(db, conversation_id):
            if refuse_running:
                raise ConversationBusyError()
            return False
        db.execute(
            "DELETE FROM answers WHERE conversation_id=?", (conversation_id,)
        )
        db.execute(
            "DELETE FROM ask_trace_steps WHERE job_id IN "
            "(SELECT id FROM ask_jobs WHERE conversation_id=? "
            "AND status<>'running')",
            (conversation_id,),
        )
        db.execute(
            "DELETE FROM ask_jobs WHERE conversation_id=? AND status<>'running'",
            (conversation_id,),
        )
        parent = db.execute(
            "DELETE FROM conversations WHERE id=? "
            "AND NOT EXISTS (SELECT 1 FROM answers a "
            "WHERE a.conversation_id=conversations.id) "
            "AND NOT EXISTS (SELECT 1 FROM ask_jobs j "
            "WHERE j.conversation_id=conversations.id)",
            (conversation_id,),
        )
        return parent.rowcount == 1

    def delete_conversation(self, conversation_id: str) -> None:
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            parent = db.execute(
                "SELECT id FROM conversations WHERE id=?", (conversation_id,)
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
        """Delete inactive conversations in one guarded write transaction.

        SQLite's writer lease gives parity with PostgreSQL parent-row leases;
        cutoff/ownership, answers and running jobs are still revalidated at
        deletion so both adapters expose the same durable lifecycle contract.
        """
        if older_than_days < 1:
            raise ValueError("older_than_days must be >= 1")
        # 用注入的仓库时钟(self.seams.now())而非真实 datetime.now():本 store 其余
        # 每处时间都走 seams.now(),唯独这里曾漏用真实钟。这会让「固定注入时钟」的
        # 用例在真实日期越过基准日后,把本应「新鲜」的会话也算成过期(见
        # test_content_store_conformance 里固定的 NOW)。生产里 seams.now() 即真实
        # 时间,行为不变;测试里则恢复确定性。
        cutoff = (
            datetime.fromisoformat(self.seams.now()) - timedelta(days=older_than_days)
        ).replace(microsecond=0).isoformat()
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            candidates = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM conversations "
                    "WHERE notebook_id=? AND created_by=? "
                    "AND julianday(updated_at)<julianday(?) "
                    "ORDER BY id",
                    (notebook_id, user_id, cutoff),
                ).fetchall()
            ]
            deleted_ids: list[str] = []
            for conversation_id in candidates:
                eligible = db.execute(
                    "SELECT 1 FROM conversations c WHERE c.id=? "
                    "AND c.notebook_id=? AND c.created_by=? "
                    "AND julianday(c.updated_at)<julianday(?) "
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
        ``report_store.share_report``). The WATERMARK is deliberately NOT
        idempotent — "share" and "update to latest" are the same call (see
        the design doc §四), so every invocation advances
        ``shared_through_at``/``shared_through_id`` to the answer the boundary
        resolves to, even when the token itself does not change.

        ``expected_through_id`` closes the disclosure TOCTOU (codex #522 R2 P1):
        it is the newest answer id the CLIENT saw in the same turns it computed
        its disclosure from. When given and it resolves to an answer OF THIS
        conversation, the watermark is pinned to EXACTLY that answer — even if a
        newer answer has since landed, that newer one is NOT published, because
        the user only reviewed (and consented to) up to here. When given but it
        no longer resolves (the answer was deleted), we ``raise
        ConversationShareWatermarkStale`` rather than silently fall back to the
        latest answer: the client's disclosure describes a snapshot that can no
        longer be reproduced, and publishing "latest" would bypass consent (the
        API layer maps the raise to a 409 that tells the user to reload). When
        omitted/empty (a legacy or no-body caller), the watermark falls back to
        the conversation's current latest answer, the historical behaviour.

        The watermark is ADVANCE-ONLY (codex #522 R3): once a share has pinned it
        to some answer, a later request whose boundary sorts BEFORE the published
        one — a stale browser tab, or a slow concurrent share — is rejected with
        ``ConversationShareWatermarkStale`` (→ 409) rather than allowed to REGRESS
        the link and unpublish turns another share already made public. An equal
        boundary is an idempotent no-op (re-sharing the same snapshot is normal);
        a strictly newer one advances as before. The regression is measured with
        the SAME canonical keyset the public snapshot uses — evaluated in SQL
        against both answer rows so it can never diverge from that order — so a
        same-instant tie-broken answer is compared by ``rowid``, not merged.

        Raises ``KeyError`` when the conversation does not exist in this
        notebook, and ``ConversationHasNoShareableAnswer`` when it has no
        committed answer to bound the snapshot — the "at least one written
        answer" policy (design doc §七 item 5) is enforced HERE, atomically in
        the same write transaction, so a zero-answer conversation never has a
        token minted (no more share-then-compensate rollback). The row-level
        ``created_by`` gate still belongs to the API/service layer that calls
        this method.
        """
        candidate = new_capability_token("cshr")
        expected = str(expected_through_id or "").strip()
        with self.database.write() as db:
            conv = db.execute(
                "SELECT id, shared_through_id FROM conversations "
                "WHERE id=? AND notebook_id=?",
                (conversation_id, notebook_id),
            ).fetchone()
            if conv is None:
                raise KeyError(conversation_id)
            if expected:
                boundary = db.execute(
                    "SELECT id, created_at FROM answers "
                    "WHERE id=? AND conversation_id=?",
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
                    "SELECT id, created_at FROM answers WHERE conversation_id=? "
                    + CONVERSATION_ANSWERS_ORDER_DESC + " LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                through_at = latest["created_at"] if latest is not None else None
                through_id = latest["id"] if latest is not None else None
            # "At least one written answer" (design doc §七 item 5), enforced
            # atomically INSIDE this write transaction (codex #522 R5): a
            # zero-answer conversation resolves no boundary (``through_id is
            # None``), so we raise BEFORE the UPDATE and never mint a token. An
            # ``expected_through_id`` that no longer resolves is handled above by
            # the Stale raise, so ``through_id is None`` here means only the
            # zero-answer fallback. This replaces the old "mint a NULL-watermark
            # token, then compensate in the route" path, which left a permanent
            # token-without-watermark row if the process died between the steps.
            if through_id is None:
                raise ConversationHasNoShareableAnswer(conversation_id)
            # Advance-only (codex #522 R3): reject a request whose boundary sorts
            # BEFORE the already-published one. ``current_id`` is the live
            # watermark; the comparison runs in SQL over both answer rows using
            # the SAME canonical keyset the public snapshot uses (rowid tie-break,
            # never a pure timestamp), so it can never diverge from that order. A
            # request boundary that does not resolve (zero-answer fallback) or a
            # current boundary that no longer resolves (its answer was deleted)
            # skips the check and advances — matching the deleted-watermark
            # fallback in ``public_conversation_by_token``. An equal boundary is an
            # idempotent no-op (short-circuited by ``current_id != through_id``),
            # not a regression.
            current_id = conv["shared_through_id"]
            if through_id and current_id and current_id != through_id:
                regresses = db.execute(
                    "SELECT 1 FROM answers r, answers c "
                    "WHERE r.id=? AND c.id=? AND r.conversation_id=? "
                    "AND c.conversation_id=? AND ("
                    "julianday(r.created_at) < julianday(c.created_at) "
                    "OR (julianday(r.created_at) = julianday(c.created_at) AND ("
                    "r.created_at < c.created_at "
                    "OR (r.created_at = c.created_at AND r.rowid < c.rowid))))",
                    (through_id, current_id, conversation_id, conversation_id),
                ).fetchone()
                if regresses is not None:
                    raise ConversationShareWatermarkStale(expected or through_id)
            issued = db.execute(
                "UPDATE conversations SET share_token=COALESCE(share_token,?), "
                "shared_through_at=?, shared_through_id=? "
                "WHERE id=? AND notebook_id=? "
                "RETURNING share_token, shared_through_at, shared_through_id",
                (candidate, through_at, through_id, conversation_id, notebook_id),
            ).fetchone()
        return {
            "share_token": str(issued["share_token"]),
            "shared_through_at": str(issued["shared_through_at"] or ""),
            "shared_through_id": str(issued["shared_through_id"] or ""),
        }

    def unshare_conversation(self, notebook_id: str, conversation_id: str) -> None:
        """Revoke the public link. The next public request 404s, same as an
        unknown token (mirrors ``report_store.unshare_report``)."""
        with self.database.write() as db:
            db.execute(
                "UPDATE conversations SET share_token=NULL, "
                "shared_through_at=NULL, shared_through_id=NULL "
                "WHERE id=? AND notebook_id=?",
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
                "FROM conversations WHERE id=? AND notebook_id=?",
                (conversation_id, notebook_id),
            ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return {
            "share_token": str(row["share_token"] or ""),
            "shared_through_at": str(row["shared_through_at"] or ""),
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
                "WHERE id=? AND notebook_id=?",
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
        full canonical sort tuple, not a pure timestamp interval (codex #522
        R2 P2): the canonical order tie-breaks two answers at the same instant
        by ``rowid`` (insertion order), so ``created_at <= shared_through_at``
        alone would also pull in the tie-break-LATER answer at the watermark's
        exact instant — an answer that sorts AFTER the watermark and was never
        meant to be published. We resolve the watermark answer's
        ``(created_at, rowid)`` from ``shared_through_id`` and include exactly
        the rows whose ``(julianday(created_at), created_at, rowid)`` tuple is
        ``<=`` the watermark's — the lexicographic prefix ending at the
        watermark row. (This is the keyset the T1 docstring said a correct fix
        would need — the stored ``shared_through_id`` is only the id, so the
        rowid must be looked up.) An in-flight turn (question submitted, no
        answer row written yet) is excluded by construction: the predicate only
        ever matches committed ``answers`` rows, never a running ``ask_jobs``
        entry.

        When ``shared_through_id`` no longer resolves (the watermark answer was
        deleted after the share), the keyset has no anchor, so we fall back to
        the pure ``julianday(created_at) <= julianday(shared_through_at)``
        interval — slightly less precise on a same-instant tie, but the design
        edge case must NOT fail closed and 404 an already-shared conversation.

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
                "FROM conversations WHERE share_token=?",
                (clean,),
            ).fetchone()
            if conv is None or not conv["shared_through_at"]:
                return None
            watermark = None
            through_id = conv["shared_through_id"]
            if through_id:
                watermark = db.execute(
                    "SELECT created_at, rowid AS rid FROM answers "
                    "WHERE id=? AND conversation_id=?",
                    (through_id, conv["id"]),
                ).fetchone()
            # ``LIMIT MAX_TURNS + 1`` (cap + 1) applied AFTER the watermark keyset
            # predicate and canonical ORDER BY: it bounds the fetch to exactly what
            # the projection renders (the oldest ``MAX_TURNS`` under the ASC order)
            # plus one extra row, which is precisely what the projection needs to
            # set ``truncated_turns`` (``len(turns) > MAX_TURNS``). Without it a
            # >MAX_TURNS conversation makes every anonymous page read — and every
            # image request, both routed through here — load and deserialize the
            # whole conversation's payloads (codex #522 R6 P2). Never touches the
            # keyset/order itself, only caps the row count.
            if watermark is not None:
                # Keyset on the full canonical sort tuple
                # (julianday(created_at), created_at, rowid) <= the watermark
                # row's — the exact prefix ending at the watermark answer.
                wm_at = watermark["created_at"]
                rows = db.execute(
                    "SELECT id, question, payload, created_at FROM answers "
                    "WHERE conversation_id=? AND ("
                    "julianday(created_at) < julianday(?) "
                    "OR (julianday(created_at) = julianday(?) AND ("
                    "created_at < ? OR (created_at = ? AND rowid <= ?)))"
                    ") " + CONVERSATION_ANSWERS_ORDER_ASC + " LIMIT ?",
                    (conv["id"], wm_at, wm_at, wm_at, wm_at, watermark["rid"],
                     MAX_TURNS + 1),
                ).fetchall()
            else:
                # Watermark answer deleted: fall back to the pure created_at
                # interval rather than fail closed on the already-shared link.
                rows = db.execute(
                    "SELECT id, question, payload, created_at FROM answers "
                    "WHERE conversation_id=? AND julianday(created_at) <= julianday(?) "
                    + CONVERSATION_ANSWERS_ORDER_ASC + " LIMIT ?",
                    (conv["id"], conv["shared_through_at"], MAX_TURNS + 1),
                ).fetchall()
        turns = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            turns.append(
                {
                    "answer_id": row["id"],
                    "question": row["question"],
                    "payload": payload,
                    "created_at": row["created_at"],
                }
            )
        return {
            "id": conv["id"],
            "notebook_id": conv["notebook_id"],
            "created_by": conv["created_by"],
            "title": conv["title"] or "",
            "created_at": conv["created_at"],
            "shared_through_at": conv["shared_through_at"],
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
        now = self.seams.now()
        feedback_id = self.seams.new_id("fb")
        with self.database.write() as db:
            answer = db.execute(
                "SELECT notebook_id FROM answers WHERE id = ?",
                (answer_id,),
            ).fetchone()
            if answer is None:
                raise KeyError(answer_id)
            db.execute(
                "INSERT INTO feedback (id, answer_id, notebook_id, rating, comment, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (feedback_id, answer_id, answer["notebook_id"], payload.rating, payload.comment, now),
            )
        return FeedbackResponse(
            id=feedback_id,
            answer_id=answer_id,
            rating=payload.rating,
            comment=payload.comment,
        )
