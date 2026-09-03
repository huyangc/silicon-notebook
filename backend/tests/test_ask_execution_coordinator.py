"""Task 23 contract: Ask cancellation + detached execution live in
app.services.ask_execution (AskCancellationRegistry / AskExecutionCoordinator),
composed by the runtime and shared with the facade's frozen surface.

Frozen baseline sequence (routes._stream_ask_events before the move):

  begin durable job in ONE transaction (payload.conversation_id mutated in
  that transaction body — the coordinator observes the returned value and
  never mutates a second time) → register cancel event → emit started →
  emit the synthetic start step WITHOUT persistence (auto mode: job begun as
  ``auto``, the step moves inside the worker after ``resolve`` selects the
  engine — see the auto-mode section below) → submit the worker
  through the copied-context job helper → per real trace: persist first,
  fail-open (log and still deliver) → the engine saves the answer → finish job
  transaction → unregister cancel event → (failed/cancelled) empty-
  conversation cleanup in a LATER transaction → terminal final/cancelled/
  error event → sentinel.

Task 24: the worker executes the runtime-owned AskService (``ask`` resolver
injected at composition; on_trace reaches streaming engines only) — the fake
below stands in for it with the same registry-driven split.

Transport disconnect only stops route queue consumption — it NEVER sets the
cancel event (PR#220 WS2a detached execution).  The explicit cancel endpoint
is the only true cancellation entry, via the registry.  No terminal event may
be put on the delivery queue while its job remains registered."""
from __future__ import annotations

import ast
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import contextvars

import pytest

from app.core.config import Settings
from app.core.request_context import request_user_id, reset_request_user, set_request_user
from app.models.schemas import AskRequest, AskResponse, NotebookCreate, TraceStep, UserProfile
from app.services import background_jobs
from app.services.ask_execution import AskCancellationRegistry, AskExecutionCoordinator
from app.services.ask_modes import ASK_MODES
from app.services.cancellation import raise_if_cancelled
from app.services.sqlite_repository import SQLiteRepository


# ---------------------------------------------------------------------------
# fakes (identity-explicit AskStateStorePort + deterministic submitter)
# ---------------------------------------------------------------------------

class RecordingAskState:
    """AskStateStorePort 假件:共享 calls 账目供跨组件次序断言。"""

    def __init__(self, calls: list, fail_trace: bool = False):
        self.calls = calls
        self.fail_trace = fail_trace

    def begin_durable_job(self, notebook_id, payload, mode, user_id):
        self.calls.append(("begin", notebook_id, mode, user_id))
        payload.conversation_id = "conv-t23"
        return "askjob-t23", "conv-t23"

    def append_trace(self, notebook_id, job_id, step, user_id):
        self.calls.append(("trace", job_id, step["step_type"], user_id))
        if self.fail_trace:
            raise RuntimeError("trace persistence down")

    def save_answer(self, notebook_id, conversation_id, question, response, user_id):
        self.calls.append(("answer", conversation_id))
        return "ans-t23"

    def update_job_mode(self, job_id, mode):
        self.calls.append(("mode", job_id, mode))

    def finish_job(self, job_id, status, *, answer_id="", error=""):
        self.calls.append(("finish", status, answer_id, error))
        return "conv-t23"

    def cleanup_empty_conversation(self, conversation_id):
        self.calls.append(("cleanup", conversation_id))


class InlineSubmitter:
    """同步跑 worker(仍经 copy_context)——次序断言全确定化。"""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, name=None, notify_pending=False, **kwargs):
        self.submitted.append(name)
        contextvars.copy_context().run(fn, *args, **kwargs)
        return threading.current_thread()


class RaisingSubmitter:
    def __init__(self, exc):
        self.exc = exc

    def submit(self, fn, *args, name=None, notify_pending=False, **kwargs):
        raise self.exc


class _RecordingLogger:
    def __init__(self):
        self.exceptions = []

    def exception(self, msg, *args):
        self.exceptions.append(msg % args if args else msg)


def _event_log(logger=None):
    return SimpleNamespace(logger=logger or _RecordingLogger())


def _response(conversation_id="conv-t23"):
    return AskResponse(answer_id="ans-t23", conversation_id=conversation_id,
                       conclusion="", answer="a", grounded=True, anchors=[],
                       related_knowledge=[], citations=[], llm_mode="x")


