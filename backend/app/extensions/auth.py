"""Fail-closed, hard-deadline host for the single ``auth.provider``."""
from __future__ import annotations

from dataclasses import dataclass
import inspect
import math
import re
import threading
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from app.domain.auth_provider import (
    AUTH_PROVIDER_AUTHORIZATION_URL_MAX_CHARS,
    AUTH_PROVIDER_CONFIGURATION_GENERATION_MAX_CHARS,
    AUTH_PROVIDER_DISPLAY_NAME_MAX_CHARS,
    AUTH_PROVIDER_ENDPOINT_MAX_CHARS,
    AUTH_PROVIDER_ID_MAX_CHARS,
    AUTH_PROVIDER_NAMESPACE_MAX_CHARS,
    AUTH_PROVIDER_PUBLIC_LABEL_MAX_CHARS,
    AUTH_PROVIDER_SUBJECT_MAX_CHARS,
    AUTH_PROVIDER_USERNAME_MAX_CHARS,
    AuthProviderDescriptor,
    AuthProviderError,
    ExternalIdentity,
)
from app.extension_sdk.auth import (
    AUTH_PROVIDER_POINT,
    AuthProviderCodeContext,
    AuthProviderDescription,
    AuthProviderFailure,
    AuthProviderFailureKind,
)
from app.extension_sdk.contracts import AvailabilityStatus, ContributionKind
from app.extensions.discovery import ExtensionDiscoveryError
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError


_STABLE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_STABLE_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_PKCE_VALUE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_RESERVED_PARAMETERS = frozenset({
    "state", "redirect_uri", "code_challenge", "code_challenge_method",
})
_MAX_AUTHORIZATION_PARAMETERS = 32
_MAX_PARAMETER_NAME_CHARS = 128
_MAX_PARAMETER_VALUE_CHARS = 2048
_MAX_STATE_CHARS = 2048
_MAX_CODE_CHARS = 4096
_JOIN_SLICE_SECONDS = 0.05
_AUTH_WORKER_SLOTS = threading.BoundedSemaphore(8)


@dataclass(frozen=True, slots=True)
class _FrozenProvider:
    contribution_id: str
    descriptor: AuthProviderDescriptor
    authorization_parameters: tuple[tuple[str, str], ...]
    authenticate_code: object


@dataclass(slots=True)
class _WorkerCell:
    lock: threading.Lock
    done: threading.Event
    abandoned: bool = False
    result: object = None
    failure_code: str = "auth_provider_failed"


