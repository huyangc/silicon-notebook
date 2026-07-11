from app.services.retrieval import score_relations, RELEVANCE_FLOOR

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
    # embedder_configured needs all four vars set so embed paths activate
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "http://fake-embed")
    monkeypatch.setenv("EMBED_API_KEY", "fake-key")
    monkeypatch.setenv("EMBED_MODEL", "fake-model")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


@pytest.fixture
def repo_no_embedder(tmp_path, monkeypatch):
    """No EMBED_* env vars → embedder_configured=False, so _embed_relations_batch
    is a no-op and relation_embeddings stays empty even though a FakeEmbedder is
    manually attached (mirrors test_mix_overlay.py's real fixture shape — the
    codebase's actual "no vector coverage" scenario, not a synthetic one)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _rel(rid, text):
    return {"id": rid, "source_object_id": "s", "target_object_id": "t",
            "edge_type": "derived_from", "text": text}


def test_score_relations_keyword_only_full_match_is_one():
    # 无向量、关键词全命中 → _fuse 归一化后 relevance == 1.0(与 score_knowledge 同尺)
    hits = score_relations("cascode", [_rel("r1", "Regulated Cascode —derived_from→ Cascode.")])
    assert hits and hits[0].relation_id == "r1"
    assert abs(hits[0].relevance - 1.0) < 1e-9


def test_score_relations_uses_explicit_sims_and_stays_bounded():
    # 语义路径用显式 sims(测试 embedder 无语义);relevance ∈ [0,1]
    hits = score_relations("zzz no keyword overlap", [_rel("r1", "alpha beta gamma")],
                           query_vector=[0.1] * 4, relation_sims={"r1": 0.9})
    assert hits and 0.0 <= hits[0].relevance <= 1.0
    assert hits[0].relevance > 0.5  # 语义 0.9 主导


def test_score_relations_drops_below_floor():
    # 关键词 0、无向量 → relevance 0 < floor → 丢弃
    hits = score_relations("totally unrelated terms", [_rel("r1", "alpha beta")])
    assert hits == []


def test_score_relations_sorted_desc():
    rels = [_rel("r1", "cascode mirror"), _rel("r2", "cascode output resistance gain")]
    hits = score_relations("cascode output resistance", rels)
    scores = [h.relevance for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert hits[0].relation_id == "r2"  # 更全匹配 query 的边排前


def test_retrieve_relations_scored_keyword_path(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "Regulated Cascode"}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []},
        {"local_id": "c", "object_type": "concept", "payload": {"name": "Current Mirror"}, "evidence": []},
    ]
    relations = [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "derived_from", "evidence": []},
        {"source_local_id": "c", "target_local_id": "b", "edge_type": "about", "evidence": []},
    ]
    repo.store_kg(nb.id, None, objects, relations)
    hits = repo.retrieval.candidates._retrieve_relations_scored(nb.id, "regulated cascode")
    assert hits, "应至少命中一条关系"
    assert "Regulated Cascode" in hits[0].text  # 含 'Regulated Cascode' 的边排第一(关键词)


def test_federated_retrieve_relations_spans_base(repo):
    base = repo.create_notebook(NotebookCreate(name="textbook"))
    repo.mark_notebook_base(base.id)
    repo.store_kg(base.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Bandgap Reference"}, "evidence": []},
         {"local_id": "b", "object_type": "concept", "payload": {"name": "PTAT Current"}, "evidence": []}],
        [{"source_local_id": "a", "target_local_id": "b", "edge_type": "depends_on", "evidence": []}])
    personal = repo.create_notebook(NotebookCreate(name="my notes"))
    hits = repo.federated_retrieve_relations(personal.id, "bandgap reference ptat")
    assert hits, "个人本应能联邦检索到 base 库的关系"
    assert hits[0].tier == "base"
    assert hits[0].notebook_id == base.id


# ── P0-1/2: 候选界定(top-K 先定再 hydration,空向量早退) ──────────────────
#
# Step 0 现状流结论(见 docs/superpowers/plans, task-2-report.md 详述):
# `_mix_retrieve`(chunk 问答默认 overlay 路径,chunk_kg_overlay_enabled 默认
# True + rerank 配置齐 + 库里有 KG 就走)调用 `_chunk_kg_overlay` →
# `federated_retrieve_relations` → `_retrieve_relations_scored`，这条路径
# **不受** RELATION_RETRIEVAL_ENABLED 门控(该 flag 只挂在 `_graph_seed_fusion`
# 上，是 reasoning 模式的另一条路)。所以即便 RELATION_RETRIEVAL_ENABLED=False
# (默认)，只要 notebook 有 KG，每次默认 chunk 问答都会跑 `_relations_with_names`
# 的全量 JOIN——这正是 P0-2 的问题。
#
# 但 `_relations_with_names` 的产出文本同时喂给 score_relations 的关键词打分
# (不只是语义)，所以"先按向量 sim 定 top-K 再 hydrate"只有在向量矩阵非空时
# 才是安全的候选信号；向量矩阵整体为空(no embedder / 未回填)时唯一安全的
# 界定就是"关系表本身是空的"早退——否则会静默丢弃纯关键词命中(真实存在的
# 场景，test_mix_overlay.py 的 fixture 就是 embedder_configured=False 但手动
# 挂了 FakeEmbedder，relation_embeddings 永远不会被填，只能靠关键词命中)。

def _mk_relation(nb_local_ids, src, tgt, edge_type="about"):
    return {"source_local_id": src, "target_local_id": tgt, "edge_type": edge_type, "evidence": []}


def _seed_many_relations(repo, nb_id, n_pairs):
    """n_pairs 个 concept 对，每对一条 edge_type='about' 关系，payload 名字各异
    以便关键词/语义都有可区分信号。"""
    objects = []
    relations = []
    for i in range(n_pairs):
        objects.append({"local_id": f"s{i}", "object_type": "concept",
                        "payload": {"name": f"Source Concept {i} cascode"}, "evidence": []})
        objects.append({"local_id": f"t{i}", "object_type": "concept",
                        "payload": {"name": f"Target Concept {i} mirror"}, "evidence": []})
        relations.append(_mk_relation(None, f"s{i}", f"t{i}"))
    repo.store_kg(nb_id, None, objects, relations)


def _oracle_relations_with_names(repo, notebook_id):
    """旧(界定前)_relations_with_names 全量实现，逐字拷贝作 oracle。"""
    import json as _json
    from app.services.retrieval import relation_embed_text, _payload_text
    with repo._connect() as db:
        rows = db.execute(
            "SELECT r.id AS id, r.source_object_id AS s, r.target_object_id AS t, "
            "r.edge_type AS et, r.evidence AS ev, so.payload AS sp, tp.payload AS tpl "
            "FROM knowledge_relations r "
            "JOIN knowledge_objects so ON so.id = r.source_object_id "
            "JOIN knowledge_objects tp ON tp.id = r.target_object_id "
            "WHERE r.notebook_id = ?", (notebook_id,)).fetchall()
    out = []
    for r in rows:
        spans = [e.get("quoted_span", "") for e in _json.loads(r["ev"] or "[]")
                 if isinstance(e, dict)]
        src_name = _payload_text(_json.loads(r["sp"] or "{}"))[:80]
        tgt_name = _payload_text(_json.loads(r["tpl"] or "{}"))[:80]
        out.append({
            "id": r["id"], "source_object_id": r["s"], "target_object_id": r["t"],
            "edge_type": r["et"],
            "text": relation_embed_text(src_name, r["et"], tgt_name, spans),
        })
    return out


def _oracle_retrieve_relations_scored(repo, notebook_id, query):
    """旧(界定前)_retrieve_relations_scored 全量实现，逐字拷贝作 oracle。"""
    from app.services.retrieval import score_relations
    from app.services.vector_index import query_sims
    relations = _oracle_relations_with_names(repo, notebook_id)
    query_vector = repo.retrieval.candidates._embed_query(query)
    with repo._connect() as db:
        rel_ids, rel_mat = repo.retrieval.candidates._vector_matrix(db, notebook_id, "relation_embeddings", "relation_id")
    relation_sims = query_sims(query_vector, rel_ids, rel_mat) if query_vector else None
    return score_relations(query, relations, query_vector=query_vector,
                           relation_sims=relation_sims,
                           downweight_edges=repo.settings.kg_about_downweight_enabled)


def _connect_spy(repo, monkeypatch):
    calls = {"n": 0}
    orig = repo._connect

    def _wrapped():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(repo, "_connect", _wrapped)
    return calls


def _execute_spy(repo, monkeypatch):
    """Spy on every SQL statement executed via repo._connect()'s connections,
    using sqlite3's built-in trace callback (Connection is an immutable C type
    so its .execute cannot be monkeypatched directly). Wraps repo._connect to
    install the callback on each new connection. Returns a list appended to
    live with every executed SQL string."""
    sqls = []
    orig_connect = repo._connect

    def _wrapped():
        conn = orig_connect()
        conn.set_trace_callback(sqls.append)
        return conn

    monkeypatch.setattr(repo, "_connect", _wrapped)
    return sqls


def test_retrieve_relations_scored_empty_relations_no_join(repo, monkeypatch):
    """空 notebook(zero relations) → 不执行任何 relations JOIN，直接 []。"""
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    sqls = _execute_spy(repo, monkeypatch)
    hits = repo.retrieval.candidates._retrieve_relations_scored(nb.id, "anything")
    assert hits == []
    assert not any("FROM knowledge_relations r" in s for s in sqls)


def test_retrieve_relations_scored_no_embedder_keeps_keyword_path(repo_no_embedder):
    """relation_embeddings 整体为空(no embedder,如 test_mix_overlay.py 的真实
    fixture)时，必须保留关键词全量回退——不能静默丢弃纯关键词命中。"""
    repo = repo_no_embedder
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "Regulated Cascode"}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []},
    ]
    relations = [{"source_local_id": "a", "target_local_id": "b", "edge_type": "derived_from", "evidence": []}]
    repo.store_kg(nb.id, None, objects, relations)
    with repo._connect() as db:
        rel_ids, _ = repo.retrieval.candidates._vector_matrix(db, nb.id, "relation_embeddings", "relation_id")
    assert rel_ids == [], "fixture 前提:no embedder → relation_embeddings 应为空"
    hits = repo.retrieval.candidates._retrieve_relations_scored(nb.id, "regulated cascode")
    assert hits, "关键词全命中应仍能检索到(向量矩阵空不代表无法检索)"
    assert "Regulated Cascode" in hits[0].text
    want = _oracle_retrieve_relations_scored(repo, nb.id, "regulated cascode")
    got_t = [(h.relation_id, round(h.relevance, 9), round(h.score, 9)) for h in hits]
    want_t = [(h.relation_id, round(h.relevance, 9), round(h.score, 9)) for h in want]
    assert got_t == want_t, "空向量回退分支应与旧全量实现逐位等价"


def test_retrieve_relations_scored_equals_oracle_with_vectors(repo):
    """有向量覆盖时：候选界定后的结果(集合/分数/排序)与旧全量 oracle 一致
    (relation_recall 远大于关系数，界定不裁剪任何东西)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_many_relations(repo, nb.id, n_pairs=5)
    got = repo.retrieval.candidates._retrieve_relations_scored(nb.id, "cascode mirror concept")
    want = _oracle_retrieve_relations_scored(repo, nb.id, "cascode mirror concept")
    got_t = [(h.relation_id, round(h.relevance, 9), round(h.score, 9)) for h in got]
    want_t = [(h.relation_id, round(h.relevance, 9), round(h.score, 9)) for h in want]
    assert got_t == want_t