class FakeAskService:
    """AskService 假件:按同一 ask_modes 注册表分派 on_trace(只达 streaming
    引擎)——把测试 runner 保持在冻结的 handler 签名上。"""

    def __init__(self, fn):
        self.fn = fn

    def ask(
        self, notebook_id, payload, *, user_id, job_id="", on_trace=None,
        cancel_event=None,
    ):
        from app.services.ask_modes import resolve_mode
        spec = resolve_mode(getattr(payload, "mode", None))
        if spec.streaming:
            return self.fn(notebook_id, payload, on_trace=on_trace,
                           cancel_event=cancel_event)
        return self.fn(notebook_id, payload, cancel_event=cancel_event)


def _coordinator(state, registry=None, submitter=None, logger=None, runner=None):
    service = FakeAskService(runner)
    return AskExecutionCoordinator(
        ask_state=state,
        cancellations=registry if registry is not None else AskCancellationRegistry(),
        job_submitter=submitter if submitter is not None else InlineSubmitter(),
        event_log=_event_log(logger),
        ask=lambda: service,
    )


def _drain(events, timeout=2.0):
    out = []
    while True:
        item = events.get(timeout=timeout)
        if item is None:
            return out
        out.append(item)


# ---------------------------------------------------------------------------
# delivery order: started → synthetic start → … → terminal → sentinel
# ---------------------------------------------------------------------------

def test_started_is_emitted_before_progress_and_final_closes_the_stream():
    calls = []
    def runner(notebook_id, payload, cancel_event=None):
        return _response()

    coordinator = _coordinator(RecordingAskState(calls), runner=runner)
    events = coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="chunk"), ASK_MODES["chunk"],
        user_id="user-t23")
    delivered = _drain(events)
    kinds = [e["event"] for e in delivered]
    assert delivered[0] == {
        "event": "started",
        "job_id": "askjob-t23",
        "conversation_id": "conv-t23",
    }
    assert kinds[1] == "progress"
    assert delivered[1]["step"]["step_type"] == "start"
    assert delivered[1]["step"]["detail"] == {"mode": "chunk"}
    assert kinds[-1] == "final"
    assert delivered[-1]["response"]["answer_id"] == "ans-t23"


def test_submit_failure_finishes_durable_job_and_unregisters_cancellation():
    calls = []
    registry = AskCancellationRegistry()
    coordinator = _coordinator(
        RecordingAskState(calls),
        registry=registry,
        submitter=RaisingSubmitter(RuntimeError("thread start failed")),
        runner=lambda *_args, **_kwargs: _response(),
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        coordinator.start(
            "nb-1", AskRequest(question="Q?", mode="chunk"),
            ASK_MODES["chunk"], user_id="user-t23",
        )

    assert ("finish", "failed", "", "RuntimeError: thread start failed") in calls
    assert ("cleanup", "conv-t23") in calls
    assert registry.get("askjob-t23") is None


def test_synthetic_start_is_emitted_but_never_persisted():
    calls = []
    def runner(notebook_id, payload, on_trace=None, cancel_event=None):
        return _response()

    coordinator = _coordinator(RecordingAskState(calls), runner=runner)
    events = coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="reasoning"), ASK_MODES["reasoning"],
        user_id="user-t23")
    delivered = _drain(events)
    assert any(e["event"] == "progress" and e["step"]["step_type"] == "start"
               for e in delivered)
    assert not any(c[0] == "trace" for c in calls)   # 合成 start 绝不进 ask_trace_steps


# ---------------------------------------------------------------------------
# auto mode: durable job + started FIRST, engine selection inside the worker
# ---------------------------------------------------------------------------

def test_auto_mode_begins_the_job_and_queues_started_before_resolving_the_engine():
    calls = []
    order = []

    def resolve(cancel_event):
        order.append(("resolve", list(calls)))
        assert isinstance(cancel_event, threading.Event)
        return AskRequest(question="Q?", mode="chunk", conversation_id="conv-t23"), ASK_MODES["chunk"]

    def runner(notebook_id, payload, cancel_event=None):
        assert payload.mode == "chunk"
        return _response()

    coordinator = _coordinator(RecordingAskState(calls), runner=runner)
    events = coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="auto"), None,
        user_id="user-t23", resolve=resolve)
    delivered = _drain(events)

    # The job row exists (under the request-only auto id) before resolve runs.
    assert order[0][1][0] == ("begin", "nb-1", "auto", "user-t23")
    assert ("mode", "askjob-t23", "chunk") in calls
    assert calls.index(("mode", "askjob-t23", "chunk")) < calls.index(("finish", "done", "ans-t23", ""))
    # started is first; the synthetic start step reports the RESOLVED engine.
    assert delivered[0]["event"] == "started"
    assert delivered[1]["step"]["step_type"] == "start"
    assert delivered[1]["step"]["detail"] == {"mode": "chunk"}
    assert delivered[-1]["event"] == "final"
    # The synthetic start step is delivery-only in auto mode as well: persisting
    # it would make a reconnect replay one step longer than the live stream.
    assert not any(c[0] == "trace" for c in calls)


