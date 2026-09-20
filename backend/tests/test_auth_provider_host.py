from __future__ import annotations

from dataclasses import dataclass
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from app.domain.auth_provider import AuthProviderError, ExternalIdentity
from app.extension_sdk import (
    AUTH_PROVIDER_POINT,
    EXTENSION_API_VERSION,
    AuthProviderDescription,
    Availability,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
)
from app.extensions import build_extension_runtime
from app.extensions.registry import ExtensionRegistryError


_DECLARATION = ContributionDeclaration(
    "test.auth.provider", AUTH_PROVIDER_POINT, ContributionKind.PROVIDER
)


class _Provider:
    def __init__(self) -> None:
        self.description = AuthProviderDescription(
            provider_id="test",
            provider_namespace="test.production",
            configuration_generation="initial",
            public_label="统一登录",
            authorization_endpoint="https://identity.example/authorize",
            supports_pkce=True,
        )
        self.parameters = (
            ("client_id", "public-client"),
            ("response_type", "code"),
        )
        self.identity: object = ExternalIdentity(
            "test.production", "subject-1", "user-1", "测试用户"
        )
        self.delay = 0.0
        self.seen = None

    def describe(self):
        return self.description

    def authorization_parameters(self):
        return self.parameters

    def authenticate_code(self, context):
        self.seen = context
        if self.delay:
            time.sleep(self.delay)
        return self.identity


@dataclass
class _Bundle:
    provider: _Provider
    trust: str = "builtin"

    @property
    def manifest(self):
        return ExtensionManifest(
            id="test.auth",
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name="test auth",
            trust=self.trust,
            contributions=(_DECLARATION,),
        )

    def register(self, registrar):
        registrar.add_provider(ExtensionContribution(
            _DECLARATION, self.provider, lambda _context: Availability.available()
        ))


def _host(provider: _Provider, **kwargs):
    return build_extension_runtime((_Bundle(provider),), **kwargs).auth_provider


def test_empty_runtime_has_no_auth_provider():
    assert build_extension_runtime().auth_provider.describe() is None


def test_host_freezes_description_and_builds_core_owned_authorization_url():
    provider = _Provider()
    host = _host(provider)

    descriptor = host.describe()
    assert descriptor is not None
    assert descriptor.plugin_id == "test.auth"
    assert descriptor.contribution_id == "test.auth.provider"
    assert descriptor.provider_namespace == "test.production"

    provider.parameters = (("state", "provider-state"),)
    url = host.authorization_url(
        state="core-state",
        redirect_uri="https://notebook.example/api/auth/callback",
        code_challenge="A" * 43,
    )
    query = parse_qs(urlsplit(url).query)
    assert query == {
        "client_id": ["public-client"],
        "response_type": ["code"],
        "state": ["core-state"],
        "redirect_uri": ["https://notebook.example/api/auth/callback"],
        "code_challenge": ["A" * 43],
        "code_challenge_method": ["S256"],
    }


def test_host_rejects_provider_owned_reserved_parameters_at_startup():
    provider = _Provider()
    provider.parameters = (("redirect_uri", "https://attacker.example"),)
    with pytest.raises(ExtensionRegistryError, match="invalid auth provider"):
        _host(provider)


def test_authenticate_passes_only_protocol_inputs_and_validates_namespace():
    provider = _Provider()
    host = _host(provider)
    identity = host.authenticate(
        code="one-use-code",
        redirect_uri="https://notebook.example/api/auth/callback",
        code_verifier="v" * 43,
        timeout_seconds=1.0,
    )
    assert identity == provider.identity
    assert provider.seen.code == "one-use-code"
    assert provider.seen.redirect_uri.endswith("/api/auth/callback")
    assert provider.seen.code_verifier == "v" * 43
    assert provider.seen.deadline_monotonic > time.monotonic()

    provider.identity = ExternalIdentity("other.production", "s", "u", "n")
    with pytest.raises(AuthProviderError) as exc_info:
        host.authenticate(
            code="another-code",
            redirect_uri="https://notebook.example/api/auth/callback",
            code_verifier="v" * 43,
            timeout_seconds=1.0,
        )
    assert exc_info.value.code == "invalid_auth_provider_identity"


def test_authenticate_abandons_a_late_provider_result():
    provider = _Provider()
    provider.delay = 0.1
    host = _host(provider)
    started = time.monotonic()
    with pytest.raises(AuthProviderError) as exc_info:
        host.authenticate(
            code="slow-code",
            redirect_uri="https://notebook.example/api/auth/callback",
            code_verifier="v" * 43,
            timeout_seconds=0.01,
        )
    assert exc_info.value.code == "auth_provider_timeout"
    assert time.monotonic() - started < 0.08


def test_live_admin_admission_disables_authorization_and_exchange():
    disabled: set[str] = set()
    provider = _Provider()
    host = build_extension_runtime(
        (_Bundle(provider, trust="deployment"),),
        disabled_ids_provider=lambda: disabled,
    ).auth_provider
    disabled.add("test.auth")

    with pytest.raises(AuthProviderError) as url_error:
        host.authorization_url(
            state="state",
            redirect_uri="https://notebook.example/api/auth/callback",
            code_challenge="A" * 43,
        )
    assert url_error.value.code == "auth_provider_unavailable"

    with pytest.raises(AuthProviderError) as auth_error:
        host.authenticate(
            code="code",
            redirect_uri="https://notebook.example/api/auth/callback",
            code_verifier="v" * 43,
            timeout_seconds=1.0,
        )
    assert auth_error.value.code == "auth_provider_unavailable"


def test_multiple_auth_providers_fail_registry_freeze():
    other = ContributionDeclaration(
        "test.other.provider", AUTH_PROVIDER_POINT, ContributionKind.PROVIDER
    )

    @dataclass
    class OtherBundle:
        manifest = ExtensionManifest(
            id="test.other",
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name="other",
            trust="builtin",
            contributions=(other,),
        )

        def register(self, registrar):
            registrar.add_provider(ExtensionContribution(other, _Provider()))

    with pytest.raises(ExtensionRegistryError, match="multiple single providers"):
        build_extension_runtime((_Bundle(_Provider()), OtherBundle()))
