import sqlite3

import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import (
    SQLiteRepository, set_request_user, reset_request_user,
)

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

@pytest.fixture
def alice(repo):
    return repo.create_user("a00123456", "pw")

@pytest.fixture
def bob(repo):
    return repo.create_user("b00654321", "pw")

def _nb(repo, user, name):
    tok = set_request_user(user)
    try:
        return repo.create_notebook(NotebookCreate(name=name)).id
    finally:
        reset_request_user(tok)

def _confirmed_memory(service, nb, user, title="T", content="B"):
    # 走 agent candidate → confirm，拿到一条 confirmed memory（不需 answer 夹具）
    service.embedding_scheduler = lambda fn, job: fn(job)
    service.kg_ingest_scheduler = lambda fn, key: None  # 关掉 KG 后台，测试聚焦复制
    cand = service.create_candidate(
        nb, user.id, None, f"req-{title}", title, content, [], "task"
    )
    return service.confirm(cand.id, user.id)

def test_copy_memory_into_target(repo, alice):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)
    results = repo.transfer_memories(alice.id, [mem.id], dst, "copy", extract_kg=False)
    assert len(results) == 1 and results[0]["ok"] is True
    new_id = results[0]["new_id"]
    copied = service.get(new_id, alice.id)
    assert copied.notebook_id == dst
    assert copied.source_answer_id is None
    assert copied.provenance["imported_from"]["memory_id"] == mem.id
    # 源仍在
    assert service.get(mem.id, alice.id).notebook_id == src
    # copy 模式下 source_deleted 恒为 False（不适用），且不影响既有 ok/error 语义
    assert results[0]["source_deleted"] is False
    assert results[0]["error"] is None

def test_move_memory_deletes_source(repo, alice):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)
    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)
    assert results[0]["ok"] is True
    assert results[0]["source_deleted"] is True
    with pytest.raises(KeyError):
        service.get(mem.id, alice.id)

def test_transfer_to_notebook_not_owned_rejected(repo, alice, bob):
    service = repo._runtime.memory_service
    src = _nb(repo, alice, "src")
    bob_nb = _nb(repo, bob, "bobs")
    mem = _confirmed_memory(service, src, alice)
    with pytest.raises(PermissionError):
        repo.transfer_memories(alice.id, [mem.id], bob_nb, "copy", extract_kg=False)

def test_non_confirmed_memory_not_transferable(repo, alice):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    service.embedding_scheduler = lambda fn, job: fn(job)
    service.kg_ingest_scheduler = lambda fn, key: None
    cand = service.create_candidate(src, alice.id, None, "req", "T", "B", [], "task")
    results = repo.transfer_memories(alice.id, [cand.id], dst, "copy", extract_kg=False)
    assert results[0]["ok"] is False and "confirmed" in results[0]["error"].lower()
    assert results[0]["new_id"] is None
    assert results[0]["source_deleted"] is False


# --- Amendment 2: cleanup-failure-after-commit must be reported per-item, not
# escape the whole batch. The copy commits inside create_copy_with_initial_
# revision BEFORE the source-removal step below ever runs, so an unexpected
# failure there (e.g. a busy-timeout OperationalError) must not look like "the
# copy never happened": the caller needs the new_id to know a duplicate now
# exists, source_deleted=False to know the source was NOT cleaned up, and the
# source memory itself must still be reachable (duplicate-not-loss).
def test_move_cleanup_failure_reports_per_item_and_keeps_copy(repo, alice, monkeypatch):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service.store, "delete_memory", _boom)

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    assert len(results) == 1
    result = results[0]
    assert result["ok"] is False
    assert result["new_id"] is not None
    assert result["source_deleted"] is False
    assert "database is locked" in result["error"]

    # 副本已经存在于目标 notebook——不会因为清理失败而丢失/静默重复创建
    copied = service.get(result["new_id"], alice.id)
    assert copied.notebook_id == dst
    # 源仍在（清理失败被诚实上报，而不是被悄悄吞掉后源已经没了）
    still_there = service.get(mem.id, alice.id)
    assert still_there.notebook_id == src


# --- Amendment 1 regression guard: the derived KG source must be removed
# BEFORE the memory row is deleted (sources.memory_id is not a foreign key,
# so nothing cascades — reversing this order would strand an unreachable
# derived source on a failure between the two calls).
def test_move_removes_kg_source_before_deleting_memory_row(repo, alice, monkeypatch):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)

    call_order = []
    orig_remove = service.memory_kg.remove_memory_source
    orig_delete = service.store.delete_memory

    def _remove(memory_id):
        call_order.append("remove_memory_source")
        return orig_remove(memory_id)

    def _delete(memory_id, user_id):
        call_order.append("delete_memory")
        return orig_delete(memory_id, user_id)

    monkeypatch.setattr(service.memory_kg, "remove_memory_source", _remove)
    monkeypatch.setattr(service.store, "delete_memory", _delete)

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    assert results[0]["ok"] is True
    assert call_order == ["remove_memory_source", "delete_memory"]
