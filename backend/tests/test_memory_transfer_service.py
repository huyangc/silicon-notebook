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
    # round 8 P2-A 回归闸：只有 promotion_proposed 那一条具体拒绝才带 error_
    # code——这是个普通校验失败（plain ValueError），必须诚实留 None，不是
    # 随手挂个占位 code。
    assert results[0]["error_code"] is None


# --- Amendment 2: cleanup-failure-after-commit must be reported per-item, not
# escape the whole batch. The copy commits inside create_copy_with_initial_
# revision BEFORE the source-removal step below ever runs, so an unexpected
# failure there (e.g. a busy-timeout OperationalError) must not look like "the
# copy never happened": the caller needs the new_id to know a duplicate now
# exists, status="copied_source_not_removed" to know the source was NOT cleaned
# up, and the source memory itself must still be reachable (duplicate-not-loss).
# PR review round 3 P1-2: fault-injection target moved from `delete_memory`
# (unconditional) to `delete_memory_if_unchanged` (the new atomic conditional
# delete) — move's cleanup no longer calls the former at all.
def test_move_cleanup_failure_reports_per_item_and_keeps_copy(repo, alice, monkeypatch):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service.store, "delete_memory_if_unchanged", _boom)

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    assert len(results) == 1
    result = results[0]
    assert result["ok"] is False
    assert result["new_id"] is not None
    assert result["status"] == "copied_source_not_removed"
    assert "database is locked" in result["error"]
    # round 10 P1-B：delete_memory_if_unchanged 本身抛异常（不是返回 False）
    # ——源确实原封未动，error_code 必须是 "cleanup_error"（弃用/删除都安全），
    # 不能被误判成 "source_changed"。
    assert result["error_code"] == "cleanup_error"

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
# PR review round 3 P1-2: tracked call renamed from `delete_memory` to
# `delete_memory_if_unchanged` — move's cleanup delete goes through the new
# atomic conditional method now; the ordering guarantee itself (remove
# BEFORE delete) is unchanged.
# PR review round 6 P1-C: a THIRD call joins the sequence on the successful
# path — a second, idempotent `remove_memory_source` right after the atomic
# delete confirms it actually removed the row (insurance against a source
# recreated by an in-flight _kg_ingest_job racing the window between the
# first removal and the delete — see test_move_cleans_up_source_recreated_
# between_removal_and_delete below for the full scenario). The ordering
# guarantee this test exists to pin is unchanged (remove BEFORE delete); it
# now also pins that the extra safety-net removal runs AFTER, not instead of
# or before, the delete.
def test_move_removes_kg_source_before_and_after_deleting_memory_row(repo, alice, monkeypatch):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)

    call_order = []
    orig_remove = service.memory_kg.remove_memory_source
    orig_delete = service.store.delete_memory_if_unchanged

    def _remove(memory_id):
        call_order.append("remove_memory_source")
        return orig_remove(memory_id)

    def _delete(memory_id, user_id, expected_revision):
        call_order.append("delete_memory_if_unchanged")
        return orig_delete(memory_id, user_id, expected_revision)

    monkeypatch.setattr(service.memory_kg, "remove_memory_source", _remove)
    monkeypatch.setattr(service.store, "delete_memory_if_unchanged", _delete)

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    assert results[0]["ok"] is True
    assert call_order == [
        "remove_memory_source", "delete_memory_if_unchanged", "remove_memory_source",
    ]


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
    # round 10 P1-B：故障点在 memory_kg_eligible——远早于原子删除，源确实
    # 原封未动，error_code 必须是 "cleanup_error"。
    assert result["error_code"] == "cleanup_error"
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
    # round 10 P1-B：故障点是 remove_memory_source 抛异常，远早于原子删除
    # ——第二条的源确实原封未动，error_code 必须是 "cleanup_error"。
    assert results[1]["error_code"] == "cleanup_error"
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
        lambda write, source_memory_id, changed_by, reason, expected_source_revision: mem,
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

    def _boom_on_second(write, source_memory_id, changed_by, reason, expected_source_revision):
        if source_memory_id == second.id:
            raise sqlite3.OperationalError("database is locked")
        return orig_create(write, source_memory_id, changed_by, reason, expected_source_revision)

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


