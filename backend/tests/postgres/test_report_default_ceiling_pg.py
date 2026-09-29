"""PostgreSQL twin of ``tests/test_report_default_ceiling.py``.

The production readers behind the report worker's default ceiling are SQL
(participants, per-library visible sources, the owner-scoped hidden half), so
the phase assertions run here against PostgreSQL too: an unscoped report's
intent, plan and generate phases see ``visible ∪ the creator's own hidden
half`` and a visible-only ceiling for the mounted library, persist no source
scope, and the auto-confirm refresh inherits the per-library ceilings.
"""
from __future__ import annotations

import pytest

from tests.test_report_default_ceiling import (
    assert_auto_confirm_refresh_reinstalls_inside_the_ceiling,
    assert_unscoped_report_phases_run_under_the_default_ceiling,
    build_report_fixture,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_report_default_ceiling"),
]


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def test_unscoped_report_phases_run_under_the_default_ceiling_on_postgres(
    postgres_repository, monkeypatch
):
    ids = build_report_fixture(postgres_repository, "%s")
    assert_unscoped_report_phases_run_under_the_default_ceiling(
        postgres_repository, ids, monkeypatch
    )


def test_auto_confirm_refresh_reinstalls_inside_the_ceiling_on_postgres(
    postgres_repository, monkeypatch
):
    ids = build_report_fixture(postgres_repository, "%s")
    assert_auto_confirm_refresh_reinstalls_inside_the_ceiling(
        postgres_repository, ids, monkeypatch
    )
