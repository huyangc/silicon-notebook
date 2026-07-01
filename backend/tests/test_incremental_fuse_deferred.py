import json
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
    monkeypatch.setenv("EMBED_DIM", "16")   # 与 FakeEmbedder(16) 对齐,使 Tier2 向量过 settings.embed_dim 滤
    r = SQLiteRepository(Settings(_env_file=None)); r.embedder = FakeEmbedder(dim=16); return r


def test_deferred_pair_not_reproposed_on_new_source(repo):
    """#3 回归:一个已被人工 deferred 的 canonical 概念对,在新源上传触发的
    incremental_fuse_source 增量融合中,不得被 Tier2 桥接检测重新入队为 pending。
    这是 deferred 队列「不回流」承诺的核心保证——之前的 exclude 集合只查
    decided_pairs()(仅 confirmed/rejected),漏了 deferred 状态。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    v_old = json.dumps([1.0] + [0.0]*15)
    v_new = json.dumps([0.99] + [0.0]*15)   # 与 old 余弦≈1,足以触发桥接候选
    from app.services.kg_merge import _norm
    cid_old = "K-" + _norm("Expert Routing")
    cid_new = "K-" + _norm("MoE Gating")
    with repo._write() as db:
        for oid, nm, src in [("ko-old", "Expert Routing", "src-A"), ("ko-new", "MoE Gating", "src-B")]:
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name":nm}), "[]", src, now, now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("cc-old", nb.id, cid_old, "ko-old", "Expert Routing", "concept", "", now))
        for oid, vec in [("ko-old", v_old), ("ko-new", v_new)]:
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (oid, nb.id, vec, now))

    # 首次融合:桥接候选正常入队为 pending。
    repo.incremental_fuse_source(nb.id, "src-B")
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, canonical_a, canonical_b, status FROM concept_merge_candidates WHERE notebook_id=?",
            (nb.id,)).fetchall()
    assert len(rows) == 1
    cand = rows[0]
    pair = frozenset((cand["canonical_a"], cand["canonical_b"]))
    assert pair == frozenset((cid_old, cid_new))

    # 人工审阅后 defer 该对(既不 confirm 也不 reject,只是搁置)。
    with repo._write() as db:
        db.execute("UPDATE concept_merge_candidates SET status='deferred' WHERE id=?", (cand["id"],))

    # 新源上传再次触发增量融合(模拟又一篇引用同名概念的新文档)。
    repo.incremental_fuse_source(nb.id, "src-B")

    with repo._connect() as db:
        all_rows = db.execute(
            "SELECT canonical_a, canonical_b, status FROM concept_merge_candidates WHERE notebook_id=?",
            (nb.id,)).fetchall()
    # 该 canonical 对仍只有一行(原 deferred 行),没有被重新入队成新的 pending 行。
    matching = [r for r in all_rows
                if frozenset((r["canonical_a"], r["canonical_b"])) == pair]
    assert len(matching) == 1
    assert matching[0]["status"] == "deferred"
    assert not any(r["status"] == "pending" for r in matching)
