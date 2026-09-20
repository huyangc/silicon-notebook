"""Global conversation public sharing, SQLite adapter (v77).

The scenarios themselves live in ``tests/global_ask_share_cases.py`` because
``GlobalAskStore`` is one implementation serving both backends; the PostgreSQL
twin (``tests/postgres/test_global_ask_share_store.py``) runs the very same
list, so a dialect that only works on one side fails loudly instead of drifting.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.global_ask_share_cases import CASE_IDS, CASES


@pytest.fixture
def store(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'global_share.db'}",
        storage_dir=str(tmp_path / "storage"),
    )
    repo = SQLiteRepository(settings)
    yield repo._runtime.global_ask_store
    repo.close()


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_global_ask_share_contract(store, case):
    case(store)
