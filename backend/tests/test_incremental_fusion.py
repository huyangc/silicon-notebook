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


# ── P1-3: Tier2 桥接 ANN 化(perf audit)────────────────────────────────────
# _tier2_bridge_candidates_ann 必须与暴力 kg_merge.detect_bridge_candidates 在
# 「同一 topk(5)/threshold(lo=0.82) 下的候选集合」上等价;三分支覆盖:
#   1) 有 kg ANN(即使版本漂移/stale)→ ANN 路径,任意规模可用(旧代码在
#      len(existing) > max_entities 时静默跳过 — 大库上从未真正跑过)。
#   2) 无索引 + 实体数 ≤ max_entities → 原暴力路径,byte-identical。
#   3) 无索引 + 实体数 > max_entities → 跳过 + event_log 收到 tier2_skipped(不再静默)。

def _unit(dim, i, mag=1.0):
    v = [0.0] * dim
    v[i] = mag
    return v


def _mk_vec(dim, primary_i, primary_mag, noise_i, noise_mag):
    v = [0.0] * dim
    v[primary_i] = primary_mag
    if noise_i is not None:
        v[noise_i] = noise_mag
    return v


def _seed_concept(repo, nb_id, oid, name, src, vec, now):
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
            "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (oid, nb_id, "concept", "approved", "", json.dumps({"name": name}), "[]", src, now, now))
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            (oid, nb_id, json.dumps(vec), now))


def _build_small_base(repo, nb_id, existing, now):
    """existing: [(oid, name, vec)] — each becomes its own name-seed cluster
    (distinct names) so canonical ids are trivially derivable via K-+_norm."""
    from app.services.kg_merge import _norm
    with repo._write() as db:
        for i, (oid, name, vec) in enumerate(existing):
            db.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
                "canonical_name,object_type,canonical_description,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"cc-{i}", nb_id, "K-" + _norm(name), oid, name, "concept", "", now))


def _oracle_bridge_candidates(existing, new_items, dim, *, lo=0.82, top_k=5):
    """Test-local, independent brute-force cosine oracle (NOT calling
    detect_bridge_candidates, to avoid a tautological equivalence check) —
    same math: per new item, rank existing by raw cosine desc, take top_k,
    keep those >= lo with a different canonical (name) seed, dedupe pairs."""
    import numpy as np
    from app.services.kg_merge import _norm
    ex_ids = [oid for oid, _, _ in existing]
    ex_names = {oid: nm for oid, nm, _ in existing}
    EX = np.asarray([v for _, _, v in existing], dtype="float32")
    EX = EX / (np.linalg.norm(EX, axis=1, keepdims=True) + 1e-9)
    out, seen = [], set()
    for oid, name, vec in new_items:
        q = np.asarray(vec, dtype="float32"); q = q / (np.linalg.norm(q) + 1e-9)
        sims = EX @ q
        order = np.argsort(-sims)[:top_k]
        my_cid = "K-" + _norm(name)
        for j in order:
            s = float(sims[j])
            if s < lo:
                break
            other_cid = "K-" + _norm(ex_names[ex_ids[j]])
            if other_cid == my_cid:
                continue
            a, b = sorted((my_cid, other_cid))
            if (a, b) in seen:
                continue
            seen.add((a, b))
            out.append((a, b))
    return set(out)


def test_tier2_ann_path_matches_oracle_when_over_threshold(repo, monkeypatch):
    """有 kg ANN(索引可 stale)时,max_entities=0(旧代码必静默跳过的条件)也要
    恢复桥接;ANN 产出的候选对集合与独立暴力 oracle 一致(小库 + ef 调高保精确)。"""
    from app.core.config import Settings as _S
    from app.models.schemas import NotebookCreate as _NC
    nb = repo.create_notebook(_NC(name="kb"))
    now = "2026-07-02T00:00:00"
    dim = 16
    existing = [
        ("ko-e1", "Expert Routing", _mk_vec(dim, 0, 1.0, 1, 0.05)),
        ("ko-e2", "Batch Normalization", _mk_vec(dim, 2, 1.0, 3, 0.05)),
        ("ko-e3", "Gradient Clipping", _mk_vec(dim, 4, 1.0, 5, 0.05)),
    ]
    for oid, name, vec in existing:
        _seed_concept(repo, nb.id, oid, name, "src-A", vec, now)
    _build_small_base(repo, nb.id, existing, now)
    repo.build_scale_index(nb.id)   # kg hnsw persisted, ef_construction=200 -> exact recall at n=3

    new_items = [("ko-new", "MoE Gating", _mk_vec(dim, 0, 0.99, 1, 0.04))]  # near ko-e1
    for oid, name, vec in new_items:
        _seed_concept(repo, nb.id, oid, name, "src-B", vec, now)

    monkeypatch.setattr(repo.settings, "kg_incremental_tier2_max_entities", 0)
    repo.incremental_fuse_source(nb.id, "src-B")

    with repo._connect() as db:
        rows = db.execute(
            "SELECT canonical_a, canonical_b, status FROM concept_merge_candidates WHERE notebook_id=?",
            (nb.id,)).fetchall()
    got_pairs = {tuple(sorted((r["canonical_a"], r["canonical_b"]))) for r in rows}
    assert all(r["status"] == "pending" for r in rows)   # 只入队,不自动并
    want = _oracle_bridge_candidates(existing, new_items, dim)
    assert got_pairs == want
    assert got_pairs   # sanity: the near-duplicate must actually surface >=1 candidate
    cmap = repo.cluster_map(nb.id)
    assert cmap["ko-new"] != cmap["ko-e1"]   # 未自动并簇(各自名种子簇)


