import sqlite3

import pytest
from app.core.config import Settings
from app.models.schemas import AskResponse, NotebookCreate
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

def _answer_born_memory(repo, service, nb, user, question="Q?", answer="Grounded."):
    """一条 ask_answer 出身、source_answer_id 非空的 confirmed memory。

    这是 source_answer_id=None 那条不变量唯一有意义的夹具：create_candidate
    造出来的 memory 该字段本来就是 None，用它做断言恒真、测不出任何回归。
    """
    service.embedding_scheduler = lambda fn, job: fn(job)
    service.kg_ingest_scheduler = lambda fn, key: None
    answer_id = repo._runtime.ask_state.save_answer(
        nb, None, question, AskResponse(conclusion=answer, answer=answer), user.id
    )
    return service.create_from_answer(
        nb, user.id, answer_id, "AT", "AB", [], extract_kg=False
    )

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
    assert results[0]["status"] == "copied"
    assert results[0]["error"] is None

def test_move_memory_deletes_source(repo, alice):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)
    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)
    assert results[0]["ok"] is True
    assert results[0]["status"] == "moved"
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
    assert results[0]["status"] == "failed"


# --- Amendment 2: cleanup-failure-after-commit must be reported per-item, not
# escape the whole batch. The copy commits inside create_copy_with_initial_
# revision BEFORE the source-removal step below ever runs, so an unexpected
# failure there (e.g. a busy-timeout OperationalError) must not look like "the
# copy never happened": the caller needs the new_id to know a duplicate now
# exists, status="copied_source_not_removed" to know the source was NOT cleaned
# up, and the source memory itself must still be reachable (duplicate-not-loss).
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
    assert result["status"] == "copied_source_not_removed"
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


# --- Amendment 2 (widened): the post-commit wrap must also cover the
# best-effort derived work — _event / _maybe_schedule_kg / _schedule_embed —
# not just the move-only source removal. Every test above passes
# extract_kg=False, which short-circuits _maybe_schedule_kg before it ever
# touches memory_kg; extract_kg=True is the DEFAULT and is what the REST route
# will pass, and on that path memory_kg_eligible does two unguarded DB reads.
# A failure there used to escape transfer() entirely (batch returns None, the
# already-committed copy stranded in the target with nobody told about it).
def test_kg_eligibility_failure_after_commit_is_reported_not_raised(
    repo, alice, monkeypatch
):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service.memory_kg, "memory_kg_eligible", _boom)

    # extract_kg=True (the default) — this is the path the REST route uses.
    results = repo.transfer_memories(alice.id, [mem.id], dst, "copy", extract_kg=True)

    # 整批仍然返回（此前是异常直接冒出 transfer()，调用方拿到 None）
    assert len(results) == 1
    result = results[0]
    # 派生工作失败不该让一次已提交的复制变成失败；副本完整可用
    assert result["ok"] is True
    assert result["status"] == "copied"
    assert result["new_id"] is not None
    assert service.get(result["new_id"], alice.id).notebook_id == dst


def test_move_kg_eligibility_failure_keeps_source_and_reports_copy(
    repo, alice, monkeypatch
):
    """同一个故障点，move 模式：收尾在删源**之前**就炸了 → 源必须还在，且
    这条结果必须诚实报成 copied_source_not_removed 并带上副本 id。"""
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service.memory_kg, "memory_kg_eligible", _boom)

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=True)

    result = results[0]
    assert result["ok"] is False
    assert result["status"] == "copied_source_not_removed"
    assert result["new_id"] is not None
    assert service.get(result["new_id"], alice.id).notebook_id == dst
    # 源没被删（收尾在 remove/delete 之前就中断了）——重复而非丢失
    assert service.get(mem.id, alice.id).notebook_id == src


