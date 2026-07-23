"""Readiness gate + /api/ready probe.

Until startup warm-up flips ready, every app route (incl. login/health) returns
503 with the phase, while the anonymous ``/api/ready`` probe and root ``/`` stay
reachable so the frontend can poll and show a "服务启动中" screen. The autouse
``_mark_service_ready`` conftest fixture marks ready before each test; here we
toggle it explicitly to exercise both states.
"""
import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.core import readiness
from app.main import app

client = TestClient(app)


def test_ready_probe_is_anonymous_and_reports_snapshot():
    r = client.get("/api/ready")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ready", "phase", "warmed_notebooks", "total_notebooks"}


def test_gate_503s_app_routes_until_ready():
    readiness.reset()  # simulate mid-startup (conftest marked ready; undo it)
    try:
        # A protected app route is blocked with 503 + phase, BEFORE auth/routing
        # (so no request constructs the repo mid-migration).
        r = client.get("/api/health")
        assert r.status_code == 503
        body = r.json()
        assert body["ready"] is False
        assert body["phase"] == "starting"
        assert "Retry-After" in r.headers
        # The anonymous probe + root stay reachable so the frontend can poll.
        assert client.get("/api/ready").status_code == 200
        assert client.get("/").status_code == 200
    finally:
        readiness.mark_ready()


def test_gate_passes_once_ready():
    readiness.mark_ready()
    r = client.get("/api/health")
    assert r.status_code != 503  # reachable (200 under test auth_optional)


def test_ready_probe_open_even_when_not_ready():
    readiness.reset()
    try:
        assert client.get("/api/ready").json()["ready"] is False
    finally:
        readiness.mark_ready()


def test_run_startup_migrates_warms_and_flips_ready():
    """End-to-end wiring: run_startup constructs the repo (migrate + seed), warms
    the open-path caches, and flips readiness to ready — exactly what the lifespan
    daemon thread does in production."""
    from app.services import startup_warmup

    lease = startup_warmup.begin_lifecycle()
    assert lease is not None
    repo = startup_warmup.run_startup(lease)  # synchronous here (prod uses a thread)
    try:
        snap = readiness.snapshot()
        assert snap["phase"] == "ready"
        assert readiness.is_ready() is True
        assert snap["error"] is None
    finally:
        startup_warmup.close_repository(lease, repo)


def test_startup_failure_stays_not_ready_and_redacts_connection(monkeypatch, caplog):
    from types import SimpleNamespace

    from app.api import deps
    from app.core import config
    from app.services import startup_warmup

    secret = "postgresql://secret-user:secret-password@db.example:5432/notebook"
    calls = []

    def fail_repository():
        calls.append("factory")
        raise RuntimeError(f"driver leaked {secret}")

    fail_repository.cache_clear = lambda: calls.append("clear")

    monkeypatch.setattr(deps, "repository", fail_repository)
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(database_url=secret),
    )
    lease = startup_warmup.begin_lifecycle()
    assert lease is not None
    startup_warmup.run_startup(lease)

    snapshot = readiness.snapshot()
    assert snapshot["ready"] is False
    assert snapshot["phase"] == "error"
    assert snapshot["error"] == (
        "RuntimeError: database initialization failed "
        "(database=postgresql host=db.example:5432 db=notebook)"
    )
    diagnostics = snapshot["error"] + "\n" + caplog.text
    assert "secret-user" not in diagnostics
    assert "secret-password" not in diagnostics

    # Construction failure keeps its exact lease until the owning context's
    # finally; stale cleanup cannot release it or mutate the error snapshot.
    assert startup_warmup.begin_lifecycle() is None
    startup_warmup.close_repository(object(), None)
    assert readiness.snapshot() == snapshot
    assert calls == ["factory", "clear"]
    startup_warmup.close_repository(lease, None)
    assert readiness.snapshot()["phase"] == "stopped"
    startup_warmup.close_repository(lease, None)
    assert calls == ["factory", "clear"]


