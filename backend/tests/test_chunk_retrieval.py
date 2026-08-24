import json
from types import SimpleNamespace

import pytest
from app.services.retrieval import score_chunks, RetrievedChunk
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import AskRequest, NotebookCreate
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


def _ck(cid, text):
    return {"chunk_id": cid, "source_id": "s1", "source_title": "Doc",
            "section_path": "1", "text": text, "element_ids": ["e1"]}


def test_score_chunks_keyword_only_filters_floor():
    chunks = [_ck("c1", "deepseek mixture of experts routing"),
              _ck("c2", "unrelated cooking recipe tomato")]
    out = score_chunks("deepseek experts routing", chunks, query_vector=None, chunk_sims=None, limit=10)
    ids = [c.chunk_id for c in out]
    assert "c1" in ids and "c2" not in ids      # c2 低于 RELEVANCE_FLOOR 被丢
    assert all(isinstance(c, RetrievedChunk) for c in out)
    assert out[0].relevance > 0 and out[0].object_id == out[0].chunk_id


def test_score_chunks_caps_to_limit_sorted():
    chunks = [_ck(f"c{i}", f"shared term token{i}") for i in range(20)]
    out = score_chunks("shared term", chunks, query_vector=None, chunk_sims=None, limit=5)
    assert len(out) == 5
    assert all(out[i].score >= out[i+1].score for i in range(len(out)-1))


def test_score_chunks_deduplicates_same_source_text_before_limit():
    chunks = [
        _ck("header-1", "Cosmos 3: Omnimodal World Models for Physical AI"),
        _ck("header-2", "  Cosmos 3: Omnimodal\nWorld Models for Physical AI "),
        _ck("abstract", "Cosmos 3 introduces an omnimodal architecture."),
    ]

    out = score_chunks("Cosmos 3 omnimodal", chunks, limit=2)

    assert len(out) == 2
    assert sum(chunk.chunk_id.startswith("header-") for chunk in out) == 1
    assert "abstract" in {chunk.chunk_id for chunk in out}


def test_score_chunks_uses_semantic_sims():
    chunks = [_ck("c1", "no keyword overlap here")]
    # 仅语义信号(关键词 0): chunk_sims 给高余弦 → 仍能过 floor。
    out = score_chunks("totally different words", chunks,
                       query_vector=[0.1]*4, chunk_sims={"c1": 0.9}, limit=10)
    assert [c.chunk_id for c in out] == ["c1"]
    assert out[0].relevance >= 0.5


