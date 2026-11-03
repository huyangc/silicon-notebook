"""Backend-neutral cases for the auto user-memory suggestion run (plan T7b;
design §4.2 cursor table, §4.3, §4.4, §5.1, §6.4). SQLite:
``tests/test_memory_suggest_job.py``; PostgreSQL twin:
``tests/postgres/test_memory_suggest_job_pg.py``.

Each case takes an ``Env`` built on a real repository runtime (its
``memory_suggest_store``, ``memory_service``, ``sharing_store`` and
``sharing``), plus ``run`` (a write with ``?`` placeholders), ``rows`` (dict
rows back) and ``json`` (the backend's JSON parameter adapter). The run under
test is a ``MemorySuggestJob`` over those real seats with a fake model
provider, a fake clock and a synchronous submit; the end-to-end case drives
the runtime's own job through ``_note_ask_completed`` and the light pool.

``MEMBER`` (the subject) reads ``NB`` through a membership row; the notebook
is ``OWNER``'s.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable

from app.domain.memory_suggest_contracts import (
    CLARIFIED_SCAN_MAX,
    FAILURE_REASON_INTERRUPTED,
    MEMORY_SUGGEST_FORBIDDEN_EVENT_KEYS,
    MEMORY_SUGGEST_RUN_EVENT,
    MEMORY_SUGGEST_RUN_EVENT_KEYS,
    MEMORY_SUGGEST_WORKLOAD,
    OUTCOME_BACKLOG,
    OUTCOME_DAILY_CAP,
    OUTCOME_DISABLED,
    OUTCOME_DONE,
    OUTCOME_FAILED,
    OUTCOME_MODEL_REJECTED,
    OUTCOME_MODEL_UNAVAILABLE,
    OUTCOME_NO_SIGNAL,
    OUTCOME_SKIPPED,
    SKIP_NO_READ_ACCESS,
    SKIP_USER_DISABLED,
)
from app.services.memory_suggest_job import (
    MODEL_GAVE_UP,
    MemorySuggestJob,
)

OWNER = "u-sj-owner"
MEMBER = "u-sj-member"
NB = "nb-sj"
CONV = "conv-sj"
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
#: The run's clock starts a day after the asks.
START = T0 + timedelta(days=1)

CLOCK_QUESTIONS = (
    "clock gating power saving technique",
    "clock gating power saving methods",
    "clock gating power saving approach",
)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def at(minutes: int) -> str:
    return iso(T0 + timedelta(minutes=minutes))


def instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ------------------------------------------------------------------ fakes
class Clock:
    def __init__(self, start: datetime = START) -> None:
        self.current = start

    def __call__(self) -> str:
        return iso(self.current)

    def advance(self, **delta: float) -> None:
        self.current = self.current + timedelta(**delta)


class CodedError(Exception):
    """A provider failure classified by ``code`` (``ModelInvocationError``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_ITEM_LINE = re.compile(r"^\[(i\d+)\] signal=(\w+)", re.MULTILINE)


def served_items(prompt: str) -> list[str]:
    return [match.group(1) for match in _ITEM_LINE.finditer(prompt)]


def keep_all(prompt: str) -> str:
    return json.dumps({"items": [
        {
            "item_id": item_id, "keep": True, "kind": "term", "conflicts_with_sources": False,
            "title": f"title {item_id}", "content": f"content {item_id}",
            "tags": ["t"], "duplicate_of": "", "refines": "",
        }
        for item_id in served_items(prompt)
    ]})


def decline_all(prompt: str) -> str:
    return json.dumps({"items": [
        {"item_id": item_id, "keep": False, "kind": "term", "title": "", "content": "", "conflicts_with_sources": False,
         "tags": [], "duplicate_of": "", "refines": ""}
        for item_id in served_items(prompt)
    ]})


def out_of_range(prompt: str) -> str:
    return json.dumps({"items": [{"item_id": "i99", "keep": False}]})


class FakeModels:
    """``configured`` / ``chat`` / ``embedding`` of the runtime provider."""

    def __init__(self) -> None:
        self.chat_ready = True
        self.embedding_ready = True
        self.embedding_error: Exception | None = None
        self.reply: Callable[[str], str] = keep_all
        self.error: Exception | None = None
        self.on_chat: Callable[[], None] | None = None
        self.prompts: list[str] = []
        self.embedded: list[list[str]] = []

    def configured(self, workload: str) -> bool:
        if workload == MEMORY_SUGGEST_WORKLOAD:
            return self.chat_ready
        return self.embedding_ready

    def chat(self, workload: str):
        assert workload == MEMORY_SUGGEST_WORKLOAD
        models = self

        class _Chat:
            def chat_json(self, messages, hint, *, max_tokens=None):
                prompt = messages[-1]["content"]
                models.prompts.append(prompt)
                if models.on_chat is not None:
                    models.on_chat()
                if models.error is not None:
                    raise models.error
                return models.reply(prompt)

        return _Chat()

    def embedding(self, workload: str):
        models = self

        class _Embedder:
            def embed_texts(self, texts):
                models.embedded.append(list(texts))
                if models.embedding_error is not None:
                    raise models.embedding_error
                return [[1.0, 0.0] for _ in texts]

        return _Embedder()


class Events:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def emit(self, event: dict) -> None:
        self.items.append(dict(event))

    def runs(self) -> list[dict]:
        return [e for e in self.items if e.get("kind") == MEMORY_SUGGEST_RUN_EVENT]


def sync_submit(fn, *args, name: str, notify_pending: bool, **kwargs):
    assert name.startswith("memorysuggest-") and notify_pending is False
    fn(*args, **kwargs)


# ------------------------------------------------------------------ env
@dataclass
class Env:
    runtime: Any
    run: Callable[[str, tuple], None]
    rows: Callable[[str, tuple], list[dict]]
    json: Callable[[Any], Any]

    def seed(self) -> None:
        for user in (OWNER, MEMBER):
            self.run(
                "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (user, f"{user}@x", user, "user", at(0), at(0)),
            )
        self.run(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (NB, "Shared", OWNER, "ready", at(0), at(0)),
        )
        self.run(
            "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) VALUES (?,?,?,?)",
            (NB, MEMBER, "reader", at(0)),
        )
        self.run(
            "INSERT INTO conversations(id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (CONV, NB, "t", MEMBER, at(0), at(0)),
        )

    def ask(
        self,
        ask_id: str,
        minute: int,
        question: str,
        *,
        clarifications: tuple[tuple[str, str], ...] = (),
        evidence: str = "grounded",
        excerpt: str = "the answer text",
        status: str = "done",
        citations: list | None = None,
        anchors: list | None = None,
    ) -> str:
        """One web ask of ``MEMBER`` (finished unless ``status`` says
        otherwise) with its answer; returns the answer id."""
        answer_id = f"ans-{ask_id}"
        payload: dict[str, Any] = {"answer": excerpt, "evidence_level": evidence, "mode": "chunk"}
        if citations is not None:
            payload["citations"] = citations
        if anchors is not None:
            payload["anchors"] = anchors
        if clarifications:
            payload["intent"] = {"clarification_answers": [
                {"id": f"a{index}", "question": q, "answer": a}
                for index, (q, a) in enumerate(clarifications, start=1)
            ]}
        self.run(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at,conversation_id) "
            "VALUES (?,?,?,?,?,?)",
            (answer_id, NB, question, self.json(payload), at(minute), CONV),
        )
        self.run(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,"
            "status,answer_id,created_at,updated_at,submitted_via,output,report_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ask_id, NB, CONV, MEMBER, "chunk", question, status, answer_id,
                at(minute), at(minute), "web", "answer", "",
            ),
        )
        return answer_id

    def clarified(self, ask_id: str, minute: int, phrase: str, **kwargs: Any) -> str:
        return self.ask(
            ask_id, minute, f"question {ask_id}",
            clarifications=((f"which {ask_id} do you mean", phrase),), **kwargs,
        )

    def short_clarified(self, ask_id: str, minute: int, answer: str, **kwargs: Any) -> str:
        """A clarified ask whose answer and clarification question together
        stay under two keyword terms: its content fingerprint is never
        registered ``proposed`` (plan appendix B2)."""
        return self.ask(
            ask_id, minute, f"question {ask_id}",
            clarifications=(("which", answer),), **kwargs,
        )

    def finish(self, ask_id: str) -> None:
        self.run("UPDATE ask_jobs SET status = 'done' WHERE id = ?", (ask_id,))

    def drafts_per_answer(self) -> dict[str, int]:
        """How many suggestion drafts name each source ask's answer in their
        provenance (a clarified draft binds no answer id)."""
        counts: dict[str, int] = {}
        for draft in self.drafts():
            record = self.runtime.memory_service.get(str(draft["id"]), MEMBER)
            answer_id = str(record.provenance.get("answer_id") or "")
            counts[answer_id] = counts.get(answer_id, 0) + 1
        return counts

    def memory(
        self,
        memory_id: str,
        *,
        status: str = "candidate",
        signal: str | None = None,
        answer_id: str | None = None,
        created: datetime = START,
    ) -> None:
        self.run(
            "INSERT INTO memory_items(id,notebook_id,created_by,source_answer_id,origin,status,"
            "title,content_md,suggested_signal,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id, NB, MEMBER, answer_id, "ask_answer", status, "t", "c", signal,
                iso(created), iso(created),
            ),
        )

    def open_window(self, first_ask_id: str, *, now: str | None = None) -> None:
        """Opt in before the synthetic asks, then deliver their completion."""
        store = self.runtime.memory_suggest_store
        store.set_memory_mode(MEMBER, "suggest", now=at(0))
        assert store.note_signal(NB, MEMBER, ask_job_id=first_ask_id, now=now or iso(START)) >= 1

    def job_row(self) -> dict | None:
        found = self.rows(
            "SELECT status, cursor_created_at, cursor_id, cursor_inclusive, "
            "consecutive_failures, last_outcome, failure_reason, runs, pending_signal "
            "FROM memory_suggest_jobs WHERE notebook_id = ? AND owner_id = ?",
            (NB, MEMBER),
        )
        if not found:
            return None
        row = found[0]
        return {
            "status": str(row["status"]),
            "cursor": (
                instant(row["cursor_created_at"]), str(row["cursor_id"]),
                bool(row["cursor_inclusive"]),
            ),
            "failures": int(row["consecutive_failures"]),
            "outcome": str(row["last_outcome"]),
            "reason": str(row["failure_reason"]),
            "runs": int(row["runs"]),
            "pending": int(row["pending_signal"]),
        }

    def cursor_at(self, ask_id: str, minute: int, inclusive: bool) -> tuple:
        return (instant(at(minute)), ask_id, inclusive)

    def drafts(self) -> list[dict]:
        return self.rows(
            "SELECT id, status, suggested_signal, source_answer_id FROM memory_items "
            "WHERE notebook_id = ? AND created_by = ? AND suggested_signal IS NOT NULL "
            "ORDER BY id",
            (NB, MEMBER),
        )

    def fingerprints(self) -> dict[str, str]:
        return {
            str(row["fingerprint"]): str(row["kind"])
            for row in self.rows(
                "SELECT fingerprint, kind FROM memory_suggest_fingerprints "
                "WHERE notebook_id = ? AND owner_id = ?",
                (NB, MEMBER),
            )
        }

    def quota_used(self) -> int:
        state = self.runtime.memory_suggest_store.user_state(MEMBER)
        return int(state["quota_used"]) if state else 0


