from types import SimpleNamespace

import pytest

from app.api.collab_pulse_middleware import CollabPulseMiddleware
from app.api.collab_pulse_routes import PULSE_ROUTE_RULES, Branch, Capability, NoneKind
from app.domain.collaboration import CollabMark, PulseActor
from tests.notebook_route_inventory import unsafe_notebook_routes
from tests.test_notebook_capability_matrix import client as matrix_client

client = matrix_client


def test_pulse_classification_covers_the_actual_closed_route_inventory(client):
    assert set(PULSE_ROUTE_RULES) == unsafe_notebook_routes(client.app)
    assert all(isinstance(rule, (Capability, Branch, NoneKind)) for rule in PULSE_ROUTE_RULES.values())
    assert all(rule.reason for rule in PULSE_ROUTE_RULES.values() if isinstance(rule, NoneKind))


def test_capability_rules_are_backed_by_their_exact_stable_guard(client):
    from app.api import deps
    from tests.notebook_route_inventory import route_for

    def dependency_calls(dependant):
        yield dependant.call
        for child in dependant.dependencies:
            yield from dependency_calls(child)

    for (method, path), rule in PULSE_ROUTE_RULES.items():
        if not isinstance(rule, Capability):
            continue
        calls = set(dependency_calls(route_for(client.app, method, path).dependant))
        if rule.name == "notebook:delete":
            assert calls & {deps.require_notebook_delete, deps.require_notebook_cancel_delete}
        else:
            assert deps.require_notebook_capability(rule.name) in calls, (method, path, rule.name)


@pytest.mark.anyio
@pytest.mark.parametrize("method,path,status,source,expected", [
    ("PATCH", "/api/notebooks/{notebook_id}", 200, "guard", 1),
    ("PATCH", "/api/notebooks/{notebook_id}", 409, "guard", 0),
    ("PATCH", "/api/notebooks/{notebook_id}", 200, "explicit", 0),
    ("POST", "/api/notebooks/{notebook_id}/knowhow/{table_id}/append", 200, "guard", 0),
    ("POST", "/api/notebooks/{notebook_id}/knowhow/{table_id}/append", 200, "explicit", 1),
    ("POST", "/api/notebooks/{notebook_id}/knowhow/{table_id}/transfer", 409, "explicit", 0),
    ("POST", "/api/notebooks/{notebook_id}/knowhow/{table_id}/transfer", 413, "explicit", 0),
    ("POST", "/api/notebooks/{notebook_id}/knowhow/import/preview", 200, "guard", 0),
    ("GET", "/api/notebooks/{notebook_id}", 200, "guard", 0),
])
async def test_observer_uses_only_the_classified_mark_source_and_success(method, path, status, source, expected, monkeypatch):
    from app.api import deps

    recorded = []
    monkeypatch.setattr(deps, "collaboration_service", lambda: SimpleNamespace(record=lambda *args: recorded.append(args)))
    capability = "notebook:manage" if method == "PATCH" or method == "GET" else "knowhow:write"
    mark = CollabMark(source, "nb", capability, "owner", PulseActor("actor", "显示名", "editor"))
    scope = {"type": "http", "method": method, "route": SimpleNamespace(path_format=path), "state": {"collab_marks": [mark, mark]}}
    messages = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})

    async def send(message):
        messages.append(message)

    await CollabPulseMiddleware(app)(scope, None, send)
    assert len(recorded) == expected
    if recorded:
        assert recorded[0][2] == method + " " + path
    assert messages[-1]["body"] == b"done"


@pytest.mark.anyio
async def test_observer_failure_never_replaces_the_committed_response(monkeypatch):
    from app.api import deps

    def unavailable():
        raise RuntimeError("private-data-must-not-escape")

    monkeypatch.setattr(deps, "collaboration_service", unavailable)
    scope = {"type": "http", "method": "PATCH", "route": SimpleNamespace(path_format="/api/notebooks/{notebook_id}"), "state": {"collab_marks": [CollabMark("guard", "nb", "notebook:manage", "owner", PulseActor("actor", "名字", "editor"))]}}
    messages = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        messages.append(message)

    await CollabPulseMiddleware(app)(scope, None, send)
    assert messages == [{"type": "http.response.start", "status": 200, "headers": []}]


@pytest.mark.anyio
async def test_d14_owner_reuses_the_guard_but_nonowner_authority_is_fresh(monkeypatch):
    from fastapi import HTTPException, Request

    from app.api import knowhow_routes
    from app.models.identity import UserProfile

    reads = []
    monkeypatch.setattr(knowhow_routes, "_kh_access", lambda: SimpleNamespace(notebook_access_level=lambda *args: reads.append(args) or SimpleNamespace(level="read")))
    owner = UserProfile(id="owner", email="", display_name="主人", role="user")
    mark = CollabMark("guard", "nb", "knowhow:write", "owner", PulseActor("owner", "主人", "owner"))
    request = Request({"type": "http", "state": {"collab_marks": [mark]}})
    await knowhow_routes._refuse_named_editor("nb", owner, request)
    assert reads == []
    editor = owner.model_copy(update={"id": "editor"})
    mark = CollabMark("guard", "nb", "knowhow:write", "owner", PulseActor("editor", "编辑者", "editor"))
    request = Request({"type": "http", "state": {"collab_marks": [mark]}})
    with pytest.raises(HTTPException) as refused:
        await knowhow_routes._refuse_named_editor("nb", editor, request)
    assert refused.value.status_code == 404
    assert reads == [("nb", "editor")]
