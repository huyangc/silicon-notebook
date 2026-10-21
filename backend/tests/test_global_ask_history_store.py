"""Agent history reads of global conversations, SQLite adapter.

The scenarios live in ``tests/global_ask_history_store_cases.py`` because
``GlobalAskStore`` is one implementation serving both backends; the PostgreSQL
twin (``tests/postgres/test_global_ask_history_store_pg.py``) runs the very
same list.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.global_ask_history_store_cases import CASE_IDS, CASES


@pytest.fixture
def store(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'global_history.db'}",
        storage_dir=str(tmp_path / "storage"),
    )
    repo = SQLiteRepository(settings)
    yield repo._runtime.global_ask_store
    repo.close()


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_global_ask_history_store_contract(store, case):
    case(store)
