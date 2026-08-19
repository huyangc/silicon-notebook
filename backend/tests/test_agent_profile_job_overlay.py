"""Agentic Memory P1 (T5): the per-(notebook, member) OVERLAY chain.

This is the privacy-sensitive half of the feature, so what this file pins is
first and foremost the isolation — stated as behaviour, next to the static
guard that states it as structure:

1. **One member's activity moves only their own counter.** A's ask must not
   bump B's chain, and must not bump the SHARED base chain either (``''`` is
   the base's owner sentinel, so a missing identity would).
2. **One member's prompt contains only their own questions.** The strongest
   statement available at runtime: two members ask in the same notebook, the
   overlay runs for A, and B's question text appears nowhere in what the model
   was handed.
3. **A finished report reaches the threshold on its own** (design §5.3) —
   directly, without first parking the counter at the threshold.
4. **Losing access discards the overlay.** Removal is what turns "unreadable"
   into "gone".

Plus the run protocol T4 established and this chain must not diverge from:
every exit path settles (``BaseException`` included), a malformed reply keeps
the previous notes, ``settle`` consumes exactly the ``claim`` snapshot so
mid-run signals survive, and the event stays counts-only — never naming the
member it ran for.

Built on the stores plus a bare migrated ``SqliteDatabase`` (not the full
repository composition) for the same reason as ``test_agent_profile_job_base``:
what is under test is this service's protocol, not the ingestion stack.
"""
from __future__ import annotations

import itertools
import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import contextvars
import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, AskResponse
from app.repositories.ports import (
    AGENT_PROFILE_MALFORMED_MESSAGE,
    AGENT_PROFILE_MODEL_UNAVAILABLE_MESSAGE,
    AGENT_PROFILE_TRACE_SAMPLE,
)
from app.repositories.sqlite.agent_profile_store import AgentProfileStore
from app.repositories.sqlite.ask_state_store import AskStateStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.sqlite.query_store import QueryStore
from app.repositories.sqlite.source_store import SourceStore
from app.services import background_jobs
from app.services.agent_profile_job import (
    AGENT_PROFILE_WORKLOAD,
    BASE_CHAIN_OWNER,
    OVERLAY_LABELS,
    AgentProfileConsolidationService,
)
from app.services.ask_execution import AskCancellationRegistry, AskExecutionCoordinator
from app.services.ask_modes import ASK_MODES
from app.services.notebook_sharing import NotebookSharingService

NOW = "2026-08-18T00:00:00+00:00"
NOTEBOOK_ID = "nb-1"
USER_A = "user-a"
USER_B = "user-b"

A_QUESTION = "阿尔法项目的时序收敛怎么做"
B_QUESTION = "贝塔团队的预算表在哪一份文档里"


# --------------------------------------------------------------------- doubles
class _Client:
    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    settings = None  # no ``settings`` -> cap_kwargs stays off

    def chat_json(self, messages, schema_hint, **kwargs):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        return self.reply(prompt) if callable(self.reply) else self.reply


class _Models:
    def __init__(self, client: "_Client | None"):
        self.client = client
        self.chat_calls = 0

    def configured(self, workload_id: str) -> bool:
        assert workload_id == AGENT_PROFILE_WORKLOAD
        return self.client is not None

    def chat(self, workload_id: str):
        assert workload_id == AGENT_PROFILE_WORKLOAD
        assert self.client is not None
        self.chat_calls += 1
        return self.client


class _EventLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event, **_kwargs) -> None:
        self.events.append(event)


class _Submitter:
    """Records submissions instead of starting threads (synchronous by default)."""

    def __init__(self, *, run: bool = True, fail: "BaseException | None" = None):
        self.run = run
        self.fail = fail
        self.calls: list[dict] = []

    def __call__(self, fn, *args, name=None, notify_pending=False, **kwargs):
        self.calls.append({"name": name, "args": args, "notify_pending": notify_pending})
        if self.fail is not None:
            raise self.fail
        if self.run:
            fn(*args, **kwargs)
        return None


# -------------------------------------------------------------------- fixtures
@pytest.fixture
def harness(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_PROFILE_OVERLAY_TRIGGER", "3")
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    database = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(database, settings).migrate()

    with database.write() as db:
        for user_id in (USER_A, USER_B):
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                (user_id, f"{user_id}@example.test", user_id, "user", "active",
                 NOW, NOW),
            )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
            "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (NOTEBOOK_ID, "NB", "", "engineering", "ready", USER_A, NOW, NOW),
        )

    from app.repositories.sqlite import knowledge_counts_cache

    knowledge_counts_cache.invalidate(NOTEBOOK_ID)

    seams = SimpleNamespace(now=lambda: NOW, new_id=lambda prefix: f"{prefix}-1")

    # The profile store gets a COUNTING id seam rather than the constant one
    # the other seats share: it mints the chain's ``claim_token`` generation,
    # and a constant would make two successive claims indistinguishable —
    # i.e. it would reintroduce, inside the test harness, exactly the ABA the
    # token exists to close.
    claim_ids = itertools.count(1)

    return {
        "settings": settings,
        "database": database,
        "profiles": AgentProfileStore(
            database,
            new_id=lambda prefix: f"{prefix}-{next(claim_ids)}",
            now=seams.now,
        ),
        "sources": SourceStore(database, now=lambda: NOW),
        "queries": QueryStore(database, settings),
        "ask_state": AskStateStore(database, seams),
        "event_log": _EventLog(),
    }


def _run_overlay(service, claimed):
    """Run exactly the way ``start_overlay`` does: the claim's snapshot AND its
    generation token (Agentic Memory P2 — the token is what makes a stale
    worker's settle/write land on nothing)."""
    return service.run_overlay(
        NOTEBOOK_ID, USER_A, claimed.pending_signal, claimed.token
    )


def _service(harness, *, client: "_Client | None" = None, models=None):
    return AgentProfileConsolidationService(
        settings=harness["settings"],
        profiles=harness["profiles"],
        database=harness["database"],
        sources=harness["sources"],
        queries=harness["queries"],
        models=models if models is not None else _Models(client),
        event_log=harness["event_log"],
        ask_state=harness["ask_state"],
    )


def _with_submitter(monkeypatch, submitter):
    monkeypatch.setattr(background_jobs, "submit", submitter)
    return submitter


def _add_ask(
    harness,
    job_id: str,
    *,
    user_id: str,
    question: str,
    notebook_id: str = NOTEBOOK_ID,
    status: str = "done",
    created_at: str = NOW,
    steps: tuple[dict, ...] = (),
) -> None:
    """One persisted ask plus its trace rows, written directly.

    Direct SQL rather than ``begin_durable_job``: what these tests are about is
    the READ's predicate, and going through the writer would drag conversation
    lifecycle into a file about isolation.
    """
    with harness["database"].write() as db:
        db.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,"
            "question,status,trace_json,answer_id,error,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,'','','',?,?)",
            (job_id, notebook_id, f"conv-{job_id}", user_id, "reasoning",
             question, status, created_at, created_at),
        )
        for seq, step in enumerate(steps):
            db.execute(
                "INSERT INTO ask_trace_steps(job_id,seq,step_json,created_at) "
                "VALUES (?,?,?,?)",
                (job_id, seq, json.dumps(step, ensure_ascii=False), created_at),
            )


