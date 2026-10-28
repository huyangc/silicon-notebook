"""Agent history reads of global conversations, PostgreSQL adapter.

Runs the SAME case list as the SQLite twin
(``tests/test_global_ask_history_store.py``); see
``tests/global_ask_history_store_cases.py`` for why the scenarios live in one
place.
"""
from __future__ import annotations

import pytest

from app.repositories.global_ask_store import GlobalAskStore
from app.repositories.postgres.migrator import PostgresMigrator
from tests.global_ask_history_store_cases import CASE_IDS, CASES

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def store(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 84
    return GlobalAskStore(postgres_database, marker="%s")


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_global_ask_history_store_contract(store, case):
    case(store)