@dataclass
class World:
    env: Env
    job: MemorySuggestJob
    models: FakeModels
    clock: Clock
    events: Events
    settings: SimpleNamespace
    submitted: list = field(default_factory=list)

    def start(self) -> bool:
        return self.job.start(NB, MEMBER)

    def last_event(self) -> dict:
        return self.events.runs()[-1]


def _settings(env: Env, **overrides: Any) -> SimpleNamespace:
    values = {
        key: value
        for key, value in env.runtime.settings.model_dump().items()
        if key.startswith("auto_memory_suggest_")
    }
    # Most cases run more model calls in one day than the shipped cap allows.
    values["auto_memory_suggest_daily_cap"] = 100
    # Existing two-signal cases remain isolated from the third signal.
    values["auto_memory_suggest_corrected_enabled"] = False
    values.update(overrides)
    return SimpleNamespace(**values)


def build(env: Env, *, submit: Callable | None = sync_submit, **overrides: Any) -> World:
    env.seed()
    models, clock, events = FakeModels(), Clock(), Events()
    settings = _settings(env, **overrides)
    runtime = env.runtime
    job = MemorySuggestJob(
        store=runtime.memory_suggest_store,
        access=runtime.sharing_store,
        settings=settings,
        models=models,
        event_log=events,
        new_id=runtime.seams.new_id,
        memory=runtime.memory_service,
        clock=clock,
        submit=submit,
    )
    return World(env=env, job=job, models=models, clock=clock, events=events, settings=settings)