def _reply(**values) -> str:
    return json.dumps({
        "blocks": [
            {"label": label, "value": value} for label, value in values.items()
        ]
    })


def _blocks(harness, owner_id: str) -> dict[str, dict]:
    return {
        row["label"]: row
        for row in harness["profiles"].read_blocks(NOTEBOOK_ID, owner_id)
        if row["owner_id"] == owner_id
    }


def _job(harness, owner_id: str) -> dict:
    return harness["profiles"].job_row(NOTEBOOK_ID, owner_id) or {}


# =====================================================================
# 1. one member's activity moves only their own chain
# =====================================================================

def test_an_ask_advances_only_the_asking_member_s_counter(harness, monkeypatch):
    """A's ask must not touch B's chain — and must not touch the SHARED base.

    The base's owner is ``''``, so "the identity got lost somewhere" and "this
    signal belongs to everyone" are the same value. That is why an empty
    ``user_id`` is refused outright rather than defaulted.
    """
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))
    service = _service(harness)

    service.note_ask_completed(NOTEBOOK_ID, USER_A)
    service.note_ask_completed(NOTEBOOK_ID, USER_A)

    assert _job(harness, USER_A)["pending_signal"] == 2
    assert harness["profiles"].job_row(NOTEBOOK_ID, USER_B) is None
    assert harness["profiles"].job_row(NOTEBOOK_ID, BASE_CHAIN_OWNER) is None
    assert submitter.calls == []          # below the threshold: nothing scheduled


def test_a_missing_identity_never_bumps_the_shared_base(harness, monkeypatch):
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))
    service = _service(harness)

    service.note_ask_completed(NOTEBOOK_ID, "")

    assert harness["profiles"].job_row(NOTEBOOK_ID, BASE_CHAIN_OWNER) is None
    assert submitter.calls == []


def test_the_threshold_schedules_exactly_one_run_per_batch(harness, monkeypatch):
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))
    service = _service(harness)

    for _ in range(3):                                  # trigger == 3
        service.note_ask_completed(NOTEBOOK_ID, USER_A)

    assert len(submitter.calls) == 1
    # The worker is handed BOTH halves of the claim: the snapshot it may
    # consume, and the generation token that says which incarnation of the row
    # it holds (Agentic Memory P2).
    notebook, member, snapshot, token = submitter.calls[0]["args"]
    assert (notebook, member, snapshot) == (NOTEBOOK_ID, USER_A, 3)
    assert token == _job(harness, USER_A)["claim_token"] != ""
    assert submitter.calls[0]["notify_pending"] is False
    # ⚠ The job name must not name the member: thread names reach the queue
    # warning logs, and "whose searching is being consolidated" is exactly the
    # fact this feature keeps out of shared channels.
    assert USER_A not in submitter.calls[0]["name"]
    assert NOTEBOOK_ID in submitter.calls[0]["name"]
    # Claimed, so a fourth ask finds the slot taken rather than scheduling again.
    service.note_ask_completed(NOTEBOOK_ID, USER_A)
    assert len(submitter.calls) == 1
    assert _job(harness, USER_A)["status"] == "running"


# =====================================================================
# 2. one member's prompt contains only their own questions
# =====================================================================

def test_the_prompt_never_contains_another_member_s_question(harness, monkeypatch):
    """The isolation, stated as behaviour.

    Both members ask in the same notebook; the overlay consolidates A. B's
    question — and B's trace summaries — must appear nowhere in the prompt,
    and A's must.
    """
    _with_submitter(monkeypatch, _Submitter())
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "阿尔法时序检索得到 0 个候选节点",
         "detail": {"count": 0}, "duration_ms": 120},
    ))
    _add_ask(harness, "job-b", user_id=USER_B, question=B_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "贝塔预算表检索得到 0 个候选节点",
         "detail": {"count": 0}, "duration_ms": 90},
    ))
    client = _Client(_reply(retrieval_notes="按项目代号检索更容易命中"))
    service = _service(harness, client=client)

    assert service.start_overlay(NOTEBOOK_ID, USER_A) is True

    prompt = client.prompts[0]
    assert A_QUESTION in prompt
    assert "阿尔法时序检索得到 0 个候选节点" in prompt
    assert B_QUESTION not in prompt
    assert "贝塔预算表" not in prompt


def test_the_store_read_is_scoped_before_the_service_ever_sees_it(harness):
    """Same property one layer down: the SQL, not a Python filter, is what
    excludes the other member. ``test_agent_profile_isolation_guard.py`` pins
    that the predicate is written into the statement; this pins that it works.
    """
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "s", "detail": {"count": 1}},
    ))
    _add_ask(harness, "job-b", user_id=USER_B, question=B_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "s", "detail": {"count": 1}},
    ))

    rows = harness["ask_state"].recent_user_ask_traces(
        NOTEBOOK_ID, USER_A, job_limit=40, step_limit=600
    )

    assert [row["question"] for row in rows] == [A_QUESTION]
    assert [row["job_id"] for row in rows] == ["job-a"]
    assert len(rows[0]["steps"]) == 1


def test_the_read_is_bounded_by_both_asks_and_trace_rows(harness):
    for index in range(5):
        _add_ask(
            harness, f"job-{index}", user_id=USER_A, question=f"问题{index}",
            created_at=f"2026-08-1{index}T00:00:00+00:00",
            steps=tuple(
                {"step_type": "retrieve", "summary": f"s{n}", "detail": {"count": n}}
                for n in range(4)
            ),
        )

    rows = harness["ask_state"].recent_user_ask_traces(
        NOTEBOOK_ID, USER_A, job_limit=2, step_limit=3
    )

    # Newest first, and the step cap bites across asks rather than per ask.
    assert [row["question"] for row in rows] == ["问题4", "问题3"]
    assert sum(len(row["steps"]) for row in rows) == 3
    # T5 repair round (双评审): the 3-row cap must fall off the OLDEST job's
    # tail, not whichever job's id happens to sort last lexicographically.
    # "问题4" (newest) keeps all 3 of the steps the cap allows; "问题3"
    # (older) is truncated to nothing. Ordering by ``t.job_id ASC`` instead
    # of ``j.created_at DESC`` would have handed "问题3" (id "job-3" <
    # "job-4") the steps and starved the ask the member just finished.
    assert [len(row["steps"]) for row in rows] == [3, 0]


def test_the_projection_drops_everything_but_type_summary_duration_and_count(harness):
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "reflect", "summary": "继续", "duration_ms": 12,
         "detail": {"count": 3, "error": "boom: 内部异常正文",
                    "evidence": "证据原文不得外泄"}},
    ))

    step = harness["ask_state"].recent_user_ask_traces(
        NOTEBOOK_ID, USER_A, job_limit=5, step_limit=5
    )[0]["steps"][0]

    assert step == {"step_type": "reflect", "summary": "继续",
                    "duration_ms": 12, "count": 3}


# =====================================================================
# 3. a finished report reaches the threshold on its own
# =====================================================================

