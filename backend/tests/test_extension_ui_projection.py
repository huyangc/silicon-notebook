from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    Availability,
    AvailabilityStatus,
    ExtensionManifest,
    UiContributionDeclaration,
)
from app.extensions import build_extension_registry
from app.extensions.ui_projection import project_ui_contributions, ui_contribution_contract


@dataclass
class _UiBundle:
    manifest: ExtensionManifest

    def register(self, _registrar) -> None:
        return None


def _bundle() -> _UiBundle:
    return _UiBundle(ExtensionManifest(
        id="sample-ui",
        version="1.2.3",
        api_version=EXTENSION_API_VERSION,
        display_name="Sample UI",
        trust="builtin",
        contributions=(),
        ui_contributions=(UiContributionDeclaration(
            id="sample-panel",
            slot="workspace.side_panel",
            capability="sample.ui.available",
        ),),
    ))


def test_projection_is_live_stable_and_sanitizes_internal_reasons():
    state = {"status": AvailabilityStatus.DISABLED}

    def decision(_context):
        return Availability(state["status"], "secret_path_or_reason")

    registry = build_extension_registry(
        (_bundle(),),
        capability_decisions={"sample.ui.available": decision},
    )

    disabled = project_ui_contributions(registry, object())
    assert [row.contribution_id for row in disabled] == ["sample-panel"]
    assert disabled[0].available is False
    assert disabled[0].unavailable_reason == "disabled"
    assert "secret" not in repr(disabled[0])

    state["status"] = AvailabilityStatus.AVAILABLE
    available = project_ui_contributions(registry, object())
    assert available[0].available is True
    assert available[0].unavailable_reason is None


def test_projection_sanitizes_hostile_baseexception_but_propagates_process_signals():
    class HardAbort(BaseException):
        pass

    def hard_abort(_context):
        raise HardAbort("secret backend diagnostic")

    registry = build_extension_registry(
        (_bundle(),),
        capability_decisions={"sample.ui.available": hard_abort},
    )
    assert project_ui_contributions(registry, None)[0].unavailable_reason == "unavailable"

    for signal in (KeyboardInterrupt, SystemExit):
        def process_signal(_context, signal=signal):
            raise signal()

        signal_registry = build_extension_registry(
            (_bundle(),),
            capability_decisions={"sample.ui.available": process_signal},
        )
        with pytest.raises(signal):
            project_ui_contributions(signal_registry, None)


def test_projection_evaluates_each_unique_capability_once_per_request():
    calls = 0

    def decision(_context):
        nonlocal calls
        calls += 1
        return Availability.available()

    manifest = _bundle().manifest
    bundle = _UiBundle(ExtensionManifest(
        **{
            **manifest.__dict__,
            "ui_contributions": (
                *manifest.ui_contributions,
                UiContributionDeclaration(
                    "sample-detail", "source.detail_section", "sample.ui.available"
                ),
            ),
        }
    ))
    registry = build_extension_registry(
        (bundle,),
        capability_decisions={"sample.ui.available": decision},
    )
    assert len(project_ui_contributions(registry, object())) == 2
    assert calls == 1


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def _auth(client: TestClient) -> dict[str, str]:
    client.post("/api/auth/register", json={"username": "z00654321", "password": "pw"})
    response = client.post("/api/auth/login", json={"username": "z00654321", "password": "pw"})
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_system_extensions_is_authenticated_and_projects_agent_profile_ui(client):
    assert client.get("/api/system/extensions").status_code == 401
    response = client.get("/api/system/extensions", headers=_auth(client))
    assert response.status_code == 200
    assert response.json() == {
        "api_version": "1",
        "extensions": [{
            "plugin_id": "builtin.ask_agent_profile",
            "display_name": "Ask agent-profile completion",
            "version": "1.0.0",
            "contribution_id": "builtin.ask_agent_profile.workspace_panel",
            "available": True,
            "unavailable_reason": None,
        }],
    }


def test_agent_profile_ui_projection_reuses_live_wiring_predicate(client, monkeypatch):
    from app.services import reasoning_retrieval

    headers = _auth(client)
    monkeypatch.setattr(
        reasoning_retrieval,
        "profile_wiring_active",
        lambda _settings, _store: False,
    )
    response = client.get("/api/system/extensions", headers=headers)
    assert response.status_code == 200
    assert response.json()["extensions"] == [{
        "plugin_id": "builtin.ask_agent_profile",
        "display_name": "Ask agent-profile completion",
        "version": "1.0.0",
        "contribution_id": "builtin.ask_agent_profile.workspace_panel",
        "available": False,
        "unavailable_reason": "disabled",
    }]


def test_system_extensions_reads_exact_app_registry_and_live_decision(client):
    state = {"available": False}

    def decision(_context):
        return (
            Availability.available()
            if state["available"]
            else Availability(AvailabilityStatus.UNAVAILABLE, "raw_secret_reason")
        )

    registry = build_extension_registry(
        (_bundle(),),
        capability_decisions={"sample.ui.available": decision},
    )
    client.app.state.extension_ui_projection = lambda context: project_ui_contributions(
        registry, context
    )
    headers = _auth(client)
    first = client.get("/api/system/extensions", headers=headers)
    assert first.json() == {
        "api_version": "1",
        "extensions": [{
            "plugin_id": "sample-ui",
            "display_name": "Sample UI",
            "version": "1.2.3",
            "contribution_id": "sample-panel",
            "available": False,
            "unavailable_reason": "unavailable",
        }],
    }
    assert "raw_secret_reason" not in first.text

    state["available"] = True
    second = client.get("/api/system/extensions", headers=headers)
    assert second.json()["extensions"][0] == {
        **first.json()["extensions"][0],
        "available": True,
        "unavailable_reason": None,
    }


def test_default_backend_ui_topology_matches_cross_stack_contract():
    from app.extensions import default_extension_runtime

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "ui_extension_contract.json").read_text()
    )
    actual = ui_contribution_contract(default_extension_runtime().registry)
    assert fixture == {"api_version": "1", "contributions": actual}