# ------------------------------------------------------------------ cursor table
def assert_success_cursor_to_last_fully_processed(env: Env) -> None:
    """Three clarified asks, two items per run: the first run sends two and
    stops the cursor ON the third (inclusive); the second sends the third and
    moves to the end of the ascending scan (the plain ask after it)."""
    world = build(env, auto_memory_suggest_items_per_run=2)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.clarified("ask-2", 2, "delta echo foxtrot")
    env.clarified("ask-3", 3, "golf hotel india")
    env.ask("ask-4", 4, "plain question without clarification")
    env.open_window("ask-1")

    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_DONE and row["status"] == "idle"
    assert row["cursor"] == env.cursor_at("ask-3", 3, True)
    assert row["failures"] == 0
    assert len(env.drafts()) == 2
    assert served_items(world.models.prompts[-1]) == ["i1", "i2"]
    assert "question ask-3" not in world.models.prompts[-1]

    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_DONE
    assert row["cursor"] == env.cursor_at("ask-4", 4, False)
    assert "question ask-3" in world.models.prompts[-1]
    assert len(env.drafts()) == 3
    event = world.last_event()
    assert event["signals_by_kind"] == {"clarified": 1}
    assert event["candidates_written"] == 1 and event["model_status"] == "ok"


def assert_no_signal_advances_to_end_of_ascending_scan_no_quota(env: Env) -> None:
    """More plain asks than the ascending scan reads: the cursor stops at the
    scan's last row, never at the newest ask of the descending window; no
    model call, no quota."""
    world = build(env)
    total = CLARIFIED_SCAN_MAX + 1
    for index in range(total):
        env.ask(f"ask-{index:03d}", index, f"lookup{index:03d}")  # one term: never clustered
    env.open_window("ask-000")

    assert world.start() is True
    row = env.job_row()
    last = CLARIFIED_SCAN_MAX - 1
    assert row["outcome"] == OUTCOME_NO_SIGNAL
    assert row["cursor"] == env.cursor_at(f"ask-{last:03d}", last, False)
    assert world.models.prompts == [] and world.models.embedded == []
    assert env.quota_used() == 0
    assert world.last_event()["model_status"] == "not_called"


def assert_skipped_backlog_disabled_dailycap_cursor_unchanged(env: Env) -> None:
    world = build(env, auto_memory_suggest_daily_cap=1)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    before = env.job_row()

    def unchanged(outcome: str, reason: str = "") -> None:
        row = env.job_row()
        assert row["outcome"] == outcome, row
        assert row["reason"] == reason, row
        assert row["cursor"] == before["cursor"]
        assert row["failures"] == before["failures"] == 0
        assert row["status"] == "idle"
        assert world.models.prompts == []
        assert [d for d in env.drafts() if not str(d["id"]).startswith("mem-open")] == []

    # skipped: read access lost (the job row survives a raw membership delete)
    env.run("DELETE FROM notebook_members WHERE notebook_id = ? AND user_id = ?", (NB, MEMBER))
    assert world.start() is True
    unchanged(OUTCOME_SKIPPED, SKIP_NO_READ_ACCESS)
    env.run(
        "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) VALUES (?,?,?,?)",
        (NB, MEMBER, "reader", at(0)),
    )

    # backlog: five open suggestions of the owner's here
    for index in range(5):
        env.memory(f"mem-open-{index}", signal="clarified")
    assert world.start() is True
    unchanged(OUTCOME_BACKLOG)
    env.run("DELETE FROM memory_items WHERE notebook_id = ? AND created_by = ?", (NB, MEMBER))

    # disabled: the deployment switch is off
    world.settings.auto_memory_suggest_enabled = False
    assert world.start() is True
    unchanged(OUTCOME_DISABLED)
    world.settings.auto_memory_suggest_enabled = True

    # daily_cap: today's one call is already spent
    store = env.runtime.memory_suggest_store
    assert store.reserve_daily_quota(
        MEMBER, today_utc=world.clock.current.date().isoformat(), cap=1, now=world.clock(),
    )
    assert world.start() is True
    unchanged(OUTCOME_DAILY_CAP)
    assert [e["outcome"] for e in world.events.runs()] == [
        OUTCOME_SKIPPED, OUTCOME_BACKLOG, OUTCOME_DISABLED, OUTCOME_DAILY_CAP,
    ]


def assert_model_rejected_retries_same_batch_no_negative_cache(env: Env) -> None:
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.clarified("ask-2", 2, "delta echo foxtrot")
    env.open_window("ask-1")
    before = env.job_row()

    def half_good(prompt: str) -> str:
        good = json.loads(decline_all(prompt))["items"][:1]
        return json.dumps({"items": [*good, {"item_id": "i9", "keep": False}]})

    world.models.reply = half_good
    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_MODEL_REJECTED
    assert row["reason"] == "item_id_out_of_range"
    assert row["cursor"] == before["cursor"] and row["failures"] == 1
    assert env.fingerprints() == {} and env.drafts() == []
    first_prompt = world.models.prompts[-1]

    world.models.reply = keep_all
    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_DONE and row["failures"] == 0
    assert row["cursor"] == env.cursor_at("ask-2", 2, False)
    # The same batch went out again.
    assert served_items(world.models.prompts[-1]) == served_items(first_prompt)
    assert "question ask-1" in world.models.prompts[-1]
    assert len(env.drafts()) == 2


def assert_three_failures_give_up_event_and_cursor_passes(env: Env) -> None:
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.ask("ask-2", 2, "plain question")
    env.open_window("ask-1")
    start_cursor = env.job_row()["cursor"]

    world.models.error = CodedError("malformed_response")
    assert world.start() is True
    assert env.job_row()["failures"] == 1 and env.job_row()["cursor"] == start_cursor
    world.models.error = None
    world.models.reply = lambda prompt: "not json at all"
    assert world.start() is True
    assert env.job_row()["failures"] == 2 and env.job_row()["cursor"] == start_cursor
    assert world.last_event()["model_status"] == "rejected"
    world.models.reply = out_of_range
    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_MODEL_REJECTED
    assert row["failures"] == 0
    assert row["cursor"] == env.cursor_at("ask-2", 2, False)
    event = world.last_event()
    assert event["outcome"] == OUTCOME_MODEL_REJECTED and event["model_status"] == MODEL_GAVE_UP
    assert len(world.models.prompts) == 3
    assert env.fingerprints() == {} and env.drafts() == []
    # Each failure spent one call of the day.
    assert env.quota_used() == 3