def test_a_finished_report_claims_directly_without_parking_the_counter(
    harness, monkeypatch
):
    """Design §5.3 + codex R7 P2:报告先落满阈值信号、再当场认领。

    bump 先行让任何交错都有人接(worker settle 后的复查看得见它,或本调用自己
    的 claim 把它连同快照一并消费);空闲链路上的净效果与旧的直接认领一致——
    快照含刚落的满阈值,settle 时恰好消费掉,不会停在阈值上等下一次提问点火。
    """
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))
    service = _service(harness)

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    assert len(submitter.calls) == 1
    # claim 的快照就是刚落的满阈值信号(3):它会随这次 run 的 settle 被消费。
    # 第四个参数是 P2 的 claim 代际。
    notebook, member, snapshot, token = submitter.calls[0]["args"]
    assert (notebook, member, snapshot) == (NOTEBOOK_ID, USER_A, 3)
    assert token == _job(harness, USER_A)["claim_token"] != ""
    assert _job(harness, USER_A)["status"] == "running"
    assert _job(harness, USER_A)["pending_signal"] == 3


def test_a_report_finishing_while_a_run_is_in_flight_keeps_the_ask_signal(
    harness, monkeypatch
):
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))
    service = _service(harness)
    service.note_ask_completed(NOTEBOOK_ID, USER_A)      # pending == 1
    assert service.start_overlay(NOTEBOOK_ID, USER_A) is True

    service.note_report_completed(NOTEBOOK_ID, USER_A)   # busy -> 落成满阈值信号

    # codex R6 P2:撞上在飞 run 的报告不再被丢弃——落成一个满阈值的 bump,
    # 由在飞 run 的终态重排消费(恰好一次,只是晚一点),既有 ask 信号照常保留。
    assert len(submitter.calls) == 1                      # 没有第二个并发 job
    assert _job(harness, USER_A)["pending_signal"] == 1 + 3   # ask 1 + 报告满阈 3


def test_the_report_engine_signals_the_report_author_and_never_the_request_user():
    """The engine passes ``self.user_id`` — the report's creator — explicitly.

    Reports run on a background thread where ``current_user()`` falls back to
    the seeded admin, so a ContextVar-derived owner would consolidate someone
    else's private notes from this report.
    """
    from app.services.report_engine import ReportEngine, ReportEngineDependencies

    calls: list[tuple] = []
    deps = ReportEngineDependencies(
        reports=None, retrieval=None, evidence_context=None, model_clients=None,
        model_errors=None, source_query=None, communities=None, settings=None,
        event_log=None,
        agent_profile_jobs=SimpleNamespace(
            note_report_completed=lambda nb, uid: calls.append((nb, uid))
        ),
    )
    engine = ReportEngine(deps, user_id=USER_B)

    engine._note_report_completed(NOTEBOOK_ID)

    assert calls == [(NOTEBOOK_ID, USER_B)]


def test_the_report_engine_swallows_a_failing_notification():
    from app.services.report_engine import ReportEngine, ReportEngineDependencies

    def _boom(_nb, _uid):
        raise RuntimeError("scheduler down")

    deps = ReportEngineDependencies(
        reports=None, retrieval=None, evidence_context=None, model_clients=None,
        model_errors=None, source_query=None, communities=None, settings=None,
        event_log=None,
        agent_profile_jobs=SimpleNamespace(note_report_completed=_boom),
    )

    # A stored, finished report must not be re-judged "failed" because a
    # background refresh could not be scheduled.
    ReportEngine(deps, user_id=USER_A)._note_report_completed(NOTEBOOK_ID)


# =====================================================================
# 4. losing access discards the overlay
# =====================================================================

def test_removing_a_member_discards_their_overlay_and_leaves_the_base(harness):
    profiles = harness["profiles"]
    profiles.write_block(
        NOTEBOOK_ID, USER_A, "retrieval_notes", value="A 的心得",
        evidence=[], expected_revision=0, origin="user", actor=USER_A,
    )
    profiles.write_block(
        NOTEBOOK_ID, USER_B, "retrieval_notes", value="B 的心得",
        evidence=[], expected_revision=0, origin="user", actor=USER_B,
    )
    profiles.write_block(
        NOTEBOOK_ID, BASE_CHAIN_OWNER, "corpus_shape", value="共享底座",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    # B also has a job/status row — the part of the overlay ``clear_all``
    # alone (block rows only) does NOT reach.
    profiles.bump_signal(NOTEBOOK_ID, USER_B, delta=2)
    removed: list[tuple] = []
    sharing = NotebookSharingService(
        store=SimpleNamespace(
            remove_member=lambda nb, uid: removed.append((nb, uid))
        ),
        copies=None, catalog=None, summaries=None, database=harness["database"],
        copy_stats=lambda _nb: {}, profiles=profiles,
    )

    sharing.remove_member(NOTEBOOK_ID, USER_B)

    assert removed == [(NOTEBOOK_ID, USER_B)]
    assert _blocks(harness, USER_B) == {}
    assert "retrieval_notes" in _blocks(harness, USER_A)      # untouched
    assert "corpus_shape" in _blocks(harness, BASE_CHAIN_OWNER)
    # T5 repair round: the job row (and its pending_signal counter) is gone
    # too, not just the blocks.
    assert profiles.job_row(NOTEBOOK_ID, USER_B) is None


def test_a_removed_and_rejoined_member_starts_their_counter_at_zero(harness):
    """T5 repair round: rejoining is a blank slate for BOTH halves of the
    overlay. Without clearing the job row, a member removed mid-batch and
    re-added would come back with a ``pending_signal`` already partway to the
    next consolidation run — counting activity from before they even had
    access again."""
    profiles = harness["profiles"]
    profiles.bump_signal(NOTEBOOK_ID, USER_B, delta=2)
    assert profiles.job_row(NOTEBOOK_ID, USER_B)["pending_signal"] == 2
    sharing = NotebookSharingService(
        store=SimpleNamespace(
            remove_member=lambda nb, uid: None,
            add_member=lambda nb, uid: None,
        ),
        copies=None, catalog=None, summaries=None, database=harness["database"],
        copy_stats=lambda _nb: {}, profiles=profiles,
    )

    sharing.remove_member(NOTEBOOK_ID, USER_B)
    assert profiles.job_row(NOTEBOOK_ID, USER_B) is None
    sharing.add_member(NOTEBOOK_ID, USER_B)
    # Re-added: no row exists until this member's activity bumps one again,
    # and when it does, it starts from zero — not from where it left off.
    assert profiles.job_row(NOTEBOOK_ID, USER_B) is None
    assert profiles.bump_signal(NOTEBOOK_ID, USER_B, delta=1) == 1


def test_a_failing_overlay_cleanup_never_blocks_the_access_change():
    """Membership first, cleanup second: an access change must not depend on a
    cleanup succeeding, and the read-side gate already covers leftover rows.
    Both halves of the cleanup (blocks AND the job row) are independently
    fail-open — one raising must not stop the other or the access change."""
    removed: list[tuple] = []
    cleared: list[str] = []
    sharing = NotebookSharingService(
        store=SimpleNamespace(
            remove_member=lambda nb, uid: removed.append((nb, uid))
        ),
        copies=None, catalog=None, summaries=None, database=None,
        copy_stats=lambda _nb: {},
        profiles=SimpleNamespace(
            clear_all=lambda _nb, _uid: (_ for _ in ()).throw(RuntimeError("db down")),
            clear_job_row=lambda nb, uid: cleared.append((nb, uid)),
        ),
    )

    sharing.remove_member(NOTEBOOK_ID, USER_B)

    assert removed == [(NOTEBOOK_ID, USER_B)]
    # The job-row clear still ran despite the block-row clear raising.
    assert cleared == [(NOTEBOOK_ID, USER_B)]


def test_a_failing_job_row_cleanup_never_blocks_the_access_change():
    """The reverse of the previous test: ``clear_job_row`` raising must not
    stop membership removal either, and must not prevent ``clear_all`` from
    having already run."""
    removed: list[tuple] = []
    cleared: list[str] = []
    sharing = NotebookSharingService(
        store=SimpleNamespace(
            remove_member=lambda nb, uid: removed.append((nb, uid))
        ),
        copies=None, catalog=None, summaries=None, database=None,
        copy_stats=lambda _nb: {},
        profiles=SimpleNamespace(
            clear_all=lambda nb, uid: cleared.append((nb, uid)),
            clear_job_row=lambda _nb, _uid: (_ for _ in ()).throw(RuntimeError("db down")),
        ),
    )

    sharing.remove_member(NOTEBOOK_ID, USER_B)

    assert removed == [(NOTEBOOK_ID, USER_B)]
    assert cleared == [(NOTEBOOK_ID, USER_B)]


# =====================================================================
# 5. the run protocol (terminal settle, fail-open, consumed semantics)
# =====================================================================

def test_a_successful_run_writes_the_member_s_own_blocks(harness, monkeypatch):
    _with_submitter(monkeypatch, _Submitter())
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "初检索得到 0 个候选节点",
         "detail": {"count": 0}},
    ))
    client = _Client(_reply(
        retrieval_notes="用项目代号做关键词更容易命中",
        usage_gaps="反复找预算类材料但库里没有",
    ))
    service = _service(harness, client=client)

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    blocks = _blocks(harness, USER_A)
    assert set(blocks) == set(OVERLAY_LABELS)
    assert blocks["retrieval_notes"]["value"] == "用项目代号做关键词更容易命中"
    assert _job(harness, USER_A)["status"] == "done"
    assert _job(harness, USER_A)["blocks_written"] == 2
    # Nothing was written into the shared base.
    assert _blocks(harness, BASE_CHAIN_OWNER) == {}


