"""后台全量预审 job：分批清空 pending；进度；fail-open 不死循环；单飞。"""
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
import app.services.concept_merge_review as cmr


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


def _seed_pending(repo, nb, n):
    with repo._write() as db:
        for i in range(n):
            db.execute(
                "INSERT INTO concept_merge_candidates "
                "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?, 'pending', '', '')",
                (f"m{i}", nb, f"K-a{i}", f"K-b{i}", f"a{i}", f"b{i}", 0.9))


def test_job_drains_all_pending(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_pending(repo, nb, 250)
    # 复审：一律 keep_separate 高置信 → rejected（离队）
    monkeypatch.setattr(cmr, "review_merge_candidates",
                        lambda client, pending, **k: [{"candidate_id": c["id"], "decision": "keep_separate",
                                                       "confidence": 0.95, "rationale": ""} for c in pending])
    res = repo.run_merge_review_job(nb, batch=100)
    assert res["status"] == "done"
    assert res["total"] == 250
    assert repo.pending_merges(nb) == []
    st = repo.merge_review_job_status(nb)
    assert st["status"] == "done" and st["done"] == 250


def test_job_failopen_no_infinite_loop(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_pending(repo, nb, 30)
    # 复审始终返回空（LLM 失败） → reviewed=0 每批 → stall 中止，不死循环
    monkeypatch.setattr(cmr, "review_merge_candidates", lambda client, pending, **k: [])
    res = repo.run_merge_review_job(nb, batch=10)
    assert res["status"] == "failed"
    # pending 未被清（没有决定），但 job 已中止
    assert len(repo.pending_merges(nb)) == 30


def test_status_idle_when_never_run(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    assert repo.merge_review_job_status(nb)["status"] == "idle"


def test_review_pending_merges_batch_matches_old_slice_order(repo):
    """review_pending_merges must fetch its batch via SQL LIMIT that preserves
    the same order the old `pending_merges(nb)[:limit]` Python-slice used
    (implicit rowid / insertion order), not just any `limit`-sized subset."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_pending(repo, nb, 30)
    old_slice_ids = [r["id"] for r in repo.pending_merges(nb)[:10]]
    new_batch_ids = [r["id"] for r in repo._pending_merges_batch(nb, 10)]
    assert new_batch_ids == old_slice_ids


def test_run_merge_review_job_bounded_sql_queries(repo, monkeypatch):
    """The drain loop's continuation test must be a cheap EXISTS/COUNT, not a
    full materialization of all pending rows every iteration. On a 250-row
    queue with batch=100 there are 3 batches; the loop's "is there more work"
    check must never call the full-scan pending_merges()."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_pending(repo, nb, 250)
    monkeypatch.setattr(cmr, "review_merge_candidates",
                        lambda client, pending, **k: [{"candidate_id": c["id"], "decision": "keep_separate",
                                                       "confidence": 0.95, "rationale": ""} for c in pending])

    calls = {"pending_merges_full": 0}
    orig_pending_merges = repo.__class__.pending_merges

    def spy_pending_merges(self, notebook_id):
        calls["pending_merges_full"] += 1
        return orig_pending_merges(self, notebook_id)

    monkeypatch.setattr(repo.__class__, "pending_merges", spy_pending_merges)
    res = repo.run_merge_review_job(nb, batch=100)
    assert res["status"] == "done"
    # The fixed loop must not call the full-scan pending_merges() at all for
    # its continuation check (it uses a cheap EXISTS/COUNT helper instead)
    # and review_pending_merges must fetch its own batch via SQL LIMIT.
    assert calls["pending_merges_full"] == 0


def test_startup_reconciles_stuck_running(repo, tmp_path):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    # simulate a job left 'running' by a crashed process
    with repo._write() as db:
        db.execute("INSERT INTO merge_review_jobs (notebook_id,status,total,done,started_at,updated_at,error) "
                   "VALUES (?, 'running', 5, 2, '', '', '')", (nb,))
    # a fresh repository over the SAME db file re-runs _migrate on construction;
    # 清算则由服务端启动路径显式驱动(不再是构造副作用,见
    # tests/test_startup_recovery_ownership.py)
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    from app.services.embedding import FakeEmbedder
    repo2 = SQLiteRepository(Settings())
    repo2._recover_interrupted_jobs()
    repo2.embedder = FakeEmbedder(dim=16)
    st = repo2.merge_review_job_status(nb)
    assert st["status"] == "failed"