def test_retrieve_relations_scored_bounded_by_relation_recall(repo, monkeypatch):
    """relation_recall 设小于关系总数时:候选界定生效(只 hydrate top-K)，
    _relations_with_names 的全量(无 relation_ids)分支不再被调用。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_many_relations(repo, nb.id, n_pairs=20)
    monkeypatch.setattr(repo.settings, "relation_recall", 5)
    orig = repo.retrieval.candidates._relations_with_names
    calls = []

    def _spy(db, notebook_id, relation_ids=None):
        calls.append(relation_ids)
        return orig(db, notebook_id, relation_ids=relation_ids)

    monkeypatch.setattr(repo.retrieval.candidates, "_relations_with_names", _spy)
    hits = repo.retrieval.candidates._retrieve_relations_scored(nb.id, "cascode mirror concept")
    assert len(calls) == 1
    assert calls[0] is not None and len(calls[0]) == 5   # bounded, not full 20
    assert len(hits) <= 5


def test_relations_with_names_relation_ids_filters_join(repo):
    """relation_ids= 参数只 JOIN 那几行；空列表直接 [] 不查库。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_many_relations(repo, nb.id, n_pairs=3)
    with repo._connect() as db:
        full = repo.retrieval.candidates._relations_with_names(db, nb.id)
        assert len(full) == 3
        want_id = full[0]["id"]
        bounded = repo.retrieval.candidates._relations_with_names(db, nb.id, relation_ids=[want_id])
        assert [r["id"] for r in bounded] == [want_id]
        assert repo.retrieval.candidates._relations_with_names(db, nb.id, relation_ids=[]) == []


