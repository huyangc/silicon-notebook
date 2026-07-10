import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_migration_creates_checkpoint_table(repo):
    """迁移后表存在,且 user_version 已达 SCHEMA_VERSION。"""
    from app.services.sqlite_repository import SCHEMA_VERSION
    with repo._connect() as db:
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kg_rebuild_checkpoint'"
        ).fetchone()
        uv = int(db.execute("PRAGMA user_version").fetchone()[0])
    assert row is not None
    assert uv == SCHEMA_VERSION


def test_ckpt_put_load_roundtrip(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._rebuild_ckpt_put(nb.id, "v1", "merge_review",
                           [("K-a\x1fK-b", {"decision": "merge", "confidence": 0.9})])
    loaded = repo._rebuild_ckpt_load(nb.id, "v1", "merge_review")
    assert loaded == {"K-a\x1fK-b": {"decision": "merge", "confidence": 0.9}}
    # 不同 stage / 版本互不干扰
    assert repo._rebuild_ckpt_load(nb.id, "v1", "concept_desc") == {}
    assert repo._rebuild_ckpt_load(nb.id, "v2", "merge_review") == {}


def test_ckpt_gc_drops_other_versions_only(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._rebuild_ckpt_put(nb.id, "old", "merge_review", [("k1", {"d": 1})])
    repo._rebuild_ckpt_put(nb.id, "cur", "merge_review", [("k2", {"d": 2})])
    repo._rebuild_ckpt_gc(nb.id, "cur")
    assert repo._rebuild_ckpt_load(nb.id, "old", "merge_review") == {}
    assert repo._rebuild_ckpt_load(nb.id, "cur", "merge_review") == {"k2": {"d": 2}}


def test_ckpt_clear_drops_all(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._rebuild_ckpt_put(nb.id, "cur", "merge_review", [("k1", {"d": 1})])
    repo._rebuild_ckpt_put(nb.id, "cur", "concept_desc", [("k2", {"d": 2})])
    repo._rebuild_ckpt_clear(nb.id)
    assert repo._rebuild_ckpt_load(nb.id, "cur", "merge_review") == {}
    assert repo._rebuild_ckpt_load(nb.id, "cur", "concept_desc") == {}
