"""生产事故修复:1.13M 节点库上 reasoning 模式冻结(内存持续攀升)。

机制:_ppr_retrieve 见 scale_ppr 返回 [] 时静默回退 self._ppr_graph(notebook_id)
= 全量 rustworkx 图构建(百万级节点/千万级边,Python 边循环 → 数十分钟 + 数 GB)。

Fix 1:大库(not notebook_copy_stats()["copyable"])场景下,_ppr_retrieve 遇到
scale_ppr 返回 [] 时不再构建 rustworkx 图,而是发 ppr_fallback_refused 事件 + 返回 []。
小库场景字节不变地保留旧回退路径。

Fix 2:scale_ppr 的每个 return [] 出口都发 scale_ppr_bailout 事件(fail-open,
仅 bail 路径产生,成功路径零开销),其中 zero_reset 额外带种子诊断,方便生产环境
仅凭 events.jsonl 就能诊断 scale_ppr 为何返回空。
"""
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


@pytest.fixture
def embed_repo(tmp_path, monkeypatch):
    """同 repo,但 embedder_configured=True(设置 EMBED_* env),供需要真实向量
    产出(如 knowledge_embeddings → ANN ann_labels 非空)的用例使用。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_two_doc_moe(repo, suffix=""):
    """两个源,各自一个聚到同簇的 MoE concept 节点,各自证据指向自己源里的 chunk。
    与 tests/test_ppr_retrieve.py 中的同名 helper 保持一致写法(独立副本,避免跨文件依赖)。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    sfx = suffix
    with repo._write() as db:
        now = "2026-06-22T00:00:00"
        src_a, src_b = f"src-A{sfx}", f"src-B{sfx}"
        cA, cB = f"cA{sfx}", f"cB{sfx}"
        e1, e2 = f"e1{sfx}", f"e2{sfx}"
        elA, elB = f"elA{sfx}", f"elB{sfx}"
        for sid, title in [(src_a, "DeepSeek paper"), (src_b, "GLM paper")]:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (sid, nb.id, title, "md", "ready", now, now))
        for cid, sid, text in [(cA, src_a, "DeepSeek-V3 uses Mixture-of-Experts (MoE) routing."),
                                (cB, src_b, "GLM also adopts a Mixture-of-Experts (MoE) design.")]:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (cid, nb.id, sid, text, "Intro", json.dumps([f"el-{cid}"]), now))
        for eid, sid, elid, cid, name in [
            (e1, src_a, elA, cA, "Mixture-of-Experts (MoE)"),
            (e2, src_b, elB, cB, "Mixture-of-Experts (MoE)"),
        ]:
            ev = json.dumps([{"source_id": sid, "source_title": "", "element_id": elid,
                              "element_type": "paragraph", "location_label": "p1",
                              "quoted_span": "MoE", "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (eid, nb.id, "concept", "approved", "", json.dumps({"name": name}), ev, sid, now, now))
        for eid, cid in [(e1, cA), (e2, cB)]:
            db.execute("INSERT INTO knowledge_relations "
                       "(id,notebook_id,source_object_id,target_object_id,edge_type,evidence,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"mem-{eid}", nb.id, eid, f"chunk:{cid}", "evidenced_by", "[]", now))
        for eid, name in [(e1, "Mixture-of-Experts (MoE)"), (e2, "Mixture-of-Experts (MoE)")]:
            db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
                       "canonical_name,object_type,created_at) VALUES (?,?,?,?,?,?,?)",
                       (f"cl-{eid}", nb.id, "K-moe", eid, name, "concept", now))
    return nb


def _capture_events(repo, monkeypatch):
    events = []
    orig_emit = repo.event_log.emit

    def spy_emit(event, **kw):
        events.append(event)
        return orig_emit(event, **kw)

    monkeypatch.setattr(repo.event_log, "emit", spy_emit)
    return events


# ── Fix 1: large-notebook fallback guard ────────────────────────────────────

def test_large_notebook_refuses_rustworkx_fallback(repo, monkeypatch):
    """大库(copyable=False)+ scale_ppr 强制返回空 → _ppr_retrieve 绝不调用
    self._ppr_graph(spy 验证零调用),直接返回 [],并发 ppr_fallback_refused 事件。"""
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.retrieval.graph, "scale_ppr", lambda notebook_id, question: [])
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats", lambda notebook_id: {"copyable": False, "size": {}})

    called = {"n": 0}
    orig_ppr_graph = repo.retrieval.graph._ppr_graph

    def spy_ppr_graph(notebook_id):
        called["n"] += 1
        return orig_ppr_graph(notebook_id)

    monkeypatch.setattr(repo.retrieval.graph, "_ppr_graph", spy_ppr_graph)
    events = _capture_events(repo, monkeypatch)

    result = repo.retrieval.graph._ppr_retrieve(nb.id, "Mixture of Experts")

    assert result == []
    assert called["n"] == 0, "large notebook must NOT build the rustworkx graph"
    refusal = [e for e in events if e.get("kind") == "ppr_fallback_refused"]
    assert len(refusal) == 1
    assert refusal[0]["notebook_id"] == nb.id
    assert refusal[0]["reason"] == "large_notebook"


def test_small_notebook_keeps_legacy_fallback(repo, monkeypatch):
    """小库(copyable=True)+ scale_ppr 返回空 → 旧行为不变:调用 self._ppr_graph
    (spy 验证被调用),不发 ppr_fallback_refused 事件。"""
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.retrieval.graph, "scale_ppr", lambda notebook_id, question: [])
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats", lambda notebook_id: {"copyable": True, "size": {}})

    called = {"n": 0}
    orig_ppr_graph = repo.retrieval.graph._ppr_graph

    def spy_ppr_graph(notebook_id):
        called["n"] += 1
        return orig_ppr_graph(notebook_id)

    monkeypatch.setattr(repo.retrieval.graph, "_ppr_graph", spy_ppr_graph)
    events = _capture_events(repo, monkeypatch)

    result = repo.retrieval.graph._ppr_retrieve(nb.id, "Mixture of Experts")

    assert called["n"] == 1, "small notebook must keep legacy rustworkx fallback"
    # cross-doc bridge should still work via the legacy path (byte-identical behavior)
    ids = [c.chunk_id for c in result]
    assert f"cA" in ids and f"cB" in ids
    refusal = [e for e in events if e.get("kind") == "ppr_fallback_refused"]
    assert refusal == []


def test_ppr_retrieve_success_path_emits_no_fallback_events(repo, monkeypatch):
    """scale_ppr 命中(非空)时,_ppr_retrieve 完全不走 fallback 分支 —— 无
    ppr_fallback_refused 事件,也不调用 _ppr_graph。"""
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.retrieval.graph, "scale_ppr", lambda notebook_id, question: [("cA", 1.0)])

    called = {"n": 0}
    monkeypatch.setattr(repo.retrieval.graph, "_ppr_graph", lambda notebook_id: called.__setitem__("n", called["n"] + 1))
    events = _capture_events(repo, monkeypatch)

    result = repo.retrieval.graph._ppr_retrieve(nb.id, "Mixture of Experts")

    assert called["n"] == 0
    assert [e for e in events if e.get("kind") == "ppr_fallback_refused"] == []
    assert [c.chunk_id for c in result] == ["cA"]