def test_relations_with_names_relation_ids_chunks_large_lists(repo, monkeypatch):
    """relation_ids 超过 _IN_CHUNK 时分批查询，仍返回全部命中行(无丢失)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_many_relations(repo, nb.id, n_pairs=5)
    monkeypatch.setattr(repo.retrieval.candidates, "_IN_CHUNK", 2)   # force multi-batch with only 5 rows
    with repo._connect() as db:
        full = repo.retrieval.candidates._relations_with_names(db, nb.id)
        ids = [r["id"] for r in full]
        bounded = repo.retrieval.candidates._relations_with_names(db, nb.id, relation_ids=ids)
    assert {r["id"] for r in bounded} == set(ids)
    assert len(bounded) == len(ids)


def test_mix_retrieve_overlay_no_relations_no_join(repo, monkeypatch):
    """_mix_retrieve 走 overlay(chunk_kg_overlay_enabled 默认开)时，notebook
    无任何 relation → 不触发 relations 全量 JOIN。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []}],
        [])
    sqls = _execute_spy(repo, monkeypatch)
    cand, block, id_map, kg_hits, ppr_n = repo.retrieval.candidates._mix_retrieve(nb.id, "cascode", "", ["cascode"])
    assert not any("FROM knowledge_relations r" in s for s in sqls)


def test_mix_retrieve_overlay_equivalent_with_relations(repo):
    """_mix_retrieve overlay 输出(kg_hits 集合/relevance)与界定前逻辑等价——
    用 relation_recall 足够大(默认 200 >> 关系数)确保不裁剪，直接对比
    federated_retrieve_relations 的候选界定结果与 oracle 全量结果一致。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_many_relations(repo, nb.id, n_pairs=4)
    got = repo.federated_retrieve_relations(nb.id, "cascode mirror concept")
    want = _oracle_retrieve_relations_scored(repo, nb.id, "cascode mirror concept")
    got_ids = sorted(h.relation_id for h in got)
    want_ids = sorted(h.relation_id for h in want)
    assert got_ids == want_ids
