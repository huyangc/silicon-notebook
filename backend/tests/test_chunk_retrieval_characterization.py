"""特征化测试(characterization):锁死 ask_chunk / _retrieve_chunks 当前检索选择
热路径的行为,作为后续重构的安全网。**不改任何生产代码**,每个测试都用 spy 断言
目标分支/方法真的被走到(捕获实参或调用次数),绝不空过。

对应 sqlite_repository.py:
  · ask_chunk           ~10784  (策略分发 mix/multi/single + 引用绑定分支)
  · _retrieve_chunks    ~10480  (ANN → 大库守卫 → 暴力 三段)

fixture 套路取自 tests/test_chunk_retrieval.py / test_chunk_bruteforce_guard.py:
FakeEmbedder 定长向量 + 真 P1 build+embed 路径产出 chunks。
"""
import json
import uuid

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.retrieval import RetrievalSupport, RetrievedChunk
from app.services.sqlite_repository import SQLiteRepository, _now
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_rerank_client
from tests.model_testkit import bind_chat_client


# ─────────────────────────── fixtures / helpers ───────────────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    # 清 LLM/reasoning key:llm_client.configured=False → 答案走确定性兜底,
    # 检索选择照常发生(测试焦点)。个别测试会显式塞 _FakeLLM。
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL",
               "REWRITE_LLM_API_KEY", "REWRITE_LLM_BASE_URL", "REWRITE_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_chunks(repo, texts):
    """建 notebook+source+elements,走 P1 build+embed 真路径产出 chunks(id 形如 ck-…)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = f"src-{uuid.uuid4().hex[:8]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,"
            "file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb.id, "Doc", "document", "s.md", "/tmp/s.md", 0, "h", "", "", "extracted", now, now))
        for i, t in enumerate(texts, 1):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-{i:04d}", sid, "paragraph", f"p{i}", t, "{}", now))
    repo._chunk_and_embed_source(sid)
    return nb, sid


class _FakeLLM:
    """配置好的假答案 LLM:chat_json 回定长 JSON;markers 控制答案里引用哪些 [k]。"""
    configured = True

    def __init__(self, markers=("k1",), grounded=True):
        self._markers = list(markers)
        self._grounded = grounded

    def chat_json(self, messages, schema_hint, **kw):
        ans = "Synthesized answer " + " ".join(f"[{m}]" for m in self._markers) + "."
        return json.dumps({"answer": ans, "grounded": self._grounded})


class _IdentityRerank:
    """配置齐全的假 rerank:identity 排序(返回原序下标)。用于开 overlay_on。"""
    configured = True

    def rerank(self, query, documents, on_error=None):
        return list(range(len(documents)))


class _UnconfiguredRerank(_IdentityRerank):
    configured = False


def _enable_overlay(repo, nb_id):
    """让 overlay_on 为真:chunk_kg_overlay_enabled(默认开) ∧ rerank.configured ∧ 有 KG。
    塞一个 concept + 对应源 chunk 的证据,使 _notebook_has_kg=True 且 overlay 有料。"""
    bind_rerank_client(repo, _IdentityRerank())
    repo.store_kg(
        nb_id, None,
        [{"local_id": "a", "object_type": "concept",
          "payload": {"name": "MoE routing"},
          "evidence": [{"source_id": "src-x", "source_title": "Doc",
                        "element_id": "el-x-0001", "element_type": "paragraph",
                        "location_label": "1", "quoted_span": "moe", "confidence": 1.0}]}],
        [])
    assert repo._notebook_has_kg(nb_id) is True


def test_question_supplement_cannot_evict_single_query_mmr_baseline(repo):
    baseline = RetrievedChunk(
        chunk_id="baseline", source_id="s", source_title="s", section_path="",
        text="baseline", relevance=0.2,
        retrieval_supports=(
            RetrievalSupport("semantic", "chunk", "baseline", 0.2),
        ),
    )
    supplemental = RetrievedChunk(
        chunk_id="supplement", source_id="s", source_title="s", section_path="",
        text="supplement", relevance=0.99,
        retrieval_supports=(
            RetrievalSupport("generated_question", "chunk", "supplement", 0.99),
        ),
    )

    selected = repo.retrieval.select_chunk_candidates(
        [supplemental, baseline], [], None, 1, 0.7
    )

    assert [chunk.chunk_id for chunk in selected] == ["baseline"]


def test_multi_query_aggregation_keeps_historical_collision_order(
    repo, monkeypatch
):
    def chunk(chunk_id, relevance, origin):
        return RetrievedChunk(
            chunk_id=chunk_id,
            source_id="s",
            source_title="s",
            section_path="",
            text=chunk_id,
            relevance=relevance,
            retrieval_supports=(
                RetrievalSupport(origin, "chunk", chunk_id, relevance),
            ),
        )

    semantic_a = chunk("a", 0.8, "semantic")
    generated_b = chunk("b", 0.99, "generated_question")
    semantic_b = chunk("b", 0.5, "semantic")
    results = {
        "q1": ([generated_b, semantic_a], [], None),
        "q2": ([semantic_b], [], None),
    }
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_retrieve_chunks",
        lambda _notebook_id, query: results[query],
    )

    collected, per_query, _ids, _matrix = (
        repo.retrieval.candidates._retrieve_chunks_multi("nb", ["q1", "q2"])
    )

    assert list(collected) == ["a", "b"]
    assert collected["b"] is semantic_b
    assert {support.origin for support in semantic_b.retrieval_supports} == {
        "semantic",
        "generated_question",
    }
    from app.services.retrieval import quota_fuse_baseline_first

    selected, _counts = quota_fuse_baseline_first(collected, per_query, 2)
    assert [chunk.chunk_id for chunk in selected] == ["a", "b"]


def test_multi_query_aggregation_collapses_text_before_quota(repo, monkeypatch):
    def chunk(chunk_id, text, relevance, origin):
        return RetrievedChunk(
            chunk_id=chunk_id,
            source_id="paper",
            source_title="Paper",
            section_path=chunk_id,
            text=text,
            relevance=relevance,
            retrieval_supports=(
                RetrievalSupport(origin, "chunk", chunk_id, relevance),
            ),
        )

    weak_header = chunk("header-1", "Paper title", 0.4, "semantic")
    strong_header = chunk("header-2", " paper\n title ", 0.9, "lexical")
    abstract = chunk("abstract", "We introduce the model.", 0.7, "semantic")
    results = {
        "q1": ([weak_header], [], None),
        "q2": ([strong_header, abstract], [], None),
    }
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_retrieve_chunks",
        lambda _notebook_id, query: results[query],
    )

    collected, per_query, _ids, _matrix = (
        repo.retrieval.candidates._retrieve_chunks_multi("nb", ["q1", "q2"])
    )

    assert list(collected) == ["header-2", "abstract"]
    assert collected["header-2"].relevance == 0.9
    assert {support.origin for support in collected["header-2"].retrieval_supports} == {
        "semantic", "lexical",
    }
    from app.services.retrieval import quota_fuse_baseline_first

    selected, _counts = quota_fuse_baseline_first(collected, per_query, 2)
    assert {chunk.chunk_id for chunk in selected} == {"header-2", "abstract"}


def test_multi_query_direct_collision_replaces_question_only_canonical():
    from app.services.ask_service import _merge_multi_direct_chunk_hits
    from app.services.retrieval import quota_fuse_baseline_first

    question_only = RetrievedChunk(
        chunk_id="collision",
        source_id="s",
        source_title="s",
        section_path="",
        text="collision",
        relevance=0.99,
        retrieval_supports=(
            RetrievalSupport(
                "generated_question", "chunk", "collision", 0.99
            ),
        ),
    )
    lexical = RetrievedChunk(
        chunk_id="collision",
        source_id="s",
        source_title="s",
        section_path="",
        text="collision",
        relevance=0.2,
        retrieval_supports=(
            RetrievalSupport("lexical", "chunk", "collision", 0.2),
        ),
    )
    before = RetrievedChunk(
        chunk_id="before",
        source_id="s",
        source_title="s",
        section_path="",
        text="before",
        relevance=0.2,
        retrieval_supports=(
            RetrievalSupport("lexical", "chunk", "before", 0.2),
        ),
    )
    collected = {"collision": question_only}

    _merge_multi_direct_chunk_hits(collected, [before, lexical])

    assert list(collected) == ["before", "collision"]
    assert collected["collision"] is lexical
    assert {support.origin for support in lexical.retrieval_supports} == {
        "generated_question",
        "lexical",
    }
    assert [support.origin for support in question_only.retrieval_supports] == [
        "generated_question"
    ]
    selected, _counts = quota_fuse_baseline_first(
        collected,
        [
            {"collision": question_only},
            {"before": before, "collision": lexical},
        ],
        2,
    )
    assert selected == [before, lexical]


def _collision_chunks():
    def chunk(chunk_id, relevance, origin, *, text=None):
        return RetrievedChunk(
            chunk_id=chunk_id,
            source_id="s",
            source_title="s",
            section_path="",
            text=text or chunk_id,
            relevance=relevance,
            retrieval_supports=(
                RetrievalSupport(origin, "chunk", chunk_id, relevance),
            ),
        )

    return (
        chunk("collision", 0.99, "generated_question"),
        chunk("collision", 0.2, "lexical"),
        chunk("historical", 0.3, "semantic"),
    )


def test_single_direct_collision_uses_historical_score_before_mmr(repo):
    from app.services.ask_service import _merge_direct_chunk_hits

    question_only, lexical, historical = _collision_chunks()
    merged = _merge_direct_chunk_hits(
        [question_only, historical], [lexical]
    )

    assert merged == [historical, lexical]
    assert lexical.relevance == 0.2
    assert {support.origin for support in lexical.retrieval_supports} == {
        "generated_question",
        "lexical",
    }
    selected = repo.retrieval.select_chunk_candidates(
        merged, [], None, 1, 0.7
    )
    assert selected == [historical]


def test_mix_direct_collision_restores_feature_off_rerank_tie_order(
    repo, monkeypatch
):
    nb, _ = _seed_chunks(repo, ["routing baseline " * 20])
    _enable_overlay(repo, nb.id)
    _stub_expand(monkeypatch, 1)
    bind_chat_client(repo, "ask_answer", _FakeLLM(markers=()))
    question_only, lexical, historical = _collision_chunks()

    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_mix_retrieve",
        lambda *_args: ([question_only, historical], "", {}, [], 0),
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_keyword_chunk_candidates",
        lambda *_args: [lexical],
    )
    monkeypatch.setattr(repo.settings, "exact_lookup_enabled", False)
    ask = repo._runtime.ask_component
    # Zero post-buffer budget deliberately exercises the historical
    # first-oversize rule: identity rerank must see the historical row first,
    # exactly as it would with the optional question index disabled.
    monkeypatch.setattr(
        repo.settings, "max_total_tokens", ask._MIX_PROMPT_BUFFER_TOKENS
    )
    captured = {}
    original_activate = ask._activate_selected_source_graph

    def capture_selected(notebook_id, chunks, **kwargs):
        captured["chunks"] = list(chunks)
        return original_activate(notebook_id, chunks, **kwargs)

    monkeypatch.setattr(ask, "_activate_selected_source_graph", capture_selected)

    repo.ask_chunk(nb.id, AskRequest(question="routing"))

    assert [chunk.chunk_id for chunk in captured["chunks"]] == ["historical"]


# ═══════════════════════════════════════════════════════════════════════════
# 1. 策略分发互斥:四组 (overlay_on × len(sub_queries)) 各恰好一路
# ═══════════════════════════════════════════════════════════════════════════

def _dispatch_spies(repo, monkeypatch):
    """把三个分发目标换成记账 stub(返回空但形状合法),彻底断开内部嵌套调用,
    使每次记账 == 一次顶层分发。返回 calls dict。"""
    calls = {"mix": 0, "multi": 0, "single": 0}

    def _mix(notebook_id, retrieval_query, hl, sub_queries):
        calls["mix"] += 1
        return [], "", {}, [], 0                       # candidates, kg_block, kg_id_map, kg_hits, ppr_n

    def _multi(notebook_id, sub_queries):
        calls["multi"] += 1
        return {}, [], [], None                        # collected, per_query, ids, mat

    def _single(notebook_id, query, recall=0):
        calls["single"] += 1
        return [], [], None                            # scored, ids, mat

    monkeypatch.setattr(repo.retrieval.candidates, "_mix_retrieve", _mix)
    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks_multi", _multi)
    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks", _single)
    return calls


def _stub_expand(monkeypatch, n):
    """stub expand_query 返回 n 个子查询(patch 模块属性,ask_chunk 内局部 import 会取到)。"""
    import app.services.query_rewrite as qr

    def _fake(client, question, history="", **kw):
        subs = [qr.SubQuerySpec(query=f"sub {i}") for i in range(1, n + 1)]
        return qr.ExpandedQuery(query=question, sub_queries=subs)

    monkeypatch.setattr(qr, "expand_query", _fake)


def test_ask_chunk_strategy_dispatch_is_mutually_exclusive(repo, monkeypatch):
    """穷举 (overlay_on∈{T,F} × len(sub_queries)∈{1,2}) 四组,断言每组恰好一路分发:
      overlay_on=True                       → _mix_retrieve
      overlay_on=False ∧ len(sub)>=2        → _retrieve_chunks_multi (quota_fuse 分支)
      overlay_on=False ∧ len(sub)==1        → _retrieve_chunks       (MMR 分支)
    overlay_on 由 (chunk_kg_overlay_enabled ∧ rerank.configured ∧ (has_kg∨base_has_kg)) 决定。
    """
    nb, _ = _seed_chunks(repo, ["moe routing expert " * 20, "dense baseline " * 20])
    bind_chat_client(repo, "ask_answer", _FakeLLM(markers=()))            # 配置好但不引用任何 chunk;焦点=分发

    matrix = [
        (True, 1, "mix"), (True, 2, "mix"),
        (False, 1, "single"), (False, 2, "multi"),
    ]
    for overlay_on, n_sub, expected in matrix:
        calls = _dispatch_spies(repo, monkeypatch)
        _stub_expand(monkeypatch, n_sub)
        if overlay_on:
            _enable_overlay(repo, nb.id)
        else:
            # 默认 fixture 的 rerank 未配置 → overlay_on 自然为 False
            bind_rerank_client(repo, _UnconfiguredRerank())

        repo.ask_chunk(nb.id, AskRequest(question="moe routing"))

        chosen = [k for k, v in calls.items() if v > 0]
        assert chosen == [expected], (
            f"overlay_on={overlay_on} n_sub={n_sub}: 期望仅 {expected} 被调,实际 {calls}")
        assert calls[expected] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 2. 多子查询 quota_fuse 端到端(maps 指出从未端到端覆盖)
# ═══════════════════════════════════════════════════════════════════════════

def test_ask_chunk_multi_subquery_quota_fuse_end_to_end(repo, monkeypatch):
    """query_rewrite_enabled ∧ >=2 子查询 ∧ overlay_on=False → 走 quota_fuse 分支:
    spy _retrieve_chunks_multi 被调,且 selected 数受 chunk_mmr_k 约束。"""
    nb, _ = _seed_chunks(repo, [f"alpha topic detail body {i} " * 20 for i in range(6)]
                               + [f"beta topic detail body {i} " * 20 for i in range(6)])
    _stub_expand(monkeypatch, 2)
    # overlay 关:fixture rerank 未配置。llm 配好但不引用 → citations 走非 mix「每 selected 一条」。
    bind_chat_client(repo, "ask_answer", _FakeLLM(markers=()))
    monkeypatch.setattr(repo.settings, "chunk_mmr_k", 3)

    calls = {"multi": 0}
    orig = repo.retrieval.candidates._retrieve_chunks_multi

    def _spy(notebook_id, sub_queries):
        calls["multi"] += 1
        return orig(notebook_id, sub_queries)

    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks_multi", _spy)

    resp = repo.ask_chunk(nb.id, AskRequest(question="alpha vs beta"))

    assert calls["multi"] == 1, "多子查询必须委托 _retrieve_chunks_multi(quota_fuse 分支)"
    # 非 mix 分支:每个 selected 一条 Citation → citation 数 == selected 数 ≤ chunk_mmr_k
    assert len(resp.citations) <= 3, f"selected 受 chunk_mmr_k=3 约束,实得 {len(resp.citations)}"
    assert len(resp.citations) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# 3. 单查询 MMR 用 settings 的 k / lambda(防硬编码/漂移)
# ═══════════════════════════════════════════════════════════════════════════

def test_ask_chunk_mmr_uses_settings_k_and_lambda(repo, monkeypatch):
    """单查询 MMR 分支:spy _mmr_select_chunks 捕获实参,断言 k/lambda 恰等于 settings 值。"""
    nb, _ = _seed_chunks(repo, [f"shared topic detail {i} " * 20 for i in range(8)])
    _stub_expand(monkeypatch, 1)                      # 单子查询 → MMR 分支
    bind_chat_client(repo, "ask_answer", _FakeLLM(markers=()))
    monkeypatch.setattr(repo.settings, "chunk_mmr_k", 2)
    monkeypatch.setattr(repo.settings, "chunk_mmr_lambda", 0.9)

    seen = {}
    orig = repo.retrieval.candidates._mmr_select_chunks

    def _spy(scored, ids, mat, k, lambda_):
        seen["k"] = k
        seen["lambda"] = lambda_
        return orig(scored, ids, mat, k, lambda_)

    monkeypatch.setattr(repo.retrieval.candidates, "_mmr_select_chunks", _spy)

    repo.ask_chunk(nb.id, AskRequest(question="shared topic"))

    assert seen.get("k") == 2, f"MMR k 应取 settings.chunk_mmr_k=2,实为 {seen.get('k')}"
    assert seen.get("lambda") == 0.9, f"MMR lambda 应取 settings.chunk_mmr_lambda=0.9,实为 {seen.get('lambda')}"


# ═══════════════════════════════════════════════════════════════════════════
# 4. 多查询 fuse 的 k == settings.chunk_mmr_k(复刻 10863 复用同一 knob 的隐式契约)
# ═══════════════════════════════════════════════════════════════════════════

def test_ask_chunk_multi_fuse_k_equals_mmr_k(repo, monkeypatch):
    """多查询分支:spy quota_fuse 捕获 top_n 实参,断言 == settings.chunk_mmr_k。"""
    nb, _ = _seed_chunks(repo, [f"alpha detail {i} " * 20 for i in range(6)]
                               + [f"beta detail {i} " * 20 for i in range(6)])
    _stub_expand(monkeypatch, 2)                      # 多子查询 → quota_fuse 分支
    bind_chat_client(repo, "ask_answer", _FakeLLM(markers=()))
    monkeypatch.setattr(repo.settings, "chunk_mmr_k", 4)

    # quota_fuse 在 ask_chunk 内经 `from app.services.retrieval import quota_fuse` 局部 import,
    # patch 模块属性即可拦到。
    import app.services.retrieval as rmod
    seen = {}
    orig = rmod.quota_fuse

    def _spy(collected, per_query, top_n, relevance=lambda h: h.relevance):
        seen["top_n"] = top_n
        return orig(collected, per_query, top_n, relevance=relevance)

    monkeypatch.setattr(rmod, "quota_fuse", _spy)

    repo.ask_chunk(nb.id, AskRequest(question="alpha vs beta"))

    assert seen.get("top_n") == 4, (
        f"quota_fuse 的 top_n 应复用 settings.chunk_mmr_k=4,实为 {seen.get('top_n')}")


# ═══════════════════════════════════════════════════════════════════════════
# 5. expand_query 用 settings.chunk_max_subqueries;关 rewrite 时单查询==retrieval_query
# ═══════════════════════════════════════════════════════════════════════════

def test_ask_chunk_expand_query_uses_chunk_max_subqueries(repo, monkeypatch):
    """(a) query_rewrite_enabled=True:spy expand_query 捕获 max_subqueries 实参,
        断言 == settings.chunk_max_subqueries(设非默认值)。
    (b) query_rewrite_enabled=False:sub_queries==[retrieval_query](单查询,不调 expand)。"""
    nb, _ = _seed_chunks(repo, ["topic body detail " * 20])
    bind_chat_client(repo, "ask_answer", _FakeLLM(markers=()))

    # (a) 开 rewrite,设非默认 chunk_max_subqueries
    monkeypatch.setattr(repo.settings, "query_rewrite_enabled", True)
    monkeypatch.setattr(repo.settings, "chunk_max_subqueries", 7)
    import app.services.query_rewrite as qr
    seen = {}

    def _spy_expand(client, question, history="", **kw):
        seen["max_subqueries"] = kw.get("max_subqueries")
        return qr.ExpandedQuery(query=question, sub_queries=[qr.SubQuerySpec(query=question)])

    monkeypatch.setattr(qr, "expand_query", _spy_expand)
    repo.ask_chunk(nb.id, AskRequest(question="what is topic"))
    assert seen.get("max_subqueries") == 7, (
        f"expand_query 应收到 settings.chunk_max_subqueries=7,实为 {seen.get('max_subqueries')}")

    # (b) 关 rewrite:expand 不该被调,单查询走 MMR 分支
    monkeypatch.setattr(repo.settings, "query_rewrite_enabled", False)
    called = {"expand": 0}

    def _never(*a, **k):
        called["expand"] += 1
        return qr.ExpandedQuery(query="x", sub_queries=[qr.SubQuerySpec(query="x")])

    monkeypatch.setattr(qr, "expand_query", _never)

    # spy 单查询分发目标,证明确实按 [retrieval_query] 单查询走(_retrieve_chunks 收到的 query
    # == 规整后的问题,而非某子查询)
    seen_single = {}
    orig_single = repo.retrieval.candidates._retrieve_chunks

    def _spy_single(notebook_id, query, recall=0):
        seen_single["query"] = query
        return orig_single(notebook_id, query, recall)

    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks", _spy_single)
    repo.ask_chunk(nb.id, AskRequest(question="topic"))
    assert called["expand"] == 0, "query_rewrite_enabled=False 时不应调 expand_query"
    assert seen_single.get("query") == "topic", (
        f"关 rewrite → 单查询应为 retrieval_query('topic'),实为 {seen_single.get('query')}")


# ═══════════════════════════════════════════════════════════════════════════
# 6. 大库 copyable=False → FTS 降级(不动 chunk 计数阈值);另一条大库守卫臂(10502)
# ═══════════════════════════════════════════════════════════════════════════

def test_retrieve_chunks_large_library_copyable_degrades(repo, monkeypatch):
    """monkeypatch notebook_copy_stats 返回 copyable=False(不动 chunk 计数阈值),
    断言 _retrieve_chunks 走 _retrieve_chunks_fts_degraded、绝不调 _gather_chunks。
    这是「large = not copyable」这条大库守卫臂(10502),另一条(n_chunks>threshold)已被
    test_chunk_bruteforce_guard 覆盖。"""
    nb, _ = _seed_chunks(repo, [f"bandgap reference topic {i} body detail " * 5 for i in range(3)])
    repo.backfill_chunk_fts(nb.id)
    # 阈值保持默认(20000,不动),仅令 copyable=False 触发 large 臂
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda nb_id: {"copyable": False, "size": {}})
    # 未建 scale 索引 → ANN 分支不可用,自然落到大库守卫

    fts_calls = {"n": 0}
    orig_fts = repo.retrieval.candidates._retrieve_chunks_fts_degraded

    def _spy_fts(notebook_id, query, query_vector, recall, n_chunks):
        fts_calls["n"] += 1
        return orig_fts(notebook_id, query, query_vector, recall, n_chunks)

    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks_fts_degraded", _spy_fts)

    gather_calls = {"n": 0}
    orig_gather = repo.retrieval.candidates._gather_chunks

    def _spy_gather(db, notebook_id):
        gather_calls["n"] += 1
        return orig_gather(db, notebook_id)

    monkeypatch.setattr(repo.retrieval.candidates, "_gather_chunks", _spy_gather)

    scored, ids, mat = repo.retrieval.candidates._retrieve_chunks(nb.id, "bandgap")

    assert fts_calls["n"] == 1, "copyable=False 大库必须走 _retrieve_chunks_fts_degraded"
    assert gather_calls["n"] == 0, "大库降级路径绝不 _gather_chunks 全表"
    assert len(scored) >= 1                                        # FTS 词法命中召回(候选内打分)
    assert all(c.chunk_id.startswith("ck-") for c in scored)      # 召回来自本 nb 的真实 chunk


# ═══════════════════════════════════════════════════════════════════════════
# 7. ANN fail-open(返回 None)→ 非大库场景落全表暴力并返回非空 scored(锁 10491-92 后 fallthrough)
# ═══════════════════════════════════════════════════════════════════════════

def test_retrieve_chunks_ann_failopen_falls_through_to_bruteforce(repo, monkeypatch):
    """chunk_ann_enabled=True 且已建索引,但令 _retrieve_chunks_ann 返回 None
    (模拟 dim_mismatch/异常 fail-open)。非大库(copyable=True)+ 关暴力阈值 →
    继续落全表暴力(_gather_chunks)并返回非空 scored,而非返回空。锁 10491-92 短路后的 fallthrough。"""
    nb, _ = _seed_chunks(repo, ["deepseek moe routing expert " * 20,
                                "dense baseline architecture " * 20])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)
    assert idx is not None and idx.chunk_ann_labels, "前置:须产出 chunk ANN(才有 ann 分支可短路)"

    monkeypatch.setattr(repo.settings, "chunk_ann_enabled", True)
    # 关暴力阈值(0)+小库(copyable=True,种子数据本就小)→ 走全表暴力而非 FTS 降级
    monkeypatch.setattr(repo.settings, "chunk_bruteforce_max_chunks", 0)

    ann_calls = {"n": 0}

    def _ann_none(notebook_id, query, query_vector, idx_, recall):
        ann_calls["n"] += 1
        return None                                   # fail-open

    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks_ann", _ann_none)

    gather_calls = {"n": 0}
    orig_gather = repo.retrieval.candidates._gather_chunks

    def _spy_gather(db, notebook_id):
        gather_calls["n"] += 1
        return orig_gather(db, notebook_id)

    monkeypatch.setattr(repo.retrieval.candidates, "_gather_chunks", _spy_gather)

    scored, ids, mat = repo.retrieval.candidates._retrieve_chunks(nb.id, "deepseek moe routing")

    assert ann_calls["n"] == 1, "前置:ANN 分支确实被走到并返回 None(fail-open)"
    assert gather_calls["n"] == 1, "ANN None 后须 fallthrough 到全表暴力 _gather_chunks"
    assert len(scored) >= 1, "fallthrough 暴力路径应返回非空 scored(而非空)"


# ═══════════════════════════════════════════════════════════════════════════
# 8. 默认 chunk_recall(200)接线:不 monkeypatch,断言 score_chunks 的 limit==200
# ═══════════════════════════════════════════════════════════════════════════

def test_ask_chunk_default_chunk_recall_wiring(repo, monkeypatch):
    """不 monkeypatch chunk_recall(保持默认 200),spy score_chunks 捕获 limit 实参,
    断言 == 200。maps 指出该默认值从没被 pin(其它测试都改成小值)。
    走小库全表暴力路径(未建索引 + 关暴力阈值),score_chunks(…, limit=recall) 直接可见。"""
    nb, _ = _seed_chunks(repo, ["topic body detail alpha " * 20])
    _stub_expand(monkeypatch, 1)                      # 单查询 → _retrieve_chunks
    bind_chat_client(repo, "ask_answer", _FakeLLM(markers=()))
    monkeypatch.setattr(repo.settings, "chunk_bruteforce_max_chunks", 0)   # 关守卫 → 全表暴力
    assert repo.settings.chunk_recall == 200          # 保持默认,未被 patch

    import app.services.retrieval as rmod
    seen = {}
    orig = rmod.score_chunks

    def _spy(query, chunks, query_vector=None, chunk_sims=None, limit=150):
        seen["limit"] = limit
        return orig(query, chunks, query_vector, chunk_sims, limit)

    monkeypatch.setattr(rmod, "score_chunks", _spy)

    repo.ask_chunk(nb.id, AskRequest(question="topic alpha"))

    assert seen.get("limit") == 200, (
        f"暴力路径 score_chunks 的 limit 应 == 默认 chunk_recall=200,实为 {seen.get('limit')}")


# ═══════════════════════════════════════════════════════════════════════════
# 9. mix token 预算真截断:构造候选总 token 超预算,断言 truncate_by_tokens 真实截短
# ═══════════════════════════════════════════════════════════════════════════

def test_ask_chunk_mix_token_budget_actually_trims(repo, monkeypatch):
    """overlay_on=True,构造 candidates 总 token 超预算(长文本 + 极小 max_total_tokens),
    断言 selected 被 truncate_by_tokens 真实截短(len(selected) < len(candidates))。"""
    nb, _ = _seed_chunks(repo, ["moe routing expert body " * 40])
    _enable_overlay(repo, nb.id)                      # rerank 配齐 + 有 KG → overlay_on=True
    bind_chat_client(repo, "ask_answer", _FakeLLM(markers=()))

    from app.services.retrieval import RetrievedChunk

    def _long_chunk(i):
        return RetrievedChunk(
            chunk_id=f"ck-long-{i}", source_id="src-x", source_title="Doc",
            section_path="1", text=("moe routing expert detail " * 200),  # 每条约数千 token
            element_ids=["el-x-0001"], score=1.0 - i * 0.01, relevance=1.0 - i * 0.01)

    candidates = [_long_chunk(i) for i in range(10)]

    # _mix_retrieve 直接给一堆长候选;identity rerank 保原序。
    monkeypatch.setattr(repo.retrieval.candidates, "_mix_retrieve",
                        lambda nb_id, q, hl, subs: (candidates, "", {}, [], 0))
    # 预算逼到只能容纳前 1~2 条:max_total_tokens 极小。
    monkeypatch.setattr(repo.settings, "max_total_tokens", 4000)

    # spy truncate_by_tokens 证明确实过了预算截断
    import app.services.retrieval as rmod
    seen = {}
    orig = rmod.truncate_by_tokens

    def _spy(items, key, max_tokens):
        out = orig(items, key, max_tokens)
        seen["in"] = len(items)
        seen["out"] = len(out)
        seen["budget"] = max_tokens
        return out

    monkeypatch.setattr(rmod, "truncate_by_tokens", _spy)

    repo.ask_chunk(nb.id, AskRequest(question="moe routing"))

    assert seen.get("in") == 10, "truncate_by_tokens 应收到全部 10 条候选"
    assert seen.get("out") is not None and seen["out"] < seen["in"], (
        f"预算 {seen.get('budget')} 应真实截短候选:{seen.get('out')} < {seen.get('in')}")
    assert seen["out"] >= 1                            # 至少保留 1 条(truncate 保 first)


# ═══════════════════════════════════════════════════════════════════════════
# 10. 引用绑定分支 parity:mix 只绑被 anchor 引用的 chunk;非 mix 每 selected 一条
# ═══════════════════════════════════════════════════════════════════════════

def test_ask_chunk_citation_binding_parity_mix_vs_nonmix(repo, monkeypatch):
    """锁 10897-10913 引用绑定分支:
      overlay_on=True  → 只对被答案 anchor 引用(且在 selected)的 chunk 生成 Citation。
      overlay_on=False → 每个 selected chunk 一条 Citation(与 anchor 无关)。
    """
    from app.services.retrieval import RetrievedChunk

    def _chunk(i):
        return RetrievedChunk(
            chunk_id=f"ck-cite-{i}", source_id="src-x", source_title="Doc",
            section_path=str(i), text=f"passage {i} moe routing detail body",
            element_ids=[f"el-x-{i:04d}"], score=1.0 - i * 0.1, relevance=1.0 - i * 0.1)

    selected = [_chunk(i) for i in range(3)]           # ck-cite-0/1/2

    # ── mix 分支:_mix_retrieve 给 3 条候选,rerank identity 保序,预算够(不截);
    #    _chunk_answer_context 给它们 k1/k2/k3;_FakeLLM 只引用 [k2] → 只有 ck-cite-1 有 anchor。
    nb1, _ = _seed_chunks(repo, ["moe routing expert " * 20])
    _enable_overlay(repo, nb1.id)
    monkeypatch.setattr(repo.retrieval.candidates, "_mix_retrieve",
                        lambda nb_id, q, hl, subs: (list(selected), "", {}, [], 0))
    monkeypatch.setattr(repo.settings, "max_total_tokens", 30000)  # 够大,不触发截断
    bind_chat_client(repo, "ask_answer", _FakeLLM(markers=("k2",)))        # 答案只引用 k2 → 只绑 ck-cite-1

    resp_mix = repo.ask_chunk(nb1.id, AskRequest(question="moe routing"))

    assert [a.object_id for a in resp_mix.anchors] == ["ck-cite-1"], (
        f"答案只引 k2 → anchor 应仅 ck-cite-1,实为 {[a.object_id for a in resp_mix.anchors]}")
    # mix:只绑被 anchor 引用的 chunk(1 条),而非全部 3 条 selected
    assert len(resp_mix.citations) == 1, (
        f"mix 分支只绑被引用的 chunk,期望 1 条 Citation,实得 {len(resp_mix.citations)}")
    assert resp_mix.citations[0].source_id == "src-x"
    # nb1 是 personal 库、chunk 无 notebook_id(同库路径)→ tier 应回退 nb1 自己的 tier。
    assert resp_mix.citations[0].tier == "personal", (
        f"personal 库同库 chunk 引用 tier 应为 personal,实为 {resp_mix.citations[0].tier}")

    # ── 非 mix 分支:同一批 selected,overlay_on=False → 每 selected 一条 Citation(3 条),
    #    与答案引用无关。用真实单查询 MMR 路径不好精确控 selected 集,故直接 stub
    #    _retrieve_chunks + _mmr_select_chunks 令 selected 恰为这 3 条。
    nb2, _ = _seed_chunks(repo, ["moe routing expert " * 20])
    bind_rerank_client(repo, _UnconfiguredRerank())  # overlay_on=False
    _stub_expand(monkeypatch, 1)                       # 单查询 → MMR 分支
    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks",
                        lambda nb_id, q, recall=0: (list(selected), [], None))
    monkeypatch.setattr(repo.retrieval.candidates, "_mmr_select_chunks",
                        lambda scored, ids, mat, k, lam: list(selected))
    bind_chat_client(repo, "ask_answer", _FakeLLM(markers=("k2",)))        # 答案仍只引用 k2

    resp_non = repo.ask_chunk(nb2.id, AskRequest(question="moe routing"))

    # 非 mix:每个 selected chunk 一条 Citation(3 条),与 anchor 无关
    assert len(resp_non.citations) == 3, (
        f"非 mix 分支每 selected 一条 Citation,期望 3 条,实得 {len(resp_non.citations)}")
    assert {c.source_id for c in resp_non.citations} == {"src-x"}
    assert {c.location_label for c in resp_non.citations} == {"0", "1", "2"}
    assert {c.tier for c in resp_non.citations} == {"personal"}, (
        f"nb2 是 personal 库,非 mix 分支全部 3 条 citation tier 应为 personal,"
        f"实为 {[c.tier for c in resp_non.citations]}")


# ═══════════════════════════════════════════════════════════════════════════
# 11. Citation.tier / AnswerAnchor.tier 跨层:PPR 召回的 base 库 chunk 生成的
#     Citation 与 anchor 都须标 tier="base"(真机 bug 分两波:①reasoning/mix 引用
#     了 base 库原文,前端徽章却只见 personal——根因是 Citation 此前完全不带 tier
#     [PR#216 已修]。②同一根因换了个面孔:_chunk_answer_context 构造 id_map 时
#     硬编码 tier="personal",不管 chunk.notebook_id 实际指向哪个库——citation 修
#     好了,但「来源分布」徽章读的是 anchor.tier,anchors 仍全部误标 personal。
#     这里锁 ask_chunk 的 mix 分支:_mix_retrieve 第三路概念漫游(PPR)可掺 base 库
#     chunk,citation 与 anchor 都必须如实反映其来源 tier。)
# ═══════════════════════════════════════════════════════════════════════════

def test_ask_chunk_citation_tier_reflects_cross_tier_ppr_chunk(repo, monkeypatch):
    """selected 里混一条打了 base 库 notebook_id 的 RetrievedChunk(模拟 _ppr_retrieve
    的产出:真实 base chunk 与 active 库同池但 notebook_id 指向 base)——citation.tier
    与 anchor.tier 都必须解析为 'base',同池的本库 chunk 仍是 'personal'。"""
    from app.services.retrieval import RetrievedChunk

    active_nb, _ = _seed_chunks(repo, ["moe routing expert " * 20])
    base_nb, _ = _seed_chunks(repo, ["base layer reference " * 20])
    repo.mark_notebook_base(base_nb.id)

    own_chunk = RetrievedChunk(
        chunk_id="ck-own-0", source_id="src-x", source_title="Doc",
        section_path="0", text="own passage moe routing detail",
        element_ids=["el-x-0000"], score=1.0, relevance=1.0,
        # codex r4 fix: 显式打上 active_nb.id,镜像真实 _ppr_retrieve 的产出——
        # scale_ppr 的 combined_chunk_ids 跨 base ⊕ active,逐 chunk 原样带出
        # chunk_notebook_id,对 active 库自己的命中同样会打上 active 自己的
        # id(并非留空;之前这里手写留空,side-step 了这个真实场景,没能在这
        # 条测试上暴露 ask_chunk 的 mix/plain 两处内联 Citation(...) 构造点
        # 也需要同 citations_from 一样的自库归一化)。
        notebook_id=active_nb.id)
    ppr_chunk = RetrievedChunk(
        chunk_id="ck-ppr-0", source_id="src-base", source_title="BaseDoc",
        section_path="0", text="base layer passage moe routing detail",
        element_ids=["el-base-0000"], score=0.9, relevance=0.9,
        notebook_id=base_nb.id)                        # PPR 标了来源 notebook

    _enable_overlay(repo, active_nb.id)
    monkeypatch.setattr(repo.retrieval.candidates, "_mix_retrieve",
                        lambda nb_id, q, hl, subs: ([own_chunk, ppr_chunk], "", {}, [], 1))
    monkeypatch.setattr(repo.settings, "max_total_tokens", 30000)
    bind_chat_client(repo, "ask_answer", _FakeLLM(markers=("k1", "k2")))    # 两条都被引用

    resp = repo.ask_chunk(active_nb.id, AskRequest(question="moe routing"))

    tier_by_chunk = {c.source_id: c.tier for c in resp.citations}
    assert tier_by_chunk.get("src-x") == "personal", (
        f"active 库自己的 chunk tier 应为 personal,实为 {tier_by_chunk.get('src-x')}")
    assert tier_by_chunk.get("src-base") == "base", (
        f"PPR 带来的 base 库 chunk citation.tier 应为 base,实为 {tier_by_chunk.get('src-base')}")

    # anchor.tier(「来源分布」徽章的真实数据源)必须与 citation.tier 一致,
    # 而非硬编码 personal——两个 anchor 分别对应 own_chunk(k1)/ppr_chunk(k2)。
    tier_by_anchor = {a.object_id: a.tier for a in resp.anchors}
    assert tier_by_anchor.get("ck-own-0") == "personal", (
        f"active 库自己的 chunk anchor.tier 应为 personal,实为 {tier_by_anchor.get('ck-own-0')}")
    assert tier_by_anchor.get("ck-ppr-0") == "base", (
        f"PPR 带来的 base 库 chunk anchor.tier 应为 base,实为 {tier_by_anchor.get('ck-ppr-0')}")

    # Task 14(引用徽章带库名) + codex r4 fix:citation/anchor 的 notebook_id
    # 必须反映"是否真正跨库",而非原始值是否非空——own_chunk 显式打了
    # active_nb.id(同库,PPR 对本库命中同样会打真实 notebook_id)必须归一成
    # 空串,ppr_chunk 显式标了 base_nb.id(真正跨库)才必须原样带出,前端才能
    # 查得到「模拟IC教材」这样的库名,同时不会对本库自己的证据显示一个多余的
    # 「来自「当前笔记本」」徽章。
    nb_by_chunk = {c.source_id: c.notebook_id for c in resp.citations}
    assert nb_by_chunk.get("src-x") == "", (
        f"active 库自己的 chunk citation.notebook_id 应留空,实为 {nb_by_chunk.get('src-x')!r}")
    assert nb_by_chunk.get("src-base") == base_nb.id, (
        f"PPR 带来的 base 库 chunk citation.notebook_id 应为 {base_nb.id!r},"
        f"实为 {nb_by_chunk.get('src-base')!r}")
    nb_by_anchor = {a.object_id: a.notebook_id for a in resp.anchors}
    assert nb_by_anchor.get("ck-own-0") == "", (
        f"active 库自己的 chunk anchor.notebook_id 应留空,实为 {nb_by_anchor.get('ck-own-0')!r}")
    assert nb_by_anchor.get("ck-ppr-0") == base_nb.id, (
        f"PPR 带来的 base 库 chunk anchor.notebook_id 应为 {base_nb.id!r},"
        f"实为 {nb_by_anchor.get('ck-ppr-0')!r}")


# ═══════════════════════════════════════════════════════════════════════════
# 12. _chunk_answer_context 本身:tier 按每 chunk 的 notebook_id 批量解析,
#     而非硬编码 "personal"(11 的端到端场景经 ask_chunk 全链路验证同一根因;
#     这里在最小粒度直接锁 id_map 输出,减少未来重构误伤该行为的成本)。
# ═══════════════════════════════════════════════════════════════════════════

def test_chunk_answer_context_resolves_tier_per_chunk_notebook_id(repo):
    """四个 chunk:一个 notebook_id 指向 base 库、一个指向真正另一个 personal 库、
    一个等于调用方自己(own_nb——联邦/PPR 检索对本库命中同样会打上 active 自己
    的 notebook_id,并非只在跨库命中时才打标)、一个留空(回退调用方传入的
    notebook_id)——id_map 里每条的 tier 都必须对应其真实来源库,不能被硬编码
    坍缩成清一色 'personal'。"""
    from app.services.retrieval import RetrievedChunk

    own_nb, _ = _seed_chunks(repo, ["own doc passage"])
    base_nb, _ = _seed_chunks(repo, ["base doc passage"])
    other_personal_nb, _ = _seed_chunks(repo, ["other personal doc passage"])
    repo.mark_notebook_base(base_nb.id)

    chunks = [
        RetrievedChunk(chunk_id="ck-empty", source_id="s", source_title="D",
                        section_path="1", text="empty-notebook-id chunk", relevance=0.5),
        RetrievedChunk(chunk_id="ck-base", source_id="s", source_title="D",
                        section_path="1", text="base-tier chunk", relevance=0.5,
                        notebook_id=base_nb.id),
        RetrievedChunk(chunk_id="ck-other-personal", source_id="s", source_title="D",
                        section_path="1", text="other-personal-tier chunk", relevance=0.5,
                        notebook_id=other_personal_nb.id),
        RetrievedChunk(chunk_id="ck-own", source_id="s", source_title="D",
                        section_path="1", text="own-tier chunk", relevance=0.5,
                        notebook_id=own_nb.id),
    ]

    _, id_map = repo._chunk_answer_context(chunks, notebook_id=own_nb.id)

    tier_by_chunk = {v["object_id"]: v["tier"] for v in id_map.values()}
    assert tier_by_chunk["ck-empty"] == "personal", (
        f"notebook_id 留空应回退调用方 own_nb(personal),实为 {tier_by_chunk['ck-empty']}")
    assert tier_by_chunk["ck-base"] == "base", (
        f"notebook_id 指向 base 库的 chunk tier 应为 base,实为 {tier_by_chunk['ck-base']}")
    assert tier_by_chunk["ck-other-personal"] == "personal", (
        f"notebook_id 指向另一个 personal 库的 chunk tier 应为 personal,实为 "
        f"{tier_by_chunk['ck-other-personal']}")
    assert tier_by_chunk["ck-own"] == "personal", (
        f"notebook_id 等于 active 自己的 chunk tier 应为 personal,实为 {tier_by_chunk['ck-own']}")

    # codex r2 fix: id_map 的 "notebook_id" 键只有真正跨库才非空(不像 tier 那样
    # 回退调用方 own_nb)——ck-empty 没打来源留空;ck-base/ck-other-personal 指向
    # 真正不同的库,原样带出;ck-own 的 notebook_id 原始值恰好等于调用方自己的
    # own_nb(联邦/PPR 检索对本库命中同样会打上 active 自己的 id,并非只在跨库
    # 命中时才打标——见 evidence_context.py chunk_context 的 raw_origin 归一化)
    # 必须归一成空串,否则前端引用徽章会显示一个多余的"来自「自己」"库名。
    nb_by_chunk = {v["object_id"]: v["notebook_id"] for v in id_map.values()}
    assert nb_by_chunk["ck-empty"] == "", (
        f"notebook_id 留空的 chunk,evidence 的 notebook_id 也应留空(不回退 own_nb),"
        f"实为 {nb_by_chunk['ck-empty']!r}")
    assert nb_by_chunk["ck-base"] == base_nb.id, (
        f"notebook_id 指向 base 库的 chunk,evidence 的 notebook_id 应为 {base_nb.id!r},"
        f"实为 {nb_by_chunk['ck-base']!r}")
    assert nb_by_chunk["ck-other-personal"] == other_personal_nb.id, (
        f"notebook_id 指向另一个 personal 库的 chunk,evidence 的 notebook_id 应为 "
        f"{other_personal_nb.id!r},实为 {nb_by_chunk['ck-other-personal']!r}")
    assert nb_by_chunk["ck-own"] == "", (
        f"notebook_id 等于调用方自己(own_nb)的 chunk,evidence 的 notebook_id 必须"
        f"归一成空串(不是「跨库」),实为 {nb_by_chunk['ck-own']!r}")
