from __future__ import annotations

import json
import logging
from pathlib import Path
import sys
import time

import httpx
import pytest
from pydantic import ValidationError

from app.extension_sdk import (
    AUTH_PROVIDER_POINT,
    EXTENSION_API_VERSION,
    AuthProviderCodeContext,
    AuthProviderFailure,
    AuthProviderFailureKind,
)
from app.extensions.discovery import discover_deployment_extensions
from app.extensions import build_extension_runtime


_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_ROOT = _ROOT / "examples" / "extensions" / "w3-auth"
_PLUGIN_SRC = _PLUGIN_ROOT / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

from silicon_notebook_auth_w3.bundle import BUNDLE, PLUGIN_ID  # noqa: E402
from silicon_notebook_auth_w3.client import MAX_RESPONSE_BYTES, W3Client  # noqa: E402
from silicon_notebook_auth_w3.identity import external_identity  # noqa: E402
from silicon_notebook_auth_w3.provider import W3AuthProvider  # noqa: E402
from silicon_notebook_auth_w3.settings import W3AuthSettings  # noqa: E402


def _settings(**overrides) -> W3AuthSettings:
    return W3AuthSettings(
        provider_namespace="w3.production",
        configuration_generation="initial",
        **overrides,
    )


def _environment() -> dict[str, str]:
    return {
        "W3_LOGIN_ORIGIN": "https://identity.example",
        "W3_CLIENT_ID": "client-id",
        "W3_CLIENT_SECRET": "client-secret",
    }


def _context() -> AuthProviderCodeContext:
    return AuthProviderCodeContext(
        "authorization-code",
        "https://notebook.example/api/auth/callback",
        None,
        time.monotonic() + 10.0,
    )


def test_manifest_uses_current_sdk_and_single_auth_provider_point():
    assert BUNDLE.manifest.api_version == EXTENSION_API_VERSION
    assert BUNDLE.manifest.id == PLUGIN_ID
    assert [item.point for item in BUNDLE.manifest.contributions] == [
        AUTH_PROVIDER_POINT
    ]
    assert (_PLUGIN_ROOT / "extensions.example.toml").read_text(
        encoding="utf-8"
    ).count("enabled = false") == 1


def test_core_never_imports_the_example_package():
    production_files = (_ROOT / "backend" / "app").rglob("*.py")
    assert all(
        "silicon_notebook_auth_w3" not in path.read_text(encoding="utf-8")
        for path in production_files
    )


def test_settings_accept_only_environment_references_and_explicit_identity_scope():
    settings = _settings(subject_field="immutableId", userinfo_auth_mode="bearer")
    assert settings.client_secret_env == "W3_CLIENT_SECRET"
    assert settings.subject_field == "immutableId"
    with pytest.raises(ValidationError):
        _settings(client_secret_env="raw secret")
    with pytest.raises(ValidationError):
        W3AuthSettings(configuration_generation="initial")


def test_provider_description_and_parameters_are_pure_fixed_configuration():
    settings = _settings(pkce_supported=True)
    provider = W3AuthProvider(lambda: settings, environ=_environment())
    description = provider.describe()
    assert description.authorization_endpoint == (
        "https://identity.example/saaslogin1/oauth2/authorize"
    )
    assert description.provider_namespace == "w3.production"
    assert description.supports_pkce is True
    assert provider.authorization_parameters() == (
        ("client_id", "client-id"),
        ("response_type", "code"),
        ("scope", "base.profile"),
        ("display", "page"),
    )


def test_query_mode_exchanges_json_then_fetches_and_normalizes_userinfo():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/accesstoken"):
            payload = json.loads(request.content)
            assert payload == {
                "grant_type": "authorization_code",
                "code": "authorization-code",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "redirect_uri": "https://notebook.example/api/auth/callback",
            }
            return httpx.Response(200, json={"access_token": "opaque-token"})
        assert request.url.path.endswith("/userinfo")
        assert request.url.params["scope"] == "base.profile"
        assert request.url.params["access_token"] == "opaque-token"
        assert request.url.params["client_id"] == "client-id"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={
            "uid": "a12345678",
            "displayName": "English Name",
            "displayNameCn": "中文姓名",
        })

    client = W3Client(
        _settings(),
        transport=httpx.MockTransport(handler),
        environ=_environment(),
    )
    identity = client.authenticate(_context())
    assert identity.provider_namespace == "w3.production"
    assert identity.subject == "a12345678"
    assert identity.username == "a12345678"
    assert identity.display_name == "中文姓名"
    assert len(requests) == 2