def assert_interrupted_run_counts_as_one_failure(env: Env) -> None:
    """A run a restart killed (claimed, never settled) is swept as one
    failure of its batch (B4): two more failures give the batch up."""
    world = build(env, submit=lambda *a, **k: None)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    start_cursor = env.job_row()["cursor"]
    assert world.start() is True
    assert env.job_row()["status"] == "running"

    assert world.job.sweep_on_start() == 1
    row = env.job_row()
    assert row["status"] == "idle" and row["outcome"] == OUTCOME_FAILED
    assert row["reason"] == FAILURE_REASON_INTERRUPTED and row["failures"] == 1

    world.job._submit = sync_submit
    world.models.reply = out_of_range
    assert world.start() is True
    assert env.job_row()["failures"] == 2 and env.job_row()["cursor"] == start_cursor
    assert world.start() is True
    row = env.job_row()
    assert row["failures"] == 0 and row["cursor"] == env.cursor_at("ask-1", 1, False)
    assert world.last_event()["model_status"] == MODEL_GAVE_UP


def assert_single_flight_concurrent_start(env: Env) -> None:
    """Two concurrent starts: one claim, one submission; the other returns
    at once (the trigger calls ``start_job`` on every ask past the
    threshold)."""
    submitted: list[str] = []
    lock = threading.Lock()

    def record(fn, *args, name, notify_pending, **kwargs):
        with lock:
            submitted.append(kwargs["claim_token"])

    world = build(env, submit=record)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def go() -> None:
        barrier.wait(timeout=10)
        outcome = world.start()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert sorted(results) == [False, True]
    assert len(submitted) == 1
    assert env.job_row()["status"] == "running"
    # While it runs, a further start is a no-op too.
    assert world.start() is False
    assert len(submitted) == 1


def assert_submission_failure_settles_the_claim(env: Env) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("pool closed")

    world = build(env, submit=broken)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    assert world.start() is False
    row = env.job_row()
    assert row["status"] == "idle" and row["outcome"] == OUTCOME_FAILED
    assert row["reason"] == "submission_failed" and row["failures"] == 1
    assert world.last_event()["outcome"] == OUTCOME_FAILED


# ------------------------------------------------------------------ zero-call paths
def assert_backlog_gate_no_model_call(env: Env) -> None:
    world = build(env, auto_memory_suggest_backlog_max=2)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    env.memory("mem-a", signal="clarified")
    env.memory("mem-b", signal="repeated")
    # Adopted / dismissed suggestions and ordinary candidates are no backlog.
    env.memory("mem-c", signal="clarified", status="confirmed")
    env.memory("mem-d", signal="clarified", status="rejected")
    env.memory("mem-e", status="candidate")
    assert world.start() is True
    assert env.job_row()["outcome"] == OUTCOME_BACKLOG
    assert world.models.prompts == [] and world.models.embedded == []
    assert env.quota_used() == 0

    env.run("DELETE FROM memory_items WHERE id = ?", ("mem-b",))
    assert world.start() is True
    assert env.job_row()["outcome"] == OUTCOME_DONE
    assert len(world.models.prompts) == 1


def assert_delete_window_skips(env: Env) -> None:
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    before = env.job_row()
    env.run(
        "UPDATE notebooks SET status = 'pending_delete', delete_after = ? WHERE id = ?",
        (iso(START + timedelta(days=7)), NB),
    )
    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_SKIPPED and row["reason"] == SKIP_NO_READ_ACCESS
    assert row["cursor"] == before["cursor"]
    assert world.models.prompts == [] and env.drafts() == []


def assert_lost_read_skips(env: Env) -> None:
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    before = env.job_row()
    env.run("DELETE FROM notebook_members WHERE notebook_id = ? AND user_id = ?", (NB, MEMBER))
    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_SKIPPED and row["reason"] == SKIP_NO_READ_ACCESS
    assert row["cursor"] == before["cursor"] and row["failures"] == 0
    assert world.models.prompts == [] and env.quota_used() == 0


def assert_switch_off_between_claim_and_run_skips(env: Env) -> None:
    """The claimed row survives a switch row flipped off by hand: the run
    re-checks the switch first."""
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    env.run("UPDATE memory_suggest_users SET enabled = 0 WHERE owner_id = ?", (MEMBER,))
    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_SKIPPED and row["reason"] == SKIP_USER_DISABLED
    assert world.models.prompts == []


# ------------------------------------------------------------------ revocation mid-call
def assert_disable_during_model_call_writes_nothing(env: Env) -> None:
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.clarified("ask-2", 2, "delta echo foxtrot")
    env.open_window("ask-1")

    def keep_one_decline_one(prompt: str) -> str:
        kept = json.loads(keep_all(prompt))["items"][0]
        declined = json.loads(decline_all(prompt))["items"][1]
        return json.dumps({"items": [kept, declined]})

    world.models.reply = keep_one_decline_one
    world.models.on_chat = lambda: env.runtime.memory_suggest_service.set_mode(MEMBER, "manual")
    assert world.start() is True
    assert env.job_row() is None  # turning the switch off deleted the row
    event = world.last_event()
    assert event["outcome"] == OUTCOME_SKIPPED and event["candidates_written"] == 0
    assert env.drafts() == [] and env.fingerprints() == {}


def assert_member_removed_during_call_writes_nothing(env: Env) -> None:
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    world.models.on_chat = lambda: env.runtime.sharing.remove_member(NB, MEMBER)
    assert world.start() is True
    event = world.last_event()
    assert event["outcome"] == OUTCOME_SKIPPED
    assert env.drafts() == [] and env.fingerprints() == {}
    assert env.job_row() is None


