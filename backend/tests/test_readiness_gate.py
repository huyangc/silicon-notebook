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
