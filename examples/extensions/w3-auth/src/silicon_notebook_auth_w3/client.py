"""W3 HTTP adapter. Response bodies and credentials never enter errors/logs."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import json
import logging
import math
import os
import threading
import time
from typing import Callable
from urllib.parse import quote, quote_plus, urlsplit

import httpx

from app.extension_sdk import (
    AuthProviderCodeContext,
    AuthProviderFailure,
    AuthProviderFailureKind,
    ExternalIdentity,
)

from .identity import external_identity
from .settings import W3AuthSettings


_TOKEN_PATH = "/saaslogin1/oauth2/accesstoken"
_USERINFO_PATH = "/saaslogin1/oauth2/userinfo"
MAX_RESPONSE_BYTES = 256 * 1024


_LOG_SECRET_LOCK = threading.Lock()
_LOG_SECRET_COUNTS: dict[str, int] = {}


class _ActiveSecretFilter(logging.Filter):
    """Redact only secrets belonging to an in-flight W3 HTTP request.

    httpx logs the complete request URL at INFO after a response.  W3's
    supplier-compatible query-token mode therefore needs a library-local
    defence that does not depend on the application's logger configuration.
    The filter never drops a record or changes a log level; outside the small
    synchronous request scope the active set is empty and it is a no-op.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            with _LOG_SECRET_LOCK:
                secrets = tuple(_LOG_SECRET_COUNTS)
            if not secrets:
                return True
            record.msg = _redacted(record.msg, secrets)
            record.args = _redacted(record.args, secrets)
            if record.stack_info:
                record.stack_info = _redacted(record.stack_info, secrets)
            if record.exc_info is not None:
                exc_type, exc, traceback = record.exc_info
                safe_text = _redacted(str(exc), secrets)
                if safe_text != str(exc):
                    safe_exc = RuntimeError(safe_text)
                    record.exc_info = (RuntimeError, safe_exc, traceback)
                    record.exc_text = None
            return True
        except Exception:
            # Logging must never become a provider failure. The supported log
            # record shapes are covered by tests; an exotic caller object is
            # left untouched rather than suppressing the whole HTTP record.
            return True


_ACTIVE_SECRET_FILTER = _ActiveSecretFilter()


@contextmanager
def _redact_http_logs(*values: str | None):
    encoded: set[str] = set()
    for value in values:
        if type(value) is not str or not value:
            continue
        encoded.update({value, quote(value, safe=""), quote_plus(value, safe="")})
    _install_http_log_filter()
    with _LOG_SECRET_LOCK:
        for value in encoded:
            _LOG_SECRET_COUNTS[value] = _LOG_SECRET_COUNTS.get(value, 0) + 1
    try:
        yield
    finally:
        with _LOG_SECRET_LOCK:
            for value in encoded:
                remaining = _LOG_SECRET_COUNTS.get(value, 0) - 1
                if remaining > 0:
                    _LOG_SECRET_COUNTS[value] = remaining
                else:
                    _LOG_SECRET_COUNTS.pop(value, None)


def _install_http_log_filter() -> None:
    # Create httpcore's known logger names before the first transport call.
    # Re-scan as well so a later-loaded protocol backend receives the same
    # redaction without mutating handlers or caller-owned log levels.
    names = {
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.connection_pool",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
    }
    names.update(
        name
        for name in logging.Logger.manager.loggerDict
        if name.startswith("httpx.") or name.startswith("httpcore.")
    )
    for name in names:
        logger = logging.getLogger(name)
        if _ACTIVE_SECRET_FILTER not in logger.filters:
            logger.addFilter(_ACTIVE_SECRET_FILTER)


