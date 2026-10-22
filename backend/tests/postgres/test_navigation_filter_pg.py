"""Read side of the navigation-page marker on PostgreSQL (0082), the canonical
definition: every chunk-row builder retrieval reads carries
``chunks.navigation``, elements of a navigation chunk are flagged through
``chunk_elements``, and the source-subgraph navigation signature tracks the
flags. SQLite twin ``tests/test_navigation_filter.py``; shared cases in
``tests/navigation_filter_cases.py``."""
from __future__ import annotations

import pytest

from tests import navigation_filter_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def repo(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def test_chunk_row_builders_carry_navigation(repo):
    cases.case_chunk_row_builders_carry_navigation(repo)


def test_element_rows_carry_navigation(repo):
    cases.case_element_rows_carry_navigation(repo)


def test_navigation_version_moves_with_the_flagged_set(repo):
    cases.case_navigation_version_moves_with_the_flagged_set(repo)


def test_service_builders_carry_navigation(repo, monkeypatch):
    cases.case_service_builders_carry_navigation(repo, monkeypatch)