def test_two_lifespans_start_and_close_distinct_exact_repositories(monkeypatch):
    from types import SimpleNamespace

    from app.api import deps
    from app.main import _lifespan
    from app.services import startup_warmup

    calls: list[str] = []
    instances = []

    def repository():
        index = len(instances) + 1
        calls.append(f"start{index}")
        fake = SimpleNamespace(
            warm_open_path_caches=lambda **_kwargs: calls.append(f"warm{index}") or 0,
            close=lambda: calls.append(f"close{index}"),
        )
        instances.append(fake)
        return fake

    repository.cache_clear = lambda: calls.append("clear")
    monkeypatch.setattr(deps, "repository", repository)
    monkeypatch.setattr(startup_warmup, "_reproject_legacy_knowhow_tables", lambda _repo: None)

    async def wait_ready() -> None:
        deadline = time.monotonic() + 2
        while not readiness.is_ready() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert readiness.is_ready() is True

    async def exercise() -> None:
        async with _lifespan(SimpleNamespace()):
            await wait_ready()
            assert readiness.snapshot()["phase"] == "ready"
        assert readiness.is_ready() is False
        assert readiness.snapshot()["phase"] == "stopped"
        async with _lifespan(SimpleNamespace()):
            await wait_ready()
            assert readiness.snapshot()["phase"] == "ready"
        assert readiness.is_ready() is False
        assert readiness.snapshot()["phase"] == "stopped"

    asyncio.run(exercise())

    assert len(instances) == 2
    assert instances[0] is not instances[1]
    assert calls == [
        "start1",
        "warm1",
        "close1",
        "clear",
        "start2",
        "warm2",
        "close2",
        "clear",
    ]


def test_overlapping_lifespan_fails_before_yield_and_cannot_outlive_owner(
    monkeypatch,
):
    from types import SimpleNamespace

    from app.api import deps
    from app.main import _lifespan
    from app.services import startup_warmup

    calls: list[str] = []
    fake = SimpleNamespace(
        warm_open_path_caches=lambda **_kwargs: calls.append("warm") or 0,
        close=lambda: calls.append("close"),
    )

    def repository():
        calls.append("factory")
        return fake

    repository.cache_clear = lambda: calls.append("clear")
    monkeypatch.setattr(deps, "repository", repository)
    monkeypatch.setattr(startup_warmup, "_reproject_legacy_knowhow_tables", lambda _repo: None)

    async def wait_ready() -> None:
        deadline = time.monotonic() + 2
        while not readiness.is_ready() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert readiness.is_ready() is True

    async def exercise() -> None:
        loser_entered = False
        async with _lifespan(SimpleNamespace()):
            await wait_ready()
            with pytest.raises(
                startup_warmup.LifecycleAlreadyActiveError,
                match="repository lifecycle is active",
            ):
                async with _lifespan(SimpleNamespace()):
                    loser_entered = True
            assert loser_entered is False
            assert readiness.is_ready() is True
            assert calls == ["factory", "warm"]
        assert readiness.snapshot()["phase"] == "stopped"

    asyncio.run(exercise())
    assert calls == ["factory", "warm", "close", "clear"]


