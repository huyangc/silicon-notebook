import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")   # 与 FakeEmbedder(16) 对齐,使 Tier2 向量过 settings.embed_dim 滤
    r = SQLiteRepository(Settings(_env_file=None)); bind_all_embedding_clients(r, FakeEmbedder(dim=16)); return r


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

    knowledge = repo._runtime.knowledge_lifecycle.knowledge
    original_rows = knowledge.incremental_object_rows
    exclude_source_calls = []

    def _rows_spy(db, notebook_id, source_id, object_type, *, exclude_source=False):
        if exclude_source:
            exclude_source_calls.append((notebook_id, source_id, object_type))
        return original_rows(
            db, notebook_id, source_id, object_type,
            exclude_source=exclude_source,
        )

    monkeypatch.setattr(knowledge, "incremental_object_rows", _rows_spy)
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
    # The ANN labels/vectors already represent the existing corpus. Hydrating all
    # existing concept payloads here used to be pure discarded I/O, and at scale
    # repeated once per extracted source.
    assert exclude_source_calls == []


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


# ── Fix wave(审查后):claim 密集库召回 / deprecated 对齐 / 早停有界 ────────────

def _seed_typed(repo, nb_id, oid, name, otype, src, vec, now, cid_prefix):
    """Seed a knowledge object of any type with an embedding + its own cluster row."""
    from app.services.kg_merge import _norm
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
            "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (oid, nb_id, otype, "approved", "", json.dumps({"name": name}), "[]", src, now, now))
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            (oid, nb_id, json.dumps(vec), now))
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,canonical_description,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"cc-{oid}", nb_id, cid_prefix + _norm(name), oid, name, otype, "", now))


def test_tier2_ann_claim_dense_overfetch_matches_concept_only_oracle(repo, monkeypatch):
    """生产形态回归:kg ANN 是全类型索引(47万向量里 ~31万 claim、仅 ~7万 concept)。
    固定 k=topk*pad 的近邻窗可能全被更近的 claim 占满,类型过滤后 concept 不足
    top_k → 系统性欠桥接。类型感知迭代过取(k 倍增重查)必须凑够 top_k 个 concept,
    候选集合与 concept-only 暴力 oracle 一致。"""
    from app.models.schemas import NotebookCreate as _NC
    nb = repo.create_notebook(_NC(name="kb"))
    now = "2026-07-02T00:00:00"
    dim = 16
    # 40 claims all EXTREMELY close to the new concept's vector (sim ~0.999) —
    # they occupy the entire initial k = topk*pad_factor = 20 neighbor window.
    for i in range(40):
        v = [0.0] * dim
        v[0] = 0.995
        v[1 + i % 15] = 0.03 + 0.001 * i   # distinct small noise per claim
        _seed_typed(repo, nb.id, f"kl-c{i}", f"claim {i}", "claim", "src-A", v, now, "KL-")
    # 5 concepts at sim exactly 0.9 to e0 (above lo=0.82, but strictly farther
    # than every claim -> ranked 41..45 in the raw ANN ordering).
    concepts = []
    for i in range(5):
        v = [0.0] * dim
        v[0] = 0.9
        v[1 + i] = (1 - 0.9 ** 2) ** 0.5   # unit norm -> cosine to e0 is exactly 0.9
        concepts.append((f"ko-e{i}", f"Concept {i}", v))
        _seed_concept(repo, nb.id, f"ko-e{i}", f"Concept {i}", "src-A", v, now)
    _build_small_base(repo, nb.id, concepts, now)
    repo.build_scale_index(nb.id)

    new_items = [("ko-new", "MoE Gating", _unit(dim, 0))]
    for oid, name, vec in new_items:
        _seed_concept(repo, nb.id, oid, name, "src-B", vec, now)
    monkeypatch.setattr(repo.settings, "kg_incremental_tier2_max_entities", 0)
    repo.incremental_fuse_source(nb.id, "src-B")

    with repo._connect() as db:
        rows = db.execute(
            "SELECT canonical_a, canonical_b FROM concept_merge_candidates WHERE notebook_id=?",
            (nb.id,)).fetchall()
    got_pairs = {tuple(sorted((r["canonical_a"], r["canonical_b"]))) for r in rows}
    want = _oracle_bridge_candidates(concepts, new_items, dim)   # concept-only oracle
    assert len(want) == 5          # sanity: all 5 concepts are >= lo, all should bridge
    assert got_pairs == want       # iterative over-fetch recovered ALL concepts past the claims
    for a, b in got_pairs:         # and no claim canonical ever leaked in
        assert not a.startswith("KL-") and not b.startswith("KL-")


class _CountingAnn:
    """Proxy around a real hnswlib handle counting knn_query rounds."""
    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    def set_ef(self, ef):
        return self._inner.set_ef(ef)

    def knn_query(self, *a, **kw):
        self.calls += 1
        return self._inner.knn_query(*a, **kw)