# --- PR review round 2 P1-3 (data loss): source is read ONCE at the top of
# transfer()'s per-item loop and baked into the copy; the cleanup delete at
# the end was unconditional. An edit (or a status transition like deprecate)
# landing in between destroys the newer state — the target keeps the stale
# title/content/status snapshot from read-time, and the concurrent change is
# gone forever once the source row is deleted. The interleaving here injects
# the edit via create_copy_with_initial_revision itself (monkeypatched to
# call service.update on the SAME memory right after the real copy commits —
# i.e. after the target's snapshot is already fixed in the target notebook,
# and before transfer()'s cleanup section runs): the earliest a concurrent
# writer could land relative to transfer()'s own call sequence, mirroring
# how the sibling knowhow-table race test hooks copy_table.
def test_move_does_not_delete_source_edited_after_copy_commits(repo, alice, monkeypatch):
    from app.models.schemas import MemoryUpdate

    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice, title="Original", content="Original body")

    real_create_copy = service.store.create_copy_with_initial_revision

    def _create_copy_then_concurrent_edit(
        write, source_memory_id, changed_by, reason, expected_source_revision
    ):
        copied = real_create_copy(
            write, source_memory_id, changed_by, reason, expected_source_revision
        )
        # 模拟并发写者：就在副本提交之后、transfer() 走到自己的收尾删源之前，
        # 另一个请求编辑了源 memory 的标题和正文。
        service.update(
            source_memory_id, alice.id,
            MemoryUpdate(title="Edited concurrently", content_md="New body"),
        )
        return copied

    monkeypatch.setattr(
        service.store, "create_copy_with_initial_revision", _create_copy_then_concurrent_edit
    )

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    result = results[0]
    assert result["ok"] is False
    assert result["status"] == "copied_source_not_removed"
    assert result["new_id"] is not None
    # round 10 P1-B：delete_memory_if_unchanged 返回 False（revision 复核未
    # 命中）——源是被有意保留以保护这份并发编辑的，error_code 必须是
    # "source_changed"（绝不能引导用户删除源）。
    assert result["error_code"] == "source_changed"

    # 源没被删，且带着并发编辑后的新内容——不是编辑前的旧值
    still_there = service.get(mem.id, alice.id)
    assert still_there.notebook_id == src
    assert still_there.title == "Edited concurrently"
    assert still_there.content_md == "New body"

    # 目标侧副本仍然存在（拷贝已提交，不因收尾中止而消失），但停在拷贝那一刻
    # 的旧快照——这个 guard 只挡「删」，不让复制本身变成实时同步，这是已知、
    # 可接受的局限（见报告）。
    copied = service.get(result["new_id"], alice.id)
    assert copied.notebook_id == dst
    assert copied.title == "Original"
    assert copied.content_md == "Original body"


def test_move_does_not_delete_source_deprecated_after_copy_commits(repo, alice, monkeypatch):
    """Status-only variant of the race above: title/content_md are untouched,
    only status changes (confirmed -> deprecated) — pins that the guard
    compares status too, not just content fields."""
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice, title="T", content="B")

    real_create_copy = service.store.create_copy_with_initial_revision

    def _create_copy_then_concurrent_deprecate(
        write, source_memory_id, changed_by, reason, expected_source_revision
    ):
        copied = real_create_copy(
            write, source_memory_id, changed_by, reason, expected_source_revision
        )
        service.deprecate(source_memory_id, alice.id)
        return copied

    monkeypatch.setattr(
        service.store, "create_copy_with_initial_revision",
        _create_copy_then_concurrent_deprecate,
    )

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    result = results[0]
    assert result["ok"] is False
    assert result["status"] == "copied_source_not_removed"
    assert result["new_id"] is not None
    # round 10 P1-B：同上一条用例，delete_memory_if_unchanged 返回 False——
    # error_code 必须是 "source_changed"。
    assert result["error_code"] == "source_changed"

    # 源没被删，且带着并发那次 deprecate 之后的新状态
    still_there = service.get(mem.id, alice.id)
    assert still_there.notebook_id == src
    assert still_there.status == "deprecated"


# --- PR review round 3 P1-2 (still a race, even with round 2's re-check):
# round 2 added an `embedding_revision` compare right before the cleanup
# section, but that compare and the eventual `self.store.delete_memory` were
# two INDEPENDENT statements with `remove_memory_source` (a real DB write)
# running in between them — check-then-act, not atomic. An edit landing in
# THAT specific gap (after the compare reads "unchanged", before the row is
# actually deleted) sails straight past round 2's guard: the compare already
# passed using the pre-edit snapshot, and the unconditional delete then
# removes the row — with the concurrent edit gone along with it, and no
# exception at all: `ok=True, status="moved"`, silently reporting success
# while destroying the newer content. This is a DIFFERENT bug from round 2's
# P1-3 (which was about the gap between reading `source` at the top of the
# loop and create_copy_with_initial_revision's commit) — this one is
# specifically about the SECOND check not being atomic with the delete that
# follows it. The interleaving is injected via a stub replacing
# `service.memory_kg.remove_memory_source` — called between the compare and
# the delete — which performs the concurrent edit before returning (skipping
# the real KG-source removal entirely, mirroring the sibling knowhow race
# test's teardown-stub shape in test_knowhow_transfer_service.py): the
# earliest realistic point in transfer()'s own call sequence after the
# compare has already passed.
def test_move_does_not_delete_source_edited_during_kg_source_removal(
    repo, alice, monkeypatch
):
    from app.models.schemas import MemoryUpdate

    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice, title="Original", content="Original body")

    def _concurrent_edit_during_kg_removal(memory_id):
        # 模拟并发写者恰好卡在「复核通过」和「删源行」之间的窗口——故意不调用
        # 真实的 remove_memory_source（不是这条用例要测的东西，同款先例见
        # test_knowhow_transfer_service.py 的 _ConcurrentEditDuringTeardown），
        # 只做并发编辑这一件事，正常返回。
        service.update(
            memory_id, alice.id,
            MemoryUpdate(title="Edited concurrently", content_md="New body"),
        )

    monkeypatch.setattr(
        service.memory_kg, "remove_memory_source", _concurrent_edit_during_kg_removal
    )

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    result = results[0]
    assert result["ok"] is False
    assert result["status"] == "copied_source_not_removed"
    assert result["new_id"] is not None
    assert result["error_code"] == "source_changed"  # round 10 P1-B

    # 源没被删，且带着并发编辑后的新内容——不是复核那一刻的旧快照
    still_there = service.get(mem.id, alice.id)
    assert still_there.notebook_id == src
    assert still_there.title == "Edited concurrently"
    assert still_there.content_md == "New body"

    # 目标侧副本仍然存在（拷贝已提交，不因收尾中止而消失），但停在拷贝那一刻
    # 的旧快照
    copied = service.get(result["new_id"], alice.id)
    assert copied.notebook_id == dst
    assert copied.title == "Original"
    assert copied.content_md == "Original body"