# ── Fix 2: scale_ppr bailout observability ──────────────────────────────────

def test_scale_ppr_no_participants_bailout_event(repo, monkeypatch):
    """无任何可用 base/self scale 索引 → 早退 [] + scale_ppr_bailout(no_participants)。"""
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    events = _capture_events(repo, monkeypatch)

    result = repo.retrieval.graph.scale_ppr(nb.id, "anything")

    assert result == []
    bail = [e for e in events if e.get("kind") == "scale_ppr_bailout"]
    assert len(bail) == 1
    assert bail[0]["notebook_id"] == nb.id
    assert bail[0]["reason"] == "no_participants"


def test_scale_ppr_zero_reset_bailout_carries_seed_diagnostics(repo, monkeypatch):
    """强制无 embedding(qvec=None)+ chunk 检索为空 → reset 全零 → scale_ppr_bailout
    (zero_reset)事件携带种子诊断(ann_seeds/active_seeds/chunk_seeds/embed_ok)。"""
    nb = _seed_two_doc_moe(nb_repo := repo)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (nb.id,))

    monkeypatch.setattr(repo.retrieval.candidates, "_embed_query", lambda question: None)
    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks", lambda notebook_id, query, recall=0: ([], [], None))

    events = _capture_events(repo, monkeypatch)
    result = repo.retrieval.graph.scale_ppr(nb.id, "anything")

    assert result == []
    bail = [e for e in events if e.get("kind") == "scale_ppr_bailout"]
    assert len(bail) == 1
    ev = bail[0]
    assert ev["notebook_id"] == nb.id
    assert ev["reason"] == "zero_reset"
    for key in ("ann_seeds", "active_seeds", "chunk_seeds", "embed_ok"):
        assert key in ev, f"missing seed diagnostic: {key}"
    assert ev["embed_ok"] is False
    assert ev["ann_seeds"] == 0
    assert ev["active_seeds"] == 0
    assert ev["chunk_seeds"] == 0


