"""seed_a/seed_b 迁移 + decided_seed_pairs(稳定键 + 空值回退 + 含 deferred)。"""
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _mk(repo, nb, cid, ca, cb, sa, sb, status):
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?, '', '')",
            (cid, nb, ca, cb, sa, sb, 0.9, status))


def test_columns_exist_and_decided_seed_pairs(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    # 精确 seed 行(confirmed / rejected / deferred)
    _mk(repo, nb, "m1", "K-x", "K-y", "x", "y", "confirmed")
    _mk(repo, nb, "m2", "K-p", "K-q", "p", "q", "rejected")
    _mk(repo, nb, "m3", "K-u", "K-v", "u", "v", "deferred")
    # pending 不计入
    _mk(repo, nb, "m4", "K-a", "K-b", "a", "b", "pending")
    dsp = repo.decided_seed_pairs(nb)
    assert dsp[frozenset(("x", "y"))] == "confirmed"
    assert dsp[frozenset(("p", "q"))] == "rejected"
    assert dsp[frozenset(("u", "v"))] == "deferred"
    assert frozenset(("a", "b")) not in dsp


def test_decided_seed_pairs_falls_back_to_canonical(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    # 存量行:seed_a/seed_b 为空 → 回退 strip-"K-"
    _mk(repo, nb, "m1", "K-foo", "K-bar", "", "", "rejected")
    dsp = repo.decided_seed_pairs(nb)
    assert dsp[frozenset(("foo", "bar"))] == "rejected"