def test_tier2_ann_early_stop_below_threshold_bounds_knn_calls(repo, monkeypatch):
    """迭代过取的早停:当近邻窗尾部 sim 已低于 lo 阈值时,再扩 k 取到的只会更远
    (必被阈值筛掉)→ 必须停止倍增。断言 knn_query 只跑一轮,且高分 concept 照常出候选。"""
    from app.models.schemas import NotebookCreate as _NC
    nb = repo.create_notebook(_NC(name="kb"))
    now = "2026-07-02T00:00:00"
    dim = 16
    # 2 claims very near (would be hits 1-2), 1 concept at 0.9 (hit 3),
    # 30 claims far away (sim ~0.1 — pad the index so n_labels > initial k=20,
    # making the tail of the first window below lo while k < n_labels).
    for i in range(2):
        v = [0.0] * dim; v[0] = 0.995; v[1 + i] = 0.05
        _seed_typed(repo, nb.id, f"kl-near{i}", f"near claim {i}", "claim", "src-A", v, now, "KL-")
    good = ("ko-good", "Expert Routing", _mk_vec(dim, 0, 0.9, 1, (1 - 0.9 ** 2) ** 0.5))
    _seed_concept(repo, nb.id, "ko-good", "Expert Routing", "src-A", good[2], now)
    _build_small_base(repo, nb.id, [good], now)
    for i in range(30):
        v = [0.0] * dim; v[0] = 0.1; v[2 + i % 13] = 0.99
        _seed_typed(repo, nb.id, f"kl-far{i}", f"far claim {i}", "claim", "src-A", v, now, "KL-")
    repo.build_scale_index(nb.id)

    _seed_concept(repo, nb.id, "ko-new", "MoE Gating", "src-B", _unit(dim, 0), now)
    repo._mark_unified_kg_dirty(nb.id)

    idx = repo._scale_index(nb.id, allow_stale=True)
    assert idx is not None and idx.ann_labels
    real_ann = repo._open_scale_ann(idx, "kg")
    assert real_ann is not None
    proxy = _CountingAnn(real_ann)
    cmap = repo.cluster_map(nb.id)
    new_objs = [{"object_id": "ko-new", "name": "MoE Gating"}]
    cands = repo._tier2_bridge_candidates_ann(nb.id, idx, proxy, new_objs, cmap)

    # n_labels = 33 > initial k = 20, tail of the first window is a far claim
    # (sim ~0.1 < lo) -> early stop after exactly ONE knn round, no doubling.
    assert proxy.calls == 1
    pairs = {tuple(sorted((c["canonical_a"], c["canonical_b"]))) for c in cands}
    from app.services.kg_merge import _norm
    assert pairs == {tuple(sorted(("K-" + _norm("MoE Gating"), "K-" + _norm("Expert Routing"))))}


def test_tier2_ann_symbol_only_new_concept_never_bare_K(repo):
    """空 seed 守卫的第 5 个消费点(P0):_tier2_bridge_candidates_ann 是
    kg_merge.detect_bridge_candidates 的 ANN 双胞胎。符号-only 名的新 concept
    (_norm→"")必须以按对象唯一的哨兵 canonical(K-~oid)桥接,绝不退化成裸
    "K-" —— 否则该退化 canonical 会被写进 concept_merge_candidates,既与它自己
    真实簇(place_new_concepts 已守卫成 K-~oid)错位,又让所有符号名新概念互相污染。"""
    from app.models.schemas import NotebookCreate as _NC
    from app.services.kg_merge import _norm, seed_or_unique
    nb = repo.create_notebook(_NC(name="kb"))
    now = "2026-07-02T00:00:00"
    dim = 16
    # 一个已存量的真实 concept,符号名新概念的向量紧挨着它(必过 lo=0.82)。
    exist_vec = _mk_vec(dim, 0, 0.99, 1, 0.02)
    _seed_concept(repo, nb.id, "ko-exist", "Coinage Parity", "src-A", exist_vec, now)
    _build_small_base(repo, nb.id, [("ko-exist", "Coinage Parity", exist_vec)], now)
    repo.build_scale_index(nb.id)

    # 新 concept 名归一化为空 —— 修复前 my_cid 退化为裸 "K-"。
    _seed_concept(repo, nb.id, "ko-sym", "→", "src-B", _unit(dim, 0), now)
    repo._mark_unified_kg_dirty(nb.id)

    idx = repo._scale_index(nb.id, allow_stale=True)
    assert idx is not None and idx.ann_labels
    ann = repo._open_scale_ann(idx, "kg")
    assert ann is not None
    cmap = repo.cluster_map(nb.id)
    new_objs = [{"object_id": "ko-sym", "name": "→"}]
    cands = repo._tier2_bridge_candidates_ann(nb.id, idx, ann, new_objs, cmap)

    # 向量桥接照常发生(两者很近)……
    assert cands, "expected a bridge candidate — vectors are near"
    # ……但落在按对象唯一的哨兵 canonical 上,绝无裸 "K-"。
    sentinel = "K-" + seed_or_unique(_norm("→"), "ko-sym")   # == "K-~ko-sym"
    for c in cands:
        assert "K-" not in (c["canonical_a"], c["canonical_b"])
        assert sentinel in (c["canonical_a"], c["canonical_b"])
        assert "K-" + _norm("Coinage Parity") in (c["canonical_a"], c["canonical_b"])


