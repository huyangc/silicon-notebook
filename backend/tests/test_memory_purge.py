"""Memory hard delete and member exit purge on SQLite (E5-2).

The scenarios live in ``tests/memory_purge_cases.py`` and run unchanged on
PostgreSQL through ``tests/postgres/test_memory_purge_pg.py``. This file adds
the service-level contracts that need no database projection.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.memory_purge_cases import (
    CASES,
    EMBED_DIM,
    MONKEYPATCH_CASES,
    build_world,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'memory-purge.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    return SQLiteRepository(Settings())


@pytest.fixture
def world(repo):
    return build_world(repo, postgres=False)


@pytest.mark.parametrize("case", sorted(CASES))
def test_memory_purge_scenario(world, case):
    CASES[case](world)


@pytest.mark.parametrize("case", sorted(MONKEYPATCH_CASES))
def test_memory_purge_fault_scenario(world, monkeypatch, case):
    MONKEYPATCH_CASES[case](world, monkeypatch)


def test_runtime_binds_the_exit_purge_to_the_sharing_service(repo):
    assert repo._runtime.sharing._member_memory is repo._runtime.memory_service


def test_exit_purge_order_is_clean_then_membership_then_sweep(repo, monkeypatch):
    """Pins the call order around the membership delete, independent of SQL."""
    sharing = repo._runtime.sharing
    calls: list[str] = []

    class RecordingPurge:
        def exiting_member_ids(self, notebook_id):
            calls.append("list")
            return ["user-a"]

        def purge_exiting_member(self, notebook_id, user_id):
            calls.append(f"purge:{user_id}")
            return 0

    sharing.bind_member_memory_purge(RecordingPurge())
    monkeypatch.setattr(sharing._store, "is_member", lambda nb, user: True)
    monkeypatch.setattr(
        sharing._store, "remove_member", lambda nb, user: calls.append("delete-row")
    )
    monkeypatch.setattr(
        sharing._store, "kick_all_members", lambda nb: calls.append("delete-rows")
    )
    sharing.remove_member("nb-1", "user-a")
    assert calls == ["purge:user-a", "delete-row", "purge:user-a"]
    calls.clear()
    sharing.kick_all_members("nb-1")
    assert calls == ["list", "purge:user-a", "delete-rows", "purge:user-a"]


def test_non_member_removal_never_purges(repo, monkeypatch):
    """No membership row, nothing is lost: a retained (revoked) Memory stays."""
    sharing = repo._runtime.sharing
    calls: list[str] = []

    class RecordingPurge:
        def exiting_member_ids(self, notebook_id):
            return []

        def purge_exiting_member(self, notebook_id, user_id):
            calls.append(user_id)
            return 0

    sharing.bind_member_memory_purge(RecordingPurge())
    monkeypatch.setattr(sharing._store, "is_member", lambda nb, user: False)
    sharing.remove_member("nb-1", "user-a")
    assert calls == []


def test_post_exit_sweep_failure_does_not_undo_the_exit(repo, monkeypatch):
    sharing = repo._runtime.sharing
    removed: list[str] = []

    class SecondPassFails:
        def __init__(self):
            self.calls = 0

        def exiting_member_ids(self, notebook_id):
            return []

        def purge_exiting_member(self, notebook_id, user_id):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("sweep failed")
            return 0

    sharing.bind_member_memory_purge(SecondPassFails())
    monkeypatch.setattr(sharing._store, "is_member", lambda nb, user: True)
    monkeypatch.setattr(
        sharing._store, "remove_member", lambda nb, user: removed.append(user)
    )
    sharing.remove_member("nb-1", "user-a")
    assert removed == ["user-a"]


def test_exit_purge_refuses_to_spin_on_an_undeletable_page(repo, monkeypatch):
    service = repo._runtime.memory_service
    monkeypatch.setattr(
        service.store, "exit_memory_ids", lambda nb, user, *, limit: ["mem-stuck"]
    )
    monkeypatch.setattr(service.store, "bulk_delete_memories", lambda user, ids: 0)
    with pytest.raises(RuntimeError, match="no progress"):
        service.purge_exiting_member("nb-1", "user-a")