def test_auto_mode_validates_the_resolved_submission_inside_the_worker():
    calls = []
    seen = []

    class ValidatingService(FakeAskService):
        def validate_reasoning_submission(self, notebook_id, payload):
            seen.append(payload.mode)

    def resolve(cancel_event):
        return AskRequest(question="Q?", mode="reasoning", conversation_id="conv-t23"), ASK_MODES["reasoning"]

    service = ValidatingService(lambda *a, **k: _response())
    coordinator = AskExecutionCoordinator(
        ask_state=RecordingAskState(calls),
        cancellations=AskCancellationRegistry(),
        job_submitter=InlineSubmitter(),
        event_log=_event_log(),
        ask=lambda: service,
    )
    _drain(coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="auto"), None,
        user_id="user-t23", resolve=resolve))
    # Not validated as "auto" before begin (payload unknown yet); validated once
    # with the routed payload inside the worker.
    assert seen == ["reasoning"]


def test_explicit_cancel_during_engine_selection_cancels_the_job():
    calls = []
    registry = AskCancellationRegistry()

    def resolve(cancel_event):
        # The explicit cancel endpoint sets the registered event while the
        # classifier is still running; selection must observe it.
        assert registry.cancel("askjob-t23")
        raise_if_cancelled(cancel_event)
        raise AssertionError("cancellation was not observed")

    coordinator = _coordinator(
        RecordingAskState(calls), registry=registry,
        runner=lambda *a, **k: _response())
    delivered = _drain(coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="auto"), None,
        user_id="user-t23", resolve=resolve))
    assert delivered[0]["event"] == "started"
    assert delivered[-1] == {"event": "cancelled"}
    assert ("finish", "cancelled", "", "") in calls
    assert ("cleanup", "conv-t23") in calls
    assert registry.get("askjob-t23") is None


def test_engine_selection_failure_fails_the_durable_job():
    calls = []

    def resolve(cancel_event):
        raise RuntimeError("classifier down")

    coordinator = _coordinator(RecordingAskState(calls), runner=lambda *a, **k: _response())
    delivered = _drain(coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="auto"), None,
        user_id="user-t23", resolve=resolve))
    assert delivered[0]["event"] == "started"
    assert delivered[-1] == {"event": "error", "error": "RuntimeError: classifier down"}
    assert ("finish", "failed", "", "RuntimeError: classifier down") in calls
    assert ("cleanup", "conv-t23") in calls


def test_job_mode_update_failure_is_fail_open():
    calls = []
    logger = _RecordingLogger()

    class FlakyModeState(RecordingAskState):
        def update_job_mode(self, job_id, mode):
            raise RuntimeError("mode column locked")

    def resolve(cancel_event):
        return AskRequest(question="Q?", mode="chunk", conversation_id="conv-t23"), ASK_MODES["chunk"]

    coordinator = _coordinator(FlakyModeState(calls), logger=logger, runner=lambda *a, **k: _response())
    delivered = _drain(coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="auto"), None,
        user_id="user-t23", resolve=resolve))
    assert delivered[-1]["event"] == "final"
    assert any("update_job_mode failed" in m for m in logger.exceptions)


def test_start_requires_a_mode_or_a_resolver():
    coordinator = _coordinator(RecordingAskState([]), runner=lambda *a, **k: _response())
    with pytest.raises(ValueError):
        coordinator.start("nb-1", AskRequest(question="Q?", mode="auto"), None, user_id="u")


def test_begin_mutates_payload_in_place_and_coordinator_never_mutates_again():
    calls = []

    class DecoyReturnState(RecordingAskState):
        def begin_durable_job(self, notebook_id, payload, mode, user_id):
            self.calls.append(("begin", notebook_id, mode, user_id))
            payload.conversation_id = "conv-t23"     # 事务体内就地写回(基线时点)
            return "askjob-t23", "conv-returned"     # 返回值≠payload 值:二次改写会被抓

    seen = []

    def runner(notebook_id, payload, cancel_event=None):
        seen.append(payload)
        return _response()

    payload = AskRequest(question="Q?", mode="chunk")
    coordinator = _coordinator(DecoyReturnState(calls), runner=runner)
    events = coordinator.start("nb-1", payload, ASK_MODES["chunk"],
                               user_id="user-t23")
    _drain(events)
    assert seen and seen[0] is payload               # 同一 payload 对象直达 runner
    assert payload.conversation_id == "conv-t23"     # begin 的就地写回是唯一一次改写
    assert ("begin", "nb-1", "chunk", "user-t23") in calls


