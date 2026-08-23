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
    AGENT_PROFILE_REPORT_SAMPLE,
    AGENT_PROFILE_TRACE_SAMPLE,
)
from app.repositories.sqlite.agent_profile_store import AgentProfileStore
from app.repositories.sqlite.ask_state_store import AskStateStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.sqlite.query_store import QueryStore
from app.repositories.sqlite.sharing_store import SharingStore
from app.repositories.sqlite.source_store import SourceStore
from app.services import background_jobs
from app.services.agent_profile_job import (
    AGENT_PROFILE_USAGE_SECTION_MAX_CHARS,
    AGENT_PROFILE_WORKLOAD,
    BASE_CHAIN_OWNER,
    OVERLAY_LABELS,
    AgentProfileConsolidationService,
    render_usage_block,
    summarize_usage,
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

#: ``_service``'s "leave the fixture's seat alone" sentinel — see there.
_KEEP = object()


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
        # ``kwargs`` recorded too (P2-T2): ``claim_token`` moved from a
        # positional arg to a keyword-only one, and a caller inspecting only
        # ``args`` would otherwise see it silently vanish from the record.
        self.calls.append(
            {"name": name, "args": args, "kwargs": kwargs,
             "notify_pending": notify_pending}
        )
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
        # P2-T3: the membership seat, wired with the REAL store rather than a
        # permissive double. It matters that it is real and that it is on by
        # default in every test in this file: the check now sits in front of
        # every overlay trigger, and a fixture that left it unwired would let
        # the whole file keep passing while production silently gained (or
        # lost) a gate nothing here exercises. ``USER_A`` owns ``NOTEBOOK_ID``,
        # so every pre-existing case reads through it unchanged; ``USER_B`` is
        # neither owner nor member, which is exactly the removed-member shape.
        "access": SharingStore(
            database, settings, now=seams.now,
            insert_row=SharingStore.insert_row_values,
        ),
        "event_log": _EventLog(),
    }


def _run_overlay(service, claimed):
    """Run exactly the way ``start_overlay`` does: the claim's snapshot AND its
    generation token (Agentic Memory P2 — the token is what makes a stale
    worker's settle/write land on nothing). ``claim_token`` is keyword-only on
    ``run_overlay`` (P2-T2 tightening) — no default, so a caller cannot forget
    it and silently reintroduce the ABA a missing token used to open."""
    return service.run_overlay(
        NOTEBOOK_ID, USER_A, claimed.pending_signal, claim_token=claimed.token
    )


def _service(harness, *, client: "_Client | None" = None, models=None, access=_KEEP):
    return AgentProfileConsolidationService(
        settings=harness["settings"],
        profiles=harness["profiles"],
        database=harness["database"],
        sources=harness["sources"],
        queries=harness["queries"],
        models=models if models is not None else _Models(client),
        event_log=harness["event_log"],
        ask_state=harness["ask_state"],
        # ``access=None`` is a legitimate production shape (a composition root
        # that predates P2-T3) and fails open, so it is spelled with a sentinel
        # rather than ``None``: a caller passing ``None`` means "no checker",
        # not "use the default".
        access=harness["access"] if access is _KEEP else access,
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
    # it holds (Agentic Memory P2). ``claim_token`` is keyword-only on
    # ``run_overlay`` (P2-T2), so it now rides in ``kwargs`` rather than
    # ``args``.
    notebook, member, snapshot = submitter.calls[0]["args"]
    token = submitter.calls[0]["kwargs"]["claim_token"]
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
    # P2 的 claim 代际是关键字参数(``run_overlay`` 的 ``claim_token`` 现在
    # keyword-only),因此从 ``kwargs`` 而不是 ``args`` 里取。
    notebook, member, snapshot = submitter.calls[0]["args"]
    token = submitter.calls[0]["kwargs"]["claim_token"]
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


# ------------------------------------------------- P2-T3: late notifications
# Removal clears the rows (above). What this group is about is the OTHER half:
# a notification that lands AFTER the removal must not put them back. Before
# P2-T3 it did — ``bump_signal`` is an upsert, so a late completion recreated
# the job row, and once the counter filled a run rebuilt that member's blocks
# out of traces from when they still had access (codex #520 R5).

def test_a_late_ask_notification_does_not_revive_a_removed_member(harness):
    """The R5 shape, end to end: USER_B is not a member of this notebook, and
    an Ask completion arriving for them leaves nothing behind at all."""
    service = _service(harness)

    service.note_ask_completed(NOTEBOOK_ID, USER_B)
    service.note_ask_completed(NOTEBOOK_ID, USER_B)
    service.note_ask_completed(NOTEBOOK_ID, USER_B)   # would cross the threshold

    assert harness["profiles"].job_row(NOTEBOOK_ID, USER_B) is None
    # And the member who IS entitled is untouched by the same service.
    service.note_ask_completed(NOTEBOOK_ID, USER_A)
    assert harness["profiles"].job_row(NOTEBOOK_ID, USER_A)["pending_signal"] == 1


def test_a_late_report_notification_does_not_revive_a_removed_member(harness):
    """The report hook is the sharper half of R5: its bump is a FULL-threshold
    one, so a single late report used to recreate the row AND arm it in one
    call — no counter to fill first."""
    service = _service(harness, client=_Client({"blocks": []}))

    service.note_report_completed(NOTEBOOK_ID, USER_B)

    assert harness["profiles"].job_row(NOTEBOOK_ID, USER_B) is None


def test_a_removed_member_cannot_have_a_worker_started_for_them(
    harness, monkeypatch
):
    """``start_overlay`` is the one door every path to a worker goes through
    (both hooks, plus T6's manual rebuild endpoint), and a claim is what
    creates the durable row. It refuses on its own, not because its callers
    checked first."""
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))
    service = _service(harness)

    assert service.start_overlay(NOTEBOOK_ID, USER_B) is False

    assert submitter.calls == []
    assert harness["profiles"].job_row(NOTEBOOK_ID, USER_B) is None