def test_tier2_ann_path_type_filter_excludes_non_concept(repo, monkeypatch):
    """跨类型(claim 等)不应被当作桥接候选来源/目标 —— 只桥 concept,照抄现语义。"""
    from app.models.schemas import NotebookCreate as _NC
    from app.services.kg_merge import _norm
    nb = repo.create_notebook(_NC(name="kb"))
    now = "2026-07-02T00:00:00"
    dim = 16
    existing = [("ko-e1", "Expert Routing", _mk_vec(dim, 0, 1.0, 1, 0.05))]
    for oid, name, vec in existing:
        _seed_concept(repo, nb.id, oid, name, "src-A", vec, now)
    _build_small_base(repo, nb.id, existing, now)
    # A claim with a near-identical vector and a claim-prefixed canonical cluster row.
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
            "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("kl-e1", nb.id, "claim", "approved", "", json.dumps({"name": "Expert Routing claim"}),
             "[]", "src-A", now, now))
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            ("kl-e1", nb.id, json.dumps(_mk_vec(dim, 0, 0.99, 1, 0.05)), now))
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,canonical_description,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("cc-claim", nb.id, "KL-" + _norm("Expert Routing claim"), "kl-e1",
             "Expert Routing claim", "claim", "", now))
    repo.build_scale_index(nb.id)

    _seed_concept(repo, nb.id, "ko-new", "MoE Gating", "src-B", _mk_vec(dim, 0, 0.99, 1, 0.04), now)
    monkeypatch.setattr(repo.settings, "kg_incremental_tier2_max_entities", 0)
    repo.incremental_fuse_source(nb.id, "src-B")

    with repo._connect() as db:
        rows = db.execute(
            "SELECT canonical_a, canonical_b FROM concept_merge_candidates WHERE notebook_id=?",
            (nb.id,)).fetchall()
    for r in rows:
        assert not r["canonical_a"].startswith("KL-")
        assert not r["canonical_b"].startswith("KL-")


def test_tier2_no_index_small_library_byte_identical_to_oracle(repo, monkeypatch):
    """无索引(从未 build_scale_index)+ 实体数 <= max_entities → 走原暴力路径,
    结果与独立 oracle byte-identical(小库路径完全不受本次改动影响)。"""
    from app.models.schemas import NotebookCreate as _NC
    nb = repo.create_notebook(_NC(name="kb"))
    now = "2026-07-02T00:00:00"
    dim = 16
    existing = [
        ("ko-e1", "Expert Routing", _mk_vec(dim, 0, 1.0, 1, 0.05)),
        ("ko-e2", "Batch Normalization", _mk_vec(dim, 2, 1.0, 3, 0.05)),
    ]
    for oid, name, vec in existing:
        _seed_concept(repo, nb.id, oid, name, "src-A", vec, now)
    _build_small_base(repo, nb.id, existing, now)
    # No build_scale_index call -> repo._scale_index(nb, allow_stale=True) is None.
    assert repo._scale_index(nb.id, allow_stale=True) is None

    new_items = [("ko-new", "MoE Gating", _mk_vec(dim, 0, 0.99, 1, 0.04))]
    for oid, name, vec in new_items:
        _seed_concept(repo, nb.id, oid, name, "src-B", vec, now)

    # max_entities default (50000) >= len(existing) -> legacy brute-force branch.
    repo.incremental_fuse_source(nb.id, "src-B")
    with repo._connect() as db:
        rows = db.execute(
            "SELECT canonical_a, canonical_b FROM concept_merge_candidates WHERE notebook_id=?",
            (nb.id,)).fetchall()
    got_pairs = {tuple(sorted((r["canonical_a"], r["canonical_b"]))) for r in rows}
    want = _oracle_bridge_candidates(existing, new_items, dim)
    assert got_pairs == want


