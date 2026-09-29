"""E1-3: every deep-report phase runs under the default retrieval ceiling.

``ReportExecutionCoordinator.start_plan`` / ``start_generate`` install
``default_ceiling_context`` with the phase's persisted scope dimensions, and
``ReportEngine.run``'s auto-confirm refresh re-installs the refreshed freeze
INSIDE that ceiling through ``refreshed_ceiling_context``.

Pinned here, on real stores (``assert_*`` are backend neutral;
``tests/postgres/test_report_default_ceiling_pg.py`` runs them on PostgreSQL):

* a report created without any scope: intent, plan and generate each see the
  include ceiling ``visible ∪ the creator's own hidden half`` -- never another
  member's Memory -- a VISIBLE-only ceiling for the mounted library and
  ``ceilings_total``; ``understanding.source_scope`` is still persisted as
  None;
* the auto-confirm refresh adopts the refreshed local dimension, inherits the
  outer per-library ceilings and ``ceilings_total``, and keeps the outer
  library dimension it was not handed;
* a reader failure fails the phase (report marked failed, the engine never
  runs) -- in the coordinator AND in the refresh -- instead of running
  unscoped.
"""
from __future__ import annotations

import threading
from typing import Any

import pytest

from app.services.report_engine import ReportEngine
from app.services.report_execution import (
    ReportCancellationRegistry,
    ReportExecutionCoordinator,
)
from app.services.source_scope import (
    CeilingReaders,
    current_source_scope,
    current_source_scope_payload,
    default_ceiling_context,
    source_allowed,
    source_scope_restricted,
)


NOW = "2026-09-29T00:00:00+00:00"


def build_report_fixture(repo, placeholder: str) -> dict[str, Any]:
    """Alice owns ``nb`` (shared with Bob) and ``lib`` (mounted on ``nb``).
    ``nb``: one visible source, one Knowhow projection, Alice's and Bob's
    confirmed Memory sources.  ``lib``: a visible source plus Memory and
    Knowhow projections of its own."""
    from app.models.schemas import NotebookCreate
    from app.core.request_context import reset_request_user, set_request_user

    ph = placeholder
    alice = repo.create_user("a00129101", "password-12")
    bob = repo.create_user("b00129102", "password-12")
    token = set_request_user(alice)
    try:
        nb = repo.create_notebook(NotebookCreate(name="共享报告库")).id
        lib = repo.create_notebook(NotebookCreate(name="报告参考库")).id
    finally:
        reset_request_user(token)
    runtime = repo._runtime
    runtime.sharing.add_member(nb, bob.id)
    sources = runtime.source_store

    def insert(notebook_id, source_id, source_type, memory_id=""):
        sources.insert_source(
            source_id=source_id, notebook_id=notebook_id, title=source_id,
            source_type=source_type, status="active", parse_status="parsed",
            file_name="", file_path="", file_size=0, file_hash="",
            summary="", doc_type="", memory_id=memory_id,
        )

    def memory(notebook_id, user_id, memory_id, source_id):
        with repo._write() as db:
            db.execute(
                "INSERT INTO memory_items"
                "(id,notebook_id,created_by,agent_profile_id,source_answer_id,"
                "origin,status,title,content_md,created_at,updated_at) "
                f"VALUES ({ph},{ph},{ph},NULL,NULL,'ask_answer','confirmed',"
                f"{ph},{ph},{ph},{ph})",
                (memory_id, notebook_id, user_id, "记忆", "内容", NOW, NOW),
            )
        insert(notebook_id, source_id, "memory", memory_id=memory_id)

    insert(nb, "rep-visible", "pdf")
    insert(nb, "rep-knowhow", "knowhow")
    memory(nb, alice.id, "rep-mem-alice", "rep-memory-alice")
    memory(nb, bob.id, "rep-mem-bob", "rep-memory-bob")
    insert(lib, "rep-lib-visible", "pdf")
    insert(lib, "rep-lib-knowhow", "knowhow")
    memory(lib, alice.id, "rep-mem-lib", "rep-lib-memory")
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_bases"
            "(notebook_id,base_notebook_id,created_at,created_by) "
            f"VALUES ({ph},{ph},{ph},{ph})",
            (nb, lib, NOW, alice.id),
        )
    return {"nb": nb, "lib": lib, "alice": alice, "bob": bob}