# ------------------------------------------------------------------ caps and caches
def assert_at_most_three_candidates_per_run(env: Env) -> None:
    world = build(env)
    phrases = (
        "alpha bravo charlie", "delta echo foxtrot", "golf hotel india",
        "juliet kilo lima", "mike november oscar",
    )
    for index, phrase in enumerate(phrases, start=1):
        env.clarified(f"ask-{index}", index, phrase)
    env.open_window("ask-1")
    assert world.start() is True
    drafts = env.drafts()
    assert len(drafts) == 3
    assert {d["status"] for d in drafts} == {"candidate"}
    assert {d["suggested_signal"] for d in drafts} == {"clarified"}
    assert all(d["source_answer_id"] is None for d in drafts)
    event = world.last_event()
    assert event["candidates_written"] == 3 and event["signals_by_kind"] == {"clarified": 5}
    # Kept beyond the cap: neither written nor cached as declined. Each draft
    # registers its content and its ask fingerprint (appendix B18).
    assert set(env.fingerprints().values()) == {"proposed"}
    assert len(env.fingerprints()) == 6
    # ... and not lost (review P3-1): the cursor stops ON the first of them.
    assert env.job_row()["cursor"] == env.cursor_at("ask-4", 4, True)


def assert_kept_over_cap_proposed_next_run_without_duplicates(env: Env) -> None:
    """Review P3-1: the clarified items the model kept beyond the per-run cap
    are sent again by the next run and written then; nothing is written
    twice -- short answers (content fingerprint not registrable) included."""
    world = build(env)
    env.short_clarified("ask-1", 1, "2024")
    env.short_clarified("ask-2", 2, "2025")
    env.short_clarified("ask-3", 3, "2026")
    env.short_clarified("ask-4", 4, "2027")
    env.clarified("ask-5", 5, "mike november oscar")
    env.open_window("ask-1")
    assert world.start() is True
    assert len(env.drafts()) == 3
    assert env.job_row()["cursor"] == env.cursor_at("ask-4", 4, True)

    assert world.start() is True
    assert served_items(world.models.prompts[-1]) == ["i1", "i2"]
    assert "question ask-4" in world.models.prompts[-1]
    assert "question ask-3" not in world.models.prompts[-1]
    assert env.drafts_per_answer() == {f"ans-ask-{n}": 1 for n in range(1, 6)}
    assert env.job_row()["cursor"] == env.cursor_at("ask-5", 5, False)


def assert_cursor_held_at_an_earlier_unfinished_ask(env: Env) -> None:
    """Review open question 2: an earlier ask of the same person still
    running while a later one finished. The run handles the finished one
    but stops its cursor ON the running one; once that finishes, the next
    run picks it up -- and does not propose the later (short-answer) ask a
    second time."""
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.short_clarified("ask-2", 2, "2024", status="running")
    env.short_clarified("ask-3", 3, "2025")
    env.open_window("ask-1")
    assert world.start() is True
    assert env.drafts_per_answer() == {"ans-ask-1": 1, "ans-ask-3": 1}
    assert env.job_row()["cursor"] == env.cursor_at("ask-2", 2, True)

    env.finish("ask-2")
    assert world.start() is True
    assert "question ask-2" in world.models.prompts[-1]
    assert "question ask-3" not in world.models.prompts[-1]
    assert env.drafts_per_answer() == {"ans-ask-1": 1, "ans-ask-2": 1, "ans-ask-3": 1}
    assert env.job_row()["cursor"] == env.cursor_at("ask-3", 3, False)


def assert_running_ask_before_the_window_holds_nothing(env: Env) -> None:
    """An unfinished ask older than the cursor is outside the window: the
    cursor never moves back to it."""
    world = build(env)
    env.short_clarified("ask-0", 0, "2023", status="running")
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    assert world.start() is True
    assert env.job_row()["cursor"] == env.cursor_at("ask-1", 1, False)


def assert_finished_between_sampling_reads_keeps_early_fence(env: Env, *, later_clarified: bool) -> None:
    """The first scan has a later row, but misses the still-running earlier
    ask. Complete it immediately after that read, before any other sample or
    guard read. The run must retain its BEFORE-scan fence, even with no signal
    in the nonempty first scan; the next run must draft the missed ask once.
    """
    from unittest.mock import patch

    world = build(env)
    env.ask("ask-0", 0, "starting boundary")
    env.open_window("ask-0")
    assert world.start() is True
    assert env.job_row()["cursor"] == env.cursor_at("ask-0", 0, False)
    env.clarified("ask-1", 1, "alpha bravo charlie", status="running")
    if later_clarified:
        env.clarified("ask-2", 2, "delta echo foxtrot")
    else:
        env.ask("ask-2", 2, "later completed question")
    store = env.runtime.memory_suggest_store
    original = store.clarified_scan
    sampled = []

    def complete_after_read(*args, **kwargs):
        rows = original(*args, **kwargs)
        sampled.append([row["id"] for row in rows])
        env.finish("ask-1")
        return rows

    with patch.object(store, "clarified_scan", side_effect=complete_after_read):
        assert world.start() is True
    assert sampled == [["ask-2"]]
    assert env.job_row()["cursor"] == env.cursor_at("ask-1", 1, True)
    assert "ans-ask-1" not in env.drafts_per_answer()
    assert world.start() is True
    expected = {"ans-ask-1": 1, **({"ans-ask-2": 1} if later_clarified else {})}
    assert env.drafts_per_answer() == expected
    assert env.job_row()["cursor"] == env.cursor_at("ask-2", 2, False)


def assert_old_running_ask_cannot_hide_in_window_running_ask(env: Env) -> None:
    world = build(env)
    env.clarified("old-running", 0, "old outside window", status="running")
    env.ask("ask-1", 1, "starting boundary")
    env.clarified("ask-2", 2, "alpha bravo charlie", status="running")
    env.clarified("ask-3", 3, "delta echo foxtrot")
    env.open_window("ask-1")
    assert world.start() is True
    assert env.job_row()["cursor"] == env.cursor_at("ask-2", 2, True)
    env.finish("ask-2")
    assert world.start() is True
    assert env.drafts_per_answer() == {"ans-ask-2": 1, "ans-ask-3": 1}