# Status-only variant of the race above: title/content_md are untouched, only
# status changes (confirmed -> deprecated) — pins that delete_memory_if_
# unchanged's `status='confirmed'` predicate (not just the revision number)
# actually pulls its weight, mirroring the sibling round-2 deprecate test
# above but injected in the tighter round-3 window.
def test_move_does_not_delete_source_deprecated_during_kg_source_removal(
    repo, alice, monkeypatch
):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice, title="T", content="B")

    def _concurrent_deprecate_during_kg_removal(memory_id):
        # round 10 P1-B 修复（不是 mock 手法演进，是这条用例自己一直带着的
        # 一个假阳性回收口）：直接调 store 的状态流转原语，不经过 service.
        # deprecate()——deprecate() 自己也会调 self.memory_kg.
        # remove_memory_source（正是这里被 monkeypatch 的同一个属性），经它
        # 调用会递归重入这个桩本身，对刚被上一次调用转成 deprecated 的行再
        # 转一次状态，撞上 transition_with_revision 自己的"非法状态迁移"校验
        # 炸出一个和这条竞态无关的 ValueError（`invalid memory transition:
        # deprecated -> deprecated`）——这是 mock 手法的人为产物，不是这条
        # 竞态本身要模拟的东西：真实并发的 deprecate 是另一个请求/线程独立
        # 调用，不会嵌套进这次 remove_memory_source 调用本身。只重放
        # deprecate() 真正会影响 delete_memory_if_unchanged 判据的那部分效果
        # （状态+revision），不重放它自己的 KG 收尾调用，才不会把这条用例的
        # error_code 断言意外地测成"清理本身炸了"（round 10 之前，reason 这
        # 个字段不存在，这个假阳性一直被悄悄吞掉、测不出来）。
        service.store.transition_with_revision(
            memory_id, alice.id, {"confirmed"}, "deprecated",
            fields=None, changed_by=alice.id, reason="deprecated",
        )

    monkeypatch.setattr(
        service.memory_kg, "remove_memory_source", _concurrent_deprecate_during_kg_removal
    )

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    result = results[0]
    assert result["ok"] is False
    assert result["status"] == "copied_source_not_removed"
    assert result["new_id"] is not None
    assert result["error_code"] == "source_changed"  # round 10 P1-B

    # 源没被删，且带着并发那次 deprecate 之后的新状态
    still_there = service.get(mem.id, alice.id)
    assert still_there.notebook_id == src
    assert still_there.status == "deprecated"


def test_move_deletes_source_when_untouched_during_kg_source_removal(repo, alice):
    """Companion happy-path guard: the atomic conditional delete must not turn
    every ordinary, uncontended move into a false-positive
    copied_source_not_removed."""
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice, title="T", content="B")

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    assert results[0]["ok"] is True
    assert results[0]["status"] == "moved"
    with pytest.raises(KeyError):
        service.get(mem.id, alice.id)