def _snapshot(phase: str) -> dict[str, Any]:
    scope = current_source_scope()
    assert scope is not None, f"{phase}: 报告阶段必须装着默认天花板"
    return {
        "phase": phase,
        "source_ids": set(scope.source_ids),
        "hidden": set(scope.hidden_source_ids),
        "source_provided": scope.source_provided,
        "ceilings_total": scope.ceilings_total,
        "payload": current_source_scope_payload(),
        "restricted": source_scope_restricted(),
        "base_provided": scope.base_provided,
        "base_notebook_ids": set(scope.base_notebook_ids),
    }


class _Recorder:
    """Patches the engine's three model-facing steps to record the scope
    they run under; everything around them (the coordinator, the real
    engine's ``run`` / ``prepare_intent`` / auto-confirm, the report store)
    is production code."""

    def __init__(self, monkeypatch, repo, ids) -> None:
        self.seen: list[dict[str, Any]] = []
        self.lib = ids["lib"]
        recorder = self
        reports = repo._runtime.report_store

        def intent(engine, question, history):
            recorder.seen.append(recorder._with_lib(_snapshot("intent")))
            return {
                "normalized_question": question, "resolved_question": question,
                "intent_type": "explain", "needs_clarification": False,
                "ambiguities": [], "mandatory_topics": [],
            }

        def plan(engine, notebook_id, rid, question, history="", **_kwargs):
            recorder.seen.append(recorder._with_lib(_snapshot("plan")))
            reports.update_report(
                notebook_id, rid, status="outline_ready",
                outline=[{"title": "A", "scope": "s", "sub_queries": ["q"]}],
            )
            return [{"title": "A"}]

        def generate(engine, notebook_id, rid, question, depth=2, **_kwargs):
            recorder.seen.append(recorder._with_lib(_snapshot("generate")))
            return None

        monkeypatch.setattr(ReportEngine, "_plan_intent_contract", intent)
        monkeypatch.setattr(ReportEngine, "plan_outline", plan)
        monkeypatch.setattr(ReportEngine, "generate", generate)

    def _with_lib(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        scope = current_source_scope()
        snapshot["lib_ceiling"] = scope.source_ceiling_for(self.lib)
        snapshot["lib_private_allowed"] = (
            source_allowed(self.lib, "rep-lib-memory")
            or source_allowed(self.lib, "rep-lib-knowhow")
        )
        return snapshot


def _synchronous(monkeypatch, coordinator) -> None:
    monkeypatch.setattr(
        coordinator, "job_submitter",
        lambda fn, *args, name=None, notify_pending=False, **kwargs: fn(),
    )


def _create_report(repo, ids) -> str:
    from app.core.request_context import reset_request_user, set_request_user

    token = set_request_user(ids["alice"])
    try:
        return repo._runtime.report_store.create_report(ids["nb"], "q?")
    finally:
        reset_request_user(token)


def assert_unscoped_report_phases_run_under_the_default_ceiling(
    repo, ids, monkeypatch
) -> None:
    coordinator = repo.report_execution
    assert isinstance(coordinator.ceiling_readers, CeilingReaders)
    _synchronous(monkeypatch, coordinator)
    recorder = _Recorder(monkeypatch, repo, ids)
    nb, alice = ids["nb"], ids["alice"].id
    rid = _create_report(repo, ids)

    # Phase 1: intent understanding (created without any scope).
    assert coordinator.start_plan(nb, rid, "q?", user_id=alice)
    stored = repo._runtime.report_store.get_report(nb, rid)
    assert stored["status"] == "intent_ready"
    understanding = dict(stored.get("understanding") or {})
    assert understanding.get("source_scope") is None, "不得替用户持久化一份本地范围"
    assert understanding.get("base_scope") is None

    # Phase 2: planning after the (manual) confirmation.
    assert coordinator.start_plan(
        nb, rid, "q?", user_id=alice, intent_contract=understanding
    )
    # Phase 3: generation.
    assert coordinator.start_generate(nb, rid, "q?", user_id=alice)

    assert [row["phase"] for row in recorder.seen] == ["intent", "plan", "generate"]
    for row in recorder.seen:
        assert row["source_ids"] == {"rep-visible"}, row
        assert row["hidden"] == {"rep-knowhow", "rep-memory-alice"}, (
            "隐藏半边只含创建者本人的 Memory 与共享 Knowhow,绝不含他人 Memory", row,
        )
        assert row["source_provided"] is False, row
        assert row["payload"] is None, row
        assert row["restricted"] is False, row
        assert row["ceilings_total"] is True, row
        assert row["lib_ceiling"] == frozenset({"rep-lib-visible"}), row
        assert row["lib_private_allowed"] is False, row
    final = repo._runtime.report_store.get_report(nb, rid)
    assert (final.get("understanding") or {}).get("source_scope") is None


def assert_auto_confirm_refresh_reinstalls_inside_the_ceiling(
    repo, ids, monkeypatch
) -> None:
    coordinator = repo.report_execution
    _synchronous(monkeypatch, coordinator)
    recorder = _Recorder(monkeypatch, repo, ids)
    nb, lib, alice = ids["nb"], ids["lib"], ids["alice"].id
    rid = _create_report(repo, ids)
    submitted = {
        "mode": "include", "source_ids": ["rep-visible"],
        "hidden_source_ids": ["rep-knowhow", "rep-memory-alice"],
        "narrowed": False, "owner_id": alice,
    }
    submitted_base = {"mode": "include", "notebook_ids": [lib], "narrowed": False}
    # The re-validation refreshed ONLY the local dimension (the route returns
    # None for a dimension it did not re-freeze): the source was narrowed to
    # nothing hidden while intent understanding ran.
    refreshed = {
        "mode": "include", "source_ids": ["rep-visible"],
        "hidden_source_ids": [], "narrowed": True, "owner_id": alice,
    }
    handed: list[dict] = []

    def reconfirm(understanding):
        handed.append(dict(understanding))
        return {
            "understanding": understanding,
            "source_scope": refreshed,
            "base_scope": None,
        }

    assert coordinator.start_plan(
        nb, rid, "q?", auto_generate=True, user_id=alice,
        source_scope=submitted, base_scope=submitted_base,
        scope_reconfirm=reconfirm,
    )
    assert handed, "自动确认必须经过范围重验"
    phases = [row["phase"] for row in recorder.seen]
    assert phases == ["intent", "plan", "generate"], phases
    intent_row, plan_row, generate_row = recorder.seen
    assert intent_row["hidden"] == {"rep-knowhow", "rep-memory-alice"}
    for row in (plan_row, generate_row):
        assert row["source_ids"] == {"rep-visible"}, row
        assert row["hidden"] == set(), "刷新后的本地维必须被采用"
        assert row["source_provided"] is True, row
        assert row["ceilings_total"] is True, "重装必须继承 ceilings_total"
        assert row["lib_ceiling"] == frozenset({"rep-lib-visible"}), row
        assert row["lib_private_allowed"] is False, row
        # Not handed a library dimension: the outer (submitted) one stands.
        assert row["base_provided"] is True, row
        assert row["base_notebook_ids"] == {lib}, row


class _FailingReaders:
    """``CeilingReaders`` whose participant read fails."""

    @staticmethod
    def build(message: str = "participants unavailable") -> CeilingReaders:
        def fail(_notebook_id):
            raise RuntimeError(message)

        return CeilingReaders(
            participants=fail,
            visible=lambda notebook_id: [],
            hidden=lambda notebook_id, owner_id: [],
            memory_sources=lambda notebook_id: [],
        )


class _Reports:
    def __init__(self) -> None:
        self.updates: list[tuple[tuple, dict]] = []

    def get_report(self, notebook_id, report_id):
        return {"id": report_id, "depth": 2, "understanding": {}}

    def update_report(self, *args, **kwargs):
        self.updates.append((args, kwargs))


class _NeverRunEngine:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, *args, **kwargs):
        self.calls.append("run")

    def generate(self, *args, **kwargs):
        self.calls.append("generate")


