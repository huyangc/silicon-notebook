import pytest
from app.core.config import Settings
from app.models.memory import MemoryWrite
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

def _nb(repo, user, name):
    tok = set_request_user(user)
    try:
        return repo.create_notebook(NotebookCreate(name=name)).id
    finally:
        reset_request_user(tok)

def test_create_copy_builds_four_tables_and_copies_vector(repo, alice):
    store = repo._runtime.memory_store
    src_nb, dst_nb = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    now = repo._runtime.seams.now()
    # 源用 create_candidate_with_initial_revision 建（会写 revision 1，replace_embedding 才认）
    src_write = MemoryWrite(
        id=repo._runtime.seams.new_id("memory"), notebook_id=src_nb,
        created_by=alice.id, origin="external_agent", status="candidate",
        title="T", content_md="B", tags=["x"], created_at=now, updated_at=now,
        provenance={"client_request_id": "r1"},
    )
    source = store.create_candidate_with_initial_revision(src_write, alice.id, "created")
    # 给源塞一条向量（revision 1 已存在）
    assert store.replace_embedding(source.id, 1, "TestModel", [0.1, 0.2, 0.3]) is True

    copy_write = MemoryWrite(
        id=repo._runtime.seams.new_id("memory"), notebook_id=dst_nb,
        created_by=alice.id, source_answer_id=None, origin="external_agent",
        status="confirmed", title="T", content_md="B", tags=["x"],
        created_at=now, updated_at=now, confirmed_by=alice.id, confirmed_at=now,
        provenance={"imported_from": {"notebook_id": src_nb, "memory_id": source.id, "action": "copy"}},
    )
    # expected_source_revision=1：与 replace_embedding 认的 revision 一致——
    # 源此刻确实是"就绪且对得上这个 revision"，round 7 P1-A 的新增判据必须
    # 放行这条已有的正常路径（回归闸，见该 round 的新增测试）。
    copied = store.create_copy_with_initial_revision(
        copy_write, source.id, alice.id, "copied", 1
    )

    assert copied.id == copy_write.id
    assert copied.notebook_id == dst_nb
    assert copied.provenance["imported_from"]["memory_id"] == source.id
    assert copied.embedding_status == "ready"
    with repo._connect() as db:
        vec = db.execute(
            "SELECT dimension FROM memory_embeddings WHERE memory_id=?", (copied.id,)
        ).fetchone()
        rev = db.execute(
            "SELECT COUNT(*) FROM memory_revisions WHERE memory_id=?", (copied.id,)
        ).fetchone()[0]
    assert vec["dimension"] == 3
    assert rev == 1

def test_create_copy_without_source_vector_stays_pending(repo, alice):
    """源无向量：不建 memory_embeddings 行、embedding_status 保持 schema 默认
    'pending'（等服务侧补嵌），但初始 revision 照写。钉住 `if vec is not None`
    这条分支——有人把它「简化」成无条件置 ready 必须炸。"""
    store = repo._runtime.memory_store
    src_nb, dst_nb = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    now = repo._runtime.seams.now()
    # 刻意不调 replace_embedding：源没有向量
    src_write = MemoryWrite(
        id=repo._runtime.seams.new_id("memory"), notebook_id=src_nb,
        created_by=alice.id, origin="external_agent", status="candidate",
        title="T", content_md="B", tags=["x"], created_at=now, updated_at=now,
        provenance={"client_request_id": "r1"},
    )
    source = store.create_candidate_with_initial_revision(src_write, alice.id, "created")
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id=?", (source.id,)
        ).fetchone()[0] == 0

    copy_write = MemoryWrite(
        id=repo._runtime.seams.new_id("memory"), notebook_id=dst_nb,
        created_by=alice.id, source_answer_id=None, origin="external_agent",
        status="confirmed", title="T", content_md="B", tags=["x"],
        created_at=now, updated_at=now, confirmed_by=alice.id, confirmed_at=now,
        provenance={"imported_from": {"notebook_id": src_nb, "memory_id": source.id, "action": "copy"}},
    )
    copied = store.create_copy_with_initial_revision(
        copy_write, source.id, alice.id, "copied", 1
    )

    assert copied.id == copy_write.id
    assert copied.embedding_status == "pending"
    with repo._connect() as db:
        vec_count = db.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id=?", (copied.id,)
        ).fetchone()[0]
        rev = db.execute(
            "SELECT COUNT(*) FROM memory_revisions WHERE memory_id=?", (copied.id,)
        ).fetchone()[0]
        status = db.execute(
            "SELECT embedding_status FROM memory_items WHERE id=?", (copied.id,)
        ).fetchone()["embedding_status"]
    assert vec_count == 0
    assert status == "pending"
    assert rev == 1


