"""T4:四个不走 build_matrix 的显式旁路接运行时截断 helper。

这些位点逐条 decode 向量做相似度,不经 build_matrix(收口 A)——漏接 = 查询侧
(_embed_query,T2 已截断)与语料侧混维 → cosine 对 len 不等静默返 0.0(retrieval.py)
或 zip 静默算错值(conflict_detect._cosine_sim)。原则:**先按存储维 embed_dim 过滤
(既有语义,滤旧 embedder 异维残留),通过后再 truncate_vec 截断** —— 两步分离。

覆盖(计划 §1.2「四个显式旁路」+ conflict):
  1. _gather_elements                — element 兜底逐向量 cosine;
  2. incremental_fuse_source Tier2   — 暴力分支(detect_bridge_candidates 收运行时维);
  3. _tier2_bridge_candidates_ann    — 守卫改比运行时生效维 + new_vecs 截断;
  4. _stream_seed_reps Pass B        — BLOB 与 legacy-JSON 两分支(后者绕过
                                       decode_vector,是计划点名的已知漏点);
  5. resolve_notebook_conflicts      — 补缺失的存储维门 + 截断;
     conflict_detect._cosine_sim     — 混维零容忍(显式返 0.0,不再 zip 静默)。

计划:docs/superpowers/specs/2026-07-03-runtime-dim-1024-plan.md T4。
"""
import json

import numpy as np
import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from app.services.vector_index import encode_vector
from tests.model_testkit import bind_chat_client, bind_all_embedding_clients

NOW = "2026-07-03T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """存储维 32 / 运行时维 16 的双维 repo(同 test_runtime_dim_truncation.dual_dim_repo:
    FakeEmbedder 按存储原生维出向量,截断只发生在读侧)。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_RUNTIME_DIM", "16")
    for k, v in {"EMBED_DIM": "32"}.items():
        monkeypatch.setenv(k, v)
    from app.core.config import get_settings
    get_settings.cache_clear()
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=32))
    yield r
    get_settings.cache_clear()


def _insert_ko(repo, nb_id, oid, name, *, object_type="concept", source_id="s1",
               payload_extra=None):
    payload = {"name": name}
    payload.update(payload_extra or {})
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,'approved',?,'[]',?,?,?)",
            (oid, nb_id, object_type, json.dumps(payload), source_id, NOW, NOW))


def _insert_kvec(repo, nb_id, oid, vec, *, blob=True):
    raw = encode_vector(vec) if blob else json.dumps([float(x) for x in vec])
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            (oid, nb_id, raw, NOW))


def _insert_source(repo, nb_id, sid):
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)", (sid, nb_id, sid, "md", "ready", NOW, NOW))


# ── 1. _gather_elements:element 兜底逐向量 cosine ───────────────────────────