def test_usage_gaps_evidence_is_the_server_s_own_zero_hit_count(harness, monkeypatch):
    """Design §5.1's documented exception: the overlay's evidence is a count of
    empty retrievals, computed here from the same sample the prompt rendered —
    never a number the model restated, and never a source id list."""
    _with_submitter(monkeypatch, _Submitter())
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "空一", "detail": {"count": 0}},
        {"step_type": "retrieve", "summary": "空二", "detail": {"count": 0}},
        {"step_type": "retrieve", "summary": "有", "detail": {"count": 5}},
        # ⚠ ``reflect`` also carries counts, and zero there means "no new
        # sub-queries", not "found nothing". Counting it would inflate the one
        # number this block is grounded in.
        {"step_type": "reflect", "summary": "无新查询", "detail": {"count": 0}},
    ))
    service = _service(harness, client=_Client(_reply(
        retrieval_notes="n", usage_gaps="g",
    )))

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    blocks = _blocks(harness, USER_A)
    assert blocks["usage_gaps"]["evidence"] == [
        {"claim_index": 0, "zero_hit_queries": 2}
    ]
    # retrieval_notes is about how searching went; no document is its source.
    assert blocks["retrieval_notes"]["evidence"] == []


def test_zero_hit_counting_matches_real_trace_step_shapes(harness, monkeypatch):
    """Step-detail fixtures here are the ACTUAL shapes ``reasoning_retrieval.py``
    emits, not a simplified ``{"count": n}`` stand-in — the T5 repair round
    found ``_TRACE_COUNT_KEYS``/``_ZERO_HIT_STEP_TYPES`` had drifted from the
    real emitters (missing ``found``/``returned_total`` entirely, and reading
    ``retrieve``'s follow-up ``"new"`` key as though it meant zero hits).

    - ``exact_lookup`` with ``{"found": 0, ...}`` (the "按名称精确查找" record
      site) is a genuine empty search and MUST count.
    - ``retrieve`` with ``{"query": ..., "new": 0}`` (the "补充子查询"/
      "补充已确认方向" record sites) means "nothing NEW beyond what this run
      already had", not "found nothing", and MUST NOT count.
    """
    _with_submitter(monkeypatch, _Submitter())
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "exact_lookup", "summary": "按名称精确查找:新增 0 段原文",
         "detail": {"terms": ["set_db"], "found": 0, "phase": "seed"}},
        {"step_type": "retrieve", "summary": "补充子查询: 已有候选",
         "detail": {"query": "已有候选", "new": 0}},
    ))
    service = _service(harness, client=_Client(_reply(
        retrieval_notes="n", usage_gaps="g",
    )))

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    blocks = _blocks(harness, USER_A)
    assert blocks["usage_gaps"]["evidence"] == [
        {"claim_index": 0, "zero_hit_queries": 1}
    ]


def test_an_empty_sample_settles_done_without_paying_for_a_call(harness, monkeypatch):
    _with_submitter(monkeypatch, _Submitter())
    models = _Models(_Client(_reply(retrieval_notes="不该被写出来")))
    service = _service(harness, models=models)

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    assert models.chat_calls == 0
    assert _job(harness, USER_A)["status"] == "done"
    assert _blocks(harness, USER_A) == {}


@pytest.mark.parametrize("reply", [
    "not json at all",
    json.dumps({"blocks": "nope"}),
    json.dumps({"blocks": [{"label": "corpus_shape", "value": "抢底座的标签"}]}),
    json.dumps({"blocks": [{"label": "retrieval_notes", "value": {"t": 1}}]}),
])
def test_an_unusable_reply_keeps_the_previous_notes(harness, monkeypatch, reply):
    _with_submitter(monkeypatch, _Submitter())
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "s", "detail": {"count": 1}},
    ))
    harness["profiles"].write_block(
        NOTEBOOK_ID, USER_A, "retrieval_notes", value="原有心得",
        evidence=[], expected_revision=0, origin="user", actor=USER_A,
    )
    service = _service(harness, client=_Client(reply))

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    assert _blocks(harness, USER_A)["retrieval_notes"]["value"] == "原有心得"
    job = _job(harness, USER_A)
    assert job["status"] == "failed"
    assert job["failure_reason"] == AGENT_PROFILE_MALFORMED_MESSAGE
    # A base label in an overlay reply is rejected, so the base stays empty.
    assert _blocks(harness, BASE_CHAIN_OWNER) == {}


def test_an_unconfigured_model_settles_failed_with_a_showable_reason(
    harness, monkeypatch
):
    _with_submitter(monkeypatch, _Submitter())
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION)
    service = _service(harness, client=None)

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    assert _job(harness, USER_A)["failure_reason"] == (
        AGENT_PROFILE_MODEL_UNAVAILABLE_MESSAGE
    )