def test_failed_lifespan_retains_ownership_until_exit_then_next_cycle_starts(
    monkeypatch,
):
    from types import SimpleNamespace

    from app.api import deps
    from app.main import _lifespan
    from app.services import startup_warmup

    calls: list[str] = []
    instances = []
    error_published = threading.Event()
    ready_published = threading.Event()
    original_mark_error = readiness.mark_error
    original_mark_ready = readiness.mark_ready

    def mark_error(error: str) -> None:
        original_mark_error(error)
        error_published.set()

    def mark_ready() -> None:
        original_mark_ready()
        ready_published.set()

    def repository():
        index = len(instances) + 1
        calls.append(f"factory{index}")

        def warm(**_kwargs):
            calls.append(f"warm{index}")
            if index == 1:
                raise RuntimeError("first lifecycle warm failure")
            return 0

        fake = SimpleNamespace(
            warm_open_path_caches=warm,
            close=lambda: calls.append(f"close{index}"),
        )
        instances.append(fake)
        return fake

    repository.cache_clear = lambda: calls.append("clear")
    monkeypatch.setattr(deps, "repository", repository)
    monkeypatch.setattr(readiness, "mark_error", mark_error)
    monkeypatch.setattr(readiness, "mark_ready", mark_ready)
    monkeypatch.setattr(startup_warmup, "_reproject_legacy_knowhow_tables", lambda _repo: None)

    async def exercise() -> None:
        loser_entered = False
        async with _lifespan(SimpleNamespace()):
            assert await asyncio.to_thread(error_published.wait, 2)
            assert readiness.snapshot()["phase"] == "error"
            assert client.get("/api/health").status_code == 503
            assert client.post("/mcp").status_code == 503

            with pytest.raises(startup_warmup.LifecycleAlreadyActiveError):
                async with _lifespan(SimpleNamespace()):
                    loser_entered = True
            assert loser_entered is False
            assert readiness.snapshot()["phase"] == "error"
            assert calls == ["factory1", "warm1", "close1", "clear"]

        assert readiness.snapshot()["phase"] == "stopped"
        assert readiness.is_ready() is False

        async with _lifespan(SimpleNamespace()):
            assert await asyncio.to_thread(ready_published.wait, 2)
            assert readiness.is_ready() is True
            assert calls == [
                "factory1",
                "warm1",
                "close1",
                "clear",
                "factory2",
                "warm2",
            ]
        assert readiness.snapshot()["phase"] == "stopped"

    asyncio.run(exercise())
    assert len(instances) == 2
    assert calls == [
        "factory1",
        "warm1",
        "close1",
        "clear",
        "factory2",
        "warm2",
        "close2",
        "clear",
    ]


def test_close_repository_without_active_cycle_is_noop_and_never_calls_factory(
    monkeypatch,
):
    from app.api import deps
    from app.services import startup_warmup

    calls = []

    def repository():
        calls.append("factory")
        raise AssertionError("shutdown must not construct a repository")

    repository.cache_clear = lambda: calls.append("clear")
    monkeypatch.setattr(deps, "repository", repository)

    startup_warmup.close_repository(None, None)

    assert calls == []


def test_close_failure_still_stops_clears_and_allows_a_fresh_next_cycle(
    monkeypatch, caplog
):
    from types import SimpleNamespace

    from app.api import deps
    from app.services import startup_warmup

    calls = []
    instances = []

    def repository():
        index = len(instances) + 1
        calls.append(f"start{index}")

        def close():
            calls.append(f"close{index}")
            if index == 1:
                raise RuntimeError("close detail must not escape")

        fake = SimpleNamespace(
            warm_open_path_caches=lambda **_kwargs: calls.append(f"warm{index}") or 0,
            close=close,
        )
        instances.append(fake)
        return fake

    repository.cache_clear = lambda: calls.append("clear")
    monkeypatch.setattr(deps, "repository", repository)
    monkeypatch.setattr(startup_warmup, "_reproject_legacy_knowhow_tables", lambda _repo: None)

    first_lease = startup_warmup.begin_lifecycle()
    assert first_lease is not None
    first = startup_warmup.run_startup(first_lease)
    assert first is instances[0]
    assert readiness.is_ready() is True
    startup_warmup.close_repository(first_lease, first)
    assert readiness.is_ready() is False
    assert readiness.snapshot()["phase"] == "stopped"

    second_lease = startup_warmup.begin_lifecycle()
    assert second_lease is not None
    second = startup_warmup.run_startup(second_lease)
    assert second is instances[1]
    assert second is not first
    assert readiness.is_ready() is True
    startup_warmup.close_repository(second_lease, second)
    assert readiness.is_ready() is False
    assert readiness.snapshot()["phase"] == "stopped"
    assert calls == [
        "start1",
        "warm1",
        "close1",
        "clear",
        "start2",
        "warm2",
        "close2",
        "clear",
    ]
    assert "close detail" not in caplog.text


