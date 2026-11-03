"""Fresh-auth hints preserve the live-switch fallback and request ownership."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from threading import Event, Thread, local

import httpx
import pytest
from starlette.requests import Request

from app.api import deps
from app.core.request_context import get_request_user, reset_request_user, set_request_user
from app.domain.extensions import CompletedAskNotification
from app.repositories.memory_mode_epoch import memory_mode_write, stable_mode_epoch
from tests.ask_testkit import seed_ask_evidence
from tests.test_memory_api import _client, _register


@pytest.fixture
def authenticated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    monkeypatch.setenv("EXTENSIONS_CONFIG", "")
    for name in ("OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL", "EMBED_PROVIDER"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("AUTO_MEMORY_SUGGEST_TRIGGER", "1000000")
    client = _client(tmp_path, monkeypatch)
    headers, owner = _register(client, "e00000001")
    nb = client.post("/api/notebooks", headers=headers, json={"name": "epoch"}).json()["id"]
    repo = deps.repository()
    seed_ask_evidence(repo, nb)
    attempts = []

    def forbidden_http(*args, **kwargs):
        attempts.append(1)
        raise AssertionError("external HTTP/model calls forbidden")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_http)
    yield client, repo, headers, owner, nb
    assert attempts == []


def _request(headers):
    return Request({"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()], "path_params": {}})


@contextmanager
def _authenticated(headers):
    # asyncio.run copies ContextVars, so keep the dependency and completion in
    # the same task; callers receive an async callback rather than a fake hint.
    async def run(callback):
        dependency = deps.get_current_user(_request(headers))
        user = await anext(dependency)
        try:
            return callback(user)
        finally:
            await dependency.aclose()
    yield lambda callback: asyncio.run(run(callback))


def _notification(owner, nb):
    return CompletedAskNotification(owner, nb, "chunk", submitted_via="web", ask_job_id="epoch-job")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode,expected_reads", [("manual", 0), ("suggest", 1)])
def test_real_asgi_ask_copies_fresh_auth_into_background_completion(
    authenticated_runtime, monkeypatch, stream, mode, expected_reads,
):
    client, repo, headers, owner, nb = authenticated_runtime
    runtime = repo._runtime
    store = runtime.memory_suggest_store
    store.set_memory_mode(owner, mode, now="2026-11-01T00:00:00+00:00")
    seen, sql, completed = [], [], Event()
    observing = local()
    connect = runtime.database.connect
    new_connection = runtime.database._new_connection

    def traced_new_connection(*args, **kwargs):
        connection = new_connection(*args, **kwargs)
        connection.set_trace_callback(lambda statement: sql.append(statement) if getattr(observing, "active", False) else None)
        return connection

    def traced_connect():
        connection = connect()
        connection.set_trace_callback(lambda statement: sql.append(statement) if getattr(observing, "active", False) else None)
        return connection

    monkeypatch.setattr(runtime.database, "connect", traced_connect)
    monkeypatch.setattr(runtime.database, "_new_connection", traced_new_connection)
    original = runtime.memory_suggest_trigger.note_ask_completed

    def observed(notification):
        observing.active = True
        try:
            original(notification)
        finally:
            observing.active = False

    monkeypatch.setattr(runtime.memory_suggest_trigger, "note_ask_completed", observed)
    host = runtime.ask_completed_observers
    original_host = host.observe_application

    def observed_host(context, *, event_sink=None):
        actor = get_request_user()
        seen.append((actor.id, actor.memory_mode, actor._memory_mode_epoch,
                     context.notification.actor_id,context.skip_builtin_memory_suggest,
                     context.memory_suggest is not None))
        try:
            return original_host(context,event_sink=event_sink)
        finally:
            completed.set()

    monkeypatch.setattr(host,"observe_application",observed_host)
    response = client.post(f"/api/notebooks/{nb}/ask" + ("/stream" if stream else ""), headers=headers,
                           json={"question": "What evidence is available?", "mode": "chunk"})
    assert response.status_code == 200, response.text
    assert completed.wait(10), "real background completion did not run"
    assert len(seen) == 1
    assert seen[0][0:2] == (owner, mode) and seen[0][3] == owner
    assert seen[0][2] is not None
    assert seen[0][4:] == (mode == "manual",True)
    reads = [statement for statement in sql if statement.lstrip().upper().startswith("SELECT") and "memory_suggest_users" in statement]
    writes = [statement for statement in sql if statement.lstrip().upper().startswith("INSERT") and "memory_suggest_jobs" in statement]
    assert len(reads) == expected_reads, sql
    assert bool(writes) == (mode == "suggest"), sql
    if mode == "manual":
        assert sql == []
    else:
        product_statements = [statement for statement in sql if statement.lstrip().upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE"))]
        assert len(product_statements) == 2, sql
    assert get_request_user() is None


def test_auth_read_straddling_write_and_old_optional_context_never_get_certified(authenticated_runtime, monkeypatch):
    _client, repo, headers, owner, _nb = authenticated_runtime
    identity = repo._runtime.identity
    original = identity.resolve_session

    def changed(token):
        user = original(token)
        with memory_mode_write():
            pass
        return user

    monkeypatch.setattr(identity, "resolve_session", changed)
    with _authenticated(headers) as run:
        assert run(lambda actor: actor._memory_mode_epoch) is None
    old = original(headers["Authorization"].removeprefix("Bearer "))
    token = set_request_user(old)
    monkeypatch.setattr(deps, "get_settings", lambda: type("Settings", (), {"auth_optional": True})())
    try:
        with _authenticated({}) as run:
            assert run(lambda actor: actor._memory_mode_epoch) is None
        assert get_request_user() is old
    finally:
        reset_request_user(token)
    assert get_request_user() is None


@pytest.mark.parametrize("change", ["enable", "disable", "rollback", "replacement", "different_actor"])
def test_inflight_auth_hint_falls_back_after_switch_or_owner_changes(authenticated_runtime, monkeypatch, change):
    _client, repo, headers, owner, nb = authenticated_runtime
    store = repo._runtime.memory_suggest_store
    trigger = repo._runtime.memory_suggest_trigger
    reads = []
    original = store.user_state
    monkeypatch.setattr(store, "user_state", lambda who: (reads.append(who), original(who))[1])

    def complete(actor):
        assert actor._memory_mode_epoch is not None
        target = owner
        if change == "enable":
            store.set_memory_mode(owner, "suggest", now="2026-11-01T00:00:00+00:00")
        elif change == "disable":
            store.set_memory_mode(owner, "suggest", now="2026-11-01T00:00:00+00:00")
            store.set_memory_mode(owner, "manual", now="2026-11-01T00:00:01+00:00")
        elif change == "rollback":
            with pytest.raises(KeyError):
                store.set_memory_mode("missing-user", "suggest", now="2026-11-01T00:00:00+00:00")
        elif change == "replacement":
            type(store)(store.database, new_id=store.new_id)
        else:
            target = "different-actor"
        trigger.note_ask_completed(_notification(target, nb))
        assert reads == [target]
        with store.database.connect() as db:
            row = db.execute("SELECT pending_signal FROM memory_suggest_jobs WHERE owner_id=?", (owner,)).fetchone()
        assert bool(row) == (change == "enable")

    with _authenticated(headers) as run:
        run(complete)


def test_overlapping_and_failed_writes_never_publish_stable_epoch():
    before = stable_mode_epoch()
    with memory_mode_write():
        assert stable_mode_epoch() is None
        with pytest.raises(RuntimeError), memory_mode_write():
            assert stable_mode_epoch() is None
            raise RuntimeError("rollback")
        assert stable_mode_epoch() is None
    assert stable_mode_epoch() is not None and stable_mode_epoch() != before


@pytest.mark.parametrize("new_mode,missing", [("suggest", False), ("manual", False), ("suggest", True)])
def test_real_mode_transaction_inflight_invalidates_hint_before_any_database_io(
    authenticated_runtime, monkeypatch, new_mode, missing,
):
    _client, repo, headers, owner, nb = authenticated_runtime
    store = repo._runtime.memory_suggest_store
    trigger = repo._runtime.memory_suggest_trigger
    started, release = Event(), Event()
    write, user_state = store.database.write, store.user_state
    reads, failures = [], []

    @contextmanager
    def paused_write():
        started.set()
        assert release.wait(10)
        with write() as db:
            yield db

    def change():
        try:
            store.set_memory_mode("missing-user" if missing else owner, new_mode, now="2026-11-01T00:00:00+00:00")
        except KeyError:
            failures.append("rollback")

    def complete(actor):
        monkeypatch.setattr(store.database, "write", paused_write)
        monkeypatch.setattr(store, "user_state", lambda who: (reads.append(who), user_state(who))[1])
        worker = Thread(target=change)
        worker.start()
        try:
            assert started.wait(10)
            assert stable_mode_epoch() is None
            trigger.note_ask_completed(_notification(owner, nb))
            assert reads == [owner]
        finally:
            release.set()
            worker.join(10)
        assert not worker.is_alive()
        assert failures == (["rollback"] if missing else [])
        assert stable_mode_epoch() is not None and stable_mode_epoch() != actor._memory_mode_epoch
        trigger.note_ask_completed(_notification(owner, nb))
        assert reads == [owner, owner]

    with _authenticated(headers) as run:
        run(complete)


def test_private_stamp_never_appears_in_user_schema_or_payload(authenticated_runtime):
    _client, repo, headers, _owner, _nb = authenticated_runtime
    with _authenticated(headers) as run:
        assert run(lambda actor: (actor._memory_mode_epoch is not None,
                                  "_memory_mode_epoch" not in actor.model_dump(),
                                  "_memory_mode_epoch" not in actor.model_json_schema()["properties"])) == (True, True, True)


def test_real_session_reads_produce_fresh_request_owned_profiles(authenticated_runtime):
    _client, repo, headers, _owner, _nb = authenticated_runtime
    token = headers["Authorization"].removeprefix("Bearer ")
    first = repo._runtime.identity.resolve_session(token)
    second = repo._runtime.identity.resolve_session(token)
    assert first is not second and first == second


def test_nested_optional_dependency_copies_without_recertifying_outer_hint(authenticated_runtime, monkeypatch):
    _client, _repo, headers, _owner, _nb = authenticated_runtime
    monkeypatch.setattr(deps,"get_settings",lambda:type("Settings",(),{"auth_optional":True})())

    async def run():
        outer = deps.get_current_user(_request(headers))
        actor = await anext(outer)
        stamp = actor._memory_mode_epoch
        assert stamp is not None
        inner = deps.get_current_user(_request({}))
        try:
            inherited = await anext(inner)
            assert inherited is not actor and inherited._memory_mode_epoch is None
            assert actor._memory_mode_epoch == stamp
        finally:
            await inner.aclose()
        assert get_request_user() is actor and actor._memory_mode_epoch == stamp
        await outer.aclose()
        assert get_request_user() is None

    asyncio.run(run())