# ---------------------------------------------------------------------------
# trace persistence: persist-then-deliver, fail-open
# ---------------------------------------------------------------------------

def test_real_trace_is_persisted_before_delivery():
    calls = []
    persist_entered = threading.Event()
    release_persist = threading.Event()

    class GatedState(RecordingAskState):
        def append_trace(self, notebook_id, job_id, step, user_id):
            super().append_trace(notebook_id, job_id, step, user_id)
            persist_entered.set()
            assert release_persist.wait(2)

    def runner(notebook_id, payload, on_trace=None, cancel_event=None):
        on_trace(TraceStep(step_type="plan", summary="s-real", detail={}))
        return _response()

    coordinator = _coordinator(GatedState(calls), submitter=background_jobs,
                               runner=runner)
    events = coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="reasoning"), ASK_MODES["reasoning"],
        user_id="user-t23")
    assert persist_entered.wait(2)
    # 持久化尚未返回 → 真实 trace 绝不能已在交付队列上(persist 先行)
    snapshot = [e for e in list(events.queue) if e is not None]
    assert not any(e["event"] == "progress" and e["step"].get("summary") == "s-real"
                   for e in snapshot)
    release_persist.set()
    delivered = _drain(events)
    assert any(e["event"] == "progress" and e["step"].get("summary") == "s-real"
               for e in delivered)
    assert ("trace", "askjob-t23", "plan", "user-t23") in calls


def test_trace_persistence_failure_logs_and_still_delivers():
    calls = []
    logger = _RecordingLogger()
    def runner(notebook_id, payload, on_trace=None, cancel_event=None):
        on_trace(TraceStep(step_type="plan", summary="s1", detail={}))
        return _response()

    coordinator = _coordinator(RecordingAskState(calls, fail_trace=True), logger=logger,
                               runner=runner)
    events = coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="reasoning"), ASK_MODES["reasoning"],
        user_id="user-t23")
    delivered = _drain(events)
    assert any(e["event"] == "progress" and e["step"].get("summary") == "s1"
               for e in delivered)                     # fail-open:照常交付
    assert [e["event"] for e in delivered][-1] == "final"   # 也绝不拖垮 ask
    assert logger.exceptions and "askjob-t23" in logger.exceptions[0]


# ---------------------------------------------------------------------------
# terminal sequence: answer → finish commit → unregister → cleanup → delivery
# ---------------------------------------------------------------------------

def test_answer_save_occurs_before_job_finish():
    calls = []
    state = RecordingAskState(calls)

    def runner(notebook_id, payload, cancel_event=None):
        state.save_answer(notebook_id, payload.conversation_id, payload.question,
                          _response(), "user-t23")
        return _response()

    coordinator = _coordinator(state, runner=runner)
    events = coordinator.start("nb-1", AskRequest(question="Q?", mode="chunk"),
                               ASK_MODES["chunk"], user_id="user-t23")
    _drain(events)
    assert calls.index(("answer", "conv-t23")) < calls.index(("finish", "done", "ans-t23", ""))


def test_finish_commit_precedes_unregister_which_precedes_cleanup_and_delivery():
    calls = []
    unregister_entered = threading.Event()
    release_unregister = threading.Event()

    class GatedRegistry(AskCancellationRegistry):
        def unregister(self, key):
            calls.append(("unregister", key))
            unregister_entered.set()
            assert release_unregister.wait(2)
            super().unregister(key)

    def runner(notebook_id, payload, cancel_event=None):
        raise RuntimeError("boom")

    coordinator = _coordinator(RecordingAskState(calls), registry=GatedRegistry(),
                               submitter=background_jobs, runner=runner)
    events = coordinator.start("nb-1", AskRequest(question="Q?", mode="chunk"),
                               ASK_MODES["chunk"], user_id="user-t23")
    assert unregister_entered.wait(2)
    # 终态 job 行已提交、注销进行中——终态事件此刻绝不能已在交付队列上
    assert ("finish", "failed", "", "RuntimeError: boom") in calls
    snapshot = [e for e in list(events.queue) if e is not None]
    assert all(e["event"] in {"started", "progress"} for e in snapshot)
    # 空会话清理在注销之后(更晚的独立事务)
    assert not any(c[0] == "cleanup" for c in calls)
    release_unregister.set()
    delivered = _drain(events)
    assert [e["event"] for e in delivered][-1] == "error"
    finish_at = calls.index(("finish", "failed", "", "RuntimeError: boom"))
    unregister_at = calls.index(("unregister", "askjob-t23"))
    cleanup_at = calls.index(("cleanup", "conv-t23"))
    assert finish_at < unregister_at < cleanup_at


