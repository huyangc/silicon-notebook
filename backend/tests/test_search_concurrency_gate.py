"""Z8 (P0 止血 + 批 0 评审 P1): server-side concurrency gate on notebook search.

``search_notebook`` runs an unindexed three-leg ILIKE scan. Before this gate,
neither the HTTP ``GET /notebooks/{id}/search`` route nor the MCP
``search_notebook_context`` tool put any limit on how many of those scans
could run at once -- a handful of concurrent typists (the collection view
fans one search out per visible notebook) could exhaust the whole DB
connection pool.

The gate's FIRST shape -- a ``threading.BoundedSemaphore`` acquired with a
blocking ``with`` from a worker thread -- fixed that by creating a worse
failure: every waiter sat on one of the 40 anyio worker-thread tokens that
every synchronous endpoint in the process shares, so one search burst starved
the entire API surface (``/notebooks``, source lists, checkup, upload) for as
long as the burst lasted. The gate is now an ``asyncio`` semaphore acquired
on the event loop, so a waiter is a suspended coroutine costing no thread.

These tests drive the REAL route and tool bodies against a fake blocking
search (a ``threading.Event``, never a real database) to prove:

* at most ``SEARCH_CONCURRENCY_LIMIT`` (4) searches run concurrently;
* the HTTP route and the MCP tool wait on the exact same semaphore instance,
  so one entry point's load throttles the other;
* **waiters occupy no worker thread** -- an unrelated synchronous endpoint
  still gets a thread out of the shared pool while a search burst queues.
  This is the P1 nail: flip the route back to ``def`` + a threading
  semaphore and this one goes red while the other two stay green.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import ask_routes
from app.api.mcp_tools import memory_context
from app.models.ask import NotebookSearchResponse
from app.services import search_concurrency
from app.services.search_concurrency import (
    search_concurrency_gate,
    search_concurrency_limit,
)


# How long a fake "search" body blocks before giving up on its release event.
# Bounds the whole module if an assertion fires while threads are parked.
_BODY_TIMEOUT = 5.0


async def _settle(turns: int = 50) -> None:
    """Give every runnable task a chance to progress, without wall clock."""
    for _ in range(turns):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# The gate itself: shape, ceiling, and the single-loop assumption it enforces.
# ---------------------------------------------------------------------------


def test_search_concurrency_limit_defaults_to_four_and_is_settings_tunable() -> None:
    """默认 4 对齐前端 SEARCH_FANOUT_LIMIT;上限是部署可调的 Settings 字段
    (SEARCH_CONCURRENCY_LIMIT, ge=1, codex #627 R2 P2),不再是模块硬编码常量。"""
    assert search_concurrency_limit() == 4
    from app.core.config import Settings
    field = Settings.model_fields["search_concurrency_limit"]
    assert field.default == 4
    assert str(field.validation_alias) == "SEARCH_CONCURRENCY_LIMIT"


def test_gate_is_an_asyncio_semaphore_admitting_four_with_the_fifth_pending() -> None:
    """Four coroutines hold the gate; the fifth stays pending until one
    releases. An ``asyncio`` semaphore is the whole point: waiting happens on
    the event loop, so a waiter is a suspended coroutine rather than a
    blocked worker thread."""

    async def scenario() -> None:
        gate = search_concurrency_gate()
        assert isinstance(gate, asyncio.Semaphore)

        entered: list[object] = []
        release = asyncio.Event()

        async def holder(name: object) -> None:
            async with gate:
                entered.append(name)
                await release.wait()

        holders = [
            asyncio.create_task(holder(i)) for i in range(search_concurrency_limit())
        ]
        await _settle()
        assert len(entered) == search_concurrency_limit()

        fifth = asyncio.create_task(holder("fifth"))
        await _settle()  # every chance to (wrongly) proceed
        assert "fifth" not in entered, (
            "a 5th concurrent holder entered without waiting for a free slot"
        )

        release.set()
        await asyncio.gather(*holders, fifth)
        assert "fifth" in entered
        assert len(entered) == search_concurrency_limit() + 1

    asyncio.run(scenario())


def test_gate_is_one_lazily_built_instance_and_demands_a_running_loop() -> None:
    """``asyncio.Semaphore`` is loop-affine and there is no loop at import
    time, so the gate is a lazy singleton. Off the loop it must fail loudly
    rather than hand back something that silently does not throttle."""
    with pytest.raises(RuntimeError, match="event loop"):
        search_concurrency_gate()

    async def scenario() -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
        return search_concurrency_gate(), search_concurrency_gate()

    first, second = asyncio.run(scenario())
    assert first is second


def test_gate_refuses_a_second_live_event_loop() -> None:
    """One semaphore cannot gate two live loops -- and ``Semaphore.acquire``
    only notices a foreign loop on the *contended* path, i.e. it would go
    silently un-gated exactly when the gate matters. The accessor checks
    instead."""

    async def bind() -> asyncio.Semaphore:
        return search_concurrency_gate()

    other = asyncio.new_event_loop()
    try:
        bound = other.run_until_complete(bind())
        assert bound is not None

        async def from_a_second_loop() -> None:
            with pytest.raises(RuntimeError, match="second live event loop"):
                search_concurrency_gate()

        # `other` is still open, so this really is two live loops.
        asyncio.run(from_a_second_loop())
    finally:
        other.close()

    # A *closed* loop is a finished one, not a competitor: the gate rebinds.
    assert asyncio.run(bind()) is not None


# ---------------------------------------------------------------------------
# Shared fake search body + route invocation the way the server does it.
# ---------------------------------------------------------------------------


class _ConcurrencyProbe:
    """Tracks how many fake search bodies are inside the gate at once."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current = 0
        self.max_seen = 0
        self.entered: list[str] = []

    def enter(self, name: str) -> None:
        with self.lock:
            self.current += 1
            self.max_seen = max(self.max_seen, self.current)
            self.entered.append(name)

    def exit(self) -> None:
        with self.lock:
            self.current -= 1


def _fake_http_repo(probe: _ConcurrencyProbe, release: threading.Event):
    """Fake ``notebook_catalog_repository()`` whose ``search_notebook`` blocks
    on ``release`` while inside the gate -- exercises the REAL
    ``ask_routes.search_notebook`` route body, not the semaphore in
    isolation."""

    class _FakeCatalogRepo:
        def search_notebook(self, notebook_id: str, q: str) -> NotebookSearchResponse:
            probe.enter(threading.current_thread().name)
            try:
                release.wait(_BODY_TIMEOUT)
                return NotebookSearchResponse(query=q, hits=[])
            finally:
                probe.exit()

    return _FakeCatalogRepo()


async def _call_search_route(notebook_id: str = "nb-1", q: str = "q"):
    """Invoke the real ``/search`` endpoint the way Starlette dispatches it.

    An ``async def`` endpoint is awaited on the event loop; a plain ``def``
    endpoint is handed to the shared worker-thread pool. Branching here
    rather than hard-coding ``await`` is what keeps the mutation check
    honest: put the route back to ``def`` + a threading semaphore and this
    helper still runs it exactly as the server would, so
    ``test_waiting_searches_leave_the_shared_thread_pool_free`` fails for the
    real reason instead of on a TypeError.
    """
    endpoint = ask_routes.search_notebook
    if inspect.iscoroutinefunction(endpoint):
        return await endpoint(notebook_id, q)
    return await asyncio.to_thread(endpoint, notebook_id, q)


async def _await_holders(probe: _ConcurrencyProbe, count: int, what: str) -> None:
    deadline = time.monotonic() + _BODY_TIMEOUT
    while probe.current < count:
        assert time.monotonic() < deadline, f"{what} never all reached the gate"
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# The P1 nail: a waiting search costs no worker thread.
# ---------------------------------------------------------------------------


def test_waiting_searches_leave_the_shared_thread_pool_free(monkeypatch) -> None:
    """A search burst must not starve unrelated synchronous endpoints.

    The loop's default executor stands in for Starlette's anyio worker pool:
    a fixed, process-wide budget of threads that EVERY synchronous endpoint
    draws from. Six threads, four searches holding the gate, six more
    queueing behind it -- and then one unrelated synchronous call.

    * event-loop gate (current): the four winners hold four threads, the six
      waiters are suspended coroutines holding none, and the unrelated call
      gets one of the two free threads immediately.
    * thread-blocking gate (the P1 bug): the waiters grab every remaining
      thread and block in them, the pool is exhausted, and the unrelated call
      never runs -- ``wait_for`` times out and this test goes red.
    """
    pool_size, holder_count, waiter_count = 6, search_concurrency_limit(), 6
    probe = _ConcurrencyProbe()
    release = threading.Event()
    monkeypatch.setattr(
        ask_routes,
        "notebook_catalog_repository",
        lambda: _fake_http_repo(probe, release),
    )

    async def scenario() -> None:
        pool = ThreadPoolExecutor(
            max_workers=pool_size, thread_name_prefix="shared-api-pool"
        )
        asyncio.get_running_loop().set_default_executor(pool)

        holders = [asyncio.create_task(_call_search_route()) for _ in range(holder_count)]
        await _await_holders(probe, holder_count, "the 4 gate holders")

        waiters = [asyncio.create_task(_call_search_route()) for _ in range(waiter_count)]
        # Real time, not just loop turns: a thread-blocking gate needs the
        # waiters to actually be scheduled onto pool threads before the
        # starvation it causes is observable.
        await asyncio.sleep(0.25)
        assert probe.current == holder_count, (
            "the gate let more than 4 searches run at once"
        )

        try:
            served = await asyncio.wait_for(
                asyncio.to_thread(lambda: "unrelated endpoint served"), timeout=2
            )
        except asyncio.TimeoutError:  # pragma: no cover - the P1 failure mode
            release.set()
            raise AssertionError(
                "an unrelated synchronous endpoint could not get a worker "
                "thread while 6 searches were merely WAITING for the gate -- "
                "waiters are occupying the shared pool, which is the whole "
                "全站饿死 failure this gate must not cause"
            )
        assert served == "unrelated endpoint served"

        release.set()
        results = await asyncio.gather(*holders, *waiters)
        assert len(results) == holder_count + waiter_count
        assert probe.max_seen == search_concurrency_limit()

    asyncio.run(scenario())


def test_http_search_route_is_async_and_threads_the_blocking_body() -> None:
    """Structural companion to the test above: the two properties that make
    a waiter free are (a) the endpoint is a coroutine function, so waiting
    happens on the loop, and (b) the blocking search still leaves the loop
    via ``asyncio.to_thread``. Losing (b) would keep this file's timing tests
    green while stalling every other coroutine in the process."""
    assert inspect.iscoroutinefunction(ask_routes.search_notebook)
    source = inspect.getsource(ask_routes.search_notebook)
    assert "async with search_concurrency_gate()" in source
    assert "asyncio.to_thread" in source
    # The rationale above lives in comments, not a docstring: FastAPI copies a
    # route function's docstring into OpenAPI's operation.description, and the
    # OpenAPI shape is a frozen contract (test_repository_api_contract.py).
    # Explaining the fix must not widen the public API.
    assert ask_routes.search_notebook.__doc__ is None


def test_http_search_route_still_maps_missing_notebook_to_404(monkeypatch) -> None:
    """Response/exception semantics are unchanged by sync -> async: the
    repository's ``KeyError`` must still surface as a 404 from inside the
    threaded body."""

    class _MissingRepo:
        def search_notebook(self, notebook_id: str, q: str):
            raise KeyError(notebook_id)

    monkeypatch.setattr(ask_routes, "notebook_catalog_repository", _MissingRepo)

    with pytest.raises(ask_routes.HTTPException) as excinfo:
        asyncio.run(_call_search_route("missing-nb", "q"))
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Notebook not found"


# ---------------------------------------------------------------------------
# One gate, two entry points.
# ---------------------------------------------------------------------------


class _FakeMcpRepo:
    def __init__(self, probe: _ConcurrencyProbe, name: str, release: threading.Event):
        self._probe = probe
        self._name = name
        self._release = release

    def search_notebook(self, notebook_id: str, query: str):
        self._probe.enter(self._name)
        try:
            self._release.wait(_BODY_TIMEOUT)
            return SimpleNamespace(hits=[])
        finally:
            self._probe.exit()

    def require_agent_access(self, principal, scope, notebook_id):
        return None


class _FakeCtx:
    async def report_progress(self, *args, **kwargs) -> None:  # pragma: no cover
        pass


def _capture_search_notebook_context(monkeypatch, repo):
    """Register the real ``memory_context`` tools against a minimal capturing
    server (mirrors ``tests/test_mcp_bundle_architecture.py``'s
    ``_CaptureServer``) and hand back the actual, unmodified
    ``search_notebook_context`` coroutine function -- not a reimplementation
    of it."""

    captured: dict[str, object] = {}

    class _CaptureServer:
        def tool(self, *, description: str):
            def register(function):
                captured[function.__name__] = function
                return function
            return register

    principal = SimpleNamespace(owner_id="user-mcp", profile_name="agent-mcp")

    def fake_selected_notebook(ctx, repo_arg, scope, record=True):
        return principal, "nb-mcp"

    monkeypatch.setattr(memory_context, "_selected_notebook", fake_selected_notebook)
    memory_context.register_memory_context_tools(_CaptureServer(), lambda: repo)
    return captured["search_notebook_context"]


def test_both_entry_points_use_the_same_gate_accessor() -> None:
    # Identity of the accessor, not of a per-module copy: a second
    # ``asyncio.Semaphore(4)`` would behave the same in isolation but would
    # NOT throttle the other entry point. The behavioural proof is the test
    # below; this one just localises the failure if someone re-imports.
    assert ask_routes.search_concurrency_gate is search_concurrency.search_concurrency_gate
    assert (
        memory_context.search_concurrency_gate
        is search_concurrency.search_concurrency_gate
    )


def test_mcp_entry_point_shares_the_gate_with_the_http_entry_point(monkeypatch) -> None:
    """4 HTTP callers hold every slot; a concurrent MCP caller must wait for
    one of THEM to release -- proof the two entry points gate the same
    resource, not two independent ones. Both wait on one event loop, exactly
    as they do in production (the MCP server is an ASGI sub-app of the same
    FastAPI app)."""
    probe = _ConcurrencyProbe()
    release = threading.Event()
    monkeypatch.setattr(
        ask_routes,
        "notebook_catalog_repository",
        lambda: _fake_http_repo(probe, release),
    )
    mcp_repo = _FakeMcpRepo(probe, "mcp-caller", release)
    search_notebook_context = _capture_search_notebook_context(monkeypatch, mcp_repo)

    async def scenario() -> None:
        holders = [
            asyncio.create_task(_call_search_route())
            for _ in range(search_concurrency_limit())
        ]
        await _await_holders(probe, search_concurrency_limit(), "the 4 HTTP holders")

        mcp_call = asyncio.create_task(
            search_notebook_context(query="q", ctx=_FakeCtx(), limit=12)
        )
        await asyncio.sleep(0.25)
        assert "mcp-caller" not in probe.entered, (
            "the MCP tool ran a search concurrently with 4 full HTTP holders "
            "-- it is not sharing the HTTP route's gate"
        )

        release.set()
        await asyncio.gather(*holders)
        payload = await mcp_call

        assert "mcp-caller" in probe.entered
        assert probe.max_seen == search_concurrency_limit()
        assert payload["notebook_id"] == "nb-mcp"

    asyncio.run(scenario())


def test_mcp_tool_takes_the_gate_before_dispatching_its_worker_thread() -> None:
    """Structural companion: the MCP handler must ``async with`` the gate on
    the event loop and run ``_run_with_progress`` inside it. Acquiring from
    inside ``load`` (the worker thread) is the P1 shape -- it would park a
    waiting agent call on an anyio worker token, which is the shared budget
    every synchronous endpoint draws from."""
    tree = ast.parse(Path(memory_context.__file__).read_text(encoding="utf-8"))
    handler = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "search_notebook_context"
    )

    gate_blocks = [
        node for node in ast.walk(handler)
        if isinstance(node, ast.AsyncWith)
        and any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "id", "") == "search_concurrency_gate"
            for item in node.items
        )
    ]
    assert len(gate_blocks) == 1, (
        "search_notebook_context must acquire the shared gate exactly once, "
        "with `async with` on the event loop"
    )
    assert any(
        isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "_run_with_progress"
        for node in ast.walk(gate_blocks[0])
    ), (
        "_run_with_progress -- which dispatches the blocking load() into a "
        "worker thread -- must run INSIDE the gate, i.e. the gate is taken "
        "before the thread is dispatched"
    )

    load_fn = next(
        node for node in ast.walk(handler)
        if isinstance(node, ast.FunctionDef) and node.name == "load"
    )
    assert "search_concurrency_gate" not in {
        node.id for node in ast.walk(load_fn) if isinstance(node, ast.Name)
    }, (
        "load() runs in a worker thread; acquiring the gate there is exactly "
        "the thread-starving shape this fix removed"
    )