def test_a_failing_access_check_admits_the_notification(harness):
    """FAIL-OPEN, asserted positively rather than left implied.

    Every caller of this check hangs off an already-delivered answer, so a
    database hiccup in a background bookkeeping read must not change what the
    user's request did — and failing closed would be strictly worse than the
    residual it closes: a transient error would silently stop consolidating
    for members who are perfectly entitled to it, with nothing reporting it.
    The accepted end state is "revives only when the access read is broken".
    """
    class _BrokenAccess:
        def user_can_read_notebook(self, _nb, _uid):
            raise RuntimeError("db down")

    service = _service(harness, access=_BrokenAccess())

    service.note_ask_completed(NOTEBOOK_ID, USER_A)

    assert harness["profiles"].job_row(NOTEBOOK_ID, USER_A)["pending_signal"] == 1


def test_an_unwired_access_seat_admits_the_notification(harness):
    """``access=None`` is a real production shape (a composition root that
    predates P2-T3) and takes the same fail-open direction as a raising check
    — this feature never breaks its host, and it never silently stops working
    for an entitled member either."""
    service = _service(harness, access=None)

    service.note_ask_completed(NOTEBOOK_ID, USER_B)

    assert harness["profiles"].job_row(NOTEBOOK_ID, USER_B)["pending_signal"] == 1


def test_the_access_check_uses_the_read_side_predicate(harness, monkeypatch):
    """A read-only SHARE member — not the owner — still gets consolidated.

    The predicate is ``user_can_read_notebook`` (the read side's only
    definition), NOT ownership and NOT "is this the notebook's creator". A
    check that drifted to a narrower one would fail closed for every shared
    notebook's members, and the failure mode is silence: their notes would
    simply stop refreshing.
    """
    _with_submitter(monkeypatch, _Submitter(run=False))
    harness["access"].add_member(NOTEBOOK_ID, USER_B)
    service = _service(harness)

    service.note_ask_completed(NOTEBOOK_ID, USER_B)

    assert harness["profiles"].job_row(NOTEBOOK_ID, USER_B)["pending_signal"] == 1


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
        noted=lambda nb, uid, mode_id="chunk": noted.append(("note", nb, uid)) or calls.append(("note",)),
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


def test_post_completion_observers_run_after_the_final_event():
    import app.services.ask_execution as ask_execution_module

    calls: list = []

    class _RecordingQueue(queue.Queue):
        def put(self, item, *args, **kwargs):
            if isinstance(item, dict) and item.get("event") == "final":
                calls.append(("put_final",))
            if item is None:
                calls.append(("sentinel",))
            return super().put(item, *args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ask_execution_module.queue, "Queue", _RecordingQueue)
    try:
        coordinator = _ask_coordinator(
            calls,
            runner=_response,
            noted=lambda notebook_id, user_id, mode_id: calls.append(
                ("observe", notebook_id, user_id, mode_id)
            ),
        )
        delivered = _drain(coordinator.start(
            "nb-t5",
            AskRequest(question="Q?", mode="chunk"),
            ASK_MODES["chunk"],
            user_id=USER_A,
        ))
    finally:
        monkeypatch.undo()

    assert delivered[-1]["event"] == "final"
    assert calls == [
        ("finish", "done"),
        ("put_final",),
        ("observe", "nb-t5", USER_A, "chunk"),
        ("sentinel",),
    ]


