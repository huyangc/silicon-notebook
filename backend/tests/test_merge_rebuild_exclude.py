"""rebuild 用 seed 键排除 confirmed/rejected/deferred;pending 行写 seed_a/seed_b;
canonical id 漂移后按 seed 键仍排除。"""
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


def _pending_pairs(repo, nb):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT seed_a, seed_b FROM concept_merge_candidates "
            "WHERE notebook_id=? AND status='pending'", (nb,)).fetchall()
    return {frozenset((r["seed_a"], r["seed_b"])) for r in rows}


def test_rebuild_writes_seed_cols_and_excludes_decided(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    # 一批概念名,在 FakeEmbedder(dim=16) 下彼此相似度落在 [lo, hi) pending 区间,
    # rebuild 会产生若干 pending 候选(verified: concept a/c ~0.89, c/d ~0.86, b/c
    # ~0.82,均 <hi=0.94 不会被 auto-merge)。
    kg = [{"local_id": f"c{i}", "object_type": "concept",
           "payload": {"name": n, "section_path": ""}, "evidence": []}
          for i, n in enumerate(["concept a", "concept b", "concept c", "concept d"])]
    repo.store_kg(nb, None, kg, [])
    repo.rebuild_unified_kg(nb)
    p1 = _pending_pairs(repo, nb)
    assert p1, "应产生若干 pending 候选"
    # pending 行的 seed 列非空
    with repo._connect() as db:
        empties = db.execute(
            "SELECT COUNT(*) c FROM concept_merge_candidates "
            "WHERE notebook_id=? AND status='pending' AND (seed_a='' OR seed_b='')", (nb,)).fetchone()["c"]
    assert empties == 0
    # 取一条 pending 标为 deferred,rebuild 后它不应再回到 pending
    # (seed_a/seed_b 列的顺序由 cluster_seeds 内部 ANN 候选顺序决定,与 frozenset
    # 的迭代顺序无关,故 WHERE 子句须对两种列序都匹配。)
    pair = next(iter(p1))
    sa, sb = tuple(pair)
    with repo._write() as db:
        db.execute("UPDATE concept_merge_candidates SET status='deferred' "
                   "WHERE notebook_id=? AND status='pending' AND "
                   "((seed_a=? AND seed_b=?) OR (seed_a=? AND seed_b=?))",
                   (nb, sa, sb, sb, sa))
    repo.rebuild_unified_kg(nb, force=True)
    assert pair not in _pending_pairs(repo, nb)


def test_pending_refresh_revalidates_decisions_that_land_after_clustering(
    repo, monkeypatch
):
    nb = repo.create_notebook(NotebookCreate(name="nb-race")).id
    kg = [{"local_id": f"c{i}", "object_type": "concept",
           "payload": {"name": name, "section_path": ""}, "evidence": []}
          for i, name in enumerate(["concept a", "concept b", "concept c", "concept d"])]
    repo.store_kg(nb, None, kg, [])
    repo.rebuild_unified_kg(nb)
    before = _pending_pairs(repo, nb)
    assert before
    target = next(iter(before))
    seed_a, seed_b = tuple(target)
    governance_store = repo._runtime.knowledge_lifecycle.governance_store
    original_delete = governance_store.delete_pending_merges

    def reject_during_refresh(connection, notebook_id):
        connection.execute(
            "UPDATE concept_merge_candidates SET status='rejected' "
            "WHERE notebook_id=? AND status='pending' AND "
            "((seed_a=? AND seed_b=?) OR (seed_a=? AND seed_b=?))",
            (notebook_id, seed_a, seed_b, seed_b, seed_a),
        )
        original_delete(connection, notebook_id)

    monkeypatch.setattr(
        governance_store, "delete_pending_merges", reject_during_refresh
    )
    repo.rebuild_unified_kg(nb, force=True)

    assert target not in _pending_pairs(repo, nb)