def test_tier2_ann_skips_deprecated_concepts(repo, monkeypatch):
    """旧路径 existing_items 过滤 status!='deprecated';ANN 分支的命中必须做同样校验:
    指向 deprecated concept 的 hit 不产生候选(且不挤占 eligible 槽位)。"""
    from app.models.schemas import NotebookCreate as _NC
    from app.services.kg_merge import _norm
    nb = repo.create_notebook(_NC(name="kb"))
    now = "2026-07-02T00:00:00"
    dim = 16
    # Deprecated concept NEARER to the query than the alive one — if the ANN
    # branch forgot the status check it would bridge to the deprecated cluster.
    dep_vec = _mk_vec(dim, 0, 0.99, 1, 0.02)
    alive_vec = _mk_vec(dim, 0, 0.9, 2, (1 - 0.9 ** 2) ** 0.5)
    _seed_concept(repo, nb.id, "ko-dep", "Old Routing", "src-A", dep_vec, now)
    _seed_concept(repo, nb.id, "ko-alive", "Expert Routing", "src-A", alive_vec, now)
    _build_small_base(repo, nb.id, [("ko-dep", "Old Routing", dep_vec),
                                    ("ko-alive", "Expert Routing", alive_vec)], now)
    with repo._write() as db:
        db.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id='ko-dep'")
    repo.build_scale_index(nb.id)

    _seed_concept(repo, nb.id, "ko-new", "MoE Gating", "src-B", _unit(dim, 0), now)
    monkeypatch.setattr(repo.settings, "kg_incremental_tier2_max_entities", 0)
    repo.incremental_fuse_source(nb.id, "src-B")

    with repo._connect() as db:
        rows = db.execute(
            "SELECT canonical_a, canonical_b FROM concept_merge_candidates WHERE notebook_id=?",
            (nb.id,)).fetchall()
    pairs = {tuple(sorted((r["canonical_a"], r["canonical_b"]))) for r in rows}
    dep_cid = "K-" + _norm("Old Routing")
    assert all(dep_cid not in p for p in pairs)   # deprecated 概念绝不入候选
    assert pairs == {tuple(sorted(("K-" + _norm("MoE Gating"), "K-" + _norm("Expert Routing"))))}


def test_incremental_merge_pair_reads_delegate_on_caller_connections(repo, monkeypatch):
    import sqlite3
    nb = getattr(repo, "create_notebook")(NotebookCreate(name="merge delegation"))
    runtime = object.__getattribute__(repo, "_runtime")
    now = "2026-06-22T00:00:00"
    from app.services.kg_merge import _norm
    with getattr(repo, "_write")() as db:
        for oid, name, source_id, vector in (
            ("ko-old", "Expert Routing", "src-A", [1.0] + [0.0] * 15),
            ("ko-new", "MoE Gating", "src-B", [0.99] + [0.0] * 15),
        ):
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}),
                 "[]", source_id, now, now),
            )
            db.execute(
                "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                "VALUES (?,?,?,?)", (oid, nb.id, json.dumps(vector), now),
            )
        db.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("cc-old", nb.id, "K-" + _norm("Expert Routing"), "ko-old",
             "Expert Routing", "concept", now),
        )
    opened = set()
    original_connect = runtime.database.connect

    def traced_connect():
        db = original_connect()
        opened.add(id(db))
        return db

    monkeypatch.setattr(runtime.database, "connect", traced_connect)
    calls = []
    store = runtime.governance
    original = store.merge_candidate_pairs

    def wrapped(db, notebook_id, statuses):
        assert isinstance(db, sqlite3.Connection) and id(db) in opened
        calls.append((notebook_id, tuple(statuses), id(db)))
        return original(db, notebook_id, statuses)

    monkeypatch.setattr(store, "merge_candidate_pairs", wrapped)

    getattr(repo, "incremental_fuse_source")(nb.id, "src-B")

    assert [statuses for _nb, statuses, _db in calls] == [
        ("pending",), ("confirmed", "rejected", "deferred"),
    ]
    assert all(notebook_id == nb.id for notebook_id, _statuses, _db in calls)
