"""The owner's duplicate groups: two narrow passes answer exactly what the full
read they replaced answered (scenario: ``own_memory_dedup_narrowing_cases``,
shared with ``postgres/test_own_memory_dedup_narrowing_pg.py``)."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests import own_memory_dedup_narrowing_cases as cases


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return cases.build(SQLiteRepository(Settings()), lambda sql: sql)


def test_two_passes_answer_what_the_full_read_answered(world):
    cases.two_passes_answer_what_the_full_read_answered(world)


def test_blocks_follow_the_published_seed_space(world):
    cases.blocks_follow_the_published_seed_space(world)