# --- PR review round 4 P2-1: remove_memory_source (inside the try block
# above) already tears down the source's KG projection BEFORE the atomic
# delete_memory_if_unchanged retain-check runs. If a concurrent update()
# lands in that exact window, two things are true at once: (a) the atomic
# delete correctly RETAINS the memory (its revision changed under it), and
# (b) the concurrent update() itself — which gates its own re-ingest on
# `memory_source_id(item.id) is not None` — sees None (remove_memory_source
# just cleared it) and skips its own re-ingest. Neither side re-adds it: the
# confirmed memory is retained but permanently missing from KG until a
# manual action.
#
# A local `_KgStub` (duplicated from tests/test_memory_kg_lifecycle.py's own
# stub of the same shape, not cross-imported — this suite's own established
# no-cross-test-file-coupling convention) stands in for the real
# SourceIngestionService: it lets the assertions observe "is this memory's
# derived KG source present or not" directly and deterministically, with no
# LLM/embedder configuration dependency (ingest_memory_source's real
# implementation would still work without one — failures there are caught
# and turned into a 'failed'-status source row, which the real memory_
# source_id would still find — but the stub is faster, and isolates this
# test from unrelated ingestion-pipeline behavior).
#
# Injected via `service.store.delete_memory_if_unchanged` (NOT `service.
# memory_kg.remove_memory_source`, unlike the sibling round-3 races above in
# this file): both `update()` and `deprecate()` call `self.memory_kg.
# remove_memory_source` internally too, so monkeypatching that attribute
# directly would make the injected concurrent op recursively re-enter the
# very stub that invoked it. `delete_memory_if_unchanged` is called nowhere
# else in this window, so wrapping IT — performing the concurrent op first,
# then delegating to the real implementation — lands the edit in the
# identical real-world window (remove_memory_source has ALREADY run for real
# by the time delete_memory_if_unchanged is ever called) without that
# hazard.
class _KgStub:
    """Duck-typed stand-in for SourceIngestionService's four memory-KG
    primitives (mirrors tests/test_memory_kg_lifecycle.py's own _KgStub)."""

    def __init__(self, eligible: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self._eligible = eligible
        self._sources: dict[str, str] = {}

    def memory_kg_eligible(self, notebook_id: str) -> bool:
        return self._eligible

    def memory_source_id(self, memory_id: str) -> "str | None":
        return self._sources.get(memory_id)

    def ingest_memory_source(self, notebook_id, memory_id, title, content_md) -> str:
        self.calls.append(("ingest", memory_id))
        self._sources[memory_id] = f"src-{memory_id}"
        return self._sources[memory_id]

    def remove_memory_source(self, memory_id: str) -> None:
        self.calls.append(("remove", memory_id))
        self._sources.pop(memory_id, None)


def test_move_reingests_kg_source_when_edit_lands_after_source_removed(
    repo, alice, monkeypatch
):
    from app.models.schemas import MemoryUpdate

    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")

    kg = _KgStub(eligible=True)
    service.set_memory_kg_service(kg)
    service.kg_ingest_scheduler = lambda fn, key: fn(key)  # 同步：直接观察重抽
    service.embedding_scheduler = lambda fn, job: fn(job)

    cand = service.create_candidate(
        src, alice.id, None, "req-p21", "Original", "Original body", [], "task"
    )
    mem = service.confirm(cand.id, alice.id)
    assert kg.memory_source_id(mem.id) is not None, "前置条件：源确实有派生 KG 源"
    kg.calls.clear()

    real_delete = service.store.delete_memory_if_unchanged

    def _concurrent_edit_then_delete(memory_id, user_id, expected_revision):
        # remove_memory_source（真实、未打桩）此刻已经跑完——KG 源已经真的
        # 被删了。并发编辑恰好卡在这里：它自己执行时会去查
        # memory_source_id(...)，此刻已经是 None，于是它自己也会放弃重抽
        # ——这正是 bug 的前提条件，不是这条测试要验证的东西本身。
        service.update(
            memory_id, alice.id,
            MemoryUpdate(title="Edited concurrently", content_md="New body"),
        )
        return real_delete(memory_id, user_id, expected_revision)

    monkeypatch.setattr(
        service.store, "delete_memory_if_unchanged", _concurrent_edit_then_delete
    )

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    result = results[0]
    assert result["ok"] is False
    assert result["status"] == "copied_source_not_removed"
    # round 10 P1-B：delete_memory_if_unchanged 返回 False——error_code 必须
    # 是 "source_changed"。
    assert result["error_code"] == "source_changed"

    # 源没被删，且带着并发编辑后的新内容——保留分支本身的既有契约。
    still_there = service.get(mem.id, alice.id)
    assert still_there.notebook_id == src
    assert still_there.title == "Edited concurrently"

    # 这条测试要钉的新东西：KG 源必须被重新排队，不是永久缺失。
    assert kg.memory_source_id(mem.id) is not None, (
        "并发编辑保留的源必须重新排入 KG 抽取——remove_memory_source 已经把"
        "旧派生源删了，并发 update() 自己因为 memory_source_id 是 None 而"
        "跳过了重抽，必须靠 transfer() 的保留分支兜底重新排队，否则这条"
        "confirmed memory 永久从 KG 里消失"
    )


def test_move_does_not_reingest_kg_source_when_deprecated_after_source_removed(
    repo, alice, monkeypatch
):
    """Status-only companion: a concurrent DEPRECATE in the same window is a
    deliberate decision to remove the source from KG — the retain branch's
    re-schedule must not fight that decision and re-add it. (This is a
    regression guard against an overcorrected fix, not a RED-before-fix
    case: the unfixed code never re-schedules anything at all, so
    memory_source_id staying None here is trivially true both before and
    after the P2-1 fix — mirrors this file's own existing "Companion
    happy-path guard" tests, e.g. test_move_deletes_source_when_untouched_
    during_kg_source_removal above.)"""
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")

    kg = _KgStub(eligible=True)
    service.set_memory_kg_service(kg)
    service.kg_ingest_scheduler = lambda fn, key: fn(key)
    service.embedding_scheduler = lambda fn, job: fn(job)

    cand = service.create_candidate(
        src, alice.id, None, "req-p21-dep", "T", "B", [], "task"
    )
    mem = service.confirm(cand.id, alice.id)
    assert kg.memory_source_id(mem.id) is not None, "前置条件：源确实有派生 KG 源"

    real_delete = service.store.delete_memory_if_unchanged

    def _concurrent_deprecate_then_delete(memory_id, user_id, expected_revision):
        service.deprecate(memory_id, alice.id)
        return real_delete(memory_id, user_id, expected_revision)

    monkeypatch.setattr(
        service.store, "delete_memory_if_unchanged", _concurrent_deprecate_then_delete
    )

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    result = results[0]
    assert result["status"] == "copied_source_not_removed"
    assert result["error_code"] == "source_changed"  # round 10 P1-B
    still_there = service.get(mem.id, alice.id)
    assert still_there.status == "deprecated"

    assert kg.memory_source_id(mem.id) is None, (
        "并发 deprecate 是刻意让源离开 KG 的决定——保留分支的重新排队不该"
        "把它顶回去"
    )


# --- PR review round 5 P1-2: a confirmed Memory with promotion_state==
# 'proposed' has a LIVE row in promotion_candidates (governance_store.py's
# insert_promotion_candidate writes object_id=item.id, object_type='memory'
# — confirmed by grepping knowledge_governance.py's propose_memory_
# promotion/_memory_promotion_payload). That table has NO FK onto
# memory_items (migrations.py's promotion_candidates DDL: `object_id TEXT
# NOT NULL`, no REFERENCES clause) — so mode="move" deleting the memory row
# leaves that promotion_candidates row (and the admin Track-F approval
# queue entry it renders as, via governance_store.promotion_queue_rows)
# pointing at nothing. The row doesn't just look stale: approving it later
# calls memory_store.promotion_data_on -> promotion_rows_on, which raises
# KeyError on the now-missing memory_id (memory_service.py's own
# propose_promotion docstring chain and knowledge_governance.py:1100's
# memory_by_id lookup both key off exactly this), or governance_store.
# validate_promotion_approval_access_on similarly assumes the memory row is
# still there — either way, approval fails for an admin who has no way to
# know why a queue entry with no payload is broken.
#
# State-name verification done before writing this guard (not assumed):
# memory_items.promotion_state's own CHECK constraint (migrations.py) only
# ever allows 'none'/'proposed'/'approved'/'rejected' — there is no
# 'under_review' value on the MEMORY row (grepped every write site:
# propose_promotion_on writes 'proposed', set_promotion_rejected-equivalent
# paths write 'none'/'rejected', approval writes 'approved'; nothing writes
# 'under_review' to memory_items). promotion_candidates.status DOES have a
# distinct 'under_review' value in its own state machine (its DDL comment
# and governance_store.promotion_queue_rows's default-queue filter both
# reference it), and active_promotion_for_object's "is there a LIVE
# candidate for this object" query treats status NOT IN ('approved',
# 'rejected') as live — i.e. BOTH 'proposed' and 'under_review' count as
# live on the promotion_candidates side. But since memory_items.
# promotion_state never itself takes the value 'under_review' (nothing
# writes it there), blocking on promotion_state=='proposed' already covers
# every live promotion_candidates state from the memory row's own point of
# view — there is no distinct memory-side state a 'under_review' candidate
# could be hiding behind that this guard would miss.
# 'approved' is deliberately NOT blocked: governance_store.
# approve_promotion_as_reviewer's own branch either finds-or-inserts a row
# in knowledge_objects (the base KG) and flips promotion_candidates.status
# to 'approved' — the base-KG object exists independently of the memory row
# from that point on (test_admin_approval_is_idempotent_and_keeps_memory_
# private in test_memory_promotion.py pins exactly this: base_object_ids
# survive fine on their own). Deleting the memory row after that doesn't
# orphan anything live in the approval queue.
def test_move_rejects_memory_with_active_promotion_proposal(repo, alice):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice, title="RC 补偿", content="补偿改善稳定性。")
    repo.propose_memory_promotion(mem.id, alice.id)

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    assert len(results) == 1
    result = results[0]
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["new_id"] is None
    assert "审批队列" in result["error"] and "移动" in result["error"]
    # round 8 P2-A：这条拒绝必须带上机器可判的 error_code，前端才能把它从
    # "N 条失败，请稍后重试"的通用兜底里摘出来，单独给可操作的提示（重试对
    # 这一条永远无效，得先去待确认中心处理提案）。
    assert result["error_code"] == "promotion_proposed"

    # 源 memory 分毫未动，仍在审批队列里等待处理
    still_there = service.get(mem.id, alice.id)
    assert still_there.notebook_id == src
    assert still_there.promotion_state == "proposed"
    with repo._connect() as db:
        candidate = db.execute(
            "SELECT status FROM promotion_candidates WHERE object_id=? AND object_type='memory'",
            (mem.id,),
        ).fetchone()
    assert candidate is not None and candidate["status"] == "proposed", (
        "promotion_candidates 那一行必须还在、还指向一个真实存在的 memory——"
        "move 若照旧删了源，这行会永久孤立在审批队列里，approve 时因 memory_id"
        "查不到而失败，且没有任何 UI 路径能清理它"
    )


