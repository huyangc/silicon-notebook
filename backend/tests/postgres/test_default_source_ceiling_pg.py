"""PostgreSQL twin of the default-ceiling wiring test.

``tests/test_default_source_ceiling.py`` pins the constructor's contract with
in-memory readers and one SQLite wiring test.  The readers themselves are SQL
(``participant_notebook_ids``' mount-validity join, ``all_visible_source_ids``,
the owner-scoped ``hidden_source_ids``, ``memory_source_ids``), so the same
assertions run here against PostgreSQL: another member's Memory never enters
the ceiling, a mounted library contributes its visible sources only, a library
mounted mid-run is refused, and a closed Memory channel withholds the asker's
own Memory without the drift probe reporting drift.
"""
from __future__ import annotations

import pytest

from tests.test_default_source_ceiling import (
    assert_default_ceiling_over_real_stores,
    build_real_fixture,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_default_source_ceiling"),
]


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def test_default_ceiling_over_real_postgres_stores(postgres_repository):
    ids = build_real_fixture(postgres_repository, "%s")
    assert_default_ceiling_over_real_stores(postgres_repository, ids)
