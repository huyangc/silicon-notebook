"""Suggestion expiry and its owning startup/periodic lifecycle (T13)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core import readiness
from app.core.config import Settings
from app.services import startup_warmup
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_suggest_adoption_cases as adoption
from tests import memory_suggest_expiry_cases as cases
from tests import memory_suggest_write_cases as writes


@pytest.fixture
def world(tmp_path):
    repo = SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path / 'expiry.db'}",
        storage_dir=str(tmp_path / "storage"), _env_file=None,
        event_log_enabled=False, llm_log_enabled=False,
    ))
    database = repo._runtime.database

    def run(sql, params=()):
        with database.write(operation="test.seed") as db:
            db.execute(sql, params)

    def rows(sql, params=()):
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    try:
        yield adoption.build_world(writes.Env(
            store=repo._runtime.memory_store, suggest=repo._runtime.memory_suggest_store,
            run=run, rows=rows, json=json.dumps,
        ))
    finally:
        repo.close()


def test_30_days_to_rejected_expired_revision_and_event(world):
    cases.assert_30_days_to_rejected_expired_revision_and_event(world)


def test_adopted_and_manual_and_agent_candidates_untouched(world):
    cases.assert_adopted_and_manual_and_agent_candidates_untouched(world)


def test_page_limit_500_idempotent(world):
    cases.assert_page_limit_500_idempotent(world)


def test_expiry_runs_when_deployment_switch_off(world):
    cases.assert_expiry_runs_when_deployment_switch_off(world)


@pytest.fixture
def lifecycle(monkeypatch):
    from app.api import deps
    from app.core import config
    from app.services import notebook_delete

    calls = []
    ticks = []

    def expire(**kwargs):
        calls.append(("expire", readiness.is_ready(), kwargs))
        return 1

    def sweep():
        calls.append("delete")
        return 2

    def warm(**kwargs):
        calls.append("warm")
        assert ticks[-1]() == 2
        return 0

    repo = SimpleNamespace(
        settings=Settings(
            _env_file=None, auto_memory_suggest_enabled=False,
            auto_memory_suggest_clarified_enabled=False,
            auto_memory_suggest_repeated_enabled=False,
            auto_memory_suggest_corrected_enabled=False,
            auto_memory_suggest_expiry_sweep_limit=500, notebook_delete_sweep_seconds=15,
        ),
        _runtime=SimpleNamespace(
            notebook_delete=SimpleNamespace(sweep_once=sweep),
            memory_service=SimpleNamespace(now=lambda: writes.NOW, expire_suggestions=expire),
            agent_profile_jobs=SimpleNamespace(
                sweep_on_start=lambda: calls.append(("profile-recovery", readiness.is_ready())) or 1
            ),
            memory_suggest_job=SimpleNamespace(
                sweep_on_start=lambda: calls.append(("suggest-recovery", readiness.is_ready())) or 1
            ),
        ),
        _recover_interrupted_jobs=lambda: calls.append("recover"),
        warm_open_path_caches=warm,
        close=lambda: calls.append("close"),
    )

    def factory():
        return repo

    factory.cache_clear = lambda: calls.append("cache-clear")

    def start(runner, interval, *, tick):
        assert runner is repo._runtime.notebook_delete
        assert interval > 0
        ticks.append(tick)
        return lambda: calls.append("stop")

    monkeypatch.setattr(deps, "repository", factory)
    monkeypatch.setattr(config, "get_settings", lambda: repo.settings)
    monkeypatch.setattr(notebook_delete, "start_notebook_delete_sweeper", start)
    monkeypatch.setattr(startup_warmup, "_start_admission_refresher", lambda *_: None)
    for name in (
        "_reproject_legacy_knowhow_tables", "_sweep_orphan_memory_sources",
        "_backfill_global_ask_attribution", "_backfill_navigation_chunks",
        "_heal_memory_merges", "_rebuild_memory_isolated_notebooks",
    ):
        monkeypatch.setattr(startup_warmup, name, lambda *_: None)
    lease = startup_warmup.begin_lifecycle()
    assert lease is not None
    try:
        assert startup_warmup.run_startup(lease) is repo
        yield repo, calls, ticks[0], lease
    finally:
        startup_warmup.close_repository(lease, repo)


def test_startup_pass_after_readiness(lifecycle):
    _repo, calls, _tick, _lease = lifecycle
    assert calls == [
        "recover", ("profile-recovery", False), ("suggest-recovery", False),
        "delete", "warm", "delete",
        ("expire", True, {"now": writes.NOW, "limit": 500}),
    ]


def test_periodic_tick_isolated_from_delete_sweep_failure(lifecycle, caplog):
    repo, calls, tick, _lease = lifecycle

    def fail():
        raise RuntimeError("private exception text must stay out of logs")

    repo._runtime.notebook_delete.sweep_once = fail
    assert tick() == 0
    assert calls[-1] == ("expire", True, {"now": writes.NOW, "limit": 500})
    assert "private exception text" not in caplog.text


def test_expiry_failure_does_not_block_next_delete_tick(lifecycle, caplog):
    repo, calls, tick, _lease = lifecycle

    def fail(**kwargs):
        raise RuntimeError("private expiry failure")

    repo._runtime.memory_service.expire_suggestions = fail
    before = calls.count("delete")
    assert tick() == tick() == 2
    assert calls.count("delete") == before + 2
    assert "private expiry failure" not in caplog.text


def test_sweeper_stops_before_pool_close_and_stale_tick_is_inert(lifecycle):
    repo, calls, tick, lease = lifecycle
    startup_warmup.close_repository(lease, repo)
    assert calls[-3:] == ["stop", "close", "cache-clear"]
    before = list(calls)
    assert tick() == 0
    assert calls == before
    successor = startup_warmup.begin_lifecycle()
    assert successor is not None
    try:
        assert startup_warmup.run_startup(successor) is repo
        before = list(calls)
        assert tick() == 0
        assert calls == before
    finally:
        startup_warmup.close_repository(successor, repo)


def test_shutdown_during_delete_does_not_start_expiry(lifecycle):
    repo, calls, tick, lease = lifecycle

    def close_during_delete():
        startup_warmup.close_repository(lease, repo)
        return 2

    repo._runtime.notebook_delete.sweep_once = close_during_delete
    expiry_count = sum(isinstance(call, tuple) and call[0] == "expire" for call in calls)
    assert tick() == 2
    assert calls[-3:] == ["stop", "close", "cache-clear"]
    assert sum(isinstance(call, tuple) and call[0] == "expire" for call in calls) == expiry_count
