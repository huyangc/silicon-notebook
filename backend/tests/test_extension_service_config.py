"""Companion-service configuration stays deterministic, inert, and fail closed."""
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import urllib.request

import pytest

from app.extensions.discovery import ExtensionDiscoveryError, discover_deployment_extensions
from app.extension_sdk import EXTENSION_API_VERSION, ExtensionManifest
from app.extensions.service_config import ServiceConfigError, parse_service_config


def _config(tmp_path: Path, body: str) -> str:
    path = tmp_path / "extensions.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _managed(body: str = "") -> str:
    return (
        '[extensions."corp.test"]\nbundle = "not_importable:BUNDLE"\n'
        '[extensions."corp.test".services.worker]\n'
        'command = ["./.venv/bin/python", "-m", "worker"]\ncwd = "plugins"\n'
        'healthcheck_url = "http://127.0.0.1:9100/health"\n' + body
    )


def test_managed_defaults_paths_and_environment_are_preserved(tmp_path):
    specs = parse_service_config(_config(tmp_path, _managed(
        'env = { MODE = "local" }\nenv_from = { TOKEN = "WORKER_TOKEN" }\n'
    )))
    assert len(specs) == 1
    service = specs[0]
    assert service.key == "corp.test/worker"
    assert service.mode == "managed"
    assert service.command == ("./.venv/bin/python", "-m", "worker")
    assert service.cwd == tmp_path / "plugins"
    assert service.env == {"MODE": "local"}
    assert service.env_from == {"TOKEN": "WORKER_TOKEN"}
    assert service.startup_timeout_seconds == 60
    assert service.shutdown_timeout_seconds == 15
    assert service.healthcheck_timeout_seconds == 2
    # The path intentionally does not exist: install validation belongs to start.
    assert not service.cwd.exists()