def test_gather_elements_vector_truncated_and_same_space_as_query(repo):
    """element vector 截断到运行时维;与 _embed_query(T2 已截断)同空间 →
    retrieval.cosine 非零(len 不等时它静默返 0.0,正是要防的静默模式)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _insert_source(repo, nb.id, "s1")
    text = "bandgap reference startup circuit"
    with repo._write() as db:
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,text,created_at) "
            "VALUES (?,?,?,?,?,?)", ("e1", "s1", "paragraph", "p1", text, NOW))
        db.execute(
            "INSERT INTO element_embeddings (element_id,source_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?,?)",
            ("e1", "s1", nb.id, json.dumps(repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([text])[0]), NOW))

    with repo._connect() as db:
        elements = repo._gather_elements(db, nb.id)
    assert len(elements) == 1
    vec = elements[0]["vector"]
    assert vec is not None and len(vec) == 16

    from app.services.retrieval import cosine
    q = repo._embed_query(text)
    assert len(q) == 16
    assert cosine(q, vec) == pytest.approx(1.0, abs=1e-4)   # 同文本、同空间 → 非零命中


# ── 2. incremental_fuse_source Tier2 暴力分支 ────────────────────────────────

def test_tier2_bruteforce_bridges_in_runtime_space(repo, monkeypatch):
    """存量向量按存储维过滤(既有语义)通过后截断;新对象向量同样截断——
    detect_bridge_candidates 收到的两侧向量全部 == 运行时维,且仍产出桥接候选。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _insert_source(repo, nb.id, "s_old")
    _insert_source(repo, nb.id, "s_new")
    vec = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts(["precision voltage reference"])[0]  # 32 维原生
    # 存量概念(已有簇)与新概念:名字不同(不同 canonical)、向量相同(cos=1.0 ≥ lo)
    _insert_ko(repo, nb.id, "eo1", "Bandgap Voltage Reference", source_id="s_old")
    _insert_kvec(repo, nb.id, "eo1", vec)
    _insert_ko(repo, nb.id, "no1", "Precision Voltage Source", source_id="s_new")
    _insert_kvec(repo, nb.id, "no1", vec, blob=False)   # 新侧走 legacy-JSON 格式
    # 异维残留(旧 embedder,8 维)必须被存储维门挡在截断之前
    _insert_ko(repo, nb.id, "alien", "Alien Residue", source_id="s_old")
    _insert_kvec(repo, nb.id, "alien", [0.5] * 8)
    repo.append_clusters(nb.id, [{"canonical_id": "K-bandgapvoltagereference",
                                  "member_object_id": "eo1",
                                  "canonical_name": "Bandgap Voltage Reference"}])

    captured = {}
    import app.services.kg_merge as kg_merge
    orig = kg_merge.detect_bridge_candidates

    def spy(new_items, new_vectors, existing_items, existing_vectors, *a, **kw):
        captured["new_dims"] = {k: len(v) for k, v in new_vectors.items()}
        captured["ex_dims"] = {k: len(v) for k, v in existing_vectors.items()}
        return orig(new_items, new_vectors, existing_items, existing_vectors, *a, **kw)

    monkeypatch.setattr(kg_merge, "detect_bridge_candidates", spy)
    repo.incremental_fuse_source(nb.id, "s_new")

    assert captured, "Tier2 暴力分支未被执行(前置条件被破坏)"
    assert captured["new_dims"] == {"no1": 16}
    assert captured["ex_dims"].get("eo1") == 16
    assert "alien" not in captured["ex_dims"]           # 存储维门:过滤先于截断
    with repo._connect() as db:
        cands = db.execute(
            "SELECT canonical_a, canonical_b, score FROM concept_merge_candidates "
            "WHERE notebook_id=? AND status='pending'", (nb.id,)).fetchall()
    assert cands, "Tier2 暴力分支应产出桥接候选(非空)"
    assert cands[0]["score"] >= 0.82


# ── 3. _tier2_bridge_candidates_ann:守卫比运行时生效维 + new_vecs 截断 ──────

class _FakeAnn:
    """记录查询维度的假 hnsw:恒返回 label 0 / distance 0.05(sim=0.95 ≥ lo)。"""
    def __init__(self):
        self.query_dims = []

    def set_ef(self, ef):
        pass

    def knn_query(self, q, k):
        self.query_dims.append(int(np.asarray(q).size))
        return np.array([[0]]), np.array([[0.05]], dtype=np.float32)


class _FakeIdx:
    def __init__(self, dim, labels):
        self.manifest = {"dim": dim}
        self.ann_labels = labels