def test_a_base_exception_still_settles_the_row(harness, monkeypatch):
    """``KeyboardInterrupt``/``SystemExit`` sail past ``except Exception``, and
    a row left ``running`` holds this member's chain until the next restart —
    every later trigger silently no-ops against it."""
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION)

    def _interrupt(_prompt):
        raise KeyboardInterrupt()

    service = _service(harness, client=_Client(_interrupt))
    claimed = service.profiles.claim(NOTEBOOK_ID, USER_A)
    assert claimed.pending_signal == 0

    with pytest.raises(KeyboardInterrupt):
        _run_overlay(service, claimed)

    assert _job(harness, USER_A)["status"] == "failed"


def test_a_submit_failure_releases_the_claim(harness, monkeypatch):
    _with_submitter(monkeypatch, _Submitter(fail=RuntimeError("no thread")))
    service = _service(harness)

    with pytest.raises(RuntimeError):
        service.start_overlay(NOTEBOOK_ID, USER_A)

    assert _job(harness, USER_A)["status"] == "failed"
    # Nothing ran, so nothing was consumed.
    assert _job(harness, USER_A)["pending_signal"] == 0


def test_settle_consumes_exactly_the_claim_snapshot(harness, monkeypatch):
    """Signals that arrive WHILE a run is in flight must survive it."""
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "s", "detail": {"count": 1}},
    ))
    service = _service(harness, client=_Client(_reply(retrieval_notes="n")))
    for _ in range(3):
        harness["profiles"].bump_signal(NOTEBOOK_ID, USER_A)
    claimed = harness["profiles"].claim(NOTEBOOK_ID, USER_A)
    assert claimed.pending_signal == 3
    harness["profiles"].bump_signal(NOTEBOOK_ID, USER_A)   # arrives mid-run

    _run_overlay(service, claimed)

    assert _job(harness, USER_A)["pending_signal"] == 1


def test_a_member_edit_during_the_run_wins_and_is_not_retried(harness, monkeypatch):
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "s", "detail": {"count": 1}},
    ))
    profiles = harness["profiles"]
    profiles.write_block(
        NOTEBOOK_ID, USER_A, "retrieval_notes", value="旧值",
        evidence=[], expected_revision=0, origin="job", actor="",
    )

    def _edit_then_reply(_prompt):
        # The member edits while the model is answering: the run's CAS is now
        # stale, and their text must stand.
        profiles.write_block(
            NOTEBOOK_ID, USER_A, "retrieval_notes", value="我自己写的",
            evidence=[], expected_revision=1, origin="user", actor=USER_A,
        )
        return _reply(retrieval_notes="模型算出来的")

    service = _service(harness, client=_Client(_edit_then_reply))
    claimed = profiles.claim(NOTEBOOK_ID, USER_A)
    _run_overlay(service, claimed)

    assert _blocks(harness, USER_A)["retrieval_notes"]["value"] == "我自己写的"
    assert "cas_conflict:retrieval_notes" in _job(harness, USER_A)["diagnostic"]


def _retire_reply(*labels: str) -> str:
    return json.dumps({"blocks": [{"label": label, "retire": True} for label in labels]})


def test_the_member_s_own_stale_note_can_be_retired(harness, monkeypatch):
    """codex #520 R2 P2 的覆盖层一侧:同一条退役协议,同一条边界。

    这里的失败形态与底座不同但同样安静——一条早就不像这个人现在搜法的旧心得,会
    在他之后每一次提问的规划 prompt 里继续把检索往错的方向带。
    """
    _with_submitter(monkeypatch, _Submitter())
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "s", "detail": {"count": 1}},
    ))
    harness["profiles"].write_block(
        NOTEBOOK_ID, USER_A, "retrieval_notes", value="上一轮模型写的旧心得",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    service = _service(harness, client=_Client(_retire_reply("retrieval_notes")))

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    block = _blocks(harness, USER_A)["retrieval_notes"]
    assert block["value"] == ""
    assert block["updated_origin"] == "job"
    assert "retired:retrieval_notes" in _job(harness, USER_A)["diagnostic"]


def test_the_member_s_own_handwritten_note_is_never_retired(harness, monkeypatch):
    """人自己写下的检索心得,被撤掉的正是那个人。"""
    _with_submitter(monkeypatch, _Submitter())
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "s", "detail": {"count": 1}},
    ))
    harness["profiles"].write_block(
        NOTEBOOK_ID, USER_A, "retrieval_notes", value="我更常按型号搜",
        evidence=[], expected_revision=0, origin="user", actor=USER_A,
    )
    service = _service(harness, client=_Client(_retire_reply("retrieval_notes")))

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    block = _blocks(harness, USER_A)["retrieval_notes"]
    assert block["value"] == "我更常按型号搜"
    assert block["revision"] == 1, "用户那一行根本不该被写"
    assert "retire_refused:retrieval_notes" in _job(harness, USER_A)["diagnostic"]


def test_a_user_authored_note_reaches_the_prompt_marked_as_authority(
    harness, monkeypatch
):
    _with_submitter(monkeypatch, _Submitter())
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "s", "detail": {"count": 1}},
    ))
    harness["profiles"].write_block(
        NOTEBOOK_ID, USER_A, "retrieval_notes", value="我更常按型号搜",
        evidence=[], expected_revision=0, origin="user", actor=USER_A,
    )
    client = _Client(_reply(usage_gaps="g"))
    service = _service(harness, client=client)

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    assert "retrieval_notes (user-authored): 我更常按型号搜" in client.prompts[0]


def test_the_base_layer_never_leaks_into_the_overlay_prompt(harness, monkeypatch):
    """``read_blocks`` spans ``owner_id IN ('', ?)``, so the base's own text
    comes back with the overlay's. Rendering it as "your note" would have the
    model rewrite the library's shared description into a private block."""
    _with_submitter(monkeypatch, _Submitter())
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "s", "detail": {"count": 1}},
    ))
    harness["profiles"].write_block(
        NOTEBOOK_ID, BASE_CHAIN_OWNER, "corpus_shape", value="底座描述这个库",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    client = _Client(_reply(retrieval_notes="n"))

    _service(harness, client=client).note_report_completed(NOTEBOOK_ID, USER_A)

    assert "底座描述这个库" not in client.prompts[0]


def test_the_event_is_counts_only_and_names_the_overlay_chain(harness, monkeypatch):
    _with_submitter(monkeypatch, _Submitter())
    _add_ask(harness, "job-a", user_id=USER_A, question=A_QUESTION, steps=(
        {"step_type": "retrieve", "summary": "s", "detail": {"count": 1}},
    ))
    _service(harness, client=_Client(_reply(retrieval_notes="心得正文"))).\
        note_report_completed(NOTEBOOK_ID, USER_A)

    event = harness["event_log"].events[-1]
    assert event["kind"] == "agent_profile_consolidated"
    assert event["chain"] == "overlay"
    assert set(event) == {
        "kind", "chain", "notebook_id", "status", "blocks", "chars",
        "evidence", "latency_ms",
    }
    serialized = json.dumps(event, ensure_ascii=False)
    assert USER_A not in serialized          # which member never leaves the process
    assert "心得正文" not in serialized
    assert A_QUESTION not in serialized


def test_the_trigger_is_fail_open_when_the_store_is_down(harness, monkeypatch):
    service = _service(harness)
    monkeypatch.setattr(
        service.profiles, "bump_signal",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    service.note_ask_completed(NOTEBOOK_ID, USER_A)      # must not raise


def test_the_kill_switch_stops_the_chain_before_any_write(harness, monkeypatch):
    monkeypatch.setenv("AGENT_PROFILE_ENABLED", "false")
    harness["settings"] = Settings(
        database_url=harness["settings"].database_url,
    )
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))
    service = _service(harness)

    service.note_ask_completed(NOTEBOOK_ID, USER_A)
    service.note_report_completed(NOTEBOOK_ID, USER_A)

    assert harness["profiles"].job_row(NOTEBOOK_ID, USER_A) is None
    assert submitter.calls == []