@pytest.mark.parametrize("phase", ["plan", "generate"])
def test_a_reader_failure_fails_the_phase_instead_of_running_unscoped(phase):
    reports = _Reports()
    engine = _NeverRunEngine()
    coordinator = ReportExecutionCoordinator(
        reports=reports,
        engine_factory=lambda **_kwargs: engine,
        cancellations=ReportCancellationRegistry(),
        job_submitter=lambda fn, *args, name=None, notify_pending=False, **kw: fn(),
        ceiling_readers=_FailingReaders.build(),
    )
    with pytest.raises(RuntimeError, match="participants unavailable"):
        if phase == "plan":
            coordinator.start_plan("nb", "rid", "q", user_id="u")
        else:
            coordinator.start_generate("nb", "rid", "q", user_id="u")
    assert engine.calls == [], "天花板装不上时绝不能在无范围下跑报告"
    assert reports.updates[-1][1]["status"] == "failed"
    assert "participants unavailable" in reports.updates[-1][1]["error"]
    assert current_source_scope() is None


def test_a_stop_during_the_ceiling_reads_ends_the_worker_quietly():
    from app.services.cancellation import AskCancelled

    reports = _Reports()
    engine = _NeverRunEngine()

    def cancelled(_notebook_id):
        raise AskCancelled()

    coordinator = ReportExecutionCoordinator(
        reports=reports,
        engine_factory=lambda **_kwargs: engine,
        cancellations=ReportCancellationRegistry(),
        job_submitter=lambda fn, *args, name=None, notify_pending=False, **kw: fn(),
        ceiling_readers=CeilingReaders(
            participants=cancelled,
            visible=lambda notebook_id: [],
            hidden=lambda notebook_id, owner_id: [],
            memory_sources=lambda notebook_id: [],
        ),
    )
    coordinator.start_generate("nb", "rid", "q", user_id="u")
    assert engine.calls == []
    assert not any(kw.get("status") == "failed" for _a, kw in reports.updates)