# ---------------------------------------------------------------------------
# cancellation: explicit-only; the final checkpoint; completion wins after it
# ---------------------------------------------------------------------------

def test_explicit_cancel_sets_the_event_and_final_checkpoint_yields_no_answer():
    calls = []
    registry = AskCancellationRegistry()
    entered = threading.Event()

    def runner(notebook_id, payload, cancel_event=None):
        entered.set()
        assert cancel_event.wait(2)          # 唯一 set 入口=显式取消(断连绝不 set)
        raise_if_cancelled(cancel_event)     # 既有 final checkpoint 观察到取消
        raise AssertionError("cancelled run must not reach the answer")

    coordinator = _coordinator(RecordingAskState(calls), registry=registry,
                               submitter=background_jobs, runner=runner)
    events = coordinator.start("nb-1", AskRequest(question="Q?", mode="chunk"),
                               ASK_MODES["chunk"], user_id="user-t23")
    started = events.get(timeout=2)
    assert started["event"] == "started"
    assert entered.wait(2)
    assert registry.cancel(started["job_id"]) is True
    delivered = _drain(events)
    assert [e["event"] for e in delivered][-1] == "cancelled"
    assert not any(c[0] == "answer" for c in calls)          # checkpoint 处取消 → 无答案
    assert ("finish", "cancelled", "", "") in calls
    assert ("cleanup", "conv-t23") in calls                  # 取消首轮清空壳会话
    assert registry.get("askjob-t23") is None                # 已注销


def test_cancel_after_the_final_checkpoint_is_not_atomic_completed_answer_wins():
    calls = []
    registry = AskCancellationRegistry()
    state = RecordingAskState(calls)

    def runner(notebook_id, payload, cancel_event=None):
        state.save_answer(notebook_id, payload.conversation_id, payload.question,
                          _response(), "user-t23")
        return _response()

    coordinator = _coordinator(state, registry=registry, runner=runner)
    events = coordinator.start("nb-1", AskRequest(question="Q?", mode="chunk"),
                               ASK_MODES["chunk"], user_id="user-t23")
    # checkpoint 之后才到的取消不承诺原子性:job 已终态并注销,cancel 落空、答案已存
    assert registry.cancel("askjob-t23") is False
    delivered = _drain(events)
    assert [e["event"] for e in delivered][-1] == "final"
    assert ("answer", "conv-t23") in calls
    assert ("finish", "done", "ans-t23", "") in calls
    assert not any(c[0] == "cleanup" for c in calls)


# ---------------------------------------------------------------------------
# detached worker: copied request context
# ---------------------------------------------------------------------------

def test_copied_request_context_reaches_the_detached_worker():
    calls = []
    seen_user = []
    worker_thread = []

    def runner(notebook_id, payload, cancel_event=None):
        worker_thread.append(threading.current_thread())
        seen_user.append(request_user_id())
        return _response()

    coordinator = _coordinator(RecordingAskState(calls), submitter=background_jobs,
                               runner=runner)
    token = set_request_user(UserProfile(id="u-ctx", email="c@x", display_name="c",
                                         role="user", username="c00123456"))
    try:
        events = coordinator.start("nb-1", AskRequest(question="Q?", mode="chunk"),
                                   ASK_MODES["chunk"], user_id="u-ctx")
    finally:
        reset_request_user(token)
    delivered = _drain(events)
    assert [e["event"] for e in delivered][-1] == "final"
    assert worker_thread and worker_thread[0] is not threading.current_thread()
    assert seen_user == ["u-ctx"]      # per-user 模型命脉:contextvars 抵达脱离线程


# ---------------------------------------------------------------------------
# composition & module boundary
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_runtime_owns_the_coordinator_and_facade_shares_its_registry(repo):
    runtime = repo._runtime
    assert isinstance(runtime.ask_execution, AskExecutionCoordinator)
    assert runtime.ask_execution.ask_state is runtime.ask_state
    assert runtime.ask_execution.cancellations is runtime.ask_cancellations
    assert runtime.ask_execution.job_submitter is background_jobs
    # Task 24: 协调器的执行体解析到 runtime-owned AskService(一个所有者)
    assert runtime.ask_execution.ask() is runtime.ask_service()
    # 冻结的 facade 兼容座与注册表是同一对象(一个所有者)
    assert repo._ask_cancel_events is runtime.ask_cancellations.events
    assert repo._ask_cancel_lock is runtime.ask_cancellations.lock

    nb = repo.create_notebook(NotebookCreate(name="t23"))
    ev = threading.Event()
    job_id, _ = repo.begin_ask_job(nb.id, AskRequest(question="Q?", mode="chunk"),
                                   "chunk", ev)
    assert runtime.ask_cancellations.get(job_id) is ev     # facade begin 注册进同一注册表
    repo.cancel_ask_job(job_id, repo.current_user().id)     # 显式取消经注册表 set
    assert ev.is_set()
    repo.finish_ask_job(job_id, "cancelled")
    assert runtime.ask_cancellations.get(job_id) is None    # finish 注销