def test_batch_survives_post_commit_failure_on_a_later_item(repo, alice, monkeypatch):
    """两条一批、第二条收尾炸掉：此前异常冒泡会把**第一条已经完成的 move**
    （源已删）连同结果一起丢掉，调用方完全不知道它发生过。"""
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    first = _confirmed_memory(service, src, alice, title="one")
    second = _confirmed_memory(service, src, alice, title="two")

    def _boom_on_second(memory_id, *_args, **_kwargs):
        if memory_id == second.id:
            raise sqlite3.OperationalError("database is locked")
        return None

    monkeypatch.setattr(service.memory_kg, "remove_memory_source", _boom_on_second)

    results = repo.transfer_memories(
        alice.id, [first.id, second.id], dst, "move", extract_kg=False
    )

    # 两条都要有结果——第一条的成功不能被第二条的失败抹掉
    assert len(results) == 2
    assert results[0]["source_id"] == first.id
    assert results[0]["ok"] is True and results[0]["status"] == "moved"
    assert results[1]["source_id"] == second.id
    assert results[1]["ok"] is False
    assert results[1]["status"] == "copied_source_not_removed"
    assert results[1]["new_id"] is not None
    # 第一条确实搬走了；第二条的源还在
    with pytest.raises(KeyError):
        service.get(first.id, alice.id)
    assert service.get(second.id, alice.id).notebook_id == src


# --- source_answer_id=None is a data-loss guard, so it must be tested with a
# memory that actually HAS a source_answer_id. idx_memory_answer_once is a
# partial UNIQUE on (created_by, source_answer_id): preserving the field would
# make the copy's INSERT collide, _insert_memory_on would return the EXISTING
# source row, create_copy_with_initial_revision would short-circuit on
# item.id != write.id, and `copied` would BE the source — which mode="move"
# then deletes. ask_answer-born memories are created confirmed, so they sail
# straight through the transferable gate. Building the source via
# create_candidate (source_answer_id defaults to None) makes the assertion
# vacuous — it passes identically with or without the guard.
def test_answer_born_memory_copy_drops_source_answer_id(repo, alice):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _answer_born_memory(repo, service, src, alice)
    assert mem.source_answer_id is not None  # 夹具前提：这条真的带 answer id

    results = repo.transfer_memories(alice.id, [mem.id], dst, "copy", extract_kg=False)

    assert results[0]["ok"] is True
    assert results[0]["status"] == "copied"
    new_id = results[0]["new_id"]
    assert new_id != mem.id  # 没有退化成"返回源自己"
    copied = service.get(new_id, alice.id)
    assert copied.source_answer_id is None
    assert copied.notebook_id == dst
    # 源分毫未动、仍带着自己的 answer id
    still_there = service.get(mem.id, alice.id)
    assert still_there.source_answer_id == mem.source_answer_id
    assert still_there.notebook_id == src


def test_answer_born_memory_move_does_not_destroy_the_memory(repo, alice):
    """保留 source_answer_id 的回归会在这里变成**数据丢失**：copied 退化成源
    自身后，move 的删源步骤删的就是唯一那条 memory，两个 notebook 都没有了。"""
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _answer_born_memory(repo, service, src, alice)

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    assert results[0]["ok"] is True and results[0]["status"] == "moved"
    new_id = results[0]["new_id"]
    assert new_id != mem.id
    # 内容确实活在目标 notebook 里（而不是随着源一起被删掉）
    moved = service.get(new_id, alice.id)
    assert moved.notebook_id == dst
    assert moved.source_answer_id is None
    assert moved.title == mem.title and moved.content_md == mem.content_md


# --- source provenance must be preserved, but nested under imported_from:
# an ask_answer-born memory's answer_id/question/citations/evidence_level are
# the evidence that justified confirming it. Replacing provenance wholesale
# leaves a status="confirmed" memory carrying none of its own justification —
# and on move that loss is permanent, since the source row is deleted. It must
# NOT be spliced in at top level: the anchors/citations inside reference the
# SOURCE notebook's rows and do not resolve in the target.
def test_copy_preserves_source_provenance_nested_under_imported_from(repo, alice):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _answer_born_memory(repo, service, src, alice, question="Why stable?")
    assert mem.provenance["answer_id"]  # 夹具前提：源确实带着证据

    results = repo.transfer_memories(alice.id, [mem.id], dst, "copy", extract_kg=False)
    copied = service.get(results[0]["new_id"], alice.id)

    imported = copied.provenance["imported_from"]
    assert imported["memory_id"] == mem.id
    assert imported["notebook_id"] == src
    assert imported["action"] == "copy"
    # 原样留档
    assert imported["source_provenance"] == mem.provenance
    assert imported["source_provenance"]["question"] == "Why stable?"
    # 且**没有**铺到顶层——顶层只有 imported_from，消费方不会把跨库的
    # anchors/citations 当成本 notebook 的活引用
    assert set(copied.provenance) == {"imported_from"}
    assert "answer_id" not in copied.provenance
    assert "citations" not in copied.provenance