@pytest.fixture
def sqlite_repo(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'report.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    return repo, build_report_fixture(repo, "?")


def test_unscoped_report_phases_run_under_the_default_ceiling(sqlite_repo, monkeypatch):
    repo, ids = sqlite_repo
    assert_unscoped_report_phases_run_under_the_default_ceiling(repo, ids, monkeypatch)


def test_auto_confirm_refresh_reinstalls_inside_the_ceiling(sqlite_repo, monkeypatch):
    repo, ids = sqlite_repo
    assert_auto_confirm_refresh_reinstalls_inside_the_ceiling(repo, ids, monkeypatch)


@pytest.mark.parametrize("readers", ["failing", "missing"])
def test_a_refresh_that_cannot_read_fails_the_report(sqlite_repo, monkeypatch, readers):
    """The refresh runs inside the worker's ceiling (installed with working
    readers); its own readers failing -- or never wired -- must fail the
    report, never plan under the stale or no scope."""
    repo, ids = sqlite_repo
    nb, alice = ids["nb"], ids["alice"].id
    rid = _create_report(repo, ids)
    planned: list[str] = []
    monkeypatch.setattr(
        ReportEngine, "plan_outline",
        lambda *args, **kwargs: planned.append("plan"),
    )
    engine = repo.report_execution.engine_factory(user_id=alice, cancel_event=None)
    replacement = _FailingReaders.build() if readers == "failing" else None
    engine.dependencies = type(engine.dependencies)(**{
        **engine.dependencies.__dict__, "ceiling_readers": replacement,
    })
    monkeypatch.setattr(
        ReportEngine, "prepare_intent", lambda *args, **kwargs: {"x": 1}
    )
    monkeypatch.setattr(
        ReportEngine, "_auto_confirm_intent",
        lambda *args, **kwargs: (
            {"resolved_question": "q?"},
            {"mode": "include", "source_ids": ["rep-visible"], "narrowed": True},
            None,
        ),
    )
    with default_ceiling_context(nb, alice, repo._runtime.ceiling_readers()):
        result = engine.run(
            nb, rid, "q?", require_intent_review=True, auto_generate=True,
        )
    assert result is None
    assert planned == [], "刷新装不上时不得规划"
    stored = repo._runtime.report_store.get_report(nb, rid)
    assert stored["status"] == "failed"
    assert stored["progress"] == "规划失败"


def test_the_worker_threads_do_not_leak_the_ceiling(sqlite_repo, monkeypatch):
    """The ceiling is entered in the worker and exits with it: the caller's
    thread (and the next job on a pooled thread) carries no scope."""
    repo, ids = sqlite_repo
    coordinator = repo.report_execution
    done = threading.Event()
    threads: list[threading.Thread] = []

    def submit(fn, *args, name=None, notify_pending=False, **kwargs):
        def body():
            try:
                fn()
            finally:
                done.set()
        thread = threading.Thread(target=body)
        threads.append(thread)
        thread.start()
        return thread

    monkeypatch.setattr(coordinator, "job_submitter", submit)
    recorder = _Recorder(monkeypatch, repo, ids)
    rid = _create_report(repo, ids)
    assert coordinator.start_generate(ids["nb"], rid, "q?", user_id=ids["alice"].id)
    assert done.wait(10)
    threads[0].join(10)
    assert [row["phase"] for row in recorder.seen] == ["generate"]
    assert current_source_scope() is None
