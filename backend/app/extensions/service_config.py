"""Pure, dependency-light configuration for deployment companion services.

Parsing never imports bundles, starts processes, or probes endpoints. Launchers
own those effects; ordinary application/CLI discovery only validates the contract.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import heapq
import math
from pathlib import Path
import re
import tomllib
from typing import NoReturn
from urllib.parse import urlsplit


STABLE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EXTENSION_ENTRY_KEYS = frozenset({"bundle", "enabled", "settings", "services"})
SERVICE_KEYS = frozenset({
    "mode", "command", "cwd", "env", "env_from", "healthcheck_url",
    "healthcheck_command", "startup_timeout_seconds", "shutdown_timeout_seconds",
    "healthcheck_timeout_seconds", "depends_on",
})
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 15.0
DEFAULT_HEALTHCHECK_TIMEOUT_SECONDS = 2.0
MIN_SERVICE_TIMEOUT_SECONDS = 0.1
MAX_STARTUP_TIMEOUT_SECONDS = 3600.0
MAX_SHUTDOWN_TIMEOUT_SECONDS = 300.0
MAX_HEALTHCHECK_TIMEOUT_SECONDS = 30.0


class ServiceConfigError(ValueError):
    """Safe diagnostic: only validated identities and a stable reason code."""

    def __init__(self, plugin_id: str, service_id: str, reason: str) -> None:
        self.plugin_id = plugin_id if STABLE_ID.fullmatch(plugin_id) else ""
        self.service_id = service_id if STABLE_ID.fullmatch(service_id) else ""
        self.reason = reason
        super().__init__(
            f"extension service rejected: plugin={self.plugin_id} "
            f"service={self.service_id} reason={reason}"
        )


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    plugin_id: str
    service_id: str
    mode: str
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    env_from: dict[str, str]
    healthcheck_url: str
    healthcheck_command: tuple[str, ...]
    startup_timeout_seconds: float
    shutdown_timeout_seconds: float
    healthcheck_timeout_seconds: float
    depends_on: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.plugin_id}/{self.service_id}"


def parse_service_config(config_path: str) -> tuple[ServiceSpec, ...]:
    """Read one extension TOML and return services in deterministic DAG order."""
    path = config_path.strip()
    if not path:
        return ()
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except OSError:
        raise ServiceConfigError("", "", "config_unreadable") from None
    except tomllib.TOMLDecodeError:
        raise ServiceConfigError("", "", "config_invalid_toml") from None
    return parse_service_document(document, path)


def parse_service_document(
    document: dict[str, object], config_path: str,
) -> tuple[ServiceSpec, ...]:
    """Validate the outer document and services without any bundle imports."""
    if set(document) - {"extensions"}:
        raise ServiceConfigError("", "", "config_unknown_top_level_key")
    entries = document.get("extensions", {})
    if not isinstance(entries, dict):
        raise ServiceConfigError("", "", "config_extensions_not_a_table")
    config_dir = Path(config_path).absolute().parent
    services: list[ServiceSpec] = []
    for plugin_id, entry in sorted(entries.items()):
        if not STABLE_ID.fullmatch(plugin_id):
            raise ServiceConfigError("", "", "plugin_id_invalid")
        if not isinstance(entry, dict):
            raise ServiceConfigError(plugin_id, "", "plugin_entry_not_a_table")
        if set(entry) - EXTENSION_ENTRY_KEYS:
            raise ServiceConfigError(plugin_id, "", "plugin_unknown_key")
        enabled = entry.get("enabled", True)
        if type(enabled) is not bool:
            raise ServiceConfigError(plugin_id, "", "plugin_enabled_not_bool")
        if not enabled:
            continue
        _validate_bundle_shape(plugin_id, entry)
        declarations = entry.get("services", {})
        if not isinstance(declarations, dict):
            raise ServiceConfigError(plugin_id, "", "services_not_a_table")
        for service_id, declaration in sorted(declarations.items()):
            if not STABLE_ID.fullmatch(service_id):
                raise ServiceConfigError(plugin_id, "", "service_id_invalid")
            services.append(_parse_service(plugin_id, service_id, declaration, config_dir))
    return _topological_order(services)


def _validate_bundle_shape(plugin_id: str, entry: dict[str, object]) -> None:
    bundle = entry.get("bundle")
    if bundle is None:
        raise ServiceConfigError(plugin_id, "", "plugin_bundle_missing")
    if type(bundle) is not str or bundle.count(":") != 1:
        raise ServiceConfigError(plugin_id, "", "plugin_bundle_spec_invalid")
    module, _, attribute = bundle.partition(":")
    if not module or not attribute.isidentifier():
        raise ServiceConfigError(plugin_id, "", "plugin_bundle_spec_invalid")
    if not isinstance(entry.get("settings", {}), dict):
        raise ServiceConfigError(plugin_id, "", "plugin_settings_not_a_table")


def _parse_service(
    plugin_id: str, service_id: str, declaration: object, config_dir: Path,
) -> ServiceSpec:
    def fail(reason: str) -> NoReturn:
        raise ServiceConfigError(plugin_id, service_id, reason)

    if not isinstance(declaration, dict):
        fail("service_not_a_table")
    if set(declaration) - SERVICE_KEYS:
        fail("service_unknown_key")
    mode = declaration.get("mode", "managed")
    if mode not in ("managed", "external"):
        fail("service_mode_invalid")
    if mode == "external" and set(declaration) & {"command", "cwd", "env", "env_from"}:
        fail("external_service_launch_fields")
    cwd = config_dir
    command: tuple[str, ...] = ()
    if mode == "managed":
        command = _command(declaration.get("command"), fail)
        raw_cwd = declaration.get("cwd")
        if type(raw_cwd) is not str or not raw_cwd.strip() or "\0" in raw_cwd:
            fail("service_cwd_invalid")
        cwd = config_dir / raw_cwd
    env = _environment(declaration.get("env", {}), fail, reference=False)
    env_from = _environment(declaration.get("env_from", {}), fail, reference=True)
    if set(env) & set(env_from):
        fail("service_env_duplicate")
    has_url = "healthcheck_url" in declaration
    has_command = "healthcheck_command" in declaration
    if has_url == has_command:
        fail("service_healthcheck_required")
    healthcheck_url = ""
    healthcheck_command: tuple[str, ...] = ()
    if has_url:
        healthcheck_url = declaration["healthcheck_url"]
        _validate_healthcheck_url(healthcheck_url, fail)
    else:
        healthcheck_command = _command(declaration["healthcheck_command"], fail)
    depends_on = declaration.get("depends_on", [])
    if not isinstance(depends_on, list) or any(
        type(key) is not str or len(key.split("/")) != 2
        or not all(STABLE_ID.fullmatch(part) for part in key.split("/"))
        for key in depends_on
    ):
        fail("service_dependencies_invalid")
    if len(depends_on) != len(set(depends_on)):
        fail("service_dependencies_duplicate")
    return ServiceSpec(
        plugin_id=plugin_id, service_id=service_id, mode=mode, command=command,
        cwd=cwd, env=env, env_from=env_from, healthcheck_url=healthcheck_url,
        healthcheck_command=healthcheck_command,
        startup_timeout_seconds=_timeout(declaration, "startup_timeout_seconds",
            DEFAULT_STARTUP_TIMEOUT_SECONDS, MAX_STARTUP_TIMEOUT_SECONDS, fail),
        shutdown_timeout_seconds=_timeout(declaration, "shutdown_timeout_seconds",
            DEFAULT_SHUTDOWN_TIMEOUT_SECONDS, MAX_SHUTDOWN_TIMEOUT_SECONDS, fail),
        healthcheck_timeout_seconds=_timeout(declaration, "healthcheck_timeout_seconds",
            DEFAULT_HEALTHCHECK_TIMEOUT_SECONDS, MAX_HEALTHCHECK_TIMEOUT_SECONDS, fail),
        depends_on=tuple(depends_on),
    )


def _command(value: object, fail: Callable[[str], NoReturn]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(
        type(item) is not str or not item.strip() or "\0" in item for item in value
    ):
        fail("service_command_invalid")
    return tuple(value)


def _environment(
    value: object, fail: Callable[[str], NoReturn], *, reference: bool,
) -> dict[str, str]:
    if not isinstance(value, dict):
        fail("service_env_invalid")
    for name, content in value.items():
        if not ENV_NAME.fullmatch(name) or type(content) is not str or "\0" in content:
            fail("service_env_invalid")
        if reference and not ENV_NAME.fullmatch(content):
            fail("service_env_reference_invalid")
    return dict(value)


def _validate_healthcheck_url(value: object, fail: Callable[[str], NoReturn]) -> None:
    if type(value) is not str or any(char.isspace() or ord(char) < 32 for char in value):
        fail("service_healthcheck_url_invalid")
    try:
        url = urlsplit(value)
        valid = (
            url.scheme in {"http", "https"} and url.hostname
            and url.username is None and url.password is None
            and not url.query and not url.fragment
            and "?" not in value and "#" not in value
        )
        _ = url.port  # Validates malformed/out-of-range ports as well.
    except ValueError:
        valid = False
    if not valid:
        fail("service_healthcheck_url_invalid")


def _timeout(
    declaration: dict[str, object], key: str, default: float, maximum: float,
    fail: Callable[[str], NoReturn],
) -> float:
    value = declaration.get(key, default)
    if (type(value) not in (int, float)
        or not MIN_SERVICE_TIMEOUT_SECONDS <= value <= maximum
        or not math.isfinite(value)):
        fail("service_timeout_invalid")
    return float(value)


def _topological_order(services: list[ServiceSpec]) -> tuple[ServiceSpec, ...]:
    by_key = {service.key: service for service in services}
    incoming = {service.key: len(service.depends_on) for service in services}
    outgoing: dict[str, list[str]] = {key: [] for key in by_key}
    for service in services:
        for dependency in service.depends_on:
            if dependency not in by_key:
                raise ServiceConfigError(service.plugin_id, service.service_id,
                    "service_dependency_missing")
            outgoing[dependency].append(service.key)
    ready = [key for key, count in incoming.items() if count == 0]
    heapq.heapify(ready)
    ordered = []
    while ready:
        key = heapq.heappop(ready)
        ordered.append(by_key[key])
        for dependent in outgoing[key]:
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(ordered) != len(services):
        service = by_key[min(key for key, count in incoming.items() if count)]
        raise ServiceConfigError(service.plugin_id, service.service_id, "service_dependency_cycle")
    return tuple(ordered)
