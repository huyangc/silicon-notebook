"""What a named editor may not destroy, on SQLite (share-edit P1-T4).

The scenarios live in ``tests/editor_destructive_cases.py`` and run unchanged
on PostgreSQL through ``tests/postgres/test_editor_destructive_pg.py``. This
file adds the registry guard: every edit-tier write route says how it treats
an editor (``tests/editor_destructive_registry.py``).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from tests.editor_destructive_cases import CASES, build_world
from tests.editor_destructive_registry import (
    ADMIN_REQUIRED_IN_BODY,
    REGISTRY,
    AdminRequired,
)
from tests.notebook_capability_matrix_cases import _dependency_calls
from tests.notebook_route_inventory import route_for, unsafe_notebook_routes
from tests.notebook_route_levels import ROUTE_LEVELS, Capability, ReaderBody


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def test_every_edit_capability_unsafe_route_is_classified(client):
    """The registry's keys are exactly the census routes whose expected level
    is an edit-tier capability (``Capability`` or ``ReaderBody``)."""
    inventory = unsafe_notebook_routes(client.app)
    edit_tier = set()
    for key in inventory:
        level = ROUTE_LEVELS[key]
        capability = (
            level.name if isinstance(level, Capability)
            else level.capability if isinstance(level, ReaderBody) else None
        )
        if capability is not None and deps._CAPABILITY_LEVELS[capability] == "edit":
            edit_tier.add(key)
    assert set(REGISTRY) == edit_tier, (
        sorted(edit_tier - set(REGISTRY)), sorted(set(REGISTRY) - edit_tier),
    )


def test_every_admin_required_route_refuses_a_named_editor(client):
    """An ``AdminRequired`` entry is backed by a real refusal: the route's
    dependency chain carries ``_refuse_named_editor``, or the route is one of
    the few that judge it in the body (``ADMIN_REQUIRED_IN_BODY``, each pinned
    by its behaviour case). A new irreversible route registered as
    ``AdminRequired`` without either is red here, not silently open."""
    from app.api.knowhow_routes import _refuse_named_editor

    admin_required = {key for key, kind in REGISTRY.items() if isinstance(kind, AdminRequired)}
    assert ADMIN_REQUIRED_IN_BODY <= admin_required
    for method, template in sorted(admin_required):
        guarded = _refuse_named_editor in _dependency_calls(
            route_for(client.app, method, template)
        )
        in_body = (method, template) in ADMIN_REQUIRED_IN_BODY
        assert guarded != in_body, (method, template, guarded, in_body)


@pytest.mark.parametrize("case", sorted(CASES))
def test_editor_destructive(client, case):
    CASES[case](build_world(client, postgres=False))