def test_warmup_failure_closes_exact_repository_and_clears_cache_even_if_close_fails(
    monkeypatch, caplog
):
    from types import SimpleNamespace

    from app.api import deps
    from app.core import config
    from app.services import startup_warmup

    calls = []

    def close():
        calls.append("close")
        raise RuntimeError("close detail must not escape")

    fake = SimpleNamespace(
        warm_open_path_caches=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("warm detail must not escape")
        ),
        close=close,
    )

    def repository():
        calls.append("repository")
        return fake

    repository.cache_clear = lambda: calls.append("clear")
    monkeypatch.setattr(deps, "repository", repository)
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(database_url="sqlite:////tmp/safe.db"),
    )
    lease = startup_warmup.begin_lifecycle()
    assert lease is not None
    assert startup_warmup.run_startup(lease) is None

    assert calls == ["repository", "close", "clear"]
    assert readiness.snapshot()["phase"] == "error"
    assert "warm detail" not in caplog.text
    assert "close detail" not in caplog.text

    # Lifespan shutdown after the failed startup must be idempotent and must
    # not construct a brand-new cached repository merely to close it.
    startup_warmup.close_repository(lease, None)
    assert readiness.snapshot()["phase"] == "stopped"
    startup_warmup.close_repository(lease, None)
    assert calls == ["repository", "close", "clear"]


def test_concurrent_cold_start_has_one_owner_and_loser_cannot_close_winner(
    monkeypatch,
):
    from types import SimpleNamespace

    from app.api import deps
    from app.services import startup_warmup

    factory_entered = threading.Event()
    release_factory = threading.Event()
    calls: list[str] = []
    results: list[object] = []

    fake = SimpleNamespace(
        warm_open_path_caches=lambda **_kwargs: calls.append("warm") or 0,
        close=lambda: calls.append("close"),
    )

    def repository():
        calls.append("factory")
        factory_entered.set()
        assert release_factory.wait(timeout=2)
        return fake

    repository.cache_clear = lambda: calls.append("clear")
    monkeypatch.setattr(deps, "repository", repository)
    monkeypatch.setattr(startup_warmup, "_reproject_legacy_knowhow_tables", lambda _repo: None)

    lease = startup_warmup.begin_lifecycle()
    assert lease is not None

    winner = threading.Thread(
        target=lambda: results.append(startup_warmup.run_startup(lease))
    )
    winner.start()
    assert factory_entered.wait(timeout=2)
    before_loser = readiness.snapshot()

    loser_results: list[tuple[object | None, object | None]] = []

    def lose_startup_race() -> None:
        loser_lease = startup_warmup.begin_lifecycle()
        loser_results.append((loser_lease, startup_warmup.run_startup(loser_lease)))

    loser = threading.Thread(target=lose_startup_race)
    loser.start()
    loser.join(timeout=2)
    assert not loser.is_alive()
    assert loser_results == [(None, None)]
    assert calls == ["factory"]
    assert readiness.snapshot() == before_loser

    # A lifespan that failed to acquire a lease has no wildcard shutdown power.
    startup_warmup.close_repository(None, None)
    assert readiness.snapshot() == before_loser
    assert calls == ["factory"]

    release_factory.set()
    winner.join(timeout=2)
    assert not winner.is_alive()
    assert results == [fake]
    assert calls == ["factory", "warm"]
    assert readiness.is_ready() is True
    assert client.get("/api/ready").json()["ready"] is True

    # Loser exit remains a strict no-op after the winner reaches ready.
    startup_warmup.close_repository(None, None)
    assert readiness.is_ready() is True
    assert calls == ["factory", "warm"]

    startup_warmup.close_repository(lease, fake)
    assert calls == ["factory", "warm", "close", "clear"]
    assert readiness.snapshot()["phase"] == "stopped"