def test_query_token_and_exchange_secrets_are_redacted_from_http_logs(caplog):
    access_token = "access-token-that-must-never-be-logged"
    authorization_code = "authorization-code-that-must-stay-private"
    client_secret = "client-secret-that-must-stay-private"
    environment = _environment()
    environment["W3_CLIENT_SECRET"] = client_secret

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accesstoken"):
            logging.getLogger("httpcore.http11").debug(
                "request body=%s unrelated=kept", request.content
            )
            return httpx.Response(200, json={"access_token": access_token})
        logging.getLogger("httpcore.connection").debug(
            "userinfo request=%s unrelated=kept", request.url
        )
        logging.getLogger("httpx").info("completed %s", request.url)
        return httpx.Response(200, json={"uid": "CaseSensitiveUID"})

    caplog.set_level(logging.DEBUG)
    client = W3Client(
        _settings(),
        transport=httpx.MockTransport(handler),
        environ=environment,
    )
    identity = client.authenticate(AuthProviderCodeContext(
        authorization_code,
        "https://notebook.example/api/auth/callback",
        None,
        time.monotonic() + 10.0,
    ))

    assert identity.username == "CaseSensitiveUID"
    assert "unrelated=kept" in caplog.text
    assert "[REDACTED]" in caplog.text
    assert access_token not in caplog.text
    assert authorization_code not in caplog.text
    assert client_secret not in caplog.text


def test_query_token_is_redacted_from_debug_logs_on_network_failure(caplog):
    access_token = "failed-access-token-that-must-never-be-logged"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accesstoken"):
            return httpx.Response(200, json={"access_token": access_token})
        logging.getLogger("httpcore.connection").debug(
            "connection failed url=%s unrelated=failure-kept", request.url
        )
        raise httpx.ConnectError(
            f"supplier connection failed for {request.url}", request=request
        )

    caplog.set_level(logging.DEBUG)
    client = W3Client(
        _settings(),
        transport=httpx.MockTransport(handler),
        environ=_environment(),
    )
    with pytest.raises(AuthProviderFailure) as exc_info:
        client.authenticate(_context())

    assert exc_info.value.kind is AuthProviderFailureKind.UPSTREAM_UNAVAILABLE
    assert "unrelated=failure-kept" in caplog.text
    assert "[REDACTED]" in caplog.text
    assert access_token not in caplog.text


def test_confirmed_bearer_mode_keeps_token_out_of_userinfo_query():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accesstoken"):
            return httpx.Response(200, json={"access_token": "opaque-token"})
        assert "access_token" not in request.url.params
        assert request.headers["authorization"] == "Bearer opaque-token"
        return httpx.Response(200, json={"uid": "u1"})

    client = W3Client(
        _settings(userinfo_auth_mode="bearer"),
        transport=httpx.MockTransport(handler),
        environ=_environment(),
    )
    assert client.authenticate(_context()).username == "u1"


@pytest.mark.parametrize("uid", [None, "", 123, [], {}])
def test_userinfo_rejects_missing_or_non_string_uid(uid):
    with pytest.raises(ValueError, match="userinfo_uid_invalid"):
        external_identity(
            {"uid": uid},
            provider_namespace="w3.production",
            subject_field="uid",
        )


def test_explicit_immutable_subject_field_does_not_change_username():
    identity = external_identity(
        {"uid": "renamable", "immutableId": "subject-7"},
        provider_namespace="w3.production",
        subject_field="immutableId",
    )
    assert identity.subject == "subject-7"
    assert identity.username == "renamable"


def test_upstream_errors_are_typed_without_response_text():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="secret upstream response")

    client = W3Client(
        _settings(),
        transport=httpx.MockTransport(handler),
        environ=_environment(),
    )
    with pytest.raises(AuthProviderFailure) as exc_info:
        client.authenticate(_context())
    assert exc_info.value.kind is AuthProviderFailureKind.INVALID_CODE
    assert "secret upstream response" not in str(exc_info.value)


def test_redirects_are_rejected_instead_of_followed():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "https://other.example"})

    client = W3Client(
        _settings(),
        transport=httpx.MockTransport(handler),
        environ=_environment(),
    )
    with pytest.raises(AuthProviderFailure) as exc_info:
        client.authenticate(_context())
    assert exc_info.value.kind is AuthProviderFailureKind.PROTOCOL
    assert calls == 1


def test_response_body_is_bounded_even_without_content_length():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1))

    client = W3Client(
        _settings(),
        transport=httpx.MockTransport(handler),
        environ=_environment(),
    )
    with pytest.raises(AuthProviderFailure) as exc_info:
        client.authenticate(_context())
    assert exc_info.value.kind is AuthProviderFailureKind.PROTOCOL


def test_discovery_loads_the_independent_package_only_when_named(
    monkeypatch, tmp_path
):
    monkeypatch.syspath_prepend(str(_PLUGIN_SRC))
    for name, value in _environment().items():
        monkeypatch.setenv(name, value)
    config = tmp_path / "extensions.toml"
    config.write_text(
        '[extensions."examples.w3_auth"]\n'
        'bundle = "silicon_notebook_auth_w3.bundle:BUNDLE"\n'
        'enabled = true\n\n'
        '[extensions."examples.w3_auth".settings]\n'
        'provider_namespace = "w3.production"\n'
        'configuration_generation = "initial"\n',
        encoding="utf-8",
    )
    discovered = discover_deployment_extensions(str(config))
    assert [item.plugin_id for item in discovered] == [PLUGIN_ID]
    assert type(discovered[0].settings) is W3AuthSettings
    runtime = build_extension_runtime(tuple(item.bundle for item in discovered))
    assert runtime.auth_provider.describe().provider_id == "w3"