def _redacted(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, bytes):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret.encode(), b"[REDACTED]")
        return value
    if isinstance(value, tuple):
        return tuple(_redacted(item, secrets) for item in value)
    if isinstance(value, list):
        return [_redacted(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            _redacted(key, secrets): _redacted(item, secrets)
            for key, item in value.items()
        }
    rendered = str(value)
    safe = _redacted(rendered, secrets)
    return safe if safe != rendered else value


class W3Client:
    def __init__(
        self,
        settings: W3AuthSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._environ = os.environ if environ is None else environ
        self._clock = clock

    def authenticate(self, context: AuthProviderCodeContext) -> ExternalIdentity:
        origin = login_origin(self._settings, self._environ)
        client_id = required_environment(
            self._settings.client_id_env, self._environ
        )
        client_secret = required_environment(
            self._settings.client_secret_env, self._environ
        )
        verify: bool | str = True
        ca_bundle = self._environ.get(self._settings.ca_bundle_env, "").strip()
        if ca_bundle:
            verify = ca_bundle
        timeout = self._remaining_timeout(context.deadline_monotonic)
        try:
            with httpx.Client(
                verify=verify,
                follow_redirects=False,
                transport=self._transport,
                timeout=httpx.Timeout(timeout),
            ) as client:
                with _redact_http_logs(
                    context.code, client_secret, context.code_verifier
                ):
                    token_payload = _request_json(
                        client,
                        "POST",
                        f"{origin}{_TOKEN_PATH}",
                        json_body={
                            "grant_type": "authorization_code",
                            "code": context.code,
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "redirect_uri": context.redirect_uri,
                            **(
                                {"code_verifier": context.code_verifier}
                                if context.code_verifier is not None
                                else {}
                            ),
                        },
                        headers={
                            "Accept": "application/json",
                            "Cache-Control": "no-store",
                        },
                        timeout=httpx.Timeout(timeout),
                    )
                if type(token_payload) is not dict:
                    raise AuthProviderFailure(AuthProviderFailureKind.PROTOCOL)
                access_token = token_payload.get("access_token")
                if type(access_token) is not str or not access_token.strip():
                    raise AuthProviderFailure(AuthProviderFailureKind.PROTOCOL)

                timeout = self._remaining_timeout(context.deadline_monotonic)
                common_headers = {
                    "Accept": "application/json",
                    "Cache-Control": "no-store",
                }
                if self._settings.userinfo_auth_mode == "bearer":
                    common_headers["Authorization"] = f"Bearer {access_token}"
                    params = {"scope": "base.profile", "client_id": client_id}
                else:
                    params = {
                        "scope": "base.profile",
                        "access_token": access_token,
                        "client_id": client_id,
                    }
                with _redact_http_logs(access_token):
                    user_payload = _request_json(
                        client,
                        "GET",
                        f"{origin}{_USERINFO_PATH}",
                        params=params,
                        headers=common_headers,
                        timeout=httpx.Timeout(timeout),
                    )
        except AuthProviderFailure:
            raise
        except (httpx.TimeoutException, httpx.NetworkError):
            raise AuthProviderFailure(
                AuthProviderFailureKind.UPSTREAM_UNAVAILABLE
            ) from None
        except (ValueError, TypeError, httpx.DecodingError):
            raise AuthProviderFailure(AuthProviderFailureKind.PROTOCOL) from None
        except OSError:
            raise AuthProviderFailure(AuthProviderFailureKind.CONFIGURATION) from None
        except httpx.HTTPError:
            raise AuthProviderFailure(
                AuthProviderFailureKind.UPSTREAM_UNAVAILABLE
            ) from None
        except Exception:
            raise AuthProviderFailure(
                AuthProviderFailureKind.UPSTREAM_UNAVAILABLE
            ) from None

        try:
            return external_identity(
                user_payload,
                provider_namespace=self._settings.provider_namespace,
                subject_field=self._settings.subject_field,
            )
        except (TypeError, ValueError):
            raise AuthProviderFailure(AuthProviderFailureKind.PROTOCOL) from None

    def _remaining_timeout(self, deadline: float) -> float:
        now = self._clock()
        if (
            type(deadline) not in {int, float}
            or isinstance(deadline, bool)
            or not math.isfinite(float(deadline))
        ):
            raise AuthProviderFailure(AuthProviderFailureKind.PROTOCOL)
        remaining = float(deadline) - now
        if remaining <= 0:
            raise AuthProviderFailure(
                AuthProviderFailureKind.UPSTREAM_UNAVAILABLE
            )
        return min(self._settings.timeout_seconds, remaining)


def login_origin(settings: W3AuthSettings, environ: Mapping[str, str]) -> str:
    value = required_environment(settings.login_origin_env, environ).rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AuthProviderFailure(AuthProviderFailureKind.CONFIGURATION)
    return value


def required_environment(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "")
    if type(value) is not str or not value.strip():
        raise AuthProviderFailure(AuthProviderFailureKind.CONFIGURATION)
    return value


def _raise_for_status(response: httpx.Response) -> None:
    if 200 <= response.status_code < 300:
        return
    if response.status_code in {400, 401, 403}:
        raise AuthProviderFailure(AuthProviderFailureKind.INVALID_CODE)
    if response.status_code >= 500:
        raise AuthProviderFailure(AuthProviderFailureKind.UPSTREAM_UNAVAILABLE)
    raise AuthProviderFailure(AuthProviderFailureKind.PROTOCOL)


def _request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
    timeout: httpx.Timeout | None = None,
) -> object:
    with client.stream(
        method,
        url,
        headers=headers,
        params=params,
        json=json_body,
        timeout=timeout,
    ) as response:
        _raise_for_status(response)
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise AuthProviderFailure(AuthProviderFailureKind.PROTOCOL)
            body.extend(chunk)
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AuthProviderFailure(AuthProviderFailureKind.PROTOCOL) from None


__all__ = [
    "MAX_RESPONSE_BYTES",
    "W3Client",
    "login_origin",
    "required_environment",
]
