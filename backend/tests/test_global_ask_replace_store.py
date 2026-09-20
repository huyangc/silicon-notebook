""""Edit and re-send" on a global conversation, SQLite adapter.

The scenarios live in ``tests/global_ask_replace_cases.py``; the PostgreSQL twin
(``tests/postgres/test_global_ask_replace_store.py``) runs the very same list.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.global_ask_replace_cases import CASE_IDS, CASES


@pytest.fixture
def store(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'global_replace.db'}",
        storage_dir=str(tmp_path / "storage"),
    )
    repo = SQLiteRepository(settings)
    yield repo._runtime.global_ask_store
    repo.close()


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_global_ask_replace_contract(store, case):
    case(store)