def test_coordinator_module_never_imports_the_facade():
    source = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "ask_execution.py"
    ).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert not any(m.startswith("app.services.sqlite_repository") for m in modules)
    assert not any(m.startswith("app.services.repository_runtime") for m in modules)
    # identity 显式:coordinator 不读请求 ContextVar,user_id 由调用方传入
    assert "current_user" not in source and "_REQUEST_USER" not in source


# ---------------------------------------------------------------------------
# idempotent re-submission: client_request_id → attach to the existing job and
# follow it from the store instead of running a second engine
# ---------------------------------------------------------------------------

class AttachingAskState(RecordingAskState):
    """RecordingAskState plus the idempotency-key surface. ``existing`` is the
    job the key already created (``None`` → the key is fresh and the normal
    begin path runs); ``details`` is the sequence of ``ask_job_detail`` reads
    the follower will observe, the last one repeated forever."""

    def __init__(self, calls, existing=None, details=(), answers=None,
                 existing_notebook="nb-1", probe_visible=True):
        super().__init__(calls)
        self.existing = existing
        self.existing_notebook = existing_notebook
        # False: the read-only probe misses the job (a concurrent retry landed
        # it after the probe) and only the atomic lookup+insert reports it.
        self.probe_visible = probe_visible
        self.details = list(details)
        self.answers = answers or {}
        self.detail_reads = 0

    def find_job_for_client_request(self, user_id, client_request_id):
        self.calls.append(("probe", user_id, client_request_id))
        if self.existing is None or not self.probe_visible:
            return None
        return {"job_id": self.existing[0], "notebook_id": self.existing_notebook,
                "conversation_id": self.existing[1]}

    def begin_or_attach_durable_job(self, notebook_id, payload, mode, user_id):
        from app.repositories.ports import AskRequestKeyConflict

        self.calls.append(("begin_or_attach", notebook_id, mode, user_id,
                           payload.client_request_id))
        if self.existing is not None:
            if self.existing_notebook != notebook_id:
                raise AskRequestKeyConflict(payload.client_request_id)
            payload.conversation_id = self.existing[1]
            return self.existing[0], self.existing[1], True
        payload.conversation_id = "conv-t23"
        return "askjob-t23", "conv-t23", False

    def ask_job_detail(self, job_id):
        self.detail_reads += 1
        index = min(self.detail_reads, len(self.details)) - 1
        detail = dict(self.details[index])
        detail.setdefault("job_id", job_id)
        return detail

    def ask_answer_detail(self, answer_id):
        self.calls.append(("answer_detail", answer_id))
        return self.answers.get(answer_id)


def _attach_coordinator(state, submitter=None):
    service = FakeAskService(lambda *_args, **_kwargs: pytest.fail("engine must not run"))
    return AskExecutionCoordinator(
        ask_state=state,
        cancellations=AskCancellationRegistry(),
        job_submitter=submitter if submitter is not None else InlineSubmitter(),
        event_log=_event_log(),
        ask=lambda: service,
        follow_poll_seconds=0,
    )


_STEP_A = {"step_type": "retrieve", "summary": "a", "detail": {}}
_STEP_B = {"step_type": "reflect", "summary": "b", "detail": {}}


def test_repeated_client_request_id_attaches_and_replays_the_finished_job():
    calls = []
    state = AttachingAskState(
        calls,
        existing=("askjob-old", "conv-old"),
        details=[{"status": "done", "trace": [_STEP_A, _STEP_B], "answer_id": "ans-old",
                  "error": ""}],
        answers={"ans-old": {"answer_id": "ans-old", "question": "Q?",
                             "payload": {"answer_id": "ans-old", "answer": "saved",
                                         "conversation_id": "conv-old"},
                             "created_at": "t"}},
    )
    coordinator = _attach_coordinator(state)
    payload = AskRequest(question="Q?", mode="reasoning", client_request_id="key-1")
    submitter = coordinator.job_submitter

    delivered = _drain(coordinator.start(
        "nb-1", payload, ASK_MODES["reasoning"], user_id="user-t23"))

    assert delivered == [
        {"event": "started", "job_id": "askjob-old", "conversation_id": "conv-old"},
        {"event": "progress", "step": _STEP_A},
        {"event": "progress", "step": _STEP_B},
        {"event": "final", "response": {"answer_id": "ans-old", "answer": "saved",
                                        "conversation_id": "conv-old"}},
    ]
    # The existing job's ids are written back exactly as a fresh begin would.
    assert payload.conversation_id == "conv-old"
    assert ("probe", "user-t23", "key-1") in calls
    # Nothing was begun, traced, finished or cleaned up: the job's own worker
    # owns its row. Only the store was read.
    assert not any(c[0] in {"begin", "begin_or_attach", "trace", "finish", "cleanup", "mode"}
                   for c in calls)
    assert coordinator.cancellations.get("askjob-old") is None
    assert submitter.submitted == ["ask-follow"]