def test_copy_allows_memory_with_active_promotion_proposal(repo, alice):
    """P1-2 只挡 move——copy 时源毫发无损，promotion_candidates 那行仍然对着
    源（没有被搬走/删除），继续正常可审批；副本本身是全新 memory，天然
    promotion_state='none'（provenance 走 imported_from，不复制审批状态）。"""
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice, title="RC 补偿", content="补偿改善稳定性。")
    repo.propose_memory_promotion(mem.id, alice.id)

    results = repo.transfer_memories(alice.id, [mem.id], dst, "copy", extract_kg=False)

    assert results[0]["ok"] is True
    assert results[0]["status"] == "copied"
    copied = service.get(results[0]["new_id"], alice.id)
    assert copied.notebook_id == dst
    assert copied.promotion_state == "none"
    # 源仍在，仍是 proposed（copy 完全不受这条新 guard 影响）
    still_there = service.get(mem.id, alice.id)
    assert still_there.notebook_id == src
    assert still_there.promotion_state == "proposed"


def test_move_allows_memory_with_approved_promotion(repo, alice):
    """'approved' 是终态：knowledge_objects 里已经有独立存在的 base KG 对象
    （不依赖这条 memory 行继续存在），不该被这条只为 'proposed' 设的新 guard
    误伤——否则一条早就批准过的 memory 会永久搬不走。"""
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    base = repo.create_notebook(NotebookCreate(name="Base corpus"))
    repo.mark_notebook_base(base.id)
    mem = _confirmed_memory(service, src, alice, title="RC 补偿", content="补偿改善稳定性。")
    proposal = repo.propose_memory_promotion(mem.id, alice.id)
    repo.approve_promotion(proposal["id"])
    approved = service.get(mem.id, alice.id)
    assert approved.promotion_state == "approved", "前置条件：批准确实生效"

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    assert results[0]["ok"] is True
    assert results[0]["status"] == "moved"
    with pytest.raises(KeyError):
        service.get(mem.id, alice.id)