@pytest.mark.parametrize("failure", [RuntimeError("engine down")])
def test_a_failed_ask_never_signals(failure):
    calls: list = []
    noted: list = []

    def _boom():
        raise failure

    _drain(_ask_coordinator(
        calls, runner=_boom,
        noted=lambda nb, uid, mode_id="chunk": noted.append((nb, uid)),
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
    different numbers.

    P2-T4: ``usage_stats`` now makes TWO calls (asks, then reports), so this
    fake must answer both — a fake missing the report method would make
    ``usage_stats`` raise ``AttributeError`` before ``job_limit`` is even
    captured, which is a different failure than the one this test targets."""
    captured: dict = {}

    class _Recorder:
        def recent_user_ask_traces(self, notebook_id, user_id, *, job_limit,
                                   step_limit):
            captured.update(job_limit=job_limit, step_limit=step_limit)
            return []

        def recent_user_report_traces(self, notebook_id, user_id, *,
                                      report_limit, attempt_limit):
            captured.update(report_limit=report_limit,
                            attempt_limit=attempt_limit)
            return []

    service = _service(harness)
    service.ask_state = _Recorder()
    service.usage_stats(NOTEBOOK_ID, USER_A)

    assert captured["job_limit"] == AGENT_PROFILE_TRACE_SAMPLE
    assert captured["report_limit"] == AGENT_PROFILE_REPORT_SAMPLE


# =====================================================================
# 7. the report SAMPLE (Agentic Memory P2, T4) — sections_json[i].attempted
#    joins the ask trace as the overlay chain's second usage input.
# =====================================================================


def _add_report(
    harness,
    report_id: str,
    *,
    user_id: str,
    question: str,
    notebook_id: str = NOTEBOOK_ID,
    status: str = "done",
    created_at: str = NOW,
    updated_at: str | None = None,
    attempted: tuple[tuple[dict, ...], ...] = (),
) -> None:
    """One persisted report, written directly — mirrors ``_add_ask``.

    ``attempted`` is one tuple of ``attempted`` dicts PER SECTION (a report
    can have several sections, each with its own ``attempted`` list), so the
    fixture can exercise the real nested shape
    ``sections_json[i].attempted[j]`` rather than a flattened stand-in.
    """
    sections = [{"title": f"s{i}", "attempted": list(entries)}
                for i, entries in enumerate(attempted)]
    with harness["database"].write() as db:
        db.execute(
            "INSERT INTO reports(id,notebook_id,question,sections_json,"
            "status,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (report_id, notebook_id, question, json.dumps(sections, ensure_ascii=False),
             status, user_id, created_at, updated_at or created_at),
        )


def test_a_report_only_member_is_no_longer_a_no_usage_sample(harness, monkeypatch):
    """T4 拍板:纯报告用户(零 ask、一份 done 报告)不再落 no_usage_sample。

    ``note_report_completed``'s own docstring used to register this as a gap
    ("Trigger, not input") — a member whose only activity was the report they
    just finished would trigger their own overlay refresh and find nothing to
    summarise. This closes it: the report sample alone is enough.
    """
    _with_submitter(monkeypatch, _Submitter())
    _add_report(
        harness, "rep-a", user_id=USER_A, question="仅有一份报告",
        attempted=(({"query": "q1", "new": 3, "tries": 1},),),
    )
    service = _service(harness, client=_Client(_reply(
        retrieval_notes="按报告的检索方向来看", usage_gaps="g",
    )))

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    job = _job(harness, USER_A)
    assert job["status"] == "done"
    assert job["diagnostic"] != "no_usage_sample"
    assert _blocks(harness, USER_A)["retrieval_notes"]["value"] == "按报告的检索方向来看"


def test_another_member_s_report_never_enters_the_sample(harness, monkeypatch):
    """跨用户隔离正向断言:别人 created_by 的报告不进样本。

    B 重跑 A 建的报告是 T4 已登记接受的方向安全代价(触发方是 self.user_id,
    样本谓词是 reports.created_by)——这里钉住的是它安全的那一半:B 的报告
    一个字都不出现在 A 的覆盖层输入里,即便它就在同一个 notebook。
    """
    _add_report(
        harness, "rep-b", user_id=USER_B, question="B的报告不该被A看到",
        attempted=(({"query": "q1", "new": 0, "tries": 1},),),
    )

    rows = harness["ask_state"].recent_user_report_traces(
        NOTEBOOK_ID, USER_A, report_limit=10, attempt_limit=200
    )

    assert rows == []


def test_a_report_contributes_nothing_to_the_zero_hit_evidence(harness, monkeypatch):
    """P2-T4 修复轮裁决 1:``usage_gaps`` 的证据回到 **ask-only**。

    这一条钉的是修复轮推翻的那个论证。原实现把 ``attempted[j].new == 0``
    读成「这个方向空手而归」并计进 ``zero_hit_queries``,而 ``new`` 数的是
    run **共享候选池**的新增知识对象——节问题先播种、方向重叠必然 0、无图库
    恒 0、chunk/element 命中根本不计。按它计零命中,等于用一个量别的东西的
    计数器,把「这个库没有 X 资料」固化进该成员的私有笔记。

    fixture 里三个方向的 ``new`` 全是 0(最常见的真实形态,不是构造的边角),
    只有一个 ``failed``。证据必须恰好是 **0**——不是 2(把非失败的两条计
    进去),也不是 3。
    """
    _with_submitter(monkeypatch, _Submitter())
    _add_report(
        harness, "rep-a", user_id=USER_A, question="new 恒零的真实形态",
        attempted=((
            {"query": "重叠方向甲", "new": 0, "tries": 1},
            {"query": "重叠方向乙", "new": 0, "tries": 1, "failed": True},
            {"query": "重叠方向丙", "new": 0, "tries": 1},
        ),),
    )
    service = _service(harness, client=_Client(_reply(
        retrieval_notes="n", usage_gaps="g",
    )))

    service.note_report_completed(NOTEBOOK_ID, USER_A)

    blocks = _blocks(harness, USER_A)
    assert blocks["usage_gaps"]["evidence"] == [
        {"claim_index": 0, "zero_hit_queries": 0}
    ]


def test_a_failed_direction_is_counted_but_its_wording_is_still_listed(harness):
    """P2-T4 修复轮裁决 8:``failed`` 的语义改为「执行失败计数」。

    修复轮把零命中语义整个移走之后,``failed`` 不再是「不计成零命中」的排除
    项,而是它自己的一件事:该方向的**执行**炸了。措辞仍然是这个人自己确认的
    方向,所以它照样进方向清单——传输炸了说明不了措辞不好——只是另有一个计数
    在旁边解释这份清单。
    """
    _add_report(
        harness, "rep-a", user_id=USER_A, question="有失败方向的报告",
        attempted=((
            {"query": "正常方向", "new": 2, "tries": 1},
            {"query": "炸掉的方向", "new": 5, "tries": 1, "failed": True},
        ),),
    )

    rows = harness["ask_state"].recent_user_report_traces(
        NOTEBOOK_ID, USER_A, report_limit=10, attempt_limit=200
    )

    # 投影只留 query/failed——``new`` 被丢掉,不留在任何一层。
    assert rows[0]["attempts"] == [
        {"query": "正常方向", "failed": False},
        {"query": "炸掉的方向", "failed": True},
    ]
    block = render_usage_block(summarize_usage((), rows))
    assert "正常方向; 炸掉的方向" in block
    assert "(1 of these failed)" in block


def test_only_done_reports_enter_the_sample(harness):
    """只取 status='done' 的报告——失败/取消的报告说明不了这个人怎么检索。"""
    _add_report(
        harness, "rep-failed", user_id=USER_A, question="失败的报告",
        status="failed",
        attempted=(({"query": "q", "new": 0, "tries": 1},),),
    )
    _add_report(
        harness, "rep-cancelled", user_id=USER_A, question="取消的报告",
        status="cancelled",
        attempted=(({"query": "q", "new": 0, "tries": 1},),),
    )

    rows = harness["ask_state"].recent_user_report_traces(
        NOTEBOOK_ID, USER_A, report_limit=10, attempt_limit=200
    )

    assert rows == []


def test_the_report_section_actually_reaches_the_rendered_prompt(harness):
    """P2-T4 修复轮裁决 9(渲染面守卫):报告分段与那条分母行真的在输出里。

    这一条存在的理由很具体:整段报告渲染是一块**没有任何别的用例会碰到**的
    代码——它不写库、不发事件、只是把文字拼进一个 prompt。删掉整段渲染,
    上面那些「进不进样本」的用例一条都不会红,而模型从此再也看不到这个人怎么
    写检索方向。所以这里逐项断言:分段表头在、每份报告的问题在、方向措辞在、
    以及那条**回到 ask-only 口径**的零命中分母行(裁决 1 的落点)措辞正确。
    """
    _add_report(
        harness, "rep-a", user_id=USER_A, question="宽带隙器件的热管理",
        attempted=((
            {"query": "GaN 结温测量方法", "new": 3, "tries": 1},
            {"query": "衬底导热率对比", "new": 0, "tries": 1},
        ),),
    )
    rows = harness["ask_state"].recent_user_report_traces(
        NOTEBOOK_ID, USER_A, report_limit=10, attempt_limit=200
    )

    block = render_usage_block(summarize_usage((), rows))

    assert "[Your recent deep reports in this library]" in block
    assert "reports sampled: 1" in block
    assert "- 宽带隙器件的热管理" in block
    assert "  directions: GaN 结温测量方法; 衬底导热率对比" in block
    # ⚠ 分母回到 ask-only(裁决 1)。零 ask 时它是 "0 (of 0 steps sampled)",
    # 绝不能把报告方向数加进这条分母——那正是修复轮移走的那个混算。
    assert "retrieval steps that returned nothing: 0 (of 0 steps sampled)" in block
    assert "directions searched" not in block  # 旧的空手断言措辞不得复活
    assert "came back empty" not in block


def test_a_report_whose_directions_were_all_truncated_makes_no_assertion(harness):
    """P2-T4 修复轮裁决 6(截断诚实):渲染不出方向就披露,不写「0 directions」。

    ``attempt_limit`` 截断与「这份报告一个方向都没跑」在返回的行里**无法区分**
    ——行就是不在那儿。默认配置(6 节 × 4 方向 × 10 份 = 240 > 200)让截断成为
    常态而不是边角,所以这里用 ``attempt_limit=1`` 复现同一形态:第二份报告的
    方向被全部截掉,它那一行必须给出「未取样」的披露,而不是一句它撑不起的
    「searched 0 directions」。
    """
    _add_report(
        harness, "rep-new", user_id=USER_A, question="较新的报告",
        created_at="2026-08-18T02:00:00+00:00",
        attempted=(({"query": "留下来的方向", "new": 1, "tries": 1},),),
    )
    _add_report(
        harness, "rep-old", user_id=USER_A, question="较旧的报告",
        created_at="2026-08-18T01:00:00+00:00",
        attempted=(({"query": "被截掉的方向", "new": 1, "tries": 1},),),
    )

    rows = harness["ask_state"].recent_user_report_traces(
        NOTEBOOK_ID, USER_A, report_limit=10, attempt_limit=1
    )

    # 截断按报告新鲜度落在最旧的那份尾部——刚跑完那份的方向绝不先掉。
    assert [row["report_id"] for row in rows] == ["rep-new", "rep-old"]
    assert rows[1]["attempts"] == []

    block = render_usage_block(summarize_usage((), rows))
    assert "  directions: 留下来的方向" in block
    assert "(directions not sampled)" in block
    assert "0 directions" not in block


def test_a_malformed_sections_json_does_not_poison_the_whole_sample(harness):
    """P2-T4 修复轮裁决 7:两种畸形形状实测会让整条语句 raise。

    ``json_type()`` 会**解析**它的参数,所以 ``["hello"]`` 的字符串节元素和
    ``{"attempted": ["oops"]}`` 的字符串 attempt 都会撞上 SQLite 的
    ``malformed JSON``。爆炸半径不是那一行——语句整个抛,这个成员**全部**报告
    一起从样本里消失,而 store 的 docstring 当时承诺的是「降级成没有方向」。
    这里两种形状各一份,再加一份正常报告,断言正常那份完好无损。
    """
    with harness["database"].write() as db:
        for report_id, blob, created in (
            ("rep-str-section", '["hello"]', "2026-08-18T01:00:00+00:00"),
            ("rep-str-attempt", '[{"attempted":["oops"]}]',
             "2026-08-18T02:00:00+00:00"),
        ):
            db.execute(
                "INSERT INTO reports(id,notebook_id,question,sections_json,"
                "status,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (report_id, NOTEBOOK_ID, f"畸形-{report_id}", blob, "done",
                 USER_A, created, created),
            )
    _add_report(
        harness, "rep-ok", user_id=USER_A, question="正常的报告",
        created_at="2026-08-18T03:00:00+00:00",
        attempted=(({"query": "完好的方向", "new": 1, "tries": 1},),),
    )

    rows = harness["ask_state"].recent_user_report_traces(
        NOTEBOOK_ID, USER_A, report_limit=10, attempt_limit=200
    )

    assert [row["report_id"] for row in rows] == [
        "rep-ok", "rep-str-attempt", "rep-str-section",
    ]
    assert rows[0]["attempts"] == [{"query": "完好的方向", "failed": False}]
    # 畸形的两份降级成「没有方向」,而不是把整份样本带走。
    assert rows[1]["attempts"] == [{"query": "", "failed": False}]
    assert rows[2]["attempts"] == []
    block = render_usage_block(summarize_usage((), rows))
    assert "  directions: 完好的方向" in block
    assert block.count("(directions not sampled)") == 2


def test_the_two_usage_sections_share_one_budget_and_neither_starves(harness):
    """P2-T4 修复轮裁决 3:ask 段与报告段共用**一份** 3000 字符预算。

    共享一个计数器而不分配,等于让 ask 段先到先得——四十条提问各自 120 字符
    早就超过 3000,于是「报告表头下面一条报告都没有」会是提问频繁的成员的
    **常态**。这里钉的是两件事:总量仍在一份预算内(不是偷偷开第二份),且两
    段都真的有内容。
    """
    for index in range(AGENT_PROFILE_TRACE_SAMPLE):
        _add_ask(
            harness, f"job-{index:02d}", user_id=USER_A,
            question=f"{index:02d}" + "问" * 110,
            created_at=f"2026-08-18T00:{index:02d}:00+00:00",
            steps=({"step_type": "retrieve", "summary": "查",
                    "detail": {"count": 1}},),
        )
    for index in range(3):
        _add_report(
            harness, f"rep-{index}", user_id=USER_A,
            question=f"报告{index}" + "题" * 100,
            created_at=f"2026-08-18T01:{index:02d}:00+00:00",
            attempted=(({"query": "方向" * 40, "new": 1, "tries": 1},),),
        )

    stats = _service(harness).usage_stats(NOTEBOOK_ID, USER_A)
    block = render_usage_block(stats)

    assert len(block) <= AGENT_PROFILE_USAGE_SECTION_MAX_CHARS * 1.35, (
        "两段合计必须仍在一份预算的量级内——数值宽放是因为固定表头/披露行"
        "不计入 body 预算(既有口径),但绝不该出现两份 3000 的体量。"
    )
    # 最新那条提问(样本按 created_at 倒序)必须在;它是 ask 段没被饿死的证据。
    assert "questions" in block and "- 39" in block
    assert "[Your recent deep reports in this library]" in block
    assert "- 报告0" in block, "ask 段不得吃光预算让报告段一行都渲染不出"


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


def test_a_superseded_settle_does_not_requeue(harness, monkeypatch):
    """spec P2-1 / 质量 P3-3:``superseded`` 时一个更新的世代已经持有这个
    成员自己的链路 slot,而且会在它自己的终态跑同一次剩余复查——旧世代如果
    照常重排,会在新世代已经跑完、留下一份够阈值的 ``pending_signal`` 时抢
    claim 出一份不该存在的第三世代。这与底座链路的同名用例是同一条契约在两条
    链上各自的验证:``run_overlay`` 一直把真实 settle 结果传给
    ``_maybe_requeue_overlay``(不像修复前的 ``run_base``),这里是回归钉子。
    """
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    submitter = _with_submitter(monkeypatch, _Submitter(run=False))

    def reply(_prompt: str) -> str:
        # 模拟一次竞态:旧世代还卡在模型调用里的时候,这个成员自己的链路
        # 已经被重新认领、跑完并落终态——留下一份够触发下一轮的 pending_signal。
        profiles.clear_job_row(NOTEBOOK_ID, USER_A)
        newer = profiles.claim(NOTEBOOK_ID, USER_A)
        profiles.bump_signal(NOTEBOOK_ID, USER_A, delta=3)
        profiles.settle(
            NOTEBOOK_ID, USER_A, "done", claim_token=newer.token, consumed=0,
        )
        return _reply(retrieval_notes="心得")

    service = _service(harness, client=_Client(reply))
    stale = profiles.claim(NOTEBOOK_ID, USER_A)

    _run_overlay(service, stale)

    assert _job(harness, USER_A)["status"] == "done"
    assert _job(harness, USER_A)["pending_signal"] == 3, "新世代自己的终态还没跑复查"
    assert submitter.calls == [], (
        "settle 报 superseded 时不该再抢一次——行已终态,那次抢会成功,"
        "白起一份不该存在的第三世代"
    )


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


def test_a_settle_that_itself_fails_still_wipes_conservatively(harness, monkeypatch):
    """质量评审 P2-1:``_SETTLE_UNKNOWN`` 的保守方向必须钉死在行为上,不能只
    活在注释里。

    settle 这次写入本身失败(抛异常),``_safe_settle`` 既分不清「这是移除留下
    的 gone」也分不清「这是新一代持有链路的 superseded」——它只知道自己没能
    观察到任何一个结局。P1 的 ``settle() -> bool`` 一律把这种「不知道」按更
    保守的方向解读:宁可多做一次可再生的心得重建,也不留下已撤销的私有数据。
    ``_WIPE_ON_SETTLE_OUTCOMES`` 因此把 ``_SETTLE_UNKNOWN`` 与 ``gone`` 分在
    同一边——这里直接断言 wipe 真的被触发,而不是相信那条注释。"""
    profiles = harness["profiles"]
    _seed_one_ask(harness)
    claimed = profiles.claim(NOTEBOOK_ID, USER_A)

    wipes: list[str] = []
    original_clear_all = profiles.clear_all

    def counting_clear_all(notebook_id, owner_id):
        wipes.append(owner_id)
        return original_clear_all(notebook_id, owner_id)

    def broken_settle(*_args, **_kwargs):
        raise RuntimeError("settle 的写入本身失败(抖动/连接错误)")

    monkeypatch.setattr(profiles, "settle", broken_settle)
    monkeypatch.setattr(profiles, "clear_all", counting_clear_all)
    service = _service(harness, client=_Client(_reply(retrieval_notes="心得")))

    _run_overlay(service, claimed)

    assert wipes == [USER_A], (
        "settle 自己都没能落地——按更保守的方向必须当成 gone 同样 wipe"
    )
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


def test_the_runtime_actually_wires_the_access_seat():
    """codex 复核(P2-T3 spec 评审 P2):`repository_runtime.py` 是生产里 access
    座位唯一的来源,删掉那一行整仓测试仍会全绿(overlay fixture 自己接线,守卫只
    扫 agent_profile_job.py),而 R5 会原样复活且无人报错。照 startup sweep 守卫
    的先例,把接线钉成静态断言。"""
    import ast
    from pathlib import Path as _Path

    from app.services import repository_runtime

    source = _Path(repository_runtime.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    wired = False
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "access":
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "sharing_store"
                and isinstance(value.value, ast.Name)
                and value.value.id == "self"
            ):
                wired = True
    assert wired, (
        "repository_runtime 里 AgentProfileConsolidationService 的 access 座位"
        "必须接 self.sharing_store——删掉它 R5 静默复活(所有测试仍绿)。"
    )


def test_direct_constructor_completion_compat_fans_out_to_three_chains():
    """codex 复核(P2-T5 修复轮裁决 9):`_note_ask_completed` 是两条巡固/蒸馏链路
    唯一的触发点。P1 链(agent-profile 覆盖层)按人按库触发、带两个参数;P2 链
    (检索经验蒸馏)是部署级全局计数器、零参数——两次调用必须都在,且必须各自
    落在独立的 ``try`` 块里,否则一条链路抛出的异常会把另一条的计数也吞掉(即使
    两条链路各自内部都 fail-open,"一条链坏掉不连坐另一条"这条性质必须由调用点
    自己成立,不能靠被调方的内部约定)。照 ``test_the_runtime_actually_wires_the_
    access_seat`` 的先例把接线钉成静态断言——删掉任一次调用,或把两次调用并进
    同一个 try,所有现有测试仍然全绿,只有这条断言会报红。"""
    import ast
    from pathlib import Path as _Path

    from app.services import repository_runtime

    source = _Path(repository_runtime.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_note_ask_completed_compat"
        ):
            target = node
            break
    assert target is not None, (
        "repository_runtime.py 必须定义 _note_ask_completed"
    )

    def _call_chain(node: ast.AST) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        parts: list[str] = []
        while isinstance(func, ast.Attribute):
            parts.append(func.attr)
            func = func.value
        if isinstance(func, ast.Name) and func.id == "self":
            parts.append("self")
            return ".".join(reversed(parts))
        return None

    try_blocks = [node for node in ast.walk(target) if isinstance(node, ast.Try)]
    owning_try: dict[str, ast.Try] = {}
    call_args: dict[str, int] = {}
    for block in try_blocks:
        for node in ast.walk(block):
            chain = _call_chain(node)
            if chain in (
                "self.agent_profile_jobs.note_ask_completed",
                "self.retrieval_experience_jobs.note_ask_completed",
                "self.search_profile_jobs.note_ask_completed",
            ):
                owning_try.setdefault(chain, block)
                call_args.setdefault(chain, len(node.args))

    assert "self.agent_profile_jobs.note_ask_completed" in owning_try, (
        "P1 覆盖层巡固的触发调用不在任何 try 块内,或已从 _note_ask_completed "
        "中丢失"
    )
    assert "self.retrieval_experience_jobs.note_ask_completed" in owning_try, (
        "P2 经验库蒸馏的触发调用不在任何 try 块内,或已从 _note_ask_completed "
        "中丢失——这条链路是部署级全局计数器,一旦这里的调用被删掉,蒸馏永远不会"
        "被触发,且没有任何其它测试会报红。"
    )
    assert "self.search_profile_jobs.note_ask_completed" in owning_try
    assert (
        owning_try["self.agent_profile_jobs.note_ask_completed"]
        is not owning_try["self.retrieval_experience_jobs.note_ask_completed"]
    ), "两条链路的触发调用必须落在各自独立的 try 块里,不能共用一个 try"
    assert (
        owning_try["self.search_profile_jobs.note_ask_completed"]
        is not owning_try["self.agent_profile_jobs.note_ask_completed"]
        and owning_try["self.search_profile_jobs.note_ask_completed"]
        is not owning_try["self.retrieval_experience_jobs.note_ask_completed"]
    )
    # P1 链带参(notebook_id, user_id),P2 链零参——这条差异本身就是两条链路
    # 触发语义不同的证据(一个按人按库,一个是部署级全局计数器)。
    assert call_args["self.agent_profile_jobs.note_ask_completed"] == 2
    assert call_args["self.retrieval_experience_jobs.note_ask_completed"] == 0
    assert call_args["self.search_profile_jobs.note_ask_completed"] == 1


def test_the_runtime_counts_only_reasoning_asks_toward_distillation():
    """codex #524 R4 P2:计数与采样同谓词——采样只取 mode='reasoning',计数器
    对 chunk/graph 也 +1 就会拿同一批旧 reasoning run 反复付蒸馏钱。"""
    from app.services import repository_runtime as rr

    source = Path(rr.__file__).read_text(encoding="utf-8")
    body = source[source.index("def _note_ask_completed_compat"):]
    body = body[:body.index("def ", 10)]
    guard_at = body.index('mode_id == "reasoning"')
    p2_call_at = body.index("retrieval_experience_jobs.note_ask_completed")
    assert guard_at < p2_call_at, "P2 链计数必须在 reasoning 模式判据之内"
    # P1 链不受模式过滤(巡固样本读全部模式的轨迹)
    p1_call_at = body.index("agent_profile_jobs.note_ask_completed")
    assert p1_call_at < guard_at, "P1 链不得被模式判据圈住"


def test_the_runtime_completion_lambda_accepts_the_coordinator_arity():
    """codex #524 R5 P1:coordinator 按 (nb, uid, mode_id) 三参调用,runtime 的
    接线 lambda 少一个参数时 TypeError 被 fail-open 吞掉、两条链静默死亡。
    静态钉 lambda 形参表含 mode_id(与「actually wires」守卫互补:那条钉存在,
    这条钉 arity)。"""
    from app.services import repository_runtime as rr

    source = Path(rr.__file__).read_text(encoding="utf-8")
    at = source.index("note_ask_completed=lambda")
    lambda_head = source[at:source.index(":", at)]
    assert "mode_id" in lambda_head, (
        "runtime 的 note_ask_completed 接线 lambda 必须接受 mode_id——"
        "少参的 TypeError 会被协调器 fail-open 吞掉,两条后台链静默死亡"
    )


def test_the_usage_section_total_respects_the_documented_cap():
    """codex #524 R7 P2:报告段余量按已渲染全部文本算——ask 半吃满 + 摘要段
    满载时,整段(含表头)不得实质超过 3000 上限(容差=一行截断粒度)。"""
    from app.services.agent_profile_job import (
        AGENT_PROFILE_USAGE_SECTION_MAX_CHARS,
        UsageStats,
        render_usage_block,
    )

    asks = tuple(
        {"question": "问" * 118, "status": "done",
         "steps": ({"step_type": "retrieve", "count": 0},)}
        for _ in range(40)
    )
    reports = tuple(
        {"question": "报" * 118, "created_at": "2026-08-19T00:00:00+00:00",
         "attempts": ({"query": "方" * 118, "failed": False},) * 4}
        for _ in range(10)
    )
    stats = UsageStats(
        asks=asks, failed_asks=0, zero_hit_steps=40, total_steps=40,
        empty_search_summaries=tuple("查" * 118 for _ in range(12)),
        reports=reports,
    )
    block = render_usage_block(stats)
    assert len(block) <= AGENT_PROFILE_USAGE_SECTION_MAX_CHARS + 400, (
        f"usage 段 {len(block)} 字符,远超文档承诺的 "
        f"{AGENT_PROFILE_USAGE_SECTION_MAX_CHARS}(+一行容差)"
    )


def test_report_samples_follow_completion_order_not_creation_order(harness):
    """codex #524 R15 P2:老报告在 report_limit 份更晚**创建**的报告之后才
    完成(重试/慢跑)时,按 created_at 排窗恰好把「刚触发这次巡固」的那份
    挤出去;采样与 attempt 排序都必须按终态 updated_at。"""
    _add_report(
        harness, "rep-old", user_id=USER_A, question="创建最早、完成最晚",
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-10T00:00:00+00:00",
        attempted=(({"query": "迟到方向", "new": 2, "tries": 1},),),
    )
    for i in range(3):
        _add_report(
            harness, f"rep-new-{i}", user_id=USER_A, question=f"新建 {i}",
            created_at=f"2026-08-0{i + 2}T00:00:00+00:00",
            updated_at=f"2026-08-0{i + 2}T01:00:00+00:00",
            attempted=(({"query": f"q{i}", "new": 1, "tries": 1},),),
        )

    rows = harness["ask_state"].recent_user_report_traces(
        NOTEBOOK_ID, USER_A, report_limit=3, attempt_limit=200
    )

    ids = [row["report_id"] for row in rows]
    assert "rep-old" in ids          # 完成序窗口容得下它
    assert ids[0] == "rep-old"       # 且它就是最新完成的那份
    assert rows[0]["attempts"][0]["query"] == "迟到方向"


def test_report_half_survives_a_full_load_of_empty_search_summaries(harness):
    """codex #524 R17 P2:表头与空检索摘要不计账时,吃满的问题行 + 12 条
    摘要能把 rendered_so_far 顶过总上限,报告段余量归 0——混合使用的成员
    的报告方向从此进不了巡固。ask 半区(表头+问题+摘要)整体钉在一半以内,
    报告段构造性拿到另一半。"""
    for index in range(AGENT_PROFILE_TRACE_SAMPLE):
        _add_ask(
            harness, f"job-{index:02d}", user_id=USER_A,
            question=f"{index:02d}" + "问" * 110,
            created_at=f"2026-08-18T00:{index:02d}:00+00:00",
            steps=({"step_type": "retrieve",
                    "summary": f"空手查询 {index:02d} " + "词" * 100,
                    "detail": {"count": 0}},),
        )
    _add_report(
        harness, "rep-mix", user_id=USER_A,
        question="混合成员的报告" + "题" * 80,
        created_at="2026-08-18T01:00:00+00:00",
        attempted=(({"query": "方向" * 30, "new": 1, "tries": 1},),),
    )

    stats = _service(harness).usage_stats(NOTEBOOK_ID, USER_A)
    block = render_usage_block(stats)

    assert "[Your recent deep reports in this library]" in block
    assert "- 混合成员的报告" in block, (
        "满额空检索摘要不得把报告段饿死到一行都渲染不出"
    )
    assert len(block) <= AGENT_PROFILE_USAGE_SECTION_MAX_CHARS * 1.35
