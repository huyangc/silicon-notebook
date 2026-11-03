"""Edit permission predicate and access-level lookup on PostgreSQL (share-edit P1-T1).

Runs the exact scenarios of ``tests/test_notebook_edit_access.py`` (defined once
in ``tests/notebook_edit_access_cases.py``) against the production PostgreSQL
repository. The access-level cost pin at realistic scale is
``test_access_level_explain_pins.py``.
"""
from __future__ import annotations

import pytest

from tests.notebook_edit_access_cases import CASES, build_world

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_notebook_edit_access(repo, case):
    CASES[case](build_world(repo, postgres=True))