def test_move_unaffected_for_memory_without_any_promotion(repo, alice):
    """伴生 happy-path 对照：一条从未提过审批（promotion_state='none'）的
    普通 memory，move 行为必须和这条新 guard 加入之前完全一样——防止这条
    guard 过度纠正、把所有 move 都拦下来。"""
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)
    assert mem.promotion_state == "none"

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    assert results[0]["ok"] is True
    assert results[0]["status"] == "moved"


# --- PR review round 6 P1-C: an in-flight _kg_ingest_job that finishes BETWEEN
# `self.memory_kg.remove_memory_source(source.id)` and `delete_memory_if_
# unchanged` recreates a derived source for a memory that is about to be
# deleted, and nothing cleans it up afterward. Window: the job's own post-
# ingest recheck (`_kg_ingest_job` in memory_service.py) reads the memory's
# CURRENT status — while this move is still between the two calls above, the
# memory row still exists and is still 'confirmed' (the atomic delete has not
# run yet), so the job's recheck sees `still_confirmed = True` and does NOT
# call its own cleanup `remove_memory_source`. The atomic delete then runs a
# moment later and succeeds completely normally (this is an ordinary,
# uncontested move — no concurrent EDIT of the memory itself, so revision
# still matches): the memory row is gone, but the freshly (re)created
# `sources` row (memory_id=source.id, plus whatever KG it produced) is now
# orphaned forever — nothing else in this codebase will ever call remove_
# memory_source for an id that no longer resolves to a live memory.
#
# Real background-job timing is unreliable to trigger deterministically in a
# test, so this injects the race directly: monkeypatch `service.store.
# delete_memory_if_unchanged` (the call immediately after the real, first
# `remove_memory_source` already ran) to insert a "recreated" derived source
# via the same production `SourceStore.insert_source` call `ingest_memory_
# source` itself uses — before delegating to the real delete. This lands the
# injected row in the exact real-world window: after the first removal has
# already run for real, before the delete commits.
def test_move_cleans_up_source_recreated_between_removal_and_delete(
    repo, alice, monkeypatch
):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice, title="T", content="B")
    assert service.memory_kg.memory_source_id(mem.id) is None, (
        "前置条件：_confirmed_memory 关闭了 kg_ingest_scheduler，此刻不该有派生源"
    )

    real_delete = service.store.delete_memory_if_unchanged

    def _recreate_source_then_delete(memory_id, user_id, expected_revision):
        # remove_memory_source（真实、未打桩）此刻已经跑完一次——这里模拟一个
        # 恰好在这个窗口里跑完 ingest 的并发 _kg_ingest_job：它此刻查到的
        # memory 仍是 confirmed（因为下面这次原子删除还没发生），所以它自己
        # 的收尾检查不会清理这条刚建好的源。落库形状照抄 source_ingestion.
        # ingest_memory_source 真实调用 SourceStore.insert_source 的方式。
        repo._runtime.source_store.insert_source(
            source_id=repo._runtime.seams.new_id("src"),
            notebook_id=src,
            title=mem.title,
            source_type="memory",
            status="active",
            parse_status="parsed",
            file_name="",
            file_path="",
            file_size=0,
            file_hash="fake-recreated-mid-window",
            summary="",
            doc_type="",
            memory_id=memory_id,
        )
        return real_delete(memory_id, user_id, expected_revision)

    monkeypatch.setattr(
        service.store, "delete_memory_if_unchanged", _recreate_source_then_delete
    )

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    result = results[0]
    assert result["ok"] is True
    assert result["status"] == "moved"
    with pytest.raises(KeyError):
        service.get(mem.id, alice.id)

    assert service.memory_kg.memory_source_id(mem.id) is None, (
        "窗口期内冒出来的重建派生源必须被收尾的第二次 remove_memory_source 清"
        "理掉——不能作为 sources.memory_id 指向一条已删 memory 的孤儿永久留"
        "在库里、继续参与检索"
    )


