"""Global conversation public sharing, PostgreSQL adapter (0057).

Runs the SAME case list as the SQLite twin
(``tests/test_global_ask_share_store.py``); see
``tests/global_ask_share_cases.py`` for why the scenarios live in one place.
"""
from __future__ import annotations

import pytest

from app.repositories.global_ask_store import GlobalAskStore
from app.repositories.postgres.migrator import PostgresMigrator
from tests.global_ask_share_cases import CASE_IDS, CASES


pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def store(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 57
    return GlobalAskStore(postgres_database, marker="%s")


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_global_ask_share_contract(store, case):
    case(store)
