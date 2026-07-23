"""Readiness gate + /api/ready probe.

Until startup warm-up flips ready, every app route (incl. login/health) returns
503 with the phase, while the anonymous ``/api/ready`` probe and root ``/`` stay
reachable so the frontend can poll and show a "服务启动中" screen. The autouse
``_mark_service_ready`` conftest fixture marks ready before each test; here we
toggle it explicitly to exercise both states.
"""
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

    readiness.reset()
    startup_warmup.run_startup()  # synchronous here (prod runs it in a thread)
    snap = readiness.snapshot()
    assert snap["phase"] == "ready"
    assert readiness.is_ready() is True
    assert snap["error"] is None


def test_startup_failure_stays_not_ready_and_redacts_connection(monkeypatch, caplog):
    from types import SimpleNamespace

    from app.api import deps
    from app.core import config
    from app.services import startup_warmup

    secret = "postgresql://secret-user:secret-password@db.example:5432/notebook"

    def fail_repository():
        raise RuntimeError(f"driver leaked {secret}")

    monkeypatch.setattr(deps, "repository", fail_repository)
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(database_url=secret),
    )
    readiness.reset()
    startup_warmup.run_startup()

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


def test_close_repository_closes_and_clears_the_cached_composition(monkeypatch):
    from types import SimpleNamespace

    from app.api import deps
    from app.services import startup_warmup

    calls = []
    fake = SimpleNamespace(close=lambda: calls.append("close"))

    def repository():
        return fake

    repository.cache_clear = lambda: calls.append("clear")
    monkeypatch.setattr(deps, "repository", repository)

    startup_warmup.close_repository()

    assert calls == ["close", "clear"]


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
    readiness.reset()
    startup_warmup.run_startup()

    assert calls == ["repository", "close", "clear"]
    assert readiness.snapshot()["phase"] == "error"
    assert "warm detail" not in caplog.text
    assert "close detail" not in caplog.text

    # Lifespan shutdown after the failed startup must be idempotent and must
    # not construct a brand-new cached repository merely to close it.
    startup_warmup.close_repository()
    assert calls == ["repository", "close", "clear", "clear"]
    startup_warmup.close_repository()
    assert calls == ["repository", "close", "clear", "clear", "clear"]