def test_scale_ppr_zero_reset_folds_ann_source_skip_counts(embed_repo, monkeypatch):
    """dim 不匹配 / _open_scale_ann None 的逐 base 跳过,折叠进 zero_reset 诊断的
    ann_sources_skipped 计数(而非一个 base 一条事件)。需要 KG 节点先有向量
    (knowledge_embeddings)才会产生非空 ann_labels,让 _open_scale_ann 分支被真正触达
    (故用 embed_repo:embedder_configured=True 才会真正写 knowledge_embeddings)。"""
    repo = embed_repo
    nb = _seed_two_doc_moe(repo)
    repo._embed_knowledge("e1", nb.id, {"name": "Mixture-of-Experts (MoE)"})
    repo._embed_knowledge("e2", nb.id, {"name": "Mixture-of-Experts (MoE)"})
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (nb.id,))

    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks", lambda notebook_id, query, recall=0: ([], [], None))
    monkeypatch.setattr(repo.retrieval.graph, "_open_scale_ann", lambda idx, kind: None)  # force ANN-open skip
    # 3b (active brute-force cosine) uses knowledge_embeddings directly and would
    # otherwise still seed reset (this notebook is both base and active/self) —
    # neutralize it so the zero_reset bail is solely due to the ANN-open skip.
    monkeypatch.setattr(repo.retrieval.candidates, "_vector_matrix", lambda db, nb_id, table, key: ([], None))

    events = _capture_events(repo, monkeypatch)
    result = repo.retrieval.graph.scale_ppr(nb.id, "Mixture of Experts")

    assert result == []
    bail = [e for e in events if e.get("kind") == "scale_ppr_bailout"]
    assert len(bail) == 1
    assert bail[0]["reason"] == "zero_reset"
    assert bail[0]["ann_sources_skipped"] >= 1


def test_scale_ppr_success_path_emits_no_bailout_event(repo, monkeypatch):
    """成功路径(命中非空排名)不应发任何 scale_ppr_bailout 事件(cheap:只在 bail 路径产生)。"""
    nb = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (nb.id,))

    events = _capture_events(repo, monkeypatch)
    result = repo.retrieval.graph.scale_ppr(nb.id, "Mixture of Experts")

    assert result != []
    assert [e for e in events if e.get("kind") == "scale_ppr_bailout"] == []


# ── Fix wave: graph 模式大库建图守卫(ask_graph / _chunk_kg_overlay) ─────────

def _spy_fed_rx_graph(repo, monkeypatch):
    """Spy on _federated_rx_graph — counts calls, delegates to the original."""
    called = {"n": 0}
    orig = repo.retrieval.graph._federated_rx_graph

    def spy(notebook_id):
        called["n"] += 1
        return orig(notebook_id)

    monkeypatch.setattr(repo.retrieval.graph, "_federated_rx_graph", spy)
    return called


