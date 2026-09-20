"""Edit and re-send on a global conversation, PostgreSQL adapter.

Runs the SAME case list as the SQLite twin
(``tests/test_global_ask_replace_store.py``); see
``tests/global_ask_replace_cases.py`` for why the scenarios live in one place.
"""
from __future__ import annotations

import pytest

from app.repositories.global_ask_store import GlobalAskStore
from app.repositories.postgres.migrator import PostgresMigrator
from tests.global_ask_replace_cases import CASE_IDS, CASES


pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def store(postgres_database):
    PostgresMigrator(postgres_database).migrate()
    return GlobalAskStore(postgres_database, marker="%s")


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_global_ask_replace_contract(store, case):
    case(store)
