"""Companion-service example checks: no host sockets or ambient services."""

import importlib
import io
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def example(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "examples/extensions/managed-service/src"))
    return importlib.import_module("silicon_notebook_managed_service.server")


@pytest.mark.parametrize("path,status,payload", [
    ("/health", 200, b'{"ready":true}\n'),
    ("/missing", 404, b'{"error":"not_found"}\n'),
])
def test_health_response_without_binding(example, path, status, payload):
    handler = object.__new__(example.HealthHandler)
    handler.path = path
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.do_GET()
    handler.send_response.assert_called_once_with(status)
    assert handler.wfile.getvalue() == payload


def test_help_does_not_create_server(example, monkeypatch, capsys):
    server = Mock(side_effect=AssertionError("help must not bind a socket"))
    monkeypatch.setattr(example, "HealthServer", server)
    with pytest.raises(SystemExit) as exc:
        example.main(["--help"])
    assert exc.value.code == 0
    assert "--port" in capsys.readouterr().out
    server.assert_not_called()


def test_sigterm_closes_foreground_service(example, monkeypatch):
    callbacks = {}
    old_handler = object()

    def install(sig, handler):
        callbacks[sig] = handler
        return old_handler

    server = Mock()
    server.__enter__ = Mock(return_value=server)
    server.__exit__ = Mock(return_value=False)
    server.handle_request.side_effect = lambda: callbacks[example.signal.SIGTERM](15, None)
    monkeypatch.setattr(example, "HealthServer", Mock(return_value=server))
    monkeypatch.setattr(example.signal, "signal", install)
    assert example.main([]) == 0
    server.__exit__.assert_called_once()
    assert callbacks[example.signal.SIGTERM] is old_handler
    assert callbacks[example.signal.SIGINT] is old_handler


def test_example_config_and_bundle_load_without_service_start(example, monkeypatch):
    from app.extensions.discovery import discover_deployment_extensions
    from app.extensions.service_config import parse_service_config

    root = Path(__file__).resolve().parents[2]
    config = root / "examples/extensions/managed-service/extensions.example.toml"
    monkeypatch.setattr(example, "HealthServer", Mock(side_effect=AssertionError("discovery starts nothing")))
    services = parse_service_config(str(config))
    bundles = discover_deployment_extensions(str(config))
    assert len(services) == len(bundles) == 1
    assert services[0].key == "examples.managed_service/health"
    assert services[0].cwd == config.parent
    assert bundles[0].bundle.manifest.id == services[0].plugin_id
