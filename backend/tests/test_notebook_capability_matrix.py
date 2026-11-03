"""Capability x role matrix on SQLite (share-edit P1-T3).

The four layers live in ``tests/notebook_capability_matrix_cases.py`` and run
unchanged on PostgreSQL through
``tests/postgres/test_notebook_capability_matrix_pg.py``. This file adds the
census guard: ``ROUTE_LEVELS`` covers exactly the notebook write routes.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.notebook_capability_matrix_cases import CASES, build_world
from tests.notebook_route_inventory import unsafe_notebook_routes
from tests.notebook_route_levels import ROUTE_LEVELS


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def test_route_levels_cover_inventory_exactly(client):
    """Every unsafe route carrying ``{notebook_id}`` plus every body-lookup write
    route has exactly one expected level; no entry outlives its route."""
    inventory = unsafe_notebook_routes(client.app)
    missing = inventory - set(ROUTE_LEVELS)
    stale = set(ROUTE_LEVELS) - inventory
    assert not missing, f"routes without an expected level: {sorted(missing)}"
    assert not stale, f"ROUTE_LEVELS entries without a route: {sorted(stale)}"


@pytest.mark.parametrize("case", sorted(CASES))
def test_notebook_capability_matrix(client, monkeypatch, case):
    CASES[case](build_world(client, postgres=False), monkeypatch)