# --- PR review round 7 P1-A service-level companion to test_memory_
# transfer_store.py's own test_create_copy_after_edit_does_not_copy_stale_
# vector_as_ready: that test pins the STORE's gating logic directly; this
# one proves the SERVICE-level consequence the task explicitly calls out —
# transfer() only re-schedules an embed for the copy when `copied.
# embedding_status != "ready"` (memory_service.py). Before the store-level
# fix, a copy made from a source caught mid-edit (embedding_status=
# 'pending', its OLD vector still attached) was wrongly marked 'ready',
# which SKIPPED this very re-schedule — leaving the copy permanently stuck
# with a vector that doesn't match its own text, since nothing else in this
# codebase ever revisits a 'ready' Memory's embedding. This proves the fixed
# copy (embedding_status='pending') actually gets a fresh embed scheduled,
# not merely that it avoided attaching the wrong vector.
#
# embedding_scheduler is swapped to a RECORDING, NON-EXECUTING stand-in
# (records the job's item id, never calls the real _embed) — both to set up
# the repro deterministically (the edit below must leave embedding_status=
# 'pending' with the stale memory_embeddings row still in place; this
# suite's usual synchronous `lambda fn, job: fn(job)` scheduler would
# immediately re-embed and erase that exact window) and to observe whether
# transfer() schedules a SECOND job for the copy. Local `from app.models.
# schemas import MemoryUpdate` mirrors the same local-import convention this
# file's own test_move_does_not_delete_source_edited_after_copy_commits
# already uses. Appended at EOF, same zero-line-shift convention as every
# other addition in this file.
def test_move_schedules_embed_for_copy_when_source_vector_was_stale_at_transfer_time(
    repo, alice
):
    from app.models.schemas import MemoryUpdate

    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice, title="Original", content="Original body")
    assert service.get(mem.id, alice.id).embedding_status == "ready"

    scheduled: list[str] = []

    def _recording_scheduler(fn, job):
        scheduled.append(job.item.id)
        # 故意不调用 fn(job)——模拟"已排队、还没真正跑完"，这正是这条竞态要
        # 卡住的那个窗口：编辑已提交（embedding_status 翻回 pending），但重
        # 嵌入还没落地（memory_embeddings 里那行旧向量原样留着）。

    service.embedding_scheduler = _recording_scheduler
    service.update(mem.id, alice.id, MemoryUpdate(content_md="Edited body"))
    assert service.get(mem.id, alice.id).embedding_status == "pending", (
        "前置条件：编辑必须真的落在 pending 状态、旧向量原样留着，测试才有意义"
    )
    scheduled.clear()  # 只关心 transfer 自己触发的调度，不是上面编辑那次的

    results = repo.transfer_memories(alice.id, [mem.id], dst, "copy", extract_kg=False)

    assert results[0]["ok"] is True
    new_id = results[0]["new_id"]
    copied = service.get(new_id, alice.id)
    assert copied.embedding_status == "pending", (
        "源在传输那一刻 embedding_status 是 'pending'（编辑已提交、重嵌入还"
        "没跑完）——副本不该继承一个对不上自己文本的向量、更不该标 ready"
    )
    assert new_id in scheduled, (
        "transfer() 只在 copied.embedding_status != 'ready' 时才补调度 "
        "_schedule_embed——副本被误标 ready 的旧 bug 会让这次调度被跳过，"
        "从此没有任何东西替这条副本重新嵌入"
    )


