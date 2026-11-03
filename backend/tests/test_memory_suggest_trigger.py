"""``MemorySuggestTrigger`` -- the auto user-memory decision behind the fourth
Ask-completed observer port (design 2026-10-28 §4.1 / §4.3; plan T6).

Unit cases drive the trigger with a spy store and a spy ``start_job``; the
end-to-end cases run a real SQLite runtime through ``_note_ask_completed``
(the compat path and the production observer host)."""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.request_context import set_request_user
from app.domain.extensions import CompletedAskNotification
from app.services.memory_suggest_trigger import MemorySuggestTrigger

NB = "nb-trig"
OWNER = "u-trig-owner"
FRIEND = "u-trig-friend"
NOW = "2026-10-29T09:00:00+00:00"


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "auto_memory_suggest_enabled": True,
        "auto_memory_suggest_clarified_enabled": True,
        "auto_memory_suggest_repeated_enabled": True,
        "auto_memory_suggest_trigger": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _SpyStore:
    """``user_state`` / ``note_signal`` only -- the two calls the trigger may
    make. ``note_signal`` returns the running count, or 0 when ``readable`` is
    off (the store's own read-access gate, plan B9)."""

    def __init__(self, *, enabled: bool | None = True, readable: bool = True) -> None:
        self.calls: list[tuple] = []
        self.enabled = enabled
        self.readable = readable
        self.count = 0
        self.fail_on: str = ""

    def user_state(self, owner_id: str):
        self.calls.append(("user_state", owner_id))
        if self.fail_on == "user_state":
            raise RuntimeError("secret owner u-trig-owner")
        if self.enabled is None:
            return None
        return {"owner_id": owner_id, "enabled": self.enabled}

    def note_signal(self, notebook_id: str, owner_id: str, *, ask_job_id: str, now: str) -> int:
        self.calls.append(("note_signal", notebook_id, owner_id, ask_job_id))
        if self.fail_on == "note_signal":
            raise RuntimeError("secret sql")
        if not self.readable:
            return 0
        self.count += 1
        return self.count


def _trigger(store, *, start_job="spy", **settings):
    started: list[tuple[str, str]] = []
    job = (lambda nb, owner: started.append((nb, owner))) if start_job == "spy" else start_job
    return MemorySuggestTrigger(store, _settings(**settings), job, clock=lambda: NOW), started


def _note(**fields) -> CompletedAskNotification:
    values = {"submitted_via": "web", "ask_job_id": "job-1"}
    values.update(fields)
    return CompletedAskNotification(OWNER, NB, "reasoning", **values)


# ------------------------------------------------------------------ gates
@pytest.mark.parametrize("fields", [
    {"submitted_via": "mcp"},
    {"submitted_via": ""},
    {"scope": "global", "notebook_ids": (NB,)},
])
def test_mcp_global_and_unrecorded_entries_are_never_counted(fields):
    store = _SpyStore()
    trigger, started = _trigger(store)
    for _ in range(6):
        trigger.note_ask_completed(_note(**fields))
    assert store.calls == [] and started == []


def test_an_evidence_ask_never_reaches_the_completion_hook():
    """``output="evidence"`` branches off in ``ask_current`` before any job of
    the answer kind exists and never calls the completion seam, so the trigger
    cannot count it (no answer, nothing to learn from)."""
    from app.services.ask_service import AskService

    seen: list = []
    fake = SimpleNamespace(
        _ask_evidence_current=lambda *a, **k: "evidence",
        note_ask_completed=lambda *a, **k: seen.append(a),
        _note_ask_completed=lambda *a, **k: seen.append(a),
    )
    assert AskService.ask_current(
        fake, NB, SimpleNamespace(mode="chunk"), submitted_via="web", output="evidence"
    ) == "evidence"
    assert seen == []


def test_default_user_costs_exactly_one_primary_key_read_and_no_write():
    for enabled in (None, False):
        store = _SpyStore(enabled=enabled)
        trigger, started = _trigger(store)
        trigger.note_ask_completed(_note(had_clarification=True))
        assert store.calls == [("user_state", OWNER)]
        assert started == []


def test_deployment_switch_off_touches_nothing():
    store = _SpyStore()
    trigger, started = _trigger(store, auto_memory_suggest_enabled=False)
    trigger.note_ask_completed(_note(had_clarification=True))
    assert store.calls == [] and started == []


def test_all_classes_disabled_no_writes():
    """G26: both signal classes off reads like the deployment switch off --
    not even the primary-key read."""
    store = _SpyStore()
    trigger, started = _trigger(
        store,
        auto_memory_suggest_clarified_enabled=False,
        auto_memory_suggest_repeated_enabled=False,
    )
    for _ in range(6):
        trigger.note_ask_completed(_note(had_clarification=True))
    assert store.calls == [] and started == []


def test_lost_read_access_counts_nothing_and_starts_nothing():
    """The read check lives in ``note_signal``'s own write transaction (B9):
    the trigger makes no separate access read, and a 0 back stops it."""
    store = _SpyStore(readable=False)
    trigger, started = _trigger(store)
    for _ in range(6):
        trigger.note_ask_completed(_note(had_clarification=True))
    assert [call[0] for call in store.calls] == ["user_state", "note_signal"] * 6
    assert started == []


# ------------------------------------------------------------------ hand-off
def test_threshold_is_the_fifth_web_ask():
    store = _SpyStore()
    trigger, started = _trigger(store)
    for index in range(4):
        trigger.note_ask_completed(_note(ask_job_id=f"job-{index}"))
        assert started == []
    trigger.note_ask_completed(_note(ask_job_id="job-4"))
    assert started == [(NB, OWNER)]
    # The ask id reaches the store verbatim (it seeds the cursor on insert).
    assert [call[3] for call in store.calls if call[0] == "note_signal"] == [
        f"job-{index}" for index in range(5)
    ]