def test_score_chunks_lexical_only_candidate_is_keyword_renormalized():
    chunks = [_ck("lexical", "ZXCV9000 timing controller")]
    keyword_only = score_chunks(
        "ZXCV9000 controller", chunks, query_vector=None, chunk_sims=None, limit=10
    )
    mixed_window = score_chunks(
        "ZXCV9000 controller",
        chunks,
        query_vector=[0.1] * 4,
        chunk_sims={"semantic-other": 0.9},
        limit=10,
    )
    assert keyword_only and mixed_window
    assert mixed_window[0].score == pytest.approx(keyword_only[0].score)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_chunks(repo, texts):
    """建 notebook+source+elements, 走 P1 的 build+embed 真路径产出 chunks。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    import uuid
    sid = f"src-{uuid.uuid4().hex[:8]}"; now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb.id, "Doc", "document", "s.md", "/tmp/s.md", 0, "h", "", "", "extracted", now, now))
        for i, t in enumerate(texts, 1):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-{i:04d}", sid, "paragraph", f"p{i}", t, "{}", now))
    repo._chunk_and_embed_source(sid)
    return nb, sid


def test_retrieve_chunks_returns_scored_with_matrix(repo):
    nb, _ = _seed_chunks(repo, ["deepseek v3 mixture of experts " * 20,
                                "tomato soup cooking recipe " * 20])
    scored, ids, mat = repo._retrieve_chunks(nb.id, "deepseek experts")
    assert scored and scored[0].relevance > 0
    assert len(ids) >= 1 and mat.shape[0] == len(ids)


def test_mmr_select_caps_and_subsets(repo):
    nb, _ = _seed_chunks(repo, [f"shared topic alpha detail {i} " * 20 for i in range(8)])
    scored, ids, mat = repo._retrieve_chunks(nb.id, "shared topic alpha")
    picked = repo._mmr_select_chunks(scored, ids, mat, k=3, lambda_=0.5)
    assert len(picked) <= 3
    assert {p.chunk_id for p in picked} <= {c.chunk_id for c in scored}


def test_retrieve_chunks_uses_ann_when_enabled(repo, monkeypatch):
    # 10 个 chunk;开 chunk_ann_enabled + recall=3 时只对 ANN∪FTS 有界候选
    # (各≤3,合计≤6)打分,非全表 10 条。
    nb, _ = _seed_chunks(repo, [f"topic {i} content detail body " * 10 for i in range(10)])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id)
    assert idx is not None and idx.chunk_ann_labels, "前置:build_scale_index 须产出 chunk ANN"

    monkeypatch.setattr(repo.settings, "chunk_ann_enabled", True)
    monkeypatch.setattr(repo.settings, "chunk_recall", 3)

    # 拦 sqlite_repository 内 score_chunks 绑定(_retrieve_chunks_ann 内局部导入)记录收到的候选数
    seen = {}
    import app.services.retrieval as rmod
    real = rmod.score_chunks
    def spy(query, chunks, *a, **k):
        seen["n"] = len(chunks)
        return real(query, chunks, *a, **k)
    monkeypatch.setattr(rmod, "score_chunks", spy)
    import app.services.sqlite_repository as srepo
    if hasattr(srepo, "score_chunks"):
        monkeypatch.setattr(srepo, "score_chunks", spy)

    scored, ids, mat = repo._retrieve_chunks(nb.id, "topic 3 content")
    assert seen.get("n", 10) <= 6            # 只对 ANN∪FTS 候选打分,非全部 10 条
    assert isinstance(scored, list)
    # ids 为 ANN∪FTS 候选子集且 ≤ 2*recall
    assert len(ids) <= 6
    with repo._connect() as db:
        lexical_ids = {
            hit["chunk_id"]
            for hit in repo._runtime.knowledge.chunk_fts_search(
                db, nb.id, "topic 3 content", k=3
            )
        }
    assert {c.chunk_id for c in scored} <= set(idx.chunk_ann_labels) | lexical_ids


def test_source_scoped_ann_filters_before_topk(repo, monkeypatch):
    """An excluded row may be the nearest vector but must not occupy Top-K."""
    nb = repo.create_notebook(NotebookCreate(name="scoped ann"))
    now = "2026-07-01T00:00:00"
    query = "summarize this paper"
    vector = repo._embed_query(query)
    with repo._write() as db:
        for source_id in ("selected", "excluded"):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (source_id, nb.id, source_id, "md", "ready", now, now),
            )
        for chunk_id, source_id in (
            ("c-selected", "selected"),
            ("c-excluded", "excluded"),
        ):
            db.execute(
                "INSERT INTO chunks "
                "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (chunk_id, nb.id, source_id, "untranslated body", "", "[]", now),
            )
            db.execute(
                "INSERT INTO chunk_embeddings "
                "(chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                (chunk_id, nb.id, json.dumps(vector), now),
            )
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)

    assert idx.chunk_ann_source_names is not None
    assert len(idx.chunk_ann_source_codes) == len(idx.chunk_ann_labels)
    monkeypatch.setattr(
        repo._runtime.knowledge, "chunk_fts_search", lambda *_a, **_k: []
    )
    out = repo._retrieve_chunks_ann(
        nb.id,
        query,
        vector,
        idx,
        recall=1,
        allowed_source_ids=("selected",),
    )

    assert out is not None
    assert [chunk.source_id for chunk in out[0]] == ["selected"]


def test_source_scoped_ann_without_sidecar_requests_bounded_fts_fallback(repo):
    legacy = SimpleNamespace(
        chunk_ann_labels=["c1"],
        manifest={"dim": 16},
    )

    assert repo._retrieve_chunks_ann(
        "nb",
        "query",
        [0.25] * 16,
        legacy,
        recall=1,
        allowed_source_ids=("selected",),
    ) is None


def test_retrieve_chunks_ann_includes_post_build_delta(repo, monkeypatch):
    """opt-in delta brute-force: with scale_search_include_delta=True, a source
    added AFTER the watermark (not in chunk_ann.bin) is still recalled via the
    delta matmul path. (Default is now OFF — see test_scale_delta_policy.py — so
    this test explicitly enables the opt-in to exercise the brute-force branch.)"""
    import json
    from app.models.schemas import NotebookCreate
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add_source(sid, pairs, day):  # pairs: [(chunk_id, text)]
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            for cid, txt in pairs:
                db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, sid, txt, "", "[]", now))
                v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([txt])[0]
                db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                           (cid, nb.id, json.dumps(v), now))
    # 建索引时的存量
    add_source("s1", [("c1", "alpha topic"), ("c2", "beta topic")], 1)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    # build 之后新上传一个 source(delta)——它不在 chunk_ann.bin 里
    add_source("s2", [("c3", "gamma delta topic")], 2)
    monkeypatch.setattr(repo.settings, "chunk_ann_enabled", True)

    idx = repo._scale_index(nb.id, allow_stale=True)
    assert idx is not None and getattr(idx, "chunk_ann_labels", None)
    assert "c3" not in set(idx.chunk_ann_labels)  # 前提:c3 确实不在存量 ANN
    out = repo._retrieve_chunks_ann(nb.id, "gamma delta topic", repo._embed_query("gamma delta topic"), idx, recall=10)
    assert out is not None
    scored, ids, mat = out
    assert "c3" in {c.chunk_id for c in scored}   # ⊕ delta:新上传的 c3 被召回


def test_chunk_ann_unions_lexical(repo, monkeypatch):
    """纯词法命中的 chunk 经 FTS 被召回(ANN 语义漏它)。
    显式布置向量隔离出「纯词法」通道:
      · 8 个填充 chunk:向量与 query 近正交(cosine≈0,ANN top-8 独占但语义弱到过不了
        RELEVANCE_FLOOR → score_chunks 丢弃),文本不含罕见词;
      · 目标 c_lex:向量与 query 反向(cosine=-1,ANN 必漏,排在 8 个填充之后),
        但文本含罕见词法词 XZQW9000 与 query 字面匹配(keyword=1.0)。
    纯 ANN(语义)→ 候选 8 填充全被 floor 丢、c_lex 根本没进候选 → 空;
    只有 FTS 词法∪ 把 c_lex 补进候选,keyword 分兜排序 → 被召回。"""
    import json as _json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="lex"))
    now = "2026-07-01T00:00:00"
    query = "XZQW9000"                                   # 罕见词法词即整条 query(keyword=1.0)
    qv = repo._embed_query(query)                        # query 向量
    far = [-x for x in qv]                               # 与 query 反向(cosine=-1,语义最远)
    half = len(qv) // 2
    mid = qv[:half] + far[half:]                          # 半正半反 → 与 query cosine≈0(语义弱)
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        # 8 个填充 chunk:向量近正交(ANN 命中但语义分过不了 floor),文本不含罕见词
        rows = [(f"c{i}", "alpha beta topic detail body filler", mid) for i in range(8)]
        # 目标 chunk:向量反向(ANN 必漏),但文本含罕见词法词 XZQW9000
        rows.append(("c_lex", "XZQW9000 unrelated bandgap widget spec", far))
        for cid, txt, v in rows:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, "s1", txt, "", "[]", now))
            db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (cid, nb.id, _json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    repo.backfill_chunk_fts(nb.id)

    monkeypatch.setattr(repo.settings, "chunk_ann_enabled", True)
    idx = repo._scale_index(nb.id, allow_stale=True)
    assert idx is not None and idx.chunk_ann_labels, "前置:build_scale_index 须产出 chunk ANN"
    assert "c_lex" in set(idx.chunk_ann_labels), "前置:c_lex 在存量 ANN(非 delta 路径召回)"

    # recall=8:ANN top-8 全被 8 个填充 chunk 占满(语义弱),c_lex(反向向量)语义漏
    out = repo._retrieve_chunks_ann(nb.id, query, qv, idx, recall=8)
    assert out is not None
    scored = out[0]
    assert "c_lex" in {c.chunk_id for c in scored}   # 词法命中被并入并打分排序


class _FakeLLM:
    """配置好的假 LLM:chat_json 回定长 JSON, 内含 [k1] 标记。"""
    configured = True
    def __init__(self, answer): self._answer = answer
    def chat_json(self, messages, schema_hint, **kw):
        return json.dumps({"answer": self._answer, "grounded": True})


def test_ask_chunk_deterministic_without_llm(repo):
    # fixture 清了 LLM key → llm_client.configured False → 走确定性兜底。
    nb, _ = _seed_chunks(repo, ["deepseek v3 mixture of experts routing " * 20,
                                "deepseek v2 dense baseline architecture " * 20])
    resp = repo.ask_chunk(nb.id, AskRequest(question="deepseek experts routing"))
    assert resp.answer == "" and "passage" in resp.conclusion.lower()
    assert resp.anchors == [] and resp.citations          # 有引用, 无 anchor
    assert resp.citations[0].source_id and resp.evidence_level == "inferred"


def test_ask_chunk_binds_anchor_to_chunk_with_llm(repo, monkeypatch):
    nb, _ = _seed_chunks(repo, ["deepseek v3 mixture of experts routing " * 20])
    bind_chat_client(repo, "ask_answer", _FakeLLM("DeepSeek V3 uses MoE routing [k1]."))
    resp = repo.ask_chunk(nb.id, AskRequest(question="deepseek experts"))
    assert resp.answer and resp.anchors
    a = resp.anchors[0]
    assert a.object_type == "chunk" and a.object_id.startswith("ck-")
    assert resp.conclusion and "[k1]" not in resp.conclusion   # 标记已剥离


def test_ask_routes_default_mode_to_chunk(repo, monkeypatch):
    sentinel = object()
    service = repo.__dict__["_runtime"].ask_component
    monkeypatch.setattr(service, "ask_chunk", lambda nb, p, **kwargs: sentinel)
    # AskRequest() 默认 mode 应为 "chunk" → ask() 分发到 ask_chunk
    assert AskRequest(question="x").mode == "chunk"
    assert service.ask(
        "nb-irrelevant", AskRequest(question="x"), user_id=repo.current_user().id
    ) is sentinel


def test_ask_chunk_comparison_balances_both_entities(repo, monkeypatch):
    # 种两实体 chunk;假 expand 出 2 子查询;断言两实体都进 selected
    nb, _ = _seed_chunks(repo, ["DeepSeek-V2 uses MLA attention " * 20,
                                "DeepSeek-V2 dense baseline " * 20,
                                "DeepSeek-V3 MoE 671B improvements " * 20,
                                "DeepSeek-V3 MTP training " * 20])
    import app.services.query_rewrite as qr
    monkeypatch.setattr(qr, "expand_query", lambda *a, **k: qr.ExpandedQuery(
        query="V3 vs V2", sub_queries=[qr.SubQuerySpec("DeepSeek-V3 improvements"),
                                       qr.SubQuerySpec("DeepSeek-V2 features")]))
    bind_chat_client(repo, "ask_answer", _FakeLLM("V3 improves on V2 [k1][k2]."))
    resp = repo.ask_chunk(nb.id, AskRequest(question="deepseekv3相比deepseekv2有什么改进"))
    srcs = " ".join((a.snippet or "") + (a.name or "") for a in resp.anchors).lower()
    cites = " ".join(c.quoted_span.lower() for c in resp.citations)
    assert "v2" in (srcs + cites) and "v3" in (srcs + cites)   # 两实体都被代表


def test_ask_chunk_single_subquery_still_works(repo, monkeypatch):
    nb, _ = _seed_chunks(repo, ["alpha topic " * 30, "beta topic " * 30])
    import app.services.query_rewrite as qr
    monkeypatch.setattr(qr, "expand_query", lambda *a, **k: qr.ExpandedQuery(
        query="alpha", sub_queries=[qr.SubQuerySpec("alpha topic")]))
    resp = repo.ask_chunk(nb.id, AskRequest(question="alpha"))
    assert resp.citations   # 单子查询走 MMR,正常返回


def test_chunk_ann_enabled_default_on():
    """默认开:有索引的库自动走 ANN 核⊕delta;小库无索引自然回退暴力(零影响)。"""
    from app.core.config import Settings
    assert Settings(_env_file=None).chunk_ann_enabled is True


def test_chunk_fts_backfill_and_search(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="b"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("s1", nb.id, "t", "md", "ready", now, now))
        for cid, txt in [("c1", "XZQW9000 special widget spec"),
                         ("c2", "unrelated bandgap text")]:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (cid, nb.id, "s1", txt, "", "[]", now))
    n = repo.backfill_chunk_fts(nb.id)
    assert n == 2
    # Task 13: the chunk FTS SQL moved to KnowledgeStore (kg/search keeps
    # only pure hit merging); the primitive stays connection-taking.
    chunk_fts_search = repo._runtime.knowledge.chunk_fts_search
    with repo._connect() as db:
        hits = chunk_fts_search(db, nb.id, "XZQW9000", k=10)
    assert "c1" in {h["chunk_id"] for h in hits}   # 罕见词法词命中
    assert "c2" not in {h["chunk_id"] for h in hits}