# =====================================================================
# 6. the Ask trigger's wiring
# =====================================================================

class _RecordingAskState:
    def __init__(self, calls: list):
        self.calls = calls

    def begin_durable_job(self, notebook_id, payload, mode, user_id):
        payload.conversation_id = "conv-t5"
        return "askjob-t5", "conv-t5"

    def append_trace(self, notebook_id, job_id, step, user_id):
        pass

    def finish_job(self, job_id, status, *, answer_id="", error=""):
        self.calls.append(("finish", status))
        return "conv-t5"

    def cleanup_empty_conversation(self, conversation_id):
        pass


class _InlineSubmitter:
    def submit(self, fn, *args, name=None, notify_pending=False, **kwargs):
        contextvars.copy_context().run(fn, *args, **kwargs)
        return threading.current_thread()


def _ask_coordinator(calls, *, runner, noted):
    service = SimpleNamespace(
        ask=lambda notebook_id, payload, *, user_id, job_id="", on_trace=None,
        cancel_event=None: runner(),
    )
    return AskExecutionCoordinator(
        ask_state=_RecordingAskState(calls),
        cancellations=AskCancellationRegistry(),
        job_submitter=_InlineSubmitter(),
        event_log=SimpleNamespace(logger=SimpleNamespace(exception=lambda *a: None)),
        ask=lambda: service,
        note_ask_completed=noted,
    )


def _drain(events: "queue.Queue") -> list:
    out = []
    while True:
        item = events.get(timeout=2.0)
        if item is None:
            return out
        out.append(item)


def _response() -> AskResponse:
    return AskResponse(answer_id="ans-t5", conversation_id="conv-t5",
                       conclusion="", answer="a", grounded=True, anchors=[],
                       related_knowledge=[], citations=[], llm_mode="x")


def test_a_completed_ask_signals_the_asking_member_after_the_terminal_row(
    monkeypatch,
):
    """T5 repair round (双评审): the note must fire AFTER the terminal job row
    AND after the ``final`` event is queued for the browser — never before
    the answer has actually been delivered. A three-element sequence rather
    than the previous two: ``_note_ask_completed`` used to run before
    ``events.put({"event": "final", ...})``, which meant a slow or
    exception-prone overlay notification could delay (or, if it somehow
    raised past its own fail-open guard, corrupt) the terminal event the
    browser is waiting on. Pinning ``queue.Queue.put`` lets this test see the
    real ordering without inspecting engine internals.
    """
    import app.services.ask_execution as ask_execution_module

    calls: list = []
    noted: list = []

    class _RecordingQueue(queue.Queue):
        def put(self, item, *args, **kwargs):
            if isinstance(item, dict) and item.get("event") == "final":
                calls.append(("put_final",))
            return super().put(item, *args, **kwargs)

    monkeypatch.setattr(ask_execution_module.queue, "Queue", _RecordingQueue)

    coordinator = _ask_coordinator(
        calls, runner=_response,
        noted=lambda nb, uid: noted.append(("note", nb, uid)) or calls.append(("note",)),
    )

    _drain(coordinator.start(
        "nb-t5", AskRequest(question="Q?", mode="chunk"), ASK_MODES["chunk"],
        user_id=USER_A,
    ))

    assert noted == [("note", "nb-t5", USER_A)]
    # finish the durable job → queue the "final" event → THEN signal the
    # overlay chain. Neither "note" nor "put_final" may lead "finish", and
    # "note" specifically must trail "put_final" (not just "finish").
    assert calls == [("finish", "done"), ("put_final",), ("note",)]


@pytest.mark.parametrize("failure", [RuntimeError("engine down")])
def test_a_failed_ask_never_signals(failure):
    calls: list = []
    noted: list = []

    def _boom():
        raise failure

    _drain(_ask_coordinator(
        calls, runner=_boom, noted=lambda nb, uid: noted.append((nb, uid)),
    ).start(
        "nb-t5", AskRequest(question="Q?", mode="chunk"), ASK_MODES["chunk"],
        user_id=USER_A,
    ))

    assert noted == []
    assert calls == [("finish", "failed")]


def test_a_failing_signal_never_turns_a_delivered_answer_into_an_error():
    """⚠ The call site sits inside the worker's ``try``, whose
    ``except Exception`` finishes the job as *failed* and delivers an error
    event. An exception escaping here would rewrite a delivered answer into a
    reported failure."""
    calls: list = []

    def _boom(_nb, _uid):
        raise RuntimeError("scheduler down")

    delivered = _drain(_ask_coordinator(
        calls, runner=_response, noted=_boom,
    ).start(
        "nb-t5", AskRequest(question="Q?", mode="chunk"), ASK_MODES["chunk"],
        user_id=USER_A,
    ))

    assert [event["event"] for event in delivered][-1] == "final"
    assert calls == [("finish", "done")]


def test_the_sample_size_constant_is_the_one_the_service_uses(harness):
    """The service must not carry a second spelling of the bound: a local
    default here and the real constant there is exactly how "40" becomes two
    different numbers."""
    captured: dict = {}

    class _Recorder:
        def recent_user_ask_traces(self, notebook_id, user_id, *, job_limit,
                                   step_limit):
            captured.update(job_limit=job_limit, step_limit=step_limit)
            return []

    service = _service(harness)
    service.ask_state = _Recorder()
    service.usage_stats(NOTEBOOK_ID, USER_A)

    assert captured["job_limit"] == AGENT_PROFILE_TRACE_SAMPLE


# =====================================================================
# codex R1: revocation race + threshold requeue
# =====================================================================


def _seed_one_ask(harness):
    _add_ask(
        harness, "job-r1", user_id=USER_A, question=A_QUESTION,
        steps=({"step_type": "retrieve", "summary": "查", "detail": {"count": 2}},),
    )


