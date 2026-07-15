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