def test_overlap_after_warming_started_does_not_construct_or_mutate_readiness(
    monkeypatch,
):
    from types import SimpleNamespace

    from app.api import deps
    from app.services import startup_warmup

    warm_entered = threading.Event()
    release_warm = threading.Event()
    calls: list[str] = []
    result: list[object] = []

    def warm(**_kwargs):
        calls.append("warm")
        warm_entered.set()
        assert release_warm.wait(timeout=2)
        return 0

    fake = SimpleNamespace(warm_open_path_caches=warm, close=lambda: calls.append("close"))

    def repository():
        calls.append("factory")
        return fake

    repository.cache_clear = lambda: calls.append("clear")
    monkeypatch.setattr(deps, "repository", repository)
    monkeypatch.setattr(startup_warmup, "_reproject_legacy_knowhow_tables", lambda _repo: None)

    lease = startup_warmup.begin_lifecycle()
    assert lease is not None
    winner = threading.Thread(
        target=lambda: result.append(startup_warmup.run_startup(lease))
    )
    winner.start()
    assert warm_entered.wait(timeout=2)
    warming = readiness.snapshot()
    assert warming["phase"] == "warming"

    loser_lease = startup_warmup.begin_lifecycle()
    assert loser_lease is None
    assert startup_warmup.run_startup(loser_lease) is None
    startup_warmup.close_repository(None, None)
    assert readiness.snapshot() == warming
    assert calls == ["factory", "warm"]

    release_warm.set()
    winner.join(timeout=2)
    assert not winner.is_alive()
    assert result == [fake]
    assert readiness.is_ready() is True
    startup_warmup.close_repository(lease, fake)
    assert calls == ["factory", "warm", "close", "clear"]


def test_stale_lease_wrong_repository_and_double_shutdown_are_noops(monkeypatch):
    from types import SimpleNamespace

    from app.api import deps
    from app.services import startup_warmup

    calls: list[str] = []
    fake = SimpleNamespace(
        warm_open_path_caches=lambda **_kwargs: calls.append("warm") or 0,
        close=lambda: calls.append("close"),
    )

    def repository():
        calls.append("factory")
        return fake

    repository.cache_clear = lambda: calls.append("clear")
    monkeypatch.setattr(deps, "repository", repository)
    monkeypatch.setattr(startup_warmup, "_reproject_legacy_knowhow_tables", lambda _repo: None)

    lease = startup_warmup.begin_lifecycle()
    assert lease is not None
    assert startup_warmup.run_startup(lease) is fake
    assert readiness.is_ready() is True

    startup_warmup.close_repository(object(), fake)
    startup_warmup.close_repository(lease, object())
    assert calls == ["factory", "warm"]
    assert readiness.is_ready() is True

    startup_warmup.close_repository(lease, fake)
    assert calls == ["factory", "warm", "close", "clear"]
    assert readiness.snapshot()["phase"] == "stopped"
    startup_warmup.close_repository(lease, fake)
    assert calls == ["factory", "warm", "close", "clear"]
    assert readiness.snapshot()["phase"] == "stopped"


def test_startup_failure_releases_lease_for_fresh_retry(monkeypatch):
    from types import SimpleNamespace

    from app.api import deps
    from app.core import config
    from app.services import startup_warmup

    calls: list[str] = []
    instances = []

    def repository():
        index = len(instances) + 1
        calls.append(f"factory{index}")

        def warm(**_kwargs):
            calls.append(f"warm{index}")
            if index == 1:
                raise RuntimeError("first warm fails")
            return 0

        fake = SimpleNamespace(
            warm_open_path_caches=warm,
            close=lambda: calls.append(f"close{index}"),
        )
        instances.append(fake)
        return fake

    repository.cache_clear = lambda: calls.append("clear")
    monkeypatch.setattr(deps, "repository", repository)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(database_url="sqlite:///safe.db"))
    monkeypatch.setattr(startup_warmup, "_reproject_legacy_knowhow_tables", lambda _repo: None)

    failed_lease = startup_warmup.begin_lifecycle()
    assert failed_lease is not None
    assert startup_warmup.run_startup(failed_lease) is None
    assert readiness.snapshot()["phase"] == "error"
    assert startup_warmup.begin_lifecycle() is None
    assert startup_warmup.run_startup(failed_lease) is None
    startup_warmup.close_repository(failed_lease, None)
    assert readiness.snapshot()["phase"] == "stopped"
    assert calls == ["factory1", "warm1", "close1", "clear"]

    retry_lease = startup_warmup.begin_lifecycle()
    assert retry_lease is not None
    assert retry_lease is not failed_lease
    assert readiness.snapshot()["phase"] == "starting"
    assert startup_warmup.run_startup(retry_lease) is instances[1]
    assert readiness.is_ready() is True
    startup_warmup.close_repository(retry_lease, instances[1])
    assert calls == [
        "factory1",
        "warm1",
        "close1",
        "clear",
        "factory2",
        "warm2",
        "close2",
        "clear",
    ]