def test_a_member_removed_during_the_model_call_gets_no_resurrected_blocks(harness):
    """codex R1 P1(写前复核那一侧)。

    移除发生在 LLM 调用期间:clear_all + clear_job_row 都已跑完,worker 拿着
    模型回复回来。没有写前复核,``expected_revision=0`` 的写入会把私有块整个
    **重建**——被撤销的成员重新加入后看到旧心得而不是白纸。
    """
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    claimed = profiles.claim(NOTEBOOK_ID, USER_A)
    assert claimed is not None

    def reply(_prompt: str) -> str:
        # 模型调用期间成员被移除:与 _clear_member_profile 相同的两步(codex R3
        # P1 之后的顺序——job 行先走,让 worker 的两道护栏可靠触发)
        profiles.clear_job_row(NOTEBOOK_ID, USER_A)
        profiles.clear_all(NOTEBOOK_ID, USER_A)
        return _reply(retrieval_notes="复活的心得")

    service = _service(harness, client=_Client(reply))
    result = _run_overlay(service, claimed)

    assert result["diagnostic"] == "revoked_mid_run"
    assert result["blocks_written"] == 0
    assert _blocks(harness, USER_A) == {}          # 没有任何块被重建
    assert profiles.job_row(NOTEBOOK_ID, USER_A) is None


def test_a_removal_between_the_precheck_and_the_writes_is_wiped_after_settle(
    harness, monkeypatch
):
    """codex R1 P1(写后兜底那一侧)。

    移除落在写前复核**之后**、settle **之前**的毫秒级窗口:写入已经发生,但
    settle 撞不到行(rowcount=0 → False)。此时唯一正确的动作是把刚写进去的
    行再清一遍——settle False 是「只有移除会删这行」的可靠信号。
    """
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    claimed = profiles.claim(NOTEBOOK_ID, USER_A)
    assert claimed is not None

    original_settle = profiles.settle

    def settle_after_removal(notebook_id, owner_id, status, **kwargs):
        # 写入已完成;settle 之前移除路径跑完了两步(job 行先走)
        profiles.clear_job_row(NOTEBOOK_ID, USER_A)
        profiles.clear_all(NOTEBOOK_ID, USER_A)
        return original_settle(notebook_id, owner_id, status, **kwargs)

    monkeypatch.setattr(profiles, "settle", settle_after_removal)
    service = _service(harness, client=_Client(_reply(retrieval_notes="心得")))
    _run_overlay(service, claimed)

    # 写后兜底把竞态窗口里刚重建的块清掉了
    assert _blocks(harness, USER_A) == {}


def test_a_threshold_that_filled_up_mid_run_requeues_the_next_round(
    harness, monkeypatch
):
    """codex R1 P2:满阈值的信号不再滞留。

    run 在飞期间又攒满一整个阈值——期间每次 claim 都被 running 拒。settle 只
    扣认领快照,剩余 pending 已 ≥ 阈值,但再没有触发者。修复后 run 收尾自查
    并重排下一轮;每轮消费自己的快照,总调用数仍 ≤ 信号数/阈值。
    """
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))
    profiles.bump_signal(NOTEBOOK_ID, USER_A, delta=3)
    claimed = profiles.claim(NOTEBOOK_ID, USER_A)
    assert claimed.pending_signal == 3

    def reply(_prompt: str) -> str:
        # 运行期间又攒满一个阈值(触发方的 claim 都会被 running 拒,这里直接 bump)
        profiles.bump_signal(NOTEBOOK_ID, USER_A, delta=3)
        return _reply(retrieval_notes="心得")

    service = _service(harness, client=_Client(reply))
    _run_overlay(service, claimed)

    # settle 扣掉快照 3,剩 3 ≥ 阈值 → 自动重排了一轮(行已被 claim 成 running)
    assert [c["name"] for c in submitter.calls] == [
        f"agentprofile-overlay-{NOTEBOOK_ID}"
    ]
    assert _job(harness, USER_A)["status"] == "running"


def test_a_leftover_below_the_threshold_does_not_requeue(harness, monkeypatch):
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))
    profiles.bump_signal(NOTEBOOK_ID, USER_A, delta=3)
    claimed = profiles.claim(NOTEBOOK_ID, USER_A)

    def reply(_prompt: str) -> str:
        profiles.bump_signal(NOTEBOOK_ID, USER_A, delta=1)   # 只攒了 1,不够阈值
        return _reply(retrieval_notes="心得")

    service = _service(harness, client=_Client(reply))
    _run_overlay(service, claimed)

    assert submitter.calls == []
    assert _job(harness, USER_A)["status"] == "done"
    assert _job(harness, USER_A)["pending_signal"] == 1


def test_removal_deletes_the_job_marker_before_the_blocks(harness):
    """codex R3 P1:清理顺序本身是契约。

    先清块、后删 job 行会留一个窗口:在飞 worker 的写前复核(job 行还在)通过、
    写入、settle 也成功,随后 job 行才消失——两道护栏全绿,被撤销的私有块复活。
    job 行先走,任何仍在飞的 worker 要么写前跳过、要么 settle False 触发写后兜底。
    """
    calls: list[str] = []

    class _Recorder:
        def clear_job_row(self, nb, uid):
            calls.append("job_row")

        def clear_all(self, nb, uid):
            calls.append("blocks")

    sharing = NotebookSharingService(
        store=SimpleNamespace(remove_member=lambda nb, uid: None),
        copies=None, catalog=None, summaries=None, database=harness["database"],
        copy_stats=lambda _nb: {}, profiles=_Recorder(),
    )
    sharing.remove_member(NOTEBOOK_ID, USER_B)
    assert calls == ["job_row", "blocks"]


def test_a_job_update_to_a_member_written_note_is_refused(harness):
    """codex R3 P1(身份洗白):模型对用户手写块的普通更新会把 updated_origin
    翻成 job,下一轮就能 retire 用户的断言。非空的用户块对 job 是只读的。"""
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    profiles.write_block(
        NOTEBOOK_ID, USER_A, "retrieval_notes", value="我自己的心得",
        evidence=[], expected_revision=0, origin="user", actor=USER_A,
    )
    claimed = profiles.claim(NOTEBOOK_ID, USER_A)
    service = _service(harness, client=_Client(_reply(retrieval_notes="模型想改写")))
    result = _run_overlay(service, claimed)

    row = _blocks(harness, USER_A)["retrieval_notes"]
    assert row["value"] == "我自己的心得"          # 原文保留
    assert row["updated_origin"] == "user"          # 身份没有被洗成 job
    assert "user_authoritative:retrieval_notes" in result["diagnostic"]


def test_a_member_cleared_note_hands_the_label_back_to_the_agent(harness):
    """用户清空过的块(origin=user 但值为空)不再是权威——否则清空会把这个
    label 永远冻死,而清空的产品语义是「让 AI 重新填」。"""
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    profiles.write_block(
        NOTEBOOK_ID, USER_A, "retrieval_notes", value="旧心得",
        evidence=[], expected_revision=0, origin="user", actor=USER_A,
    )
    profiles.clear_block(
        NOTEBOOK_ID, USER_A, "retrieval_notes", expected_revision=1, actor=USER_A,
    )
    claimed = profiles.claim(NOTEBOOK_ID, USER_A)
    service = _service(harness, client=_Client(_reply(retrieval_notes="新的整理")))
    _run_overlay(service, claimed)

    row = _blocks(harness, USER_A)["retrieval_notes"]
    assert row["value"] == "新的整理"
    assert row["updated_origin"] == "job"