def test_attach_follows_a_running_job_until_its_row_settles_without_repeating_steps():
    calls = []
    state = AttachingAskState(
        calls,
        existing=("askjob-old", "conv-old"),
        details=[
            {"status": "running", "trace": [_STEP_A], "answer_id": "", "error": ""},
            {"status": "running", "trace": [_STEP_A, _STEP_B], "answer_id": "", "error": ""},
            {"status": "done", "trace": [_STEP_A, _STEP_B], "answer_id": "ans-old",
             "error": ""},
        ],
        answers={"ans-old": {"answer_id": "ans-old", "question": "Q?",
                             "payload": {"answer_id": "ans-old"}, "created_at": "t"}},
    )
    coordinator = _attach_coordinator(state)

    delivered = _drain(coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="chunk", client_request_id="key-1"),
        ASK_MODES["chunk"], user_id="user-t23"))

    assert [e["event"] for e in delivered] == ["started", "progress", "progress", "final"]
    assert [e["step"] for e in delivered if e["event"] == "progress"] == [_STEP_A, _STEP_B]
    assert state.detail_reads == 3


@pytest.mark.parametrize("status,error,expected", [
    ("cancelled", "", {"event": "cancelled"}),
    ("failed", "RuntimeError: boom", {"event": "error", "error": "RuntimeError: boom"}),
    ("interrupted", "", {"event": "error", "error": "ask job interrupted"}),
])
def test_attach_reports_the_existing_jobs_terminal_row(status, error, expected):
    state = AttachingAskState(
        [], existing=("askjob-old", "conv-old"),
        details=[{"status": status, "trace": [], "answer_id": "", "error": error}],
    )
    delivered = _drain(_attach_coordinator(state).start(
        "nb-1", AskRequest(question="Q?", mode="chunk", client_request_id="key-1"),
        ASK_MODES["chunk"], user_id="user-t23"))
    assert delivered[-1] == expected
    assert len(delivered) == 2


def test_attach_with_a_missing_answer_row_ends_with_an_error_not_a_hang():
    state = AttachingAskState(
        [], existing=("askjob-old", "conv-old"),
        details=[{"status": "done", "trace": [], "answer_id": "ans-gone", "error": ""}],
    )
    delivered = _drain(_attach_coordinator(state).start(
        "nb-1", AskRequest(question="Q?", mode="chunk", client_request_id="key-1"),
        ASK_MODES["chunk"], user_id="user-t23"))
    assert delivered[-1]["event"] == "error"
    assert "ans-gone" not in delivered[-1]["error"] or "missing" in delivered[-1]["error"]


def test_attach_store_failure_still_closes_the_stream_with_an_error():
    class Broken(AttachingAskState):
        def ask_job_detail(self, job_id):
            raise KeyError(job_id)

    state = Broken([], existing=("askjob-old", "conv-old"))
    delivered = _drain(_attach_coordinator(state).start(
        "nb-1", AskRequest(question="Q?", mode="chunk", client_request_id="key-1"),
        ASK_MODES["chunk"], user_id="user-t23"))
    assert delivered[0]["event"] == "started"
    assert delivered[-1]["event"] == "error"


@pytest.mark.parametrize("probe_visible", [True, False])
def test_client_request_key_conflict_ends_the_stream_with_an_error_and_no_job(probe_visible):
    from app.services.ask_execution import KEY_CONFLICT_MESSAGE

    calls = []
    state = AttachingAskState(
        calls, existing=("askjob-elsewhere", "conv-elsewhere"),
        existing_notebook="nb-other", probe_visible=probe_visible)
    coordinator = _attach_coordinator(state)
    delivered = _drain(coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="chunk", client_request_id="key-1"),
        ASK_MODES["chunk"], user_id="user-t23"))
    # Displayable wording, not an exception dump; no job, no follower.
    assert delivered == [{"event": "error", "error": KEY_CONFLICT_MESSAGE}]
    assert coordinator.job_submitter.submitted == []
    assert not any(c[0] in {"begin", "finish", "cleanup"} for c in calls)