# --- PR review round 7 P1-A: create_copy_with_initial_revision used to copy
# "whatever memory_embeddings row currently exists for source_memory_id" and
# unconditionally mark the copy embedding_status='ready' if one did. But a
# source's memory_embeddings row can be STALE relative to its memory_items
# row: editing a confirmed memory's content flips embedding_status to
# 'pending' (_mutate_with_revision, since `values` is non-empty) but never
# touches/deletes the OLD memory_embeddings row — that only happens later,
# when a re-embed job calls replace_embedding. A transfer landing in that
# window (edit committed, re-embed not yet run) used to copy the OLD text's
# vector while marking the copy 'ready' for the NEW text: the target then
# permanently retrieves the new text against the old text's vector, and
# MemoryService.transfer only calls _schedule_embed when
# `copied.embedding_status != "ready"` (memory_service.py) — since the copy
# was wrongly marked ready, nothing would ever re-embed it.
#
# This test drives the edit through the STORE directly (update_with_revision
# — the same primitive MemoryService.update ultimately calls), not through
# the service, so the reproduction is deterministic: the service's own
# update() would immediately re-schedule an embed of its own and (with this
# suite's usual synchronous embedding_scheduler) race the very state this
# test needs to hold still to observe.
def test_create_copy_after_edit_does_not_copy_stale_vector_as_ready(repo, alice):
    store = repo._runtime.memory_store
    src_nb, dst_nb = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    now = repo._runtime.seams.now()
    src_write = MemoryWrite(
        id=repo._runtime.seams.new_id("memory"), notebook_id=src_nb,
        created_by=alice.id, origin="external_agent", status="candidate",
        title="T", content_md="OLD BODY", tags=["x"], created_at=now, updated_at=now,
        provenance={"client_request_id": "r1"},
    )
    source = store.create_candidate_with_initial_revision(src_write, alice.id, "created")
    # 源在 revision 1 上有一条 ready 向量。
    assert store.replace_embedding(source.id, 1, "TestModel", [0.1, 0.2, 0.3]) is True

    # 原地编辑正文——revision 推到 2，embedding_status 回落 'pending'，但
    # memory_embeddings 那条 revision-1 的旧向量行原样留着（_mutate_with_
    # revision 只碰 memory_items，从不碰 memory_embeddings）。
    edited = store.update_with_revision(
        source.id, alice.id, {"content_md": "NEW BODY"},
        expected={"candidate", "confirmed"}, changed_by=alice.id, reason="edited",
    )
    assert edited.embedding_status == "pending"
    with repo._connect() as db:
        stale_vec_count = db.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id=?", (source.id,)
        ).fetchone()[0]
    assert stale_vec_count == 1, "前置条件：旧向量行必须还在——正是它会被误拷的那一行"

    copy_write = MemoryWrite(
        id=repo._runtime.seams.new_id("memory"), notebook_id=dst_nb,
        created_by=alice.id, source_answer_id=None, origin="external_agent",
        status="confirmed", title=edited.title, content_md=edited.content_md,
        tags=list(edited.tags), created_at=now, updated_at=now,
        confirmed_by=alice.id, confirmed_at=now,
        provenance={"imported_from": {"notebook_id": src_nb, "memory_id": source.id, "action": "copy"}},
    )
    # expected_source_revision=2：与 update_with_revision 编辑后推到的 revision
    # 一致（"这确实是调用方此刻打算拷贝的那个 revision"）——即便revision 号
    # 本身对得上，embedding_status=='pending' 也必须单独拦下这次拷贝，这正是
    # 这条用例要证明的：仅 revision 匹配不够，两个判据缺一不可。
    copied = store.create_copy_with_initial_revision(
        copy_write, source.id, alice.id, "copied", 2
    )

    assert copied.id == copy_write.id
    assert copied.embedding_status == "pending", (
        "源的 embedding_status 是 'pending'（editing 之后还没重新嵌入）——"
        "拷贝绝不能把它此刻挂着的旧向量当成对得上新文本的向量拷走、更不能"
        "标 ready；目标会永久拿新文本配旧向量，且再没有任何东西会重新嵌入它"
    )
    with repo._connect() as db:
        copied_vec_count = db.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id=?", (copied.id,)
        ).fetchone()[0]
    assert copied_vec_count == 0, "源向量陈旧时副本不能带走任何向量行"