def test_an_internal_failure_still_requeues_a_filled_threshold(
    harness, monkeypatch
):
    """codex R5 P2:内部异常的终态路径与其他终态一样要复查剩余计数——
    run 崩溃时攒满的阈值不该滞留到该成员下一次提问(可能永远不来)。"""
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))
    profiles.bump_signal(NOTEBOOK_ID, USER_A, delta=3)
    claimed = profiles.claim(NOTEBOOK_ID, USER_A)

    def reply(_prompt: str) -> str:
        profiles.bump_signal(NOTEBOOK_ID, USER_A, delta=3)
        raise RuntimeError("boom")

    service = _service(harness, client=_Client(reply))
    with pytest.raises(RuntimeError):
        _run_overlay(service, claimed)

    assert [c["name"] for c in submitter.calls] == [
        f"agentprofile-overlay-{NOTEBOOK_ID}"
    ]


def test_a_report_discarded_by_a_busy_chain_fires_after_that_run_settles(
    harness, monkeypatch
):
    """codex R6 P2 端到端:报告撞忙 → 信号落地 → 在飞 run settle 后重排接住。"""
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))
    claimed = profiles.claim(NOTEBOOK_ID, USER_A)         # 链路占住(在飞)
    service = _service(harness, client=_Client(_reply(retrieval_notes="心得")))

    service.note_report_completed(NOTEBOOK_ID, USER_A)    # busy -> 满阈值 bump
    assert submitter.calls == []                          # 没插队

    _run_overlay(service, claimed)

    assert [c["name"] for c in submitter.calls] == [
        f"agentprofile-overlay-{NOTEBOOK_ID}"
    ]                                                     # settle 后重排一轮


# =====================================================================
# Agentic Memory P2: the claim generation closes the registered R4 ABA
# =====================================================================


def test_a_removed_and_readded_member_does_not_lose_the_new_runs_work(
    harness, monkeypatch
):
    """THE ABA case, end to end (closes the registered codex #520 R4 P2).

    The member is removed, re-added, and a NEW overlay run claims the chain —
    all inside the stale run's model call. Under P1 the stale worker found a
    row (its bare existence check passed), wrote its pre-removal notes with
    ``expected_revision=0``, and then its settle landed on the replacement row:
    it consumed the NEW run's snapshot and released a slot it never held.

    With the generation carried through both writes and settle:

    * ``write_block`` refuses the stale token, so not one byte of the new
      generation's block changes;
    * ``settle`` reports ``superseded``, so the new run's ``pending_signal``
      and ``running`` status survive intact;
    * and — the half that is easiest to get backwards — the revoked-overlay
      WIPE must NOT fire. Wiping here would delete the blocks the new
      generation just wrote, which is worse than the ABA itself.
    """
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    _with_submitter(monkeypatch, _Submitter(run=False))
    profiles.bump_signal(NOTEBOOK_ID, USER_A, delta=3)
    stale = profiles.claim(NOTEBOOK_ID, USER_A)

    wipes: list[str] = []
    original_clear_all = profiles.clear_all

    def counting_clear_all(notebook_id, owner_id):
        wipes.append(owner_id)
        return original_clear_all(notebook_id, owner_id)

    monkeypatch.setattr(profiles, "clear_all", counting_clear_all)

    def reply(_prompt: str) -> str:
        # Removed, re-added, and re-claimed while this call is outstanding.
        profiles.clear_job_row(NOTEBOOK_ID, USER_A)
        profiles.clear_all(NOTEBOOK_ID, USER_A)
        wipes.clear()                       # only count wipes AFTER the removal
        profiles.bump_signal(NOTEBOOK_ID, USER_A, delta=5)
        fresh = profiles.claim(NOTEBOOK_ID, USER_A)
        # The new generation writes its own note before the stale run returns.
        profiles.write_block(
            NOTEBOOK_ID, USER_A, "retrieval_notes", value="重新加入之后的心得",
            evidence=[], expected_revision=0, origin="job", actor="",
            claim_token=fresh.token,
        )
        return _reply(retrieval_notes="移出之前的心得")

    service = _service(harness, client=_Client(reply))
    _run_overlay(service, stale)

    row = _job(harness, USER_A)
    assert row["status"] == "running", "新一代仍持有链路"
    assert row["pending_signal"] == 5, "旧 worker 不得消费新一代的快照"
    assert row["runs"] == 0
    blocks = _blocks(harness, USER_A)
    assert blocks["retrieval_notes"]["value"] == "重新加入之后的心得"
    assert blocks["retrieval_notes"]["revision"] == 1, "一次都没被覆盖"
    assert wipes == [], "superseded 绝不能触发撤销清理——那会删掉新一代刚写的块"


def test_a_real_removal_still_wipes_the_blocks_it_recreated(harness, monkeypatch):
    """The other branch of the same tri-state, kept next to the ABA case on
    purpose: when the row really is gone the wipe MUST still fire. A fix that
    made ``superseded`` safe by never wiping would pass the test above and
    silently reopen the revocation leak this one pins."""
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    claimed = profiles.claim(NOTEBOOK_ID, USER_A)

    wipes: list[str] = []
    original_clear_all = profiles.clear_all

    def counting_clear_all(notebook_id, owner_id):
        wipes.append(owner_id)
        return original_clear_all(notebook_id, owner_id)

    original_settle = profiles.settle

    def settle_after_removal(notebook_id, owner_id, status, **kwargs):
        # Removal lands in the microseconds between the writes and the settle,
        # so the pre-write probe cannot have seen it.
        profiles.clear_job_row(NOTEBOOK_ID, USER_A)
        return original_settle(notebook_id, owner_id, status, **kwargs)

    monkeypatch.setattr(profiles, "settle", settle_after_removal)
    monkeypatch.setattr(profiles, "clear_all", counting_clear_all)
    service = _service(harness, client=_Client(_reply(retrieval_notes="心得")))
    _run_overlay(service, claimed)

    assert wipes == [USER_A], "行真的没了 → 必须把这一轮重建的块清掉"
    assert _blocks(harness, USER_A) == {}


def test_a_superseded_claim_writes_nothing_and_says_so(harness, monkeypatch):
    """The write half on its own: the pre-write ``job_row`` probe PASSES here
    (a row is present — it is just a later generation's), so only the token
    inside the write transaction can stop this run."""
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    _with_submitter(monkeypatch, _Submitter(run=False))
    profiles.write_block(
        NOTEBOOK_ID, USER_A, "retrieval_notes", value="现任",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    stale = profiles.claim(NOTEBOOK_ID, USER_A)

    def reply(_prompt: str) -> str:
        profiles.clear_job_row(NOTEBOOK_ID, USER_A)
        profiles.claim(NOTEBOOK_ID, USER_A)      # a new generation takes over
        return _reply(retrieval_notes="陈旧的心得")

    service = _service(harness, client=_Client(reply))
    result = _run_overlay(service, stale)

    assert result["blocks_written"] == 0
    assert "claim_superseded" in result["diagnostic"]
    block = _blocks(harness, USER_A)["retrieval_notes"]
    assert block["value"] == "现任"
    assert block["revision"] == 1
