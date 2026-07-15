"""MCP_REQUIRE_HTTPS opt-in policy: default off (intranet-friendly)."""
from __future__ import annotations

import logging

import pytest

from app.api import mcp_server
from app.api.mcp_server import validate_mcp_deployment


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