def test_tier2_no_index_over_threshold_skips_with_event(repo, monkeypatch):
    """无索引 + 实体数 > max_entities → 跳过桥接检测,但 event_log 收到
    tier2_skipped 事件(P1-3 修复点:旧代码这里完全静默)。"""
    from app.models.schemas import NotebookCreate as _NC
    nb = repo.create_notebook(_NC(name="kb"))
    now = "2026-07-02T00:00:00"
    dim = 16
    existing = [
        ("ko-e1", "Expert Routing", _mk_vec(dim, 0, 1.0, 1, 0.05)),
        ("ko-e2", "Batch Normalization", _mk_vec(dim, 2, 1.0, 3, 0.05)),
    ]
    for oid, name, vec in existing:
        _seed_concept(repo, nb.id, oid, name, "src-A", vec, now)
    _build_small_base(repo, nb.id, existing, now)
    assert repo._scale_index(nb.id, allow_stale=True) is None   # no index built

    _seed_concept(repo, nb.id, "ko-new", "MoE Gating", "src-B", _mk_vec(dim, 0, 0.99, 1, 0.04), now)

    monkeypatch.setattr(repo.settings, "kg_incremental_tier2_max_entities", 1)  # len(existing)=2 > 1
    events = []
    orig_emit = repo.event_log.emit
    monkeypatch.setattr(repo.event_log, "emit", lambda e, **kw: (events.append(e), orig_emit(e, **kw))[0] is None)

    repo.incremental_fuse_source(nb.id, "src-B")

    with repo._connect() as db:
        n = db.execute("SELECT count(*) c FROM concept_merge_candidates WHERE notebook_id=?",
                       (nb.id,)).fetchone()["c"]
    assert n == 0   # 跳过,未入队任何候选
    skipped = [e for e in events if e.get("kind") == "tier2_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["notebook_id"] == nb.id
    assert skipped[0]["entities"] == 2
    assert skipped[0]["reason"] == "no_index_over_threshold"


def test_tier2_ann_bridge_survives_stale_index(repo, monkeypatch):
    """索引版本漂移(source-B 写入后 manifest.version 落后)也要走 ANN 路径 —— 这是
    「新↔存量」桥接的主场景:新上传对象的向量永远从 knowledge_embeddings 现读,
    不依赖索引本身是否含有它自己。"""
    from app.models.schemas import NotebookCreate as _NC
    nb = repo.create_notebook(_NC(name="kb"))
    now = "2026-07-02T00:00:00"
    dim = 16
    existing = [("ko-e1", "Expert Routing", _mk_vec(dim, 0, 1.0, 1, 0.05))]
    for oid, name, vec in existing:
        _seed_concept(repo, nb.id, oid, name, "src-A", vec, now)
    _build_small_base(repo, nb.id, existing, now)
    manifest = repo.build_scale_index(nb.id)
    fresh = repo._scale_index(nb.id)
    assert fresh is not None and fresh.manifest.get("version") is not None

    _seed_concept(repo, nb.id, "ko-new", "MoE Gating", "src-B", _mk_vec(dim, 0, 0.99, 1, 0.04), now)
    # Real upload paths (store_kg etc.) call _mark_unified_kg_dirty on every KG
    # write; _seed_concept is a raw test-only INSERT that bypasses it, so bump
    # the seq explicitly here to reproduce the real post-upload version drift.
    repo._mark_unified_kg_dirty(nb.id)
    # DB has changed (new object+embedding) -> exact-version cache miss, but the
    # on-disk index is still returned when allow_stale=True.
    assert repo._scale_index(nb.id) is None
    stale = repo._scale_index(nb.id, allow_stale=True)
    assert stale is not None

    monkeypatch.setattr(repo.settings, "kg_incremental_tier2_max_entities", 0)
    repo.incremental_fuse_source(nb.id, "src-B")
    with repo._connect() as db:
        n = db.execute("SELECT count(*) c FROM concept_merge_candidates WHERE notebook_id=?",
                       (nb.id,)).fetchone()["c"]
    assert n >= 1   # stale 索引仍能桥接「新↔存量」