class AuthProviderHost:
    """Expose a provider-neutral port without leaking registry or SDK types."""

    def __init__(self, registry: ExtensionRegistry) -> None:
        if not registry.frozen:
            raise ExtensionRegistryError("Auth provider host requires a frozen registry")
        registered = registry.contributions(AUTH_PROVIDER_POINT)
        if len(registered) > 1:
            raise ExtensionRegistryError("auth provider point has multiple providers")
        self._registry = registry
        self._provider: _FrozenProvider | None = None
        if not registered:
            return
        item = registered[0]
        declaration = item.contribution.declaration
        if declaration.kind is not ContributionKind.PROVIDER:
            raise ExtensionRegistryError("auth provider must be a provider")
        implementation = item.contribution.implementation
        manifest = next(
            candidate for candidate in registry.manifests()
            if candidate.id == item.plugin_id
        )
        try:
            describe = getattr(implementation, "describe", None)
            parameters = getattr(implementation, "authorization_parameters", None)
            authenticate = getattr(implementation, "authenticate_code", None)
            if (
                not callable(describe)
                or not callable(parameters)
                or not callable(authenticate)
                or inspect.iscoroutinefunction(describe)
                or inspect.iscoroutinefunction(parameters)
                or inspect.iscoroutinefunction(authenticate)
            ):
                raise TypeError
            provider_description = describe()
            provider_parameters = parameters()
            descriptor = _validated_descriptor(
                item.plugin_id, declaration.id, provider_description
            )
            frozen_parameters = _validated_parameters(provider_parameters)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if manifest.trust == "deployment":
                raise ExtensionDiscoveryError(
                    item.plugin_id,
                    "invalid_auth_provider",
                    exception_type=type(exc).__name__,
                ) from None
            raise ExtensionRegistryError("invalid auth provider") from exc
        self._provider = _FrozenProvider(
            declaration.id, descriptor, frozen_parameters, authenticate
        )

    def describe(self) -> AuthProviderDescriptor | None:
        provider = self._provider
        return None if provider is None else provider.descriptor

    def authorization_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None,
    ) -> str:
        provider = self._require_available()
        if not _valid_text(state, _MAX_STATE_CHARS):
            raise AuthProviderError("invalid_auth_authorization_request")
        _validate_redirect_uri(redirect_uri)
        if provider.descriptor.supports_pkce:
            if type(code_challenge) is not str or not _PKCE_VALUE.fullmatch(code_challenge):
                raise AuthProviderError("invalid_auth_pkce_challenge")
        elif code_challenge is not None:
            raise AuthProviderError("auth_provider_pkce_unsupported")
        query = [*provider.authorization_parameters]
        query.extend((
            ("state", state),
            ("redirect_uri", redirect_uri),
        ))
        if code_challenge is not None:
            query.extend((
                ("code_challenge", code_challenge),
                ("code_challenge_method", "S256"),
            ))
        parts = urlsplit(provider.descriptor.authorization_endpoint)
        result = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        if len(result) > AUTH_PROVIDER_AUTHORIZATION_URL_MAX_CHARS:
            raise AuthProviderError("auth_authorization_url_too_long")
        return result

    def authenticate(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
        timeout_seconds: float,
    ) -> ExternalIdentity:
        provider = self._provider
        if provider is None:
            raise AuthProviderError("auth_provider_unavailable")
        if not _valid_text(code, _MAX_CODE_CHARS):
            raise AuthProviderError("invalid_auth_code")
        _validate_redirect_uri(redirect_uri)
        if provider.descriptor.supports_pkce:
            if type(code_verifier) is not str or not _PKCE_VALUE.fullmatch(code_verifier):
                raise AuthProviderError("invalid_auth_pkce_verifier")
        elif code_verifier is not None:
            raise AuthProviderError("auth_provider_pkce_unsupported")
        if (
            type(timeout_seconds) not in {int, float}
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise AuthProviderError("invalid_auth_provider_timeout")
        if not _AUTH_WORKER_SLOTS.acquire(blocking=False):
            raise AuthProviderError("auth_provider_busy")

        deadline = time.monotonic() + float(timeout_seconds)
        cell = _WorkerCell(threading.Lock(), threading.Event())
        context = AuthProviderCodeContext(
            code, redirect_uri, code_verifier, deadline
        )

        def _target() -> None:
            result: object = None
            failure_code = "auth_provider_failed"
            try:
                availability = self._registry.availability(
                    provider.contribution_id, None
                )
                if availability.status is not AvailabilityStatus.AVAILABLE:
                    failure_code = "auth_provider_unavailable"
                elif time.monotonic() > deadline:
                    failure_code = "auth_provider_timeout"
                else:
                    try:
                        result = provider.authenticate_code(context)  # type: ignore[operator]
                        failure_code = ""
                    except AuthProviderFailure as exc:
                        failure_code = _failure_code(exc.kind)
                    except BaseException:
                        failure_code = "auth_provider_failed"
            except BaseException:
                failure_code = "auth_provider_unavailable"
            finally:
                with cell.lock:
                    if not cell.abandoned:
                        cell.result = result
                        cell.failure_code = failure_code
                cell.done.set()
                _AUTH_WORKER_SLOTS.release()

        worker = threading.Thread(
            target=_target,
            name="auth-provider",
            daemon=True,
        )
        worker.start()
        while not cell.done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with cell.lock:
                    cell.abandoned = True
                raise AuthProviderError("auth_provider_timeout")
            cell.done.wait(min(_JOIN_SLICE_SECONDS, remaining))
        if time.monotonic() > deadline:
            with cell.lock:
                cell.abandoned = True
            raise AuthProviderError("auth_provider_timeout")
        with cell.lock:
            result = cell.result
            failure_code = cell.failure_code
        if failure_code:
            raise AuthProviderError(failure_code)
        return _validated_identity(result, provider.descriptor.provider_namespace)

    def _require_available(self) -> _FrozenProvider:
        provider = self._provider
        if provider is None:
            raise AuthProviderError("auth_provider_unavailable")
        try:
            availability = self._registry.availability(provider.contribution_id, None)
        except BaseException:
            raise AuthProviderError("auth_provider_unavailable") from None
        if availability.status is not AvailabilityStatus.AVAILABLE:
            raise AuthProviderError("auth_provider_unavailable")
        return provider


def _validated_descriptor(
    plugin_id: str,
    contribution_id: str,
    value: object,
) -> AuthProviderDescriptor:
    if type(value) is not AuthProviderDescription:
        raise TypeError
    if (
        not _valid_stable(value.provider_id, AUTH_PROVIDER_ID_MAX_CHARS)
        or not _valid_namespace(value.provider_namespace)
        or not _valid_stable(
            value.configuration_generation,
            AUTH_PROVIDER_CONFIGURATION_GENERATION_MAX_CHARS,
        )
        or not _valid_text(value.public_label, AUTH_PROVIDER_PUBLIC_LABEL_MAX_CHARS)
        or type(value.supports_pkce) is not bool
    ):
        raise ValueError
    _validate_https_endpoint(value.authorization_endpoint)
    return AuthProviderDescriptor(
        plugin_id=plugin_id,
        contribution_id=contribution_id,
        provider_id=value.provider_id,
        provider_namespace=value.provider_namespace,
        configuration_generation=value.configuration_generation,
        public_label=value.public_label,
        authorization_endpoint=value.authorization_endpoint,
        supports_pkce=value.supports_pkce,
    )


def _validated_parameters(value: object) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple or len(value) > _MAX_AUTHORIZATION_PARAMETERS:
        raise TypeError
    accepted: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError
        name, parameter_value = item
        if (
            type(name) is not str
            or len(name) > _MAX_PARAMETER_NAME_CHARS
            or not _PARAMETER_NAME.fullmatch(name)
            or name.lower() in _RESERVED_PARAMETERS
            or name.lower() in seen
            or not _valid_text(parameter_value, _MAX_PARAMETER_VALUE_CHARS)
        ):
            raise ValueError
        seen.add(name.lower())
        accepted.append((name, parameter_value))
    return tuple(accepted)


def _validated_identity(value: object, namespace: str) -> ExternalIdentity:
    if type(value) is not ExternalIdentity:
        raise AuthProviderError("invalid_auth_provider_identity")
    if (
        value.provider_namespace != namespace
        or not _valid_text(value.subject, AUTH_PROVIDER_SUBJECT_MAX_CHARS)
        or value.subject != value.subject.strip()
        or not _valid_text(value.username, AUTH_PROVIDER_USERNAME_MAX_CHARS)
        or value.username != value.username.strip()
        or not _valid_text(value.display_name, AUTH_PROVIDER_DISPLAY_NAME_MAX_CHARS)
    ):
        raise AuthProviderError("invalid_auth_provider_identity")
    return value


def _validate_https_endpoint(value: object) -> None:
    if (
        type(value) is not str
        or len(value) > AUTH_PROVIDER_ENDPOINT_MAX_CHARS
        or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise ValueError
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError


def _validate_redirect_uri(value: object) -> None:
    if (
        type(value) is not str
        or len(value) > AUTH_PROVIDER_ENDPOINT_MAX_CHARS
        or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise AuthProviderError("invalid_auth_redirect_uri")
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise AuthProviderError("invalid_auth_redirect_uri")


def _valid_stable(value: object, limit: int) -> bool:
    return type(value) is str and len(value) <= limit and bool(_STABLE_ID.fullmatch(value))


def _valid_namespace(value: object) -> bool:
    return (
        type(value) is str
        and len(value) <= AUTH_PROVIDER_NAMESPACE_MAX_CHARS
        and bool(_STABLE_NAMESPACE.fullmatch(value))
    )


def _valid_text(value: object, limit: int) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= limit
        and bool(value.strip())
        and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    )


def _failure_code(kind: object) -> str:
    return {
        AuthProviderFailureKind.CANCELLED: "auth_provider_cancelled",
        AuthProviderFailureKind.INVALID_CODE: "auth_provider_invalid_code",
        AuthProviderFailureKind.UPSTREAM_UNAVAILABLE: "auth_provider_unavailable",
        AuthProviderFailureKind.CONFIGURATION: "auth_provider_configuration_error",
        AuthProviderFailureKind.PROTOCOL: "auth_provider_protocol_error",
    }.get(kind, "auth_provider_failed")


__all__ = ["AuthProviderHost"]
