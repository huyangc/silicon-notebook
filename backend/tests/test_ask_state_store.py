"""Task 22 contract: Ask/answer/conversation/job/trace persistence is a
runtime-owned AskStateStore component sharing the ONE SqliteDatabase boundary.

The raw store is identity-explicit (``user_id`` per call — it never reads the
request ContextVar), keeps the streaming-ask commit boundaries frozen in
transaction_phases.json (begin / trace / answer / finish are independent short
transactions; an answer may exist before job finish after a crash) and RAISES
on trace persistence failure — the facade coordinator keeps the fail-open
log-and-continue policy (error_policies.json: append_ask_trace).  Report
persistence is deliberately NOT part of this store (Gate 8 keeps it a separate
domain)."""
from __future__ import annotations

import ast
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, AskResponse, NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def store(repo):
    return repo._runtime.ask_state


@pytest.fixture
def notebook(repo):
    return repo.create_notebook(NotebookCreate(name="ask-state")).id


def _response(conversation_id: str = "") -> AskResponse:
    return AskResponse(conclusion="prior answer", answer="a",
                       conversation_id=conversation_id)


class _FailingConnection:
    """Raise on the first statement containing ``needle``; pass through else."""

    def __init__(self, connection, needle: str):
        self._connection = connection
        self._needle = needle

    def execute(self, sql, *args, **kwargs):
        if self._needle in sql:
            raise RuntimeError(f"injected failure: {self._needle}")
        return self._connection.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _Injector:
    """One-shot write-transaction failure injection on the SHARED database
    boundary (the same object the store and the facade ``_write`` seam ride)."""

    def __init__(self, database, monkeypatch):
        self._mode = None            # None | ("write",) | ("statement", needle)
        real = database.write

        @contextmanager
        def wrapped():
            mode, self._mode = self._mode, None
            if mode == ("write",):
                raise RuntimeError("injected write failure")
            with real() as db:
                if mode is not None and mode[0] == "statement":
                    yield _FailingConnection(db, mode[1])
                else:
                    yield db

        monkeypatch.setattr(database, "write", wrapped)

    def fail_next_write(self):
        self._mode = ("write",)

    def fail_statement(self, needle: str):
        self._mode = ("statement", needle)


@pytest.fixture
def failure_injector(repo, monkeypatch):
    return _Injector(repo._runtime.database, monkeypatch)


def _count_writes(database, monkeypatch, counter):
    real = database.write

    @contextmanager
    def counting():
        counter["n"] += 1
        with real() as db:
            yield db

    monkeypatch.setattr(database, "write", counting)


# ---------------------------------------------------------------------------
# composition & module boundary
# ---------------------------------------------------------------------------

def test_runtime_owns_the_ask_state_store(repo):
    runtime = repo._runtime
    assert runtime.ask_state.database is runtime.database
    assert runtime.ask_state.seams is runtime.seams


def test_ask_state_store_never_imports_the_facade_and_holds_no_report_surface(repo):
    source = (
        Path(__file__).resolve().parents[1]
        / "app" / "repositories" / "sqlite" / "ask_state_store.py"
    ).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert not any(m.startswith("app.services.sqlite_repository") for m in modules)
    assert not any(m.startswith("app.services.repository_runtime") for m in modules)
    # identity is explicit — the store never touches the request ContextVar
    assert "current_user" not in source and "_REQUEST_USER" not in source
    # missing-report behavior is NOT part of this store (Gate 8 report domain)
    store = repo._runtime.ask_state
    for report_member in ("create_report", "update_report", "get_report",
                          "list_reports", "delete_report", "export_reports"):
        assert not hasattr(store, report_member), report_member


# ---------------------------------------------------------------------------
# durable job state machine: independent short transactions
# ---------------------------------------------------------------------------

def test_begin_and_answer_and_finish_are_three_commits(
    store, failure_injector, repo, notebook, monkeypatch
):
    counter = {"n": 0}
    _count_writes(repo._runtime.database, monkeypatch, counter)
    payload = AskRequest(question="q", conversation_id="conv")
    job_id, conversation_id = store.begin_durable_job(
        notebook, payload, "chunk", "u"
    )
    assert counter["n"] == 1                       # begin = one transaction
    answer_id = store.save_answer(
        notebook, conversation_id, "q", _response(conversation_id), "u"
    )
    assert counter["n"] == 2                       # answer = its own transaction
    failure_injector.fail_next_write()
    with pytest.raises(RuntimeError):
        store.finish_job(job_id, "done", answer_id=answer_id)
    # crash contract: the committed answer survives an unfinished job …
    conversation = store.get_conversation(conversation_id)
    assert conversation.turns[-1].answer_id == answer_id
    # … and the job row keeps its pre-finish state
    assert store.ask_job_status(job_id)["status"] == "running"


def test_begin_rolls_back_new_conversation_and_job_but_payload_keeps_id(
    store, failure_injector, repo, notebook
):
    payload = AskRequest(question="q-new")         # no conversation id
    failure_injector.fail_statement("INSERT INTO ask_jobs")
    with pytest.raises(RuntimeError):
        store.begin_durable_job(notebook, payload, "chunk", "u")
    # the already-mutated payload retains the generated id exactly as baseline
    assert payload.conversation_id and payload.conversation_id.startswith("conv-")
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM conversations WHERE notebook_id=?",
            (notebook,),
        ).fetchone()["n"] == 0                     # conversation rolled back too
        assert db.execute(
            "SELECT COUNT(*) AS n FROM ask_jobs WHERE notebook_id=?",
            (notebook,),
        ).fetchone()["n"] == 0


