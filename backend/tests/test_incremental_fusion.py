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


def test_place_new_concepts_name_seed(repo):
    from app.services.kg_merge import place_new_concepts, _norm
    cid_existing = "K-" + _norm("Mixture-of-Experts (MoE)")
    existing_cmap = {"obj-old": cid_existing}
    existing_names = {cid_existing: "Mixture-of-Experts"}
    new = [{"object_id": "obj-new", "name": "Mixture-of-Experts (MoE)"},
           {"object_id": "obj-x", "name": "Quantization"}]
    rows = place_new_concepts(new, existing_cmap, existing_names,
                              seed_fn=lambda o: _norm(o["name"]), id_prefix="K-")
    by_oid = {r["member_object_id"]: r for r in rows}
    assert by_oid["obj-new"]["canonical_id"] == cid_existing          # 命中已有簇
    assert by_oid["obj-new"]["canonical_name"] == "Mixture-of-Experts"  # 复用簇名
    assert by_oid["obj-x"]["canonical_name"] == "Quantization"        # 新名→新簇


def test_incremental_fuse_appends_same_name_cross_doc(repo):
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    from app.services.kg_merge import _norm
    cid = "K-" + _norm("Mixture-of-Experts (MoE)")
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-A", nb.id, "concept", "approved", "", json.dumps({"name":"Mixture-of-Experts (MoE)"}), "[]", "src-A", now, now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("cc-A", nb.id, cid, "ko-A", "Mixture-of-Experts (MoE)", "concept", "", now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-B", nb.id, "concept", "approved", "", json.dumps({"name":"Mixture-of-Experts (MoE)"}), "[]", "src-B", now, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    cmap = repo.cluster_map(nb.id)
    assert cmap.get("ko-B") == cmap.get("ko-A")   # 新 concept 进同一跨文档簇


def test_incremental_fuse_flag_off(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "kg_incremental_fusion_enabled", False)
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-B", nb.id, "concept", "approved", "", json.dumps({"name":"X"}), "[]", "src-B", now, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    assert repo.cluster_map(nb.id).get("ko-B") is None   # flag 关→不融合