def test_keyed_retry_attaches_before_any_request_validation():
    """codex #665 r1 P2: the original submission passed validation; a retry
    must replay its job even if the notebook/mode state would now fail it."""
    class Rejecting(FakeAskService):
        def validate_reasoning_submission(self, notebook_id, payload):
            pytest.fail("validation must not run for a keyed retry that attaches")

    state = AttachingAskState(
        [], existing=("askjob-old", "conv-old"),
        details=[{"status": "cancelled", "trace": [], "answer_id": "", "error": ""}],
    )
    service = Rejecting(lambda *_a, **_k: pytest.fail("engine must not run"))
    coordinator = AskExecutionCoordinator(
        ask_state=state, cancellations=AskCancellationRegistry(),
        job_submitter=InlineSubmitter(), event_log=_event_log(), ask=lambda: service,
        follow_poll_seconds=0,
    )
    payload = AskRequest(question="Q?", mode="reasoning", client_request_id="key-1")
    delivered = _drain(coordinator.start(
        "nb-1", payload, ASK_MODES["reasoning"], user_id="user-t23"))
    assert [e["event"] for e in delivered] == ["started", "cancelled"]
    assert payload.conversation_id == "conv-old"


def test_attach_existing_alone_returns_none_for_a_fresh_or_missing_key():
    state = AttachingAskState([])
    coordinator = _attach_coordinator(state)
    assert coordinator.attach_existing(
        "nb-1", AskRequest(question="Q?", mode="chunk"), "user-t23") is None
    assert coordinator.attach_existing(
        "nb-1", AskRequest(question="Q?", mode="chunk", client_request_id="key-1"),
        "user-t23") is None


def test_probe_miss_still_attaches_when_the_atomic_begin_reports_the_winner():
    """The read-only probe raced a concurrent retry: begin_or_attach reports
    the job that landed in between, and the stream follows it."""
    state = AttachingAskState(
        [], existing=("askjob-raced", "conv-raced"), probe_visible=False,
        details=[{"status": "cancelled", "trace": [], "answer_id": "", "error": ""}],
    )
    delivered = _drain(_attach_coordinator(state).start(
        "nb-1", AskRequest(question="Q?", mode="chunk", client_request_id="key-1"),
        ASK_MODES["chunk"], user_id="user-t23"))
    assert delivered[0] == {"event": "started", "job_id": "askjob-raced",
                            "conversation_id": "conv-raced"}
    assert delivered[-1] == {"event": "cancelled"}


def test_fresh_client_request_id_begins_the_job_and_runs_the_engine_normally():
    calls = []
    state = AttachingAskState(calls)  # key unknown → attached=False
    service = FakeAskService(lambda *_a, **_k: _response())
    coordinator = AskExecutionCoordinator(
        ask_state=state, cancellations=AskCancellationRegistry(),
        job_submitter=InlineSubmitter(), event_log=_event_log(), ask=lambda: service,
    )
    delivered = _drain(coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="chunk", client_request_id="key-1"),
        ASK_MODES["chunk"], user_id="user-t23"))
    assert [e["event"] for e in delivered] == ["started", "progress", "final"]
    assert ("begin_or_attach", "nb-1", "chunk", "user-t23", "key-1") in calls
    assert ("begin", "nb-1", "chunk", "user-t23") not in calls
    assert ("finish", "done", "ans-t23", "") in calls
    assert coordinator.job_submitter.submitted == ["ask-chunk"]


def test_payload_without_a_key_keeps_the_plain_begin_path():
    calls = []
    state = AttachingAskState(calls)
    service = FakeAskService(lambda *_a, **_k: _response())
    coordinator = AskExecutionCoordinator(
        ask_state=state, cancellations=AskCancellationRegistry(),
        job_submitter=InlineSubmitter(), event_log=_event_log(), ask=lambda: service,
    )
    _drain(coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="chunk"),
        ASK_MODES["chunk"], user_id="user-t23"))
    assert ("begin", "nb-1", "chunk", "user-t23") in calls
    assert not any(c[0] == "begin_or_attach" for c in calls)


def test_auto_mode_attach_never_selects_an_engine():
    state = AttachingAskState(
        [], existing=("askjob-old", "conv-old"),
        details=[{"status": "cancelled", "trace": [], "answer_id": "", "error": ""}],
    )
    coordinator = _attach_coordinator(state)

    def resolve(_cancel_event):
        pytest.fail("engine selection must not run for an attached submission")

    delivered = _drain(coordinator.start(
        "nb-1", AskRequest(question="Q?", mode="auto", client_request_id="key-1"),
        None, user_id="user-t23", resolve=resolve))
    assert [e["event"] for e in delivered] == ["started", "cancelled"]
