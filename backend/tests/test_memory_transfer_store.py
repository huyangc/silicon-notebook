import pytest
from types import SimpleNamespace
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
    copied = store.create_copy_with_initial_revision(copy_write, source.id, alice.id, "copied")

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