def test_tier2_ann_guard_compares_runtime_dim_and_truncates_query(repo):
    """切换后持久 kg ANN 是运行时维(manifest.dim=16):守卫须比运行时生效维
    (而非 settings.embed_dim=32,否则永远 [] 静默跳过);查询向量截断后入 ANN。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _insert_source(repo, nb.id, "s1")
    _insert_ko(repo, nb.id, "ex1", "Bandgap Voltage Reference")   # 命中侧须 alive
    _insert_ko(repo, nb.id, "new1", "Precision Voltage Source")
    _insert_kvec(repo, nb.id, "new1", repo._runtime.models.embedding("retrieval_query_embedding").embed_texts(["precision"])[0])
    # PR-C:命中侧的 canonical 由方法自己按命中 id 定点折叠(不再由调用方喂一份
    # 整表 cluster_map),所以命中对象必须真有簇行。
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,canonical_description,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("cc-ex1", nb.id, "K-bandgapvoltagereference", "ex1",
             "Bandgap Voltage Reference", "concept", "", NOW))

    idx = _FakeIdx(dim=16, labels=["ex1"])
    ann = _FakeAnn()
    out = repo._tier2_bridge_candidates_ann(
        nb.id, idx, ann, [{"object_id": "new1", "name": "Precision Voltage Source"}],
        {"new1": ""})

    assert ann.query_dims and set(ann.query_dims) == {16}   # 查询已截断到运行时维
    assert out, "运行时维 ANN 下 Tier2 应产出候选而非被守卫静默拦下"
    assert out[0]["score"] >= 0.82


# ── 4. _stream_seed_reps Pass B:BLOB 与 legacy-JSON 双分支 ──────────────────

def test_stream_seed_reps_truncates_both_blob_and_json_branches(repo):
    """legacy-JSON 分支绕过 decode_vector(计划点名的已知漏点)——两分支都须:
    按存储维过滤(既有语义)通过后、rep 累加前截断;rep 维 == 运行时维。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _insert_source(repo, nb.id, "s1")
    v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts(["low dropout regulator"])[0]
    _insert_ko(repo, nb.id, "c1", "Low Dropout Regulator")
    _insert_kvec(repo, nb.id, "c1", v, blob=True)            # BLOB 分支
    _insert_ko(repo, nb.id, "c2", "Low Dropout Regulator")
    _insert_kvec(repo, nb.id, "c2", v, blob=False)           # legacy-JSON 分支
    _insert_ko(repo, nb.id, "c3", "Gamma Corrector")
    _insert_kvec(repo, nb.id, "c3", [0.5] * 8, blob=False)   # 异维残留 → 过滤

    from app.services.kg_merge import _norm
    reps, members, first = repo._stream_seed_reps(
        nb.id, "concept", lambda o: _norm(o["name"]), run_id="t4", compute_reps=True)

    assert reps, "reps 非空"
    seed = _norm("Low Dropout Regulator")
    assert seed in reps
    assert all(r.size == 16 for r in reps.values())          # rep 维 == 运行时维
    assert members[seed] == 2                                # 两分支成员都累加了
    assert _norm("Gamma Corrector") not in reps              # 异维残留仍按既有语义跳过


# ── 5. resolve_notebook_conflicts:补存储维门 + 截断 ─────────────────────────

def test_conflict_embeddings_gated_by_storage_dim_then_truncated(repo, monkeypatch):
    """conflict 的向量装载现连 dim 过滤都没有 → 补:先存储维门(异维残留出局),
    通过后截断到运行时维,detect_conflict_candidates 收到的全是同维向量。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _insert_source(repo, nb.id, "s1")
    _insert_ko(repo, nb.id, "o1", "gain is high", object_type="claim",
               payload_extra={"statement": "gain is high"})
    _insert_kvec(repo, nb.id, "o1", repo._runtime.models.embedding("retrieval_query_embedding").embed_texts(["gain is high"])[0])
    _insert_ko(repo, nb.id, "o2", "gain is low", object_type="claim",
               payload_extra={"statement": "gain is low"})
    _insert_kvec(repo, nb.id, "o2", [0.5] * 8)               # 异维残留

    class _StubLLM:
        configured = True

    bind_chat_client(repo, "kg_conflict_review", _StubLLM())
    captured = {}
    import app.services.kg.conflict_detect as cd

    def spy(objects, relations, embeddings=None, **kw):
        captured["dims"] = ({k: len(v) for k, v in embeddings.items()}
                            if embeddings else {})
        return []                                            # 空候选 → 不触 LLM 后续

    monkeypatch.setattr(cd, "detect_conflict_candidates", spy)
    summary = repo.resolve_notebook_conflicts(nb.id)

    assert summary["detected"] == 0 and not summary["skipped_llm"]
    assert captured["dims"] == {"o1": 16}                    # 截断到运行时维;异维出局


def test_conflict_cosine_sim_rejects_mixed_dims():
    """conflict_detect._cosine_sim 混维零容忍:zip 会静默按短边截断算出错误
    相似度 → 改显式返 0.0;等长行为不变。"""
    from app.services.kg.conflict_detect import _cosine_sim
    assert _cosine_sim([1.0] * 32, [1.0] * 16) == 0.0
    assert _cosine_sim([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine_sim([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