def assert_same_batch_content_fingerprint_drafts_once(env: Env, *, write_cap: int) -> None:
    world = build(env, auto_memory_suggest_candidates_per_run=write_cap)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.clarified("ask-2", 2, "alpha bravo charlie")
    env.clarified("ask-3", 3, "delta echo foxtrot")
    env.clarified("ask-4", 4, "golf hotel india")
    env.open_window("ask-1")
    assert world.start() is True
    assert "question ask-1" in world.models.prompts[0]
    assert "question ask-2" not in world.models.prompts[0]
    if write_cap == 1:
        assert env.job_row()["cursor"] == env.cursor_at("ask-3", 3, True)
        assert world.start() is True
        assert env.job_row()["cursor"] == env.cursor_at("ask-4", 4, True)
        assert world.start() is True
    assert env.drafts_per_answer() == {"ans-ask-1": 1, "ans-ask-3": 1, "ans-ask-4": 1}
    calls = len(world.models.prompts)
    assert world.start() is True
    assert len(world.models.prompts) == calls
    assert env.drafts_per_answer() == {"ans-ask-1": 1, "ans-ask-3": 1, "ans-ask-4": 1}


def assert_offset_microsecond_rescan_drafts_each_ask_once(env: Env, *, submillisecond: bool) -> None:
    world = build(env)
    env.ask("start", 0, "starting boundary")
    env.open_window("start")
    assert world.start() is True
    env.clarified("a", 2, "alpha bravo charlie")
    env.clarified("z", 1, "delta echo foxtrot", status="running")
    if submillisecond:
        timestamps = {"z": "2026-10-01T09:00:00.000100+01:00", "a": "2026-10-01T08:00:00.000200+00:00"}
        first, inclusive, last = "z", True, "a"
    else:
        timestamps = {"z": "2026-10-01T08:00:00.000100+00:00", "a": "2026-10-01T09:00:00.000100+01:00"}
        first, inclusive, last = "a", False, "z"
    for ask_id, stamp in timestamps.items():
        env.run("UPDATE ask_jobs SET created_at=? WHERE id=?", (stamp, ask_id))
    assert world.start() is True
    assert env.job_row()["cursor"] == (instant(timestamps[first]), first, inclusive)
    assert env.drafts_per_answer() == {"ans-a": 1}
    env.finish("z")
    assert world.start() is True
    assert env.drafts_per_answer() == {"ans-a": 1, "ans-z": 1}
    assert env.job_row()["cursor"] == (instant(timestamps[last]), last, False)
    calls = len(world.models.prompts)
    assert world.start() is True
    assert len(world.models.prompts) == calls
    assert env.drafts_per_answer() == {"ans-a": 1, "ans-z": 1}


