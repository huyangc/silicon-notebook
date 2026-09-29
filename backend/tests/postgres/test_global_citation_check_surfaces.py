"""Global Ask citation-check surfaces over stored rows, PostgreSQL adapter.

Runs the SAME case list as the SQLite twin
(``tests/test_global_citation_check_surfaces.py``); see
``tests/global_citation_check_surface_cases.py`` for why the scenarios live in
one place. The summary and the markers ride ``payload_json`` (no migration), so
what this pins is that every read path hands them back unchanged on PostgreSQL.
"""
from __future__ import annotations

import pytest

from app.repositories.global_ask_store import GlobalAskStore
from app.repositories.postgres.migrator import PostgresMigrator
from tests.global_citation_check_surface_cases import CASE_IDS, CASES


pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def store(postgres_database):
    PostgresMigrator(postgres_database).migrate()
    return GlobalAskStore(postgres_database, marker="%s")


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_citation_check_surface_contract(store, case):
    case(store)