def test_ask_graph_large_notebook_refuses_graph_walk(repo, monkeypatch):
    """大库(copyable=False)graph 模式端到端:PPR 分支空手(scale_ppr 无索引
    bail + Fix1 拒绝 rustworkx 回退)后,ask_graph 不得再触发 _federated_rx_graph
    全量建图 —— 早退带解释的降级回答(deterministic)+ graph_walk_refused 事件。

    大库分支下 federated_retrieve/_retrieve_scored 也已改走 FTS 词法有界兜底
    (kg_bruteforce_refused 守卫);_seed_two_doc_moe 用裸 SQL 插入 knowledge_objects,
    绕过了真实入库管线维护 kg_objects_fts 的那一步,故这里手工补一行 FTS 记录
    (镜像真实入库行为),让 federated_retrieve 仍能词法命中种子对象、
    ask_graph 得以推进到本测试真正要验证的 graph_walk_refused 守卫,
    而不是提前落到"无匹配知识"的更早退出分支。"""
    nb = _seed_two_doc_moe(repo)
    with repo._write() as db:
        db.execute("INSERT INTO kg_objects_fts (object_id, notebook_id, name) VALUES (?,?,?)",
                   ("e1", nb.id, "DeepSeek MoE 架构?"))
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda notebook_id: {"copyable": False, "size": {}})
    called = _spy_fed_rx_graph(repo, monkeypatch)
    events = _capture_events(repo, monkeypatch)

    from app.models.schemas import AskRequest
    resp = repo.ask_graph(nb.id, AskRequest(question="DeepSeek MoE 架构?", mode="graph"))

    assert called["n"] == 0, "large notebook must NOT build the federated rustworkx graph"
    assert resp.mode == "graph"
    assert resp.llm_mode == "deterministic"
    assert resp.conclusion  # 有解释文案,不是空答案
    refused = [e for e in events if e.get("kind") == "graph_walk_refused"]
    assert len(refused) == 1
    assert refused[0]["notebook_id"] == nb.id
    assert refused[0]["reason"] == "large_notebook"


def test_ask_graph_small_notebook_builds_graph_unchanged(repo, monkeypatch):
    """小库 graph 模式行为不变:关掉 PPR 分支走 BFS 图路径,_federated_rx_graph
    照常被调用,无 graph_walk_refused 事件。"""
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    called = _spy_fed_rx_graph(repo, monkeypatch)
    events = _capture_events(repo, monkeypatch)

    from app.models.schemas import AskRequest
    resp = repo.ask_graph(nb.id, AskRequest(question="MoE", mode="graph"))

    assert called["n"] >= 1, "small notebook keeps the legacy graph walk"
    assert resp.mode == "graph"
    assert [e for e in events if e.get("kind") == "graph_walk_refused"] == []


def test_chunk_kg_overlay_large_notebook_skips_graph(repo, monkeypatch):
    """chunk 模式 KG overlay(第二个请求路径调用方):大库跳过 _federated_rx_graph,
    返回空 overlay(("", {}, []),ask_chunk 继续用向量/PPR chunk 作答)+ 事件。"""
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda notebook_id: {"copyable": False, "size": {}})
    called = _spy_fed_rx_graph(repo, monkeypatch)
    events = _capture_events(repo, monkeypatch)

    block, id_map, kg_hits = repo.retrieval.candidates._chunk_kg_overlay(nb.id, "Mixture of Experts", "MoE", id_offset=1000)

    assert called["n"] == 0
    assert (block, id_map, kg_hits) == ("", {}, [])
    refused = [e for e in events if e.get("kind") == "graph_walk_refused"]
    assert len(refused) == 1
    assert refused[0]["reason"] == "large_notebook"


def test_chunk_kg_overlay_small_notebook_builds_graph(repo, monkeypatch):
    """小库 chunk overlay 行为不变:_federated_rx_graph 照常被调用,无拒绝事件。"""
    nb = _seed_two_doc_moe(repo)
    called = _spy_fed_rx_graph(repo, monkeypatch)
    events = _capture_events(repo, monkeypatch)

    block, id_map, kg_hits = repo.retrieval.candidates._chunk_kg_overlay(nb.id, "Mixture of Experts", "MoE", id_offset=1000)

    assert called["n"] == 1
    assert kg_hits  # 命中种子,真走了 overlay
    assert [e for e in events if e.get("kind") == "graph_walk_refused"] == []