def test_external_health_command_and_disabled_plugin_are_inert(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("configuration parsing must not perform lifecycle I/O")

    monkeypatch.setattr(subprocess, "Popen", unexpected)
    monkeypatch.setattr(urllib.request, "urlopen", unexpected)
    specs = parse_service_config(_config(tmp_path, '''
[extensions."corp.disabled"]
enabled = false
services = 9
[extensions."corp.external"]
bundle = "not_importable:BUNDLE"
[extensions."corp.external".services.worker]
mode = "external"
healthcheck_command = ["python3", "check.py"]
'''))
    assert len(specs) == 1
    assert specs[0].cwd == tmp_path
    assert specs[0].command == ()
    assert specs[0].healthcheck_command == ("python3", "check.py")


@pytest.mark.parametrize(("old", "new", "reason"), [
    ('cwd = "plugins"', 'cwd = ""', "service_cwd_invalid"),
    ('cwd = "plugins"', '', "service_cwd_invalid"),
    ('command = ["./.venv/bin/python", "-m", "worker"]', 'command = "serve.sh"', "service_command_invalid"),
    ('command = ["./.venv/bin/python", "-m", "worker"]', 'command = []', "service_command_invalid"),
    ('command = ["./.venv/bin/python", "-m", "worker"]', 'command = ["python", 1]', "service_command_invalid"),
    ('healthcheck_url = "http://127.0.0.1:9100/health"', '', "service_healthcheck_required"),
    ('healthcheck_url = "http://127.0.0.1:9100/health"', 'healthcheck_command = []', "service_command_invalid"),
    ('[extensions."corp.test".services.worker]', '[extensions."corp.test".services."bad/service"]', "service_id_invalid"),
    ('bundle = "not_importable:BUNDLE"', 'bundle = "malformed"', "plugin_bundle_spec_invalid"),
])
def test_rejects_invalid_shapes(tmp_path, old, new, reason):
    with pytest.raises(ServiceConfigError) as error:
        parse_service_config(_config(tmp_path, _managed().replace(old, new)))
    assert error.value.reason == reason


@pytest.mark.parametrize("url", [
    "ftp://localhost/health", "http:///health", "http://user:SECRET@localhost/health",
    "http://localhost/health?token=SECRET", "http://localhost/health#SECRET",
    "http://localhost/health?", "http://localhost:invalid/health", "http://[bad/health",
    "http://localhost/health with spaces",
])
def test_urls_reject_ambiguous_or_secret_bearing_forms(tmp_path, url):
    path = _config(tmp_path, _managed().replace("http://127.0.0.1:9100/health", url))
    with pytest.raises(ServiceConfigError) as error:
        parse_service_config(path)
    assert error.value.reason == "service_healthcheck_url_invalid"
    assert url not in str(error.value)
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("addition,reason", [
    ('unknown = "SECRET"', "service_unknown_key"),
    ('mode = "unknown"', "service_mode_invalid"),
    ('mode = "external"', "external_service_launch_fields"),
    ('healthcheck_command = ["true"]', "service_healthcheck_required"),
    ('env = { "BAD-NAME" = "SECRET" }', "service_env_invalid"),
    ('env = { TOKEN = 1 }', "service_env_invalid"),
    ('env_from = { TOKEN = "SECRET VALUE" }', "service_env_reference_invalid"),
    ('env = { TOKEN = "SECRET" }\nenv_from = { TOKEN = "WORKER_TOKEN" }', "service_env_duplicate"),
    ('depends_on = ["unknown"]', "service_dependencies_invalid"),
    ('depends_on = ["corp.other/api"]', "service_dependency_missing"),
    ('depends_on = ["corp.test/worker"]', "service_dependency_cycle"),
    ('depends_on = ["corp.test/worker", "corp.test/worker"]', "service_dependencies_duplicate"),
])
def test_validation_failure_is_content_free(tmp_path, addition, reason):
    with pytest.raises(ServiceConfigError) as error:
        parse_service_config(_config(tmp_path, _managed(addition)))
    assert error.value.reason == reason
    assert error.value.plugin_id == "corp.test"
    assert error.value.service_id == "worker"
    assert "SECRET" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize("field,maximum", [
    ("startup_timeout_seconds", 3600), ("shutdown_timeout_seconds", 300),
    ("healthcheck_timeout_seconds", 30),
])
@pytest.mark.parametrize("value", ["true", "nan", "inf", "-inf", '"2"', "0", "0.09", "10000", "9" * 400])
def test_timeout_rejects_nonfinite_bool_and_out_of_range(tmp_path, field, maximum, value):
    with pytest.raises(ServiceConfigError, match="service_timeout_invalid"):
        parse_service_config(_config(tmp_path, _managed(f"{field} = {value}")))


@pytest.mark.parametrize("field,maximum", [
    ("startup_timeout_seconds", 3600), ("shutdown_timeout_seconds", 300),
    ("healthcheck_timeout_seconds", 30),
])
def test_timeout_bounds_inclusive(tmp_path, field, maximum):
    for value in (0.1, maximum):
        service, = parse_service_config(_config(tmp_path, _managed(f"{field} = {value}")))
        assert getattr(service, field) == value


def test_dependency_order_and_disabled_dependency(tmp_path):
    body = _managed('depends_on = ["corp.z/database"]\n') + '''
[extensions."corp.z"]
bundle = "also_not_importable:BUNDLE"
[extensions."corp.z".services.database]
mode = "external"
healthcheck_url = "https://database.example/ready"
'''
    assert [s.key for s in parse_service_config(_config(tmp_path, body))] == [
        "corp.z/database", "corp.test/worker",
    ]
    with pytest.raises(ServiceConfigError, match="service_dependency_missing"):
        parse_service_config(_config(tmp_path, body.replace(
            '[extensions."corp.z"]', '[extensions."corp.z"]\nenabled = false'
        )))


def test_discovery_validates_services_before_import_and_disabled_skips(tmp_path):
    path = _config(tmp_path, _managed('startup_timeout_seconds = -1'))
    with pytest.raises(ExtensionDiscoveryError) as error:
        discover_deployment_extensions(path)
    assert error.value.reason == "service_timeout_invalid"
    path = _config(tmp_path, _managed('startup_timeout_seconds = -1').replace(
        '[extensions."corp.test"]', '[extensions."corp.test"]\nenabled = false'
    ))
    assert discover_deployment_extensions(path) == ()


def test_valid_discovery_does_not_start_or_probe_services(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("discovery must not start or probe companion services")

    module = ModuleType("not_importable")
    module.BUNDLE = SimpleNamespace(
        manifest=ExtensionManifest(id="corp.test", version="1.0.0",
            api_version=EXTENSION_API_VERSION, display_name="Example",
            trust="deployment", contributions=()),
        register=lambda registrar: None,
    )
    monkeypatch.setitem(sys.modules, "not_importable", module)
    monkeypatch.setattr(subprocess, "Popen", unexpected)
    monkeypatch.setattr(urllib.request, "urlopen", unexpected)
    loaded, = discover_deployment_extensions(_config(tmp_path, _managed()))
    assert loaded.bundle is module.BUNDLE


def test_empty_unreadable_invalid_toml_and_outer_unknown_keys(tmp_path):
    assert parse_service_config("  ") == ()
    for body, reason in [
        ('bad = "SECRET"', "config_unknown_top_level_key"),
        ('[extensions."corp.disabled"]\nenabled = false\nunknown = 3', "plugin_unknown_key"),
        ('invalid toml', "config_invalid_toml"),
    ]:
        with pytest.raises(ServiceConfigError) as error:
            parse_service_config(_config(tmp_path, body))
        assert error.value.reason == reason
    with pytest.raises(ServiceConfigError, match="config_unreadable"):
        parse_service_config(str(tmp_path / "missing.toml"))