def test_begin_existing_conversation_touch_is_atomic_with_job_insert(
    store, failure_injector, repo, notebook
):
    with repo._connect() as db:
        db.execute(
            "INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("conv-stale", notebook, "t", "u", "2000-01-01T00:00:00",
             "2000-01-01T00:00:00"),
        )
    payload = AskRequest(question="q2", conversation_id="conv-stale")
    failure_injector.fail_statement("INSERT INTO ask_jobs")
    with pytest.raises(RuntimeError):
        store.begin_durable_job(notebook, payload, "chunk", "u")
    with repo._connect() as db:
        touched = db.execute(
            "SELECT updated_at FROM conversations WHERE id='conv-stale'"
        ).fetchone()["updated_at"]
    assert touched == "2000-01-01T00:00:00"        # touch rolled back with the job


def test_finish_job_returns_conversation_id_and_cleanup_is_a_later_transaction(
    store, repo, notebook, monkeypatch
):
    payload = AskRequest(question="q")
    job_id, conversation_id = store.begin_durable_job(notebook, payload, "chunk", "u")
    counter = {"n": 0}
    _count_writes(repo._runtime.database, monkeypatch, counter)
    returned = store.finish_job(job_id, "cancelled")
    assert returned == conversation_id
    assert counter["n"] == 1                       # terminal job row only
    with repo._connect() as db:                    # cleanup has NOT run yet
        assert db.execute(
            "SELECT COUNT(*) AS n FROM conversations WHERE id=?",
            (conversation_id,),
        ).fetchone()["n"] == 1
    store.cleanup_empty_conversation(conversation_id)
    assert counter["n"] == 2                       # cleanup = separate transaction
    with pytest.raises(KeyError):
        store.get_conversation(conversation_id)
    assert store.finish_job("askjob-missing", "done") is None


# ---------------------------------------------------------------------------
# trace: raw store raises, facade coordinator logs and swallows
# ---------------------------------------------------------------------------

def test_append_trace_failure_raises_from_raw_store_but_facade_swallows(
    store, failure_injector, repo, notebook
):
    payload = AskRequest(question="q")
    job_id, _ = store.begin_durable_job(notebook, payload, "chunk", "u")

    failure_injector.fail_statement("INSERT INTO ask_trace_steps")
    with pytest.raises(RuntimeError):
        store.append_trace(notebook, job_id, {"step_type": "plan"}, "u")

    failure_injector.fail_statement("INSERT INTO ask_trace_steps")
    repo.append_ask_trace(job_id, {"step_type": "plan"})   # fail-open: no raise

    store.append_trace(notebook, job_id, {"step_type": "retrieve"}, "u")
    assert [s["step_type"] for s in store.ask_job_detail(job_id)["trace"]] == [
        "retrieve"
    ]
    # unknown job stays a no-op on the raw store as well (baseline guard)
    store.append_trace(notebook, "askjob-missing", {"step_type": "x"}, "u")


# ---------------------------------------------------------------------------
# identity is explicit
# ---------------------------------------------------------------------------

def test_store_scopes_rows_by_explicit_user_id_not_contextvar(
    store, repo, notebook
):
    payload = AskRequest(question="whose turn")
    job_id, conversation_id = store.begin_durable_job(
        notebook, payload, "chunk", "user-explicit"
    )
    with repo._connect() as db:
        assert db.execute(
            "SELECT created_by FROM conversations WHERE id=?",
            (conversation_id,),
        ).fetchone()["created_by"] == "user-explicit"
    assert store.ask_job_status(job_id)["created_by"] == "user-explicit"
    assert [c.id for c in store.list_conversations(notebook, "user-explicit")] == [
        conversation_id
    ]
    # the ContextVar user (user-local) does NOT leak into store scoping
    assert store.list_conversations(notebook, repo.current_user().id) == []
    # continuing someone else's conversation id creates a NEW own conversation
    other_payload = AskRequest(question="hijack?", conversation_id=conversation_id)
    _job2, other_conv = store.begin_durable_job(
        notebook, other_payload, "chunk", "user-b"
    )
    assert other_conv != conversation_id


# ---------------------------------------------------------------------------
# prepared turn + answer payload fidelity
# ---------------------------------------------------------------------------

def test_prepare_turn_is_one_transaction_and_feeds_history(
    store, repo, notebook, monkeypatch
):
    counter = {"n": 0}
    _count_writes(repo._runtime.database, monkeypatch, counter)
    first = store.prepare_turn(notebook, None, "q1", "u")
    assert counter["n"] == 1
    assert first.conversation_id.startswith("conv-") and first.history == ""
    store.save_answer(
        notebook, first.conversation_id, "q1", _response(first.conversation_id), "u"
    )
    followup = store.prepare_turn(notebook, first.conversation_id, "q2", "u")
    assert followup.conversation_id == first.conversation_id
    assert "User: q1" in followup.history and "prior answer" in followup.history


def test_save_answer_preserves_payload_json(store, repo, notebook):
    conv = store.prepare_turn(notebook, None, "q", "u").conversation_id
    answer_id = store.save_answer(notebook, conv, "q", _response(conv), "u")
    with repo._connect() as db:
        row = db.execute(
            "SELECT question, payload, conversation_id FROM answers WHERE id=?",
            (answer_id,),
        ).fetchone()
    payload = json.loads(row["payload"])
    assert row["question"] == "q" and row["conversation_id"] == conv
    assert payload["answer_id"] == answer_id
    assert payload["conclusion"] == "prior answer"