# ── Fix wave 3: scale_ppr 3b 在 self 已索引时跳过全量矩阵加载 ────────────────

def _spy_vector_matrix(repo, monkeypatch):
    """Spy on _vector_matrix — records (notebook_id, table) per call, delegates."""
    calls = []
    orig = repo.retrieval.candidates._vector_matrix

    def spy(db, notebook_id, table, id_col):
        calls.append((notebook_id, table))
        return orig(db, notebook_id, table, id_col)

    monkeypatch.setattr(repo.retrieval.candidates, "_vector_matrix", spy)
    return calls


def _seed_indexed_self_base(repo):
    """两文档 MoE 库 + 节点向量 + unified KG + scale index + tier=base。
    返回 notebook。self 即 participant(P0-00),3a 走它自己的 hnsw ANN。"""
    nb = _seed_two_doc_moe(repo)
    repo._embed_knowledge("e1", nb.id, {"name": "Mixture-of-Experts (MoE)"})
    repo._embed_knowledge("e2", nb.id, {"name": "Mixture-of-Experts (MoE)"})
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (nb.id,))
    return nb


def test_scale_ppr_self_indexed_skips_active_brute_force(embed_repo, monkeypatch):
    """成本分离不变量:self 已建索引(P0-00 self-participant)时,3b 不得再对同一库
    全量加载 knowledge_embeddings 矩阵(生产 49万×1024 = 数 GB/数十分钟)——
    self 种子已由 3a 经它自己的 hnsw ANN 覆盖。spy 断言 scale_ppr 全程不调
    _vector_matrix(knowledge_embeddings);排名仍含 self 的两个 chunk(种子覆盖不丢)。
    ppr_emb_synonym 关闭以隔离 combined-graph loader 里 _cross_layer_bridge 的
    同表加载(那是另一处、版本缓存内,见报告 Fix wave 3)。"""
    repo = embed_repo
    monkeypatch.setattr(repo.settings, "ppr_emb_synonym_enabled", False)
    nb = _seed_indexed_self_base(repo)

    calls = _spy_vector_matrix(repo, monkeypatch)
    ranked = repo.retrieval.graph.scale_ppr(nb.id, "Mixture of Experts")

    kg_loads = [c for c in calls if c[1] == "knowledge_embeddings"]
    assert kg_loads == [], (
        "self-indexed scale_ppr must not brute-force load knowledge_embeddings; "
        f"got loads: {kg_loads}")
    # 种子覆盖不劣化:3a(self ANN)已覆盖 self 节点 → 两个 self chunk 仍被排出。
    ranked_ids = {cid for cid, _ in ranked}
    assert {"cA", "cB"} <= ranked_ids


def test_scale_ppr_self_unindexed_keeps_active_brute_force(embed_repo, monkeypatch):
    """self 无索引(真正的小 active 库)+ 有 base 索引的联邦场景:3b 行为不变,
    照常对 active 库做有界暴力余弦(spy 断言 knowledge_embeddings 被加载,且
    加载的是 active 库自己的)。"""
    repo = embed_repo
    monkeypatch.setattr(repo.settings, "ppr_emb_synonym_enabled", False)
    base = _seed_indexed_self_base(repo)          # 联邦 base(有索引)
    active = _seed_two_doc_moe(repo, suffix="2")  # 小 active 库,无索引
    repo._embed_knowledge("e12", active.id, {"name": "Mixture-of-Experts (MoE)"})
    repo._embed_knowledge("e22", active.id, {"name": "Mixture-of-Experts (MoE)"})

    calls = _spy_vector_matrix(repo, monkeypatch)
    repo.retrieval.graph.scale_ppr(active.id, "Mixture of Experts")

    kg_loads = [c for c in calls if c[1] == "knowledge_embeddings"]
    assert (active.id, "knowledge_embeddings") in kg_loads, (
        "un-indexed active notebook must keep the bounded brute-force seed pass (3b)")