def test_incremental_fuse_claim_by_name(repo):
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    from app.services.kg_merge import seed_claim
    cid = "KL-" + seed_claim({"name": "MoE raises capacity"})
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("kl-A", nb.id, "claim", "approved", "", json.dumps({"name":"MoE raises capacity"}), "[]", "src-A", now, now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("ccl-A", nb.id, cid, "kl-A", "MoE raises capacity", "claim", "", now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("kl-B", nb.id, "claim", "approved", "", json.dumps({"name":"MoE raises capacity"}), "[]", "src-B", now, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    cmap = repo.cluster_map(nb.id)
    assert cmap.get("kl-B") == cmap.get("kl-A")   # 新 claim 进同一名种子簇


def test_tier2_bridge_enqueues_candidate_not_merge(repo):
    """新 concept 向量近一个异名异簇已有 concept → 入 concept_merge_candidates,不自动并簇。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    v_old = json.dumps([1.0] + [0.0]*15)
    v_new = json.dumps([0.99] + [0.0]*15)   # 与 old 余弦≈1
    from app.services.kg_merge import _norm
    with repo._write() as db:
        for oid, nm, src in [("ko-old", "Expert Routing", "src-A"), ("ko-new", "MoE Gating", "src-B")]:
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name":nm}), "[]", src, now, now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("cc-old", nb.id, "K-"+_norm("Expert Routing"), "ko-old", "Expert Routing", "concept", "", now))
        for oid, vec in [("ko-old", v_old), ("ko-new", v_new)]:
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (oid, nb.id, vec, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    with repo._connect() as db:
        n = db.execute("SELECT count(*) c FROM concept_merge_candidates WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
    cmap = repo.cluster_map(nb.id)
    assert n >= 1                                   # 桥接候选入队
    assert cmap["ko-new"] != cmap["ko-old"]         # 未自动并(各属自己名种子簇)


def test_incremental_fuse_idempotent(repo):
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-B", nb.id, "concept", "approved", "", json.dumps({"name":"X"}), "[]", "src-B", now, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    repo.incremental_fuse_source(nb.id, "src-B")   # 二次
    with repo._connect() as db:
        n = db.execute("SELECT count(*) c FROM concept_clusters WHERE notebook_id=? AND member_object_id='ko-B'", (nb.id,)).fetchone()["c"]
    assert n == 1                                   # 幂等,不重复成员


def test_rebuild_unified_kg_still_works(repo):
    """全量逃生口不受影响:rebuild 后所有 concept 入簇。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-A", nb.id, "concept", "approved", "", json.dumps({"name":"MoE"}), "[]", "src-A", now, now))
    repo.rebuild_unified_kg(nb.id)
    assert "ko-A" in repo.cluster_map(nb.id)


def test_incremental_procedure_seed_matches_rebuild(repo):
    """#1 回归:procedure 增量 canonical 含 steps 签名,且与 rebuild 一致
    (place_new_concepts 必须收到含 payload 的 o,而非裸 payload)。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    payload = {"name": "calibrate ADC", "steps": [{"name": "sample"}, {"name": "set ref"}]}
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("kp-B", nb.id, "procedure", "approved", "", json.dumps(payload), "[]", "src-B", now, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    inc_cid = repo.cluster_map(nb.id).get("kp-B")
    repo.rebuild_unified_kg(nb.id)
    reb_cid = repo.cluster_map(nb.id).get("kp-B")
    assert "#" in (inc_cid or "")        # steps 签名被纳入(传 o 含 payload 才有)
    assert inc_cid == reb_cid            # 增量与全量 canonical 一致


def test_tier2_no_duplicate_candidates_on_refuse(repo):
    """#2:同一桥接对重复增量不重复入队(去重已 pending)。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    v_old = json.dumps([1.0] + [0.0]*15); v_new = json.dumps([0.99] + [0.0]*15)
    from app.services.kg_merge import _norm
    with repo._write() as db:
        for oid, nm, src in [("ko-old", "Expert Routing", "src-A"), ("ko-new", "MoE Gating", "src-B")]:
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name":nm}), "[]", src, now, now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("cc-old", nb.id, "K-"+_norm("Expert Routing"), "ko-old", "Expert Routing", "concept", "", now))
        for oid, vec in [("ko-old", v_old), ("ko-new", v_new)]:
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (oid, nb.id, vec, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    repo.incremental_fuse_source(nb.id, "src-B")   # 二次:同一对不再入队
    with repo._connect() as db:
        n = db.execute("SELECT count(*) c FROM concept_merge_candidates WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
    assert n == 1


def test_incremental_fuse_cleans_orphan_cluster_rows(repo):
    """re-extraction 留下的 orphan 簇行(member 指向已删对象)被清理,活跃成员保留。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        # 活跃 concept + 簇行
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-live", nb.id, "concept", "approved", "", json.dumps({"name":"Live"}), "[]", "src-A", now, now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("cc-live", nb.id, "K-live", "ko-live", "Live", "concept", "", now))
        # orphan 簇行:member 指向不存在的对象(模拟重抽取删旧 ko-)
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("cc-orphan", nb.id, "K-old", "ko-deleted", "Old", "concept", "", now))
        # 新源 concept(触发增量融合)
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-new", nb.id, "concept", "approved", "", json.dumps({"name":"New"}), "[]", "src-B", now, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    with repo._connect() as db:
        members = {r["member_object_id"] for r in db.execute(
            "SELECT member_object_id FROM concept_clusters WHERE notebook_id=?", (nb.id,)).fetchall()}
    assert "ko-deleted" not in members   # orphan 已清
    assert "ko-live" in members          # 活跃成员保留
    assert "ko-new" in members           # 新成员加入
