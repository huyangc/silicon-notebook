"""MCP_REQUIRE_HTTPS opt-in policy: default off (intranet-friendly)."""
from __future__ import annotations

import logging

import pytest

from app.api import mcp_server
from app.api.mcp_server import validate_mcp_deployment


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_validate_default_requires_https():
    with pytest.raises(RuntimeError, match="requires HTTPS"):
        validate_mcp_deployment("0.0.0.0", "http://10.0.0.5:8000/mcp")


def test_validate_still_raises_when_required_explicitly():
    with pytest.raises(RuntimeError, match="requires HTTPS"):
        validate_mcp_deployment(
            "0.0.0.0", "http://10.0.0.5:8000/mcp", require_https=True
        )


def test_validate_allows_plain_http_when_not_required(caplog):
    with caplog.at_level(logging.WARNING, logger="app.api.mcp_server"):
        validate_mcp_deployment(
            "0.0.0.0", "http://10.0.0.5:8000/mcp", require_https=False
        )
    assert any("cleartext" in r.getMessage() for r in caplog.records)


def test_validate_warns_host_origin_when_https_but_not_required(caplog):
    with caplog.at_level(logging.WARNING, logger="app.api.mcp_server"):
        validate_mcp_deployment(
            "0.0.0.0", "https://memory.example.test/mcp", require_https=False
        )
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "Host/Origin" in msgs          # names the relaxed control
    assert "cleartext" not in msgs        # https → no cleartext-token claim


def test_validate_loopback_never_warns_or_raises(caplog):
    with caplog.at_level(logging.WARNING, logger="app.api.mcp_server"):
        # loopback bind + loopback public url: not remotely reachable
        validate_mcp_deployment(
            "127.0.0.1", "http://127.0.0.1:8000/mcp", require_https=False
        )
    assert not caplog.records


class _StubRepo:
    def resolve_agent_token(self, raw):  # no valid token → 401 path
        return None


async def _drive_middleware(require_https, scheme, client_host):
    """Return the HTTP status the middleware emits for a bare POST."""
    sent = {}

    async def inner(scope, receive, send):  # pragma: no cover - not reached on 403
        await send({"type": "http.response.start", "status": 599, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = mcp_server.AgentBearerMiddleware(
        inner, lambda: _StubRepo(), require_https=require_https
    )
    scope = {
        "type": "http",
        "scheme": scheme,
        "client": (client_host, 5555),
        "headers": [],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent["status"] = msg["status"]

    await mw(scope, receive, send)
    return sent["status"]


@pytest.mark.anyio
async def test_request_guard_blocks_remote_http_when_required():
    status = await _drive_middleware(True, "http", "198.51.100.9")
    assert status == 403


@pytest.mark.anyio
async def test_request_guard_allows_remote_http_when_not_required():
    # scheme check skipped → reaches token resolution → 401 (bad token), not 403
    status = await _drive_middleware(False, "http", "198.51.100.9")
    assert status == 401


@pytest.mark.anyio
async def test_request_guard_exempts_loopback_http_even_when_required():
    # require_https=True but loopback client over http → NOT 403; falls through
    # to token resolution → 401 (bad token).
    status = await _drive_middleware(True, "http", "127.0.0.1")
    assert status == 401


def test_create_memory_mcp_toggles_dns_rebinding(monkeypatch):
    captured = []
    real = mcp_server.TransportSecuritySettings

    def spy(**kwargs):
        captured.append(kwargs.get("enable_dns_rebinding_protection"))
        return real(**kwargs)

    monkeypatch.setattr(mcp_server, "TransportSecuritySettings", spy)
    mcp_server.create_memory_mcp(lambda: _StubRepo(), require_https=False)
    mcp_server.create_memory_mcp(lambda: _StubRepo(), require_https=True)
    assert captured == [False, True]


from app.api.deps import repository as _repository_module
from app.core.config import get_settings


def _min_app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("BACKEND_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://10.0.0.5:8000/mcp")
    get_settings.cache_clear()
    _repository_module.cache_clear()


def test_create_app_defaults_to_open(monkeypatch, tmp_path):
    _min_app_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MCP_REQUIRE_HTTPS", raising=False)
    from app.main import create_app

    app = create_app()  # must NOT raise despite 0.0.0.0 + http
    assert app is not None


def test_create_app_require_https_restores_failclosed(monkeypatch, tmp_path):
    _min_app_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MCP_REQUIRE_HTTPS", "1")

    # NOTE: `app/main.py` has a module-level `app = create_app()` (the
    # `uvicorn app.main:app` entrypoint idiom) that fires once per process on
    # the *first* import of `app.main` — using whatever env is ambient right
    # then. Under this project's parallelized (xdist) test suite that first
    # import can land in either this test or a sibling test's *own* worker
    # process, so whether the RuntimeError is raised by that module-level
    # statement or by the explicit `create_app()` call below is
    # order-dependent. Importing inside the `pytest.raises` block catches it
    # either way instead of depending on import-cache luck (see the same
    # landmine documented in test_notebook_share_copy.py::test_copy_refuses_too_large).
    with pytest.raises(RuntimeError, match="requires HTTPS"):
        from app.main import create_app

        create_app()
