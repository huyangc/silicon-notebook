"""review_pending_merges:unsure/低置信 → deferred(离队),不再是 pending。"""
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
import app.services.sqlite_repository as repomod


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "m")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _mk(repo, nb, cid, status="pending"):
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?, '', '')",
            (cid, nb, "K-x", "K-y", "x", "y", 0.9, status))


def test_unsure_becomes_deferred(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _mk(repo, nb, "m1")
    # stub 复审:返回 unsure
    monkeypatch.setattr(repomod, "review_merge_candidates" if hasattr(repomod, "review_merge_candidates") else "_noop", lambda *a, **k: None, raising=False)
    import app.services.concept_merge_review as cmr
    monkeypatch.setattr(cmr, "review_merge_candidates",
                        lambda client, pending, **k: [{"candidate_id": "m1", "decision": "unsure",
                                                       "confidence": 0.4, "rationale": "unclear"}])
    summary = repo.review_pending_merges(nb, limit=50)
    assert summary["unsure"] == 1
    with repo._connect() as db:
        row = db.execute("SELECT status FROM concept_merge_candidates WHERE id='m1'").fetchone()
    assert row["status"] == "deferred"
    assert repo.pending_merges(nb) == []