# --- structural backstop for the same data-loss chain: if the store's insert
# ever hits a unique-key collision it returns the EXISTING row instead of a new
# one, so `copied` could be the source itself — and move would then delete the
# only copy of the data while reporting success. Forcing source_answer_id=None
# closes the one known trigger (idx_memory_answer_once); this asserts the
# generic backstop, i.e. that a non-fresh id aborts the item BEFORE any delete.
def test_copy_that_returns_existing_row_aborts_without_deleting_source(
    repo, alice, monkeypatch
):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)

    # 模拟"撞唯一键 → 返回已存在的那一行（就是源自己）"
    monkeypatch.setattr(
        service.store, "create_copy_with_initial_revision",
        lambda write, source_memory_id, changed_by, reason: mem,
    )
    deleted = []
    monkeypatch.setattr(
        service.store, "delete_memory",
        lambda memory_id, user_id: deleted.append(memory_id),
    )

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    assert results[0]["ok"] is False
    assert results[0]["status"] == "failed"
    assert results[0]["new_id"] is None
    # 最要紧的一条：删源那步**根本没执行**
    assert deleted == []
    assert service.get(mem.id, alice.id).notebook_id == src


# --- Final-fix-wave Important 2: the OUTER per-item except (guarding the
# pre-commit portion of the loop: read source / validate / write copy) was
# narrowed to (KeyError, ValueError) — everything the post-commit block's own
# comment already argues for ("an escape here would drop the results of the
# whole batch, including earlier items whose sources were already deleted by
# move") applies just as much to this half. A realistic
# sqlite3.OperationalError("database is locked") from
# create_copy_with_initial_revision (BEGIN IMMEDIATE under contention with a
# background KG/embed job) used to propagate straight out of transfer()
# entirely — not just failing item 2, but discarding item 1's already-
# committed, already-source-deleted move. Appended at EOF.
def test_batch_survives_pre_commit_failure_on_a_later_item(repo, alice, monkeypatch):
    """两条一批、第二条在**写副本这一步**（COMMIT 之前）炸掉：此前这类异常不
    在 (KeyError, ValueError) 范围内，会直接冒出 transfer() 本身，把第一条
    已经完成的 move（源已删）连同结果一起丢掉，调用方拿不到任何 results。"""
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    first = _confirmed_memory(service, src, alice, title="one")
    second = _confirmed_memory(service, src, alice, title="two")

    orig_create = service.store.create_copy_with_initial_revision

    def _boom_on_second(write, source_memory_id, changed_by, reason):
        if source_memory_id == second.id:
            raise sqlite3.OperationalError("database is locked")
        return orig_create(write, source_memory_id, changed_by, reason)

    monkeypatch.setattr(service.store, "create_copy_with_initial_revision", _boom_on_second)

    # Before the fix this raised sqlite3.OperationalError out of transfer()
    # itself instead of returning a per-item result list.
    results = repo.transfer_memories(
        alice.id, [first.id, second.id], dst, "move", extract_kg=False
    )

    # 两条都要有结果——第一条（源已删的 move）不能被第二条的写入失败连累丢失
    assert len(results) == 2
    assert results[0]["source_id"] == first.id
    assert results[0]["ok"] is True and results[0]["status"] == "moved"
    assert results[1]["source_id"] == second.id
    assert results[1]["ok"] is False
    assert results[1]["status"] == "failed"
    # 第二条从未提交过副本——不像 post-commit 失败那样带 new_id
    assert results[1]["new_id"] is None
    assert "database is locked" in results[1]["error"]
    # 第一条确实搬走了；第二条的源没被碰过（这一条从未成功写副本，更不会删源）
    with pytest.raises(KeyError):
        service.get(first.id, alice.id)
    assert service.get(second.id, alice.id).notebook_id == src