# --- PR review round 8 P1-B: round 5's promotion-proposal guard (test_move_
# rejects_memory_with_active_promotion_proposal above) reads `source.
# promotion_state` at the LOOP's TOP, before create_copy_with_initial_
# revision even runs. A proposal landing AFTER that read but before the
# conditional delete can slip through it — and the exact window that lets
# it slip through is narrower and sneakier than "delete_memory_if_unchanged
# runs against a stale revision": governance_store's propose path (memory_
# store.propose_promotion_on) does BOTH an `UPDATE memory_items SET
# promotion_state='proposed'` AND an `_append_revision_on` call (a genuine
# new memory_revisions row — confirmed by reading propose_promotion_on
# directly, not assumed). If the proposal lands BEFORE `source_revision =
# self.store.embedding_revision(source.id, source)` captures its number
# (memory_service.py, a few lines below the pre-check), that capture reads
# the ALREADY-BUMPED post-proposal revision — the SAME number the atomic
# delete's WHERE clause checks a moment later. Both sides of the revision
# comparison agree, precisely BECAUSE the proposal bumped revision — the
# guard that looks like it should catch this (revision changed!) is the
# very thing a same-number match papers over. The memory disappears while
# the promotion_candidates row propose_memory_promotion just inserted for
# it is now orphaned — unapprovable (memory_store.promotion_rows_on's
# SELECT joins back to memory_items and finds nothing), permanently stuck
# in the admin Track-F queue.
#
# (Landing the proposal any LATER than this — e.g. right before delete_
# memory_if_unchanged itself, as this file's round-3 races above do for
# their own concurrent-edit scenarios — would make source_revision stale
# by the time of the proposal, so the OLD revision-only WHERE clause would
# already (correctly, if incidentally) reject the delete. That is not this
# bug: this test intercepts embedding_revision itself, one call earlier,
# so the proposal lands in the actual window the family history describes
# — before revision is captured, not after.)
def test_move_retains_memory_when_promotion_proposed_between_precheck_and_revision_capture(
    repo, alice, monkeypatch
):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice, title="RC 补偿", content="补偿改善稳定性。")
    assert mem.promotion_state == "none", (
        "前置条件：loop 顶部读到的快照必须是 'none'，round 5 的早退 pre-check "
        "才会放行——这条竞态测的正是 pre-check 放行之后才发生的并发提案"
    )

    real_embedding_revision = service.store.embedding_revision

    def _propose_then_capture_revision(memory_id, item):
        # pre-check 已经放行（它读到的 source.promotion_state 仍是
        # 'none'）。并发提案恰好卡在这里——embedding_revision 真正读出"这一
        # 刻的 revision"之前：propose_promotion_on 会同时①把 promotion_state
        # 翻成 'proposed' ②追加一条新的 memory_revisions 行。下面这次真实
        # embedding_revision 调用因此读到的已经是"提案之后"的新 revision
        # ——它和收尾那次原子删除比对用的是同一个数字，旧的 WHERE 子句（只
        # 查 revision 对不对得上）对这次并发提案完全失明：两边都对得上，
        # 删除照常成功。
        repo.propose_memory_promotion(memory_id, alice.id)
        return real_embedding_revision(memory_id, item)

    monkeypatch.setattr(
        service.store, "embedding_revision", _propose_then_capture_revision
    )

    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)

    assert len(results) == 1
    result = results[0]
    # GREEN：新 WHERE 子句（额外要求 promotion_state NOT IN ('proposed')）让
    # 这次 DELETE 匹配零行，退回既有的保留分支契约——不是新发明的结果形状。
    assert result["ok"] is False
    assert result["status"] == "copied_source_not_removed"
    assert result["new_id"] is not None
    # round 10 P1-B：delete_memory_if_unchanged 返回 False 是唯一的分类判据
    # （不深究"变了"具体是内容编辑还是这里的并发提案）——error_code 仍是
    # "source_changed"：源没有被清理操作本身搞坏，是原子删除的 WHERE 子句
    # 没匹配上，指引依旧是"别删源、去核对/重试"，不是"删除安全"。
    assert result["error_code"] == "source_changed"

    still_there = service.get(mem.id, alice.id)
    assert still_there.notebook_id == src
    assert still_there.promotion_state == "proposed"
    with repo._connect() as db:
        candidate = db.execute(
            "SELECT status FROM promotion_candidates WHERE object_id=? AND object_type='memory'",
            (mem.id,),
        ).fetchone()
    assert candidate is not None and candidate["status"] == "proposed", (
        "并发提案的 promotion_candidates 行必须还在、还指向一个真实存在的 "
        "memory——若源被删掉，这行会永久孤立在审批队列里，approve 时因 "
        "memory_id 查不到而失败，且没有任何 UI 路径能清理它"
    )