def test_a_clarified_ask_starts_the_job_immediately():
    store = _SpyStore()
    trigger, started = _trigger(store)
    trigger.note_ask_completed(_note(had_clarification=True))
    assert store.count == 1
    assert started == [(NB, OWNER)]


def test_clarified_disabled_no_immediate_trigger():
    """With the ``clarified`` class off, a clarification answer is just one
    more counted ask: no early start, the threshold still applies."""
    store = _SpyStore()
    trigger, started = _trigger(store, auto_memory_suggest_clarified_enabled=False)
    for _ in range(4):
        trigger.note_ask_completed(_note(had_clarification=True))
    assert store.count == 4 and started == []
    trigger.note_ask_completed(_note(had_clarification=True))
    assert started == [(NB, OWNER)]


def test_without_a_start_job_seat_it_only_counts():
    store = _SpyStore()
    trigger, _started = _trigger(store, start_job=None)
    for _ in range(6):
        trigger.note_ask_completed(_note(had_clarification=True))
    assert store.count == 6


@pytest.mark.parametrize("where", ["user_state", "note_signal", "start_job"])
def test_any_failure_is_fail_open_and_logged_by_class_name_only(where, caplog):
    store = _SpyStore()
    store.fail_on = where

    def boom(_nb, _owner):
        raise RuntimeError("secret start u-trig-owner")

    trigger, _ = _trigger(store, start_job=boom if where == "start_job" else "spy")
    with caplog.at_level(logging.WARNING, logger="silicon_notebook.memory_suggest"):
        trigger.note_ask_completed(_note(had_clarification=True))
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "memory suggestion ask-completed trigger failed: RuntimeError"
    ]
    assert all(record.exc_info is None for record in caplog.records)
    assert "secret" not in caplog.text


# ------------------------------------------------------------------ end to end
@pytest.fixture(autouse=True)
def _clear_request_user():
    yield
    set_request_user(None)


@pytest.fixture
def runtime(tmp_path: Path):
    from app.services.sqlite_repository import SQLiteRepository

    repo = SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            storage_dir=str(tmp_path / "storage"),
            _env_file=None,
            event_log_enabled=False,
            llm_log_enabled=False,
        )
    )
    runtime = repo._runtime
    with runtime.database.write(operation="test.seed") as db:
        for user in (OWNER, FRIEND):
            db.execute(
                "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (user, f"{user}@x", user, "user", NOW, NOW),
            )
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (NB, "n", OWNER, "ready", NOW, NOW),
        )
        db.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,"
            "status,answer_id,created_at,updated_at,submitted_via,output,report_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ask-e2e", NB, "conv", OWNER, "reasoning", "q", "done", "",
             "2026-10-29T08:30:00+00:00", NOW, "web", "answer", ""),
        )
    return runtime


def _job_rows(runtime, owner: str = OWNER) -> list[dict]:
    with runtime.database.connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT pending_signal, cursor_id, cursor_created_at FROM memory_suggest_jobs "
            "WHERE notebook_id = ? AND owner_id = ?",
            (NB, owner),
        ).fetchall()]


@pytest.mark.parametrize("through_host", [False, True])
def test_a_web_ask_of_an_opted_in_reader_creates_the_job_row_at_that_ask(
    runtime, through_host,
):
    """A real runtime, the real seam: an opted-in owner's finished web Ask
    creates the job row with its cursor at THAT ask (inclusive); MCP and a
    default (not opted-in) user write nothing. Both the direct-constructor
    compat path and the production observer host."""
    if through_host:
        from app.extensions.bootstrap import default_extension_runtime

        runtime.ask_completed_observers = (
            default_extension_runtime().ask_completed_observers
        )
    else:
        assert runtime.ask_completed_observers is None

    # Default user (no switch row): nothing.
    runtime._note_ask_completed(
        NB, OWNER, "reasoning", ask_job_id="ask-e2e", submitted_via="web"
    )
    assert _job_rows(runtime) == []

    runtime.memory_suggest_store.set_memory_mode(OWNER, "suggest", now="2026-10-29T08:00:00+00:00")
    runtime._note_ask_completed(
        NB, OWNER, "chunk", ask_job_id="ask-e2e", submitted_via="mcp"
    )
    assert _job_rows(runtime) == []

    runtime._note_ask_completed(
        NB, OWNER, "chunk", ask_job_id="ask-e2e", submitted_via="web"
    )
    (row,) = _job_rows(runtime)
    assert row["pending_signal"] == 1 and row["cursor_id"] == "ask-e2e"
    assert row["cursor_created_at"] == "2026-10-29T08:30:00+00:00"


def test_an_opted_in_non_reader_gets_no_job_row_end_to_end(runtime):
    runtime.memory_suggest_store.set_memory_mode(FRIEND, "suggest", now=NOW)
    runtime._note_ask_completed(
        NB, FRIEND, "reasoning", ask_job_id="", submitted_via="web",
        had_clarification=True,
    )
    assert _job_rows(runtime, FRIEND) == []


def test_the_runtime_trigger_seat_starts_the_job(runtime):
    """T6 shipped the trigger counting only; T7b plugs the job's ``start`` in
    (enqueue only, single-flight on the claim) and wires the job to the
    runtime's ``MemoryService``."""
    assert isinstance(runtime.memory_suggest_trigger, MemorySuggestTrigger)
    assert runtime.memory_suggest_trigger._start_job == runtime.memory_suggest_job.start
    assert runtime.memory_suggest_trigger._store is runtime.memory_suggest_store
    assert runtime.memory_suggest_job.memory is runtime.memory_service
    assert runtime.memory_suggest_job.store is runtime.memory_suggest_store