def assert_failed_settle_resend_writes_no_duplicates(env: Env) -> None:
    """Review P3-2: a run whose settle fails leaves its row ``running``; the
    restart sweep makes it a failure and the next run scans the same batch.
    The drafts it wrote (a short answer among them) and the "no" it cached
    were written in the same transaction, so nothing is proposed again."""
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.short_clarified("ask-2", 2, "2024")
    env.clarified("ask-3", 3, "delta echo foxtrot")

    def keep_two_decline_third(prompt: str) -> str:
        kept = json.loads(keep_all(prompt))["items"]
        declined = json.loads(decline_all(prompt))["items"]
        return json.dumps({"items": [kept[0], kept[1], declined[2]]})

    world.models.reply = keep_two_decline_third
    env.open_window("ask-1")
    store = env.runtime.memory_suggest_store

    def broken_settle(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("settle failed")

    store.settle = broken_settle  # shadows the method on this instance
    try:
        assert world.start() is True
    finally:
        del store.settle
    assert env.job_row()["status"] == "running"
    assert env.drafts_per_answer() == {"ans-ask-1": 1, "ans-ask-2": 1}
    assert "declined" in set(env.fingerprints().values())
    assert world.job.sweep_on_start() == 1
    world.models.reply = keep_all
    assert world.start() is True
    assert len(world.models.prompts) == 1  # nothing new to send
    assert env.job_row()["outcome"] == OUTCOME_NO_SIGNAL
    assert env.drafts_per_answer() == {"ans-ask-1": 1, "ans-ask-2": 1}


def assert_crashed_batch_given_up_after_three_restarts(env: Env) -> None:
    """Review P3-1 (quality): a batch whose run dies with the process three
    times is never sent again -- the next run samples, calls no model and
    gives up: cursor past the batch, count reset, ``gave_up`` event."""
    world = build(env, submit=lambda *a, **k: None)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.ask("ask-2", 2, "plain question")
    env.open_window("ask-1")
    start_cursor = env.job_row()["cursor"]
    for crash in range(1, 4):
        assert world.start() is True  # claimed; the "process" dies
        assert world.job.sweep_on_start() == 1
        row = env.job_row()
        assert row["failures"] == crash and row["cursor"] == start_cursor
    world.job._submit = sync_submit
    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_FAILED and row["reason"] == FAILURE_REASON_INTERRUPTED
    assert row["failures"] == 0
    assert row["cursor"] == env.cursor_at("ask-2", 2, False)
    assert world.models.prompts == [] and env.quota_used() == 0
    assert world.last_event()["model_status"] == MODEL_GAVE_UP
    assert env.drafts() == []


def assert_declined_cache_not_written_after_the_exit(env: Env) -> None:
    """Review P3-2: the member's exit completes right after the run's write
    transaction committed. The "no" was cached IN that transaction, so the
    exit's finish removed it with the rest -- nothing is written back after
    the exit (before the fix the negative cache was a separate, later write
    that outlived the exit)."""
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    world.models.reply = decline_all
    service = env.runtime.memory_service

    class ExitRightAfterTheWrite:
        def create_suggestions(self, **kwargs: Any) -> Any:
            result = service.create_suggestions(**kwargs)
            service.leave_notebook(NB, MEMBER, 0)
            return result

        def expire_suggestions(self, **kwargs: Any) -> int:
            return service.expire_suggestions(**kwargs)

    world.job.memory = ExitRightAfterTheWrite()
    assert world.start() is True
    assert world.last_event()["outcome"] == OUTCOME_DONE
    assert world.last_event()["declined_cached"] == 1
    assert not env.rows(
        "SELECT 1 AS x FROM notebook_members WHERE notebook_id = ? AND user_id = ?",
        (NB, MEMBER))
    assert env.fingerprints() == {} and env.drafts() == []
    assert env.job_row() is None


def assert_declined_not_resent_within_30_days_then_resent(env: Env) -> None:
    world = build(env)
    phrase = "three point three volt rail"
    env.clarified("ask-1", 1, phrase)
    env.open_window("ask-1")
    world.models.reply = decline_all
    assert world.start() is True
    assert set(env.fingerprints().values()) == {"declined"}
    assert world.last_event()["declined_cached"] == 1

    world.clock.advance(days=10)
    env.clarified("ask-2", 2, phrase)
    assert world.start() is True
    assert env.job_row()["outcome"] == OUTCOME_NO_SIGNAL
    assert len(world.models.prompts) == 1
    assert world.last_event()["deduped"] == 1

    world.clock.advance(days=21)  # 31 days after the "no"
    env.clarified("ask-3", 3, phrase)
    world.models.reply = keep_all
    assert world.start() is True
    assert env.job_row()["outcome"] == OUTCOME_DONE
    assert len(world.models.prompts) == 2
    # The content hash is upgraded to proposed; ask-1's own (expired) "no"
    # stays as it was, ask-3's ask hash is proposed (appendix B18).
    assert sorted(env.fingerprints().values()) == ["declined", "proposed", "proposed"]


def assert_no_re_suggest_after_reject_expire_delete_unchanged_delete(env: Env) -> None:
    world = build(env)
    service = env.runtime.memory_service
    memory_store = env.runtime.memory_store
    phrases = {
        "reject": "alpha bravo charlie",
        "expire": "delta echo foxtrot",
        "delete": "golf hotel india",
        "bulk_delete": "juliet kilo lima",
        "unchanged_delete": "mike november oscar",
    }
    minute = 0
    first = True
    for how, phrase in phrases.items():
        minute += 1
        env.clarified(f"ask-{how}-1", minute, phrase)
        if first:
            env.open_window(f"ask-{how}-1")
            first = False
        calls = len(world.models.prompts)
        assert world.start() is True, how
        assert len(world.models.prompts) == calls + 1, how
        (draft,) = [d for d in env.drafts() if d["status"] == "candidate"]
        draft_id = str(draft["id"])
        if how == "reject":
            service.reject(draft_id, MEMBER)
        elif how == "expire":
            world.clock.advance(days=31)
        elif how == "delete":
            service.delete(draft_id, MEMBER)
        elif how == "bulk_delete":
            assert service.bulk_delete(MEMBER, [draft_id]) == 1
        else:
            # The transfer's "delete only if unchanged" removes adopted
            # memories only; adopt by hand (no embedding / KG side jobs).
            env.run("UPDATE memory_items SET status = 'confirmed' WHERE id = ?", (draft_id,))
            (revision,) = env.rows(
                "SELECT MAX(revision) AS r FROM memory_revisions WHERE memory_id = ?",
                (draft_id,),
            )
            assert memory_store.delete_memory_if_unchanged(draft_id, MEMBER, int(revision["r"]))
        minute += 1
        env.clarified(f"ask-{how}-2", minute, phrase)
        calls = len(world.models.prompts)
        assert world.start() is True, how
        if how == "expire":
            assert [d["status"] for d in env.drafts() if str(d["id"]) == draft_id] == ["rejected"]
        assert env.job_row()["outcome"] == OUTCOME_NO_SIGNAL, how
        assert len(world.models.prompts) == calls, how


def assert_existing_memory_answers_filtered(env: Env) -> None:
    world = build(env)
    answer = env.clarified("ask-1", 1, "alpha bravo charlie")
    for index, question in enumerate(CLOCK_QUESTIONS, start=2):
        env.ask(f"ask-{index}", index, question)
    env.open_window("ask-1")
    env.memory("mem-own", status="rejected", answer_id=answer)
    env.memory("mem-cluster", status="confirmed", answer_id="ans-ask-3")
    assert world.start() is True
    assert env.job_row()["outcome"] == OUTCOME_NO_SIGNAL
    assert world.models.prompts == []


def assert_repeated_cluster_binds_the_chosen_answer(env: Env) -> None:
    world = build(env)
    citations = [{"source_id": "s-3", "element_id": "e-3", "index": 1}]
    anchors = [{"source_id": "s-3", "text": "anchor 3"}]
    for index, question in enumerate(CLOCK_QUESTIONS, start=1):
        extra = {"citations": citations, "anchors": anchors} if index == 3 else {
            "citations": [{"source_id": f"s-{index}"}]}
        env.ask(f"ask-{index}", index, question, excerpt=f"excerpt {index}", **extra)
    env.open_window("ask-1")
    assert world.start() is True
    (draft,) = env.drafts()
    assert draft["suggested_signal"] == "repeated"
    assert draft["source_answer_id"] == "ans-ask-3"  # the most recent grounded answer
    record = env.runtime.memory_service.get(str(draft["id"]), MEMBER)
    provenance = record.provenance
    assert provenance["answer_id"] == "ans-ask-3"
    assert provenance["question"] == CLOCK_QUESTIONS[2]
    assert provenance["evidence_level"] == "grounded"
    # The bound answer's own citations and anchors (appendix B14).
    assert provenance["citations"] == citations and provenance["anchors"] == anchors
    suggestion = provenance["suggestion"]
    assert suggestion["signal"] == "repeated" and suggestion["kind"] == "term"
    assert suggestion["cluster_size"] == 3 and suggestion["refines"] is None
    assert sorted(suggestion["answer_ids"]) == ["ans-ask-1", "ans-ask-2", "ans-ask-3"]
    assert len(env.fingerprints()) == 3


# ------------------------------------------------------------------ cursor and switch
def assert_cursor_window_start_includes_trigger(env: Env) -> None:
    world = build(env)
    env.clarified("ask-0", 0, "zulu yankee xray")  # before the window opened
    env.clarified("ask-1", 1, "alpha bravo charlie")  # the triggering ask
    env.open_window("ask-1")
    assert env.job_row()["cursor"] == env.cursor_at("ask-1", 1, True)
    assert world.start() is True
    prompt = world.models.prompts[-1]
    assert "question ask-1" in prompt and "question ask-0" not in prompt
    assert env.job_row()["cursor"] == env.cursor_at("ask-1", 1, False)


def assert_reenable_does_not_backfill(env: Env) -> None:
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    assert world.start() is True
    env.runtime.memory_suggest_service.set_mode(MEMBER, "manual")
    env.clarified("ask-2", 2, "delta echo foxtrot")  # asked while it was off
    env.clarified("ask-3", 3, "golf hotel india")
    env.open_window("ask-3")  # on again; the first web ask after opens the row
    assert world.start() is True
    prompt = world.models.prompts[-1]
    assert "question ask-3" in prompt and "question ask-2" not in prompt


def assert_disabled_class_zero_model_calls(env: Env) -> None:
    world = build(env, auto_memory_suggest_clarified_enabled=False)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.clarified("ask-2", 2, "delta echo foxtrot")
    env.open_window("ask-1")
    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_NO_SIGNAL
    assert row["cursor"] == env.cursor_at("ask-2", 2, False)
    assert world.models.prompts == [] and world.models.embedded == []


# ------------------------------------------------------------------ telemetry and degradation
def assert_event_keys_whitelisted(env: Env) -> None:
    world = build(env)
    secret = "confidential widget roadmap"
    env.clarified("ask-1", 1, secret)
    env.open_window("ask-1")
    world.models.reply = out_of_range
    world.start()  # model_rejected
    world.models.reply = keep_all
    world.start()  # done
    world.start()  # no_signal
    env.run("DELETE FROM notebook_members WHERE notebook_id = ? AND user_id = ?", (NB, MEMBER))
    world.start()  # skipped
    runs = world.events.runs()
    assert [e["outcome"] for e in runs] == [
        OUTCOME_MODEL_REJECTED, OUTCOME_DONE, OUTCOME_NO_SIGNAL, OUTCOME_SKIPPED,
    ]
    for event in runs:
        assert set(event) - {"kind"} == set(MEMORY_SUGGEST_RUN_EVENT_KEYS)
        assert not set(event) & MEMORY_SUGGEST_FORBIDDEN_EVENT_KEYS
    dumped = json.dumps(world.events.items)
    assert secret not in dumped and "question ask-1" not in dumped
    assert "title i1" not in dumped and "content i1" not in dumped


def assert_model_unavailable_terminal(env: Env) -> None:
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.open_window("ask-1")
    before = env.job_row()
    world.models.chat_ready = False
    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_MODEL_UNAVAILABLE and row["reason"] == "model_unconfigured"
    assert row["cursor"] == before["cursor"] and row["failures"] == 1
    assert world.models.prompts == [] and env.quota_used() == 0
    assert world.last_event()["model_status"] == "unavailable"

    # Bound at the readiness check but refused at the call (a provider that
    # lost its binding in between): the same terminal state.
    world.models.chat_ready = True
    world.models.error = CodedError("model_not_configured")
    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_MODEL_UNAVAILABLE and row["failures"] == 2
    assert env.drafts() == [] and env.fingerprints() == {}

    # Any other provider failure is ``failed:<class name>``, same batch.
    world.models.error = RuntimeError("boom")
    assert world.start() is True
    row = env.job_row()
    assert row["outcome"] == OUTCOME_FAILED and row["reason"] == "RuntimeError"
    assert world.last_event()["model_status"] == MODEL_GAVE_UP


def assert_embedding_unavailable_lexical_only(env: Env) -> None:
    world = build(env)
    env.clarified("ask-1", 1, "alpha bravo charlie")
    env.clarified("ask-2", 2, "delta echo foxtrot")
    env.clarified("ask-3", 3, "golf hotel india")
    env.open_window("ask-1")
    env.memory("mem-near", status="rejected", signal="clarified")

    world.models.embedding_ready = False
    world.models.reply = lambda prompt: json.dumps({"items": json.loads(keep_all(prompt))["items"][:1]})
    assert world.start() is True
    assert env.job_row()["outcome"] == OUTCOME_DONE
    assert world.models.embedded == []
    assert world.last_event()["neighbors_vector"] is False

    world.models.embedding_ready = True
    world.models.embedding_error = RuntimeError("embedding down")
    env.clarified("ask-4", 4, "juliet kilo lima")
    assert world.start() is True
    assert env.job_row()["outcome"] == OUTCOME_DONE
    assert world.last_event()["neighbors_vector"] is False

    world.models.embedding_error = None
    env.clarified("ask-5", 5, "mike november oscar")
    assert world.start() is True
    assert env.job_row()["outcome"] == OUTCOME_DONE
    assert world.last_event()["neighbors_vector"] is True
    # One batched call for the whole run.
    assert world.models.embedded[-1] == ["mike november oscar"]


# ------------------------------------------------------------------ end to end
def assert_end_to_end_clarified_web_ask(env: Env) -> None:
    """The real runtime: an opted-in member's finished web ask that carried
    clarification answers reaches ``_note_ask_completed``; the trigger
    starts the runtime's job in the light pool; the run writes one
    clarified draft with the G13 provenance."""
    env.seed()
    runtime = env.runtime
    models = FakeModels()
    runtime.memory_suggest_job.models = models
    runtime.memory_suggest_service.now = lambda: at(0)
    runtime.memory_suggest_service.set_mode(MEMBER, "suggest")
    env.ask(
        "ask-e2e", 1, "What does BMS mean here?",
        clarifications=(("Which BMS do you mean?", "battery management system board"),),
    )
    runtime._note_ask_completed(
        NB, MEMBER, "chunk", ask_job_id="ask-e2e", submitted_via="web",
        had_clarification=True,
    )
    deadline = time.monotonic() + 30
    row = env.job_row()
    while time.monotonic() < deadline and (row is None or row["runs"] < 1):
        time.sleep(0.05)
        row = env.job_row()
    assert row is not None and row["runs"] == 1, row
    assert row["outcome"] == OUTCOME_DONE and row["status"] == "idle"
    assert len(models.prompts) == 1
    (draft,) = env.drafts()
    assert draft["status"] == "candidate" and draft["suggested_signal"] == "clarified"
    assert draft["source_answer_id"] is None
    record = runtime.memory_service.get(str(draft["id"]), MEMBER)
    assert record.origin == "ask_answer"
    provenance = record.provenance
    for key in ("question", "answer_id", "conversation_id", "mode", "evidence_level"):
        assert key in provenance, key
    assert provenance["answer_id"] == "ans-ask-e2e"
    assert provenance["question"] == "What does BMS mean here?"
    assert provenance["conversation_id"] == CONV
    assert "citations" not in provenance and "anchors" not in provenance
    assert provenance["suggestion"] == {
        "signal": "clarified", "kind": "term", "answer_ids": ["ans-ask-e2e"],
        "cluster_size": 1, "refines": None, "conflicts_with_sources": False,
    }
