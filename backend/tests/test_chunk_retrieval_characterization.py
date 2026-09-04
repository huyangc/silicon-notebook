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


def _add_chunk_source(repo, nb_id, texts):
    """往已有 notebook 里追加一个来源+chunk(镜像 ``_seed_chunks``,但复用既有
    notebook 而不是新建一个)——用于模拟「冻结快照之后才完成抽取的并发上传」。"""
    sid = f"src-{uuid.uuid4().hex[:8]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,"
            "file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb_id, "Doc2", "document", "s2.md", "/tmp/s2.md", 0, "h2", "", "", "extracted", now, now))
        for i, t in enumerate(texts, 1):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-{i:04d}", sid, "paragraph", f"p{i}", t, "{}", now))
    repo._chunk_and_embed_source(sid)
    return sid


def _capture_events(repo, monkeypatch):
    """Spy on ``repo.event_log.emit`` and return the list it appends to.

    Mirrors ``tests/test_chunk_bruteforce_guard.py``'s helper of the same
    name -- same idiom, kept local here so this file's fixtures stay
    self-contained.
    """
    events = []
    orig_emit = repo.event_log.emit

    def spy_emit(event, **kw):
        events.append(event)
        return orig_emit(event, **kw)

    monkeypatch.setattr(repo.event_log, "emit", spy_emit)
    return events


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
# 6b. R1(审计 ASK-1,P0):narrowed=False 全选冻结必须恢复语料语言闸这条
#     **路由**,同时照常把冻结清单下推给 SQL;narrowed=True 的真收窄路径不回归。
# ═══════════════════════════════════════════════════════════════════════════

def test_retrieve_chunks_all_selected_frozen_scope_restores_the_language_gate(
    repo, monkeypatch
):
    """全选冻结(narrowed=False)下 _retrieve_chunks 的两个维度必须分开:

    * **路由**恢复到跟「无 scope」一样——`_lexical_corpus_langs` 收到
      source_scoped=False,语料语言闸不被跳过。它的豁免理由(「受限运行的词法臂
      是唯一候选来源,而且来源谓词把扫描收窄了」)对真收窄成立、对全选不成立:
      全选的谓词覆盖整库,正是这道闸要挡的病态全库探针(实测 64 词项 29.7s vs
      3 词项 0.26s)。
    * **过滤**不放松——`_retrieve_chunks_fts_degraded` 仍然收到物化的冻结清单
      (元素臂与 KG 臂没有自己的 actor 谓词,清单是别人的私有 Memory 与并发上传
      唯一的 LIMIT 前防线;codex #640 R1 两条 P1)。

    等价 oracle:未漂移全选与无 scope 的候选集(chunk_id/score/relevance)必须逐
    字相同——冻结清单此时恰好覆盖整库,下推它一条候选都不改。

    对照:同一份来源清单在 narrowed=True(真收窄)下 source_scoped 必须是 True。

    **变异锚点**:把 ``_lexical_gate_source_scoped`` 换回
    ``allowed_source_ids is not None`` → 全选那一组的 corpus_lang_calls 变成
    ``[True]``,本条报红。"""
    from app.models.source_scope import ResolvedSourceScope
    from app.services.source_scope import source_scope_context

    nb, sid = _seed_chunks(
        repo, [f"bandgap reference topic {i} body detail " * 5 for i in range(3)]
    )
    repo.backfill_chunk_fts(nb.id)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda nb_id: {"copyable": False, "size": {}})
    # 未建 scale 索引 → ANN 分支不可用,自然落到大库/降级路径(同 test 6)。

    corpus_lang_calls = []
    orig_corpus_langs = repo.retrieval.candidates._lexical_corpus_langs

    def _spy_corpus_langs(notebook_id, *, source_scoped=False):
        corpus_lang_calls.append(source_scoped)
        return orig_corpus_langs(notebook_id, source_scoped=source_scoped)

    monkeypatch.setattr(
        repo.retrieval.candidates, "_lexical_corpus_langs", _spy_corpus_langs
    )

    fts_degraded_calls = []
    orig_fts_degraded = repo.retrieval.candidates._retrieve_chunks_fts_degraded

    def _spy_fts_degraded(notebook_id, query, query_vector, recall, n_chunks,
                          **kwargs):
        fts_degraded_calls.append(kwargs.get("allowed_source_ids"))
        return orig_fts_degraded(
            notebook_id, query, query_vector, recall, n_chunks, **kwargs
        )

    monkeypatch.setattr(
        repo.retrieval.candidates, "_retrieve_chunks_fts_degraded", _spy_fts_degraded
    )

    # ── 等价 oracle 基线:完全不进入任何 source_scope_context(「无 scope」)。
    corpus_lang_calls.clear()
    fts_degraded_calls.clear()
    scored_unscoped, _ids_u, _mat_u = repo.retrieval.candidates._retrieve_chunks(
        nb.id, "bandgap"
    )
    unscoped_corpus_lang_calls = list(corpus_lang_calls)
    unscoped_fts_degraded_calls = list(fts_degraded_calls)

    all_selected_scope = ResolvedSourceScope(
        mode="include", source_ids=[sid], narrowed=False
    )
    corpus_lang_calls.clear()
    fts_degraded_calls.clear()
    with source_scope_context(nb.id, all_selected_scope):
        scored, ids, mat = repo.retrieval.candidates._retrieve_chunks(nb.id, "bandgap")

    assert corpus_lang_calls == [False] == unscoped_corpus_lang_calls, (
        "R1:narrowed=False 全选冻结必须让语料语言闸看到 source_scoped=False,"
        f"同无 scope 一样,实际全选={corpus_lang_calls} 无 scope={unscoped_corpus_lang_calls}"
    )
    assert unscoped_fts_degraded_calls == [None], (
        f"无 scope 本来就没有 allow-list,实际 {unscoped_fts_degraded_calls}"
    )
    assert fts_degraded_calls == [(sid,)], (
        "全选冻结仍然必须把物化的冻结清单下推到 LIMIT 之前(别人的私有 Memory "
        f"与并发上传唯一的防线),实际 {fts_degraded_calls}"
    )
    assert len(scored) >= 1
    assert all(c.chunk_id.startswith("ck-") for c in scored)
    # 逐字候选集对比(RetrievedChunk 的 __eq__ 比较 chunk_id/score/relevance/…,
    # 唯独排除 retrieval_supports,见 app.domain.retrieval——支持链路带 tuple
    # 顺序但打分与候选身份才是等价 oracle 真正要钉的东西)。
    assert scored == scored_unscoped, (
        "R1 等价 oracle:未漂移全选冻结(narrowed=False)与无 scope 的候选集必须"
        "逐字相同——冻结清单此时恰好覆盖整库,下推它一条候选都不改"
    )

    # 对照:同一份清单真收窄(narrowed=True)时不得回归——source_scoped 必须
    # 是 True,allowed_source_ids 必须原样透传成显式清单。
    corpus_lang_calls.clear()
    fts_degraded_calls.clear()
    narrowed_scope = ResolvedSourceScope(
        mode="include", source_ids=[sid], narrowed=True
    )
    with source_scope_context(nb.id, narrowed_scope):
        scored2, _ids2, _mat2 = repo.retrieval.candidates._retrieve_chunks(
            nb.id, "bandgap"
        )

    assert corpus_lang_calls == [True], (
        f"对照:narrowed=True 真收窄不得回归成 source_scoped=False,实际 {corpus_lang_calls}"
    )
    assert fts_degraded_calls == [(sid,)], (
        f"对照:narrowed=True 时 allowed_source_ids 必须原样透传,实际 {fts_degraded_calls}"
    )
    assert len(scored2) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# 6c. codex #640 R2(P1):元素回退臂(_retrieve_elements → 内部 _retrieve_chunks)
#     必须把它自己物化出的天花板标记为「上下文」而非 explicit——语料语言闸的
#     路由判据不能看「清单是否非 None」,否则全选冻结(narrowed=False)一进元素
#     回退臂就会把语言闸重新关掉,原样复现审计 ASK-1 的病态探测集(措辞见
#     _lexical_gate_source_scoped/_retrieve_chunks_baseline 的说明)。
# ═══════════════════════════════════════════════════════════════════════════

def test_retrieve_elements_all_selected_frozen_scope_keeps_the_language_gate_open(
    repo, monkeypatch
):
    """全选冻结(narrowed=False)下 ``_retrieve_elements`` 落到 chunk 回退分支
    (物化出非 None 的天花板并转发给内部 ``_retrieve_chunks``)时,语料语言闸必须
    看到 ``source_scoped=False``——同「无 scope」/test 6b 的 ``_retrieve_chunks``
    结论一致,只是这次经由 ``_retrieve_elements`` 触发,证明 P1 那次「非 None 倒推
    explicit」不会在这条调用链上死灰复燃。

    **变异锚点**:把 ``_retrieve_elements`` 对内部 ``_retrieve_chunks`` 的调用改回
    传 ``producer_explicit=True``(或把 ``_retrieve_chunks_baseline`` 的
    ``producer_explicit`` 重新从 ``allowed_source_ids is not None`` 倒推)→
    corpus_lang_calls 变成 ``[True]``,本条报红。
    """
    from app.models.source_scope import ResolvedSourceScope
    from app.services.source_scope import source_scope_context

    nb, sid = _seed_chunks(
        repo, [f"bandgap reference topic {i} body detail " * 5 for i in range(3)]
    )
    repo.backfill_chunk_fts(nb.id)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda nb_id: {"copyable": False, "size": {}})

    corpus_lang_calls = []
    orig_corpus_langs = repo.retrieval.candidates._lexical_corpus_langs

    def _spy_corpus_langs(notebook_id, *, source_scoped=False):
        corpus_lang_calls.append(source_scoped)
        return orig_corpus_langs(notebook_id, source_scoped=source_scoped)

    monkeypatch.setattr(
        repo.retrieval.candidates, "_lexical_corpus_langs", _spy_corpus_langs
    )

    all_selected_scope = ResolvedSourceScope(
        mode="include", source_ids=[sid], narrowed=False
    )
    with source_scope_context(nb.id, all_selected_scope):
        elements = repo.retrieval.candidates._retrieve_elements(
            nb.id, "bandgap", limit=4
        )

    assert corpus_lang_calls == [False], (
        "codex #640 R2 P1:全选冻结经元素回退臂也必须让语料语言闸看到 "
        f"source_scoped=False,实际 {corpus_lang_calls}"
    )
    assert len(elements) >= 1

    # 对照:同一份清单真收窄(narrowed=True)时不得回归——元素回退臂的语言闸
    # 必须继续为受限运行打开受限 lane。
    corpus_lang_calls.clear()
    narrowed_scope = ResolvedSourceScope(
        mode="include", source_ids=[sid], narrowed=True
    )
    with source_scope_context(nb.id, narrowed_scope):
        repo.retrieval.candidates._retrieve_elements(nb.id, "bandgap", limit=4)
    assert corpus_lang_calls == [True], (
        f"对照:narrowed=True 真收窄不得回归成 source_scoped=False,实际 {corpus_lang_calls}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 6c-2. codex #640 R4(P2):R2 的一刀切把「调用方自己真正给的窄清单」也判成了
#     上下文——违反 docs/product-and-api.md:89「producer-supplied allow-lists
#     enter that [restricted] lane」。出处现在由调用方声明(``producer_explicit``
#     形参),不再从清单形状倒推:
#       (a) 调用方自己给的真窄清单、未声明出处 → 默认落在契约安全侧
#           (``producer_explicit`` 缺省 True) → 语言闸判 True(受限 lane)。
#       (b) 插件缝(``_federated_retrieve_elements_impl``)的全宇宙枚举清单仍是
#           上下文,必须显式声明 ``producer_explicit=False`` → 语言闸继续判
#           False——R2-P1 的既有钉不得回退。
# ═══════════════════════════════════════════════════════════════════════════

def test_retrieve_elements_producer_supplied_narrow_list_enters_the_restricted_lane(
    repo, monkeypatch
):
    """codex #640 R4 P2(a):调用方自己给 ``_retrieve_elements`` 一份真窄清单、
    不带任何 request scope 时,语料语言闸必须看到 ``source_scoped=True``——一个
    真正 producer 级的窄清单不能被当成上下文天花板对待
    (docs/product-and-api.md:89)。

    **变异锚点**:把 ``_retrieve_elements`` 里 ``producer_explicit`` 的默认值改回
    ``False``(或让它在 ``caller_supplied_list`` 为真时仍强制 ``False``)→
    corpus_lang_calls 变成 ``[False]``,本条报红。
    """
    from app.services.source_scope import current_source_scope

    nb, sid = _seed_chunks(
        repo, [f"bandgap reference topic {i} body detail " * 5 for i in range(3)]
    )
    repo.backfill_chunk_fts(nb.id)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda nb_id: {"copyable": False, "size": {}})

    corpus_lang_calls = []
    orig_corpus_langs = repo.retrieval.candidates._lexical_corpus_langs

    def _spy_corpus_langs(notebook_id, *, source_scoped=False):
        corpus_lang_calls.append(source_scoped)
        return orig_corpus_langs(notebook_id, source_scoped=source_scoped)

    monkeypatch.setattr(
        repo.retrieval.candidates, "_lexical_corpus_langs", _spy_corpus_langs
    )

    # 没有任何 request scope 在场 -- current_source_scope() 必须为 None,证明
    # 下面的 True 判决来自 producer_explicit 的默认声明,不是request scope 的
    # 收窄位。
    assert current_source_scope() is None
    elements = repo.retrieval.candidates._retrieve_elements(
        nb.id, "bandgap", limit=4, allowed_source_ids=[sid],
    )

    assert corpus_lang_calls == [True], (
        "codex #640 R4 P2:调用方自己给的窄清单必须让语言闸判 source_scoped=True,"
        f"实际 {corpus_lang_calls}"
    )
    assert len(elements) >= 1


def test_federated_retrieve_elements_plugin_enumeration_stays_out_of_the_restricted_lane(
    repo, monkeypatch
):
    """codex #640 R4 P2(b)对照:插件元素检索缝(``_federated_retrieve_elements_impl``,
    镜像 ``PluginRetrievalAccess`` 逐次下推的全宇宙枚举清单)即便传了一份非空清单,
    也必须继续让语言闸判 ``source_scoped=False``——它是上下文,不是 producer 的
    窄清单,R2 P1 修的这条不得因 R4 反转默认值而回归。

    **变异锚点**:把 ``_federated_retrieve_elements_impl`` 对 ``_retrieve_elements``
    的调用改成不显式声明 ``producer_explicit=False``(落回 R4 的默认 True)→
    corpus_lang_calls 变成 ``[True]``,本条报红。
    """
    nb, sid = _seed_chunks(
        repo, [f"bandgap reference topic {i} body detail " * 5 for i in range(3)]
    )
    repo.backfill_chunk_fts(nb.id)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda nb_id: {"copyable": False, "size": {}})

    corpus_lang_calls = []
    orig_corpus_langs = repo.retrieval.candidates._lexical_corpus_langs

    def _spy_corpus_langs(notebook_id, *, source_scoped=False):
        corpus_lang_calls.append(source_scoped)
        return orig_corpus_langs(notebook_id, source_scoped=source_scoped)

    monkeypatch.setattr(
        repo.retrieval.candidates, "_lexical_corpus_langs", _spy_corpus_langs
    )

    # 全宇宙枚举形状:插件端口在构造时把该 notebook 全部可见来源都塞进
    # allowed_source_keys(此处只有一个来源,形状仍是「枚举」而非「筛选」)。
    elements = repo.retrieval.candidates.federated_retrieve_elements(
        nb.id, "bandgap", allowed_source_keys=[(nb.id, sid)], limit=4,
    )

    assert corpus_lang_calls == [False], (
        "codex #640 R2 P1(经 R4 复核未回归):插件元素检索缝的全宇宙枚举清单"
        f"必须继续保持语言闸打开(source_scoped=False),实际 {corpus_lang_calls}"
    )
    assert len(elements) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# 6d. codex #640 R2(P2):全选冻结之后来源宇宙漂移,chunk/keyword/KG 三条词法臂
#     必须一致地路由回受限 lane——docs/product-and-api.md:89「A frozen-universe
#     drift makes the run genuinely bounded again and returns it to the
#     restricted lane」。KG 臂的漂移路由是 codex #634 R1 已经修好、
#     test_source_scope.py 的 test_all_selected_freeze_reopens_the_restricted_lane_once_sources_drift
#     已经钉住的形态;这条测试补的是 chunk/keyword 两臂原先缺失的那一半,并且
#     三臂在同一份冻结/漂移快照下一次性证明判据一致。
# ═══════════════════════════════════════════════════════════════════════════

def test_universe_drift_after_all_selected_freeze_reopens_all_three_lexical_arms(
    repo, monkeypatch
):
    """全选冻结(narrowed=False)但宇宙未漂移时,chunk/keyword/KG 三臂都必须保持
    语言闸打开(source_scoped=False,同「无 scope」);冻结快照之外插入一个新来源
    (chunk + KG 对象都有)之后,三臂都必须改判为受限(source_scoped=True)。

    **变异锚点**:把 ``_lexical_gate_source_scoped`` verdict 公式里的
    ``or drifted`` 项去掉 → 漂移那组三个调用全部退回 ``False``,本条报红
    (chunk/keyword 两臂;KG 臂走的是独立的 ``source_candidates_restricted``
    公式,不经过 ``_lexical_gate_source_scoped``,所以那条腿的回归由
    test_source_scope.py 的既有钉子单独覆盖——这里只验证三者当前判据一致)。
    """
    from app.models.source_scope import ResolvedSourceScope
    from app.services.source_scope import source_scope_context

    nb, sid = _seed_chunks(
        repo, [f"bandgap reference topic {i} body detail " * 5 for i in range(3)]
    )
    repo.backfill_chunk_fts(nb.id)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda nb_id: {"copyable": False, "size": {}})
    repo.store_kg(nb.id, None, [{
        "local_id": "frozen", "object_type": "concept",
        "payload": {"name": "bandgap reference"},
        "evidence": [{"source_id": sid, "source_title": "Doc",
                      "element_id": f"el-{sid}-0001", "element_type": "paragraph",
                      "location_label": "p1", "quoted_span": "bandgap",
                      "confidence": 1.0}],
    }], [])

    corpus_lang_calls = []
    orig_corpus_langs = repo.retrieval.candidates._lexical_corpus_langs

    def _spy_corpus_langs(notebook_id, *, source_scoped=False):
        corpus_lang_calls.append(source_scoped)
        return orig_corpus_langs(notebook_id, source_scoped=source_scoped)

    monkeypatch.setattr(
        repo.retrieval.candidates, "_lexical_corpus_langs", _spy_corpus_langs
    )

    frozen_scope = ResolvedSourceScope(
        mode="include", source_ids=[sid], narrowed=False
    )

    def _probe_all_three_arms():
        repo.retrieval.candidates._retrieve_chunks(nb.id, "bandgap")
        repo.retrieval.candidates._keyword_chunk_candidates(nb.id, "bandgap")
        repo.retrieval.candidates._retrieve_scored(nb.id, "bandgap")

    # ── 无漂移对照:三条臂都必须保持无 scope 的语言闸(source_scoped=False)。
    corpus_lang_calls.clear()
    with source_scope_context(nb.id, frozen_scope):
        _probe_all_three_arms()
    assert corpus_lang_calls == [False, False, False], (
        "无漂移的全选冻结下,chunk/keyword/KG 三臂都不得受限,实际(顺序 "
        f"chunk/keyword/KG)= {corpus_lang_calls}"
    )

    # ── 制造漂移:冻结快照之外插入一个新来源(带 chunk + KG 对象),让可见宇宙
    #    不再等于冻结快照——同一份 frozen_scope 仍然只列 sid(narrowed=False)。
    sid2 = _add_chunk_source(repo, nb.id, ["drifted source body content " * 5])
    repo.backfill_chunk_fts(nb.id)
    repo.store_kg(nb.id, None, [{
        "local_id": "drifted", "object_type": "concept",
        "payload": {"name": "bandgap reference"},
        "evidence": [{"source_id": sid2, "source_title": "Doc2",
                      "element_id": f"el-{sid2}-0001", "element_type": "paragraph",
                      "location_label": "p1", "quoted_span": "bandgap",
                      "confidence": 1.0}],
    }], [])

    corpus_lang_calls.clear()
    with source_scope_context(nb.id, frozen_scope):
        _probe_all_three_arms()
    assert corpus_lang_calls == [True, True, True], (
        "codex #640 R2 P2:漂移之后 chunk/keyword/KG 三臂都必须回到受限词法 "
        f"lane,实际(顺序 chunk/keyword/KG)= {corpus_lang_calls}"
    )


def test_universe_drift_probe_runs_at_most_once_per_retrieval_arm_entry(
    repo, monkeypatch
):
    """codex #640 R2 P2:漂移探针在**每个检索臂入口至多调一次**——chunk 臂的
    多子查询扇出(``_retrieve_chunks_multi``)不得让 N 个子查询各自现探 N 次,
    keyword 臂本来就只调用一次。这不是把探针缓存到 run/请求上(那是 codex
    #634 R1 明令禁止的形态,见 ``_unsafe_source_scope_restricted`` 的说明)——
    这里钉的是「一次 arm 调用只探一次」,不是「一次 run 只探一次」:第二次
    独立调用 ``_retrieve_chunks_multi``/``_keyword_chunk_candidates`` 必须
    重新现探,不能复用上一次的答案(第二段断言)。

    **变异锚点**:把 ``_retrieve_chunks_multi`` 改回让每个子查询各自调用
    ``_lexical_gate_drift_probe``(即去掉 ``_CHUNK_ARM_DRIFTED`` 下传)→
    3 子查询那组探针调用次数从 1 变成 3,本条报红。
    """
    from app.models.source_scope import ResolvedSourceScope
    from app.services.source_scope import source_scope_context

    nb, sid = _seed_chunks(
        repo, [f"bandgap reference topic {i} body detail " * 5 for i in range(3)]
    )
    repo.backfill_chunk_fts(nb.id)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda nb_id: {"copyable": False, "size": {}})

    probe_calls = []
    orig_probe = repo.retrieval.candidates._unsafe_source_scope_restricted

    def _spy_probe(notebook_id):
        verdict = orig_probe(notebook_id)
        probe_calls.append(verdict)
        return verdict

    monkeypatch.setattr(
        repo.retrieval.candidates, "_unsafe_source_scope_restricted", _spy_probe
    )

    frozen_scope = ResolvedSourceScope(
        mode="include", source_ids=[sid], narrowed=False
    )

    # 3 个子查询的一次 fan-out:探针必须只被现探一次,而不是三次。
    probe_calls.clear()
    with source_scope_context(nb.id, frozen_scope):
        repo.retrieval.candidates._retrieve_chunks_multi(
            nb.id, ["bandgap", "reference", "topic"]
        )
    assert probe_calls == [False], (
        f"3 子查询 fan-out 内探针必须只现探一次,实际调用次数 {len(probe_calls)}"
        f"(结果 {probe_calls})"
    )

    # keyword 臂本就只调用一次。
    probe_calls.clear()
    with source_scope_context(nb.id, frozen_scope):
        repo.retrieval.candidates._keyword_chunk_candidates(nb.id, "bandgap")
    assert probe_calls == [False], (
        f"keyword 臂必须只现探一次,实际 {probe_calls}"
    )

    # 对照:不是「缓存到 run/请求」——同一个 with 块里再调一次 fan-out,必须
    # 重新现探(不是复用上一次留下的答案、也不是干脆不再探)。
    probe_calls.clear()
    with source_scope_context(nb.id, frozen_scope):
        repo.retrieval.candidates._retrieve_chunks_multi(nb.id, ["bandgap"])
        repo.retrieval.candidates._retrieve_chunks_multi(nb.id, ["reference"])
    assert probe_calls == [False, False], (
        "两次独立的 _retrieve_chunks_multi 调用必须各自现探一次(不是缓存到 "
        f"run/请求上只探一次),实际 {probe_calls}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 6e. codex #640 R3(P1):``_lexical_gate_source_scoped`` 必须按**被查询的**
#     notebook_id 裁决,不能借用 request scope 那个(主库)的收窄/漂移状态——
#     否则主库收窄时,联邦 element/chunk 检索对**挂载引用库**转发的全量上下文
#     清单也会被误判为「真收窄」,语言闸被错误关闭,大挂载库复现审计 ASK-1
#     的无界词法探针集(这次发生在挂载库而非主库)。
# ═══════════════════════════════════════════════════════════════════════════

def test_lexical_gate_source_scoped_judges_the_queried_notebook_not_the_scope_one(
    repo,
):
    """直接对 ``_lexical_gate_source_scoped`` 钉真值表(不经过完整检索链路,
    这个方法本身是纯读 contextvar + 形参的判定,直接测最贴近 bug 的形状)。

    真值表(scope.notebook_id = 主库,被查询的 notebook_id 分别取主库/挂载库):

    | scope 状态                  | 主库 verdict | 挂载库 verdict |
    |------------------------------|:------------:|:--------------:|
    | narrowed=True(真收窄)       | True         | False          |
    | narrowed=False 未漂移        | False        | False          |
    | narrowed=False 已漂移        | True         | False          |

    **变异锚点**:把 verdict 公式里 ``scope.notebook_id != notebook_id`` 那条
    判别去掉(直接对任何 notebook_id 都读 ``source_scope_restricted()``/
    ``drifted``)→ 挂载库那一列全部从 False 变成跟主库一样,本条报红。
    """
    from app.models.source_scope import ResolvedSourceScope
    from app.services.source_scope import source_scope_context

    candidates = repo.retrieval.candidates
    main_nb_id = "nb-main"
    mounted_nb_id = "nb-mounted"

    # ── narrowed=True(真收窄):主库 True,挂载库 False。
    narrowed_scope = ResolvedSourceScope(
        mode="include", source_ids=["s1"], narrowed=True
    )
    with source_scope_context(main_nb_id, narrowed_scope):
        assert candidates._lexical_gate_source_scoped(("s1",), main_nb_id) is True, (
            "主库真收窄:语言闸判定必须 True(源谓词已经收窄扫描)"
        )
        assert candidates._lexical_gate_source_scoped(
            ("s2", "s3"), mounted_nb_id
        ) is False, (
            "codex #640 R3 P1:挂载库不得借用主库的真收窄状态,verdict 必须 False"
        )

    # ── narrowed=False 未漂移:两者都 False(默认全选,语言闸继续工作)。
    all_selected_scope = ResolvedSourceScope(
        mode="include", source_ids=["s1"], narrowed=False
    )
    with source_scope_context(main_nb_id, all_selected_scope):
        assert candidates._lexical_gate_source_scoped(
            ("s1",), main_nb_id, drifted=False
        ) is False
        assert candidates._lexical_gate_source_scoped(
            ("s2",), mounted_nb_id, drifted=False
        ) is False

        # ── narrowed=False 已漂移:主库 True(冻结宇宙不再等于活宇宙),
        #    挂载库仍 False——``drifted`` 是主库自己探测出来的答案,不描述
        #    任何其他 notebook_id。
        assert candidates._lexical_gate_source_scoped(
            ("s1",), main_nb_id, drifted=True
        ) is True
        assert candidates._lexical_gate_source_scoped(
            ("s2",), mounted_nb_id, drifted=True
        ) is False, (
            "codex #640 R3 P1:主库漂移不得外溢到挂载库的语言闸判定"
        )

    # ── explicit 不受 notebook_id 影响:生产者自己的窄清单,无论对哪个
    #    notebook_id 都直接判 True(它不读 scope)。
    with source_scope_context(main_nb_id, narrowed_scope):
        assert candidates._lexical_gate_source_scoped(
            ("s9",), mounted_nb_id, explicit=True
        ) is True


def test_lexical_gate_ignores_active_notebook_narrowing_for_a_different_notebook_id(
    repo, monkeypatch
):
    """端到端版本:通过真正的 ``_retrieve_chunks_baseline`` 调用(联邦检索对
    挂载引用库转发的那条调用链的下一跳),证明主库收窄不会让挂载库的
    ``_lexical_corpus_langs`` 收到 ``source_scoped=True``。

    **变异锚点**:同上一条——去掉 ``notebook_id`` 判别 → 挂载库那组
    ``corpus_lang_calls`` 从 ``[False]`` 变成 ``[True]``,本条报红。
    """
    from app.models.source_scope import ResolvedSourceScope
    from app.services.source_scope import source_scope_context

    main_nb, main_sid = _seed_chunks(
        repo, [f"bandgap reference topic {i} body detail " * 5 for i in range(3)]
    )
    repo.backfill_chunk_fts(main_nb.id)
    mounted_nb, mounted_sid = _seed_chunks(
        repo, [f"bandgap reference topic {i} body detail " * 5 for i in range(3)]
    )
    repo.backfill_chunk_fts(mounted_nb.id)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda nb_id: {"copyable": False, "size": {}})

    corpus_lang_calls = []
    orig_corpus_langs = repo.retrieval.candidates._lexical_corpus_langs

    def _spy_corpus_langs(notebook_id, *, source_scoped=False):
        corpus_lang_calls.append(source_scoped)
        return orig_corpus_langs(notebook_id, source_scoped=source_scoped)

    monkeypatch.setattr(
        repo.retrieval.candidates, "_lexical_corpus_langs", _spy_corpus_langs
    )

    narrowed_scope = ResolvedSourceScope(
        mode="include", source_ids=[main_sid], narrowed=True
    )
    with source_scope_context(main_nb.id, narrowed_scope):
        # 对照:主库自身真收窄 → 必须继续判 True,不得回归。
        corpus_lang_calls.clear()
        repo.retrieval.candidates._retrieve_chunks_baseline(
            main_nb.id, "bandgap", allowed_source_ids=(main_sid,)
        )
        assert corpus_lang_calls == [True], (
            f"对照:主库真收窄必须继续判 True,实际 {corpus_lang_calls}"
        )

        # 挂载引用库:同一个「主库已收窄」的请求上下文里,联邦检索把它自己的
        # 全量上下文清单(而非真收窄)转发到这里——语言闸必须继续判 False。
        corpus_lang_calls.clear()
        repo.retrieval.candidates._retrieve_chunks_baseline(
            mounted_nb.id, "bandgap", allowed_source_ids=(mounted_sid,)
        )
        assert corpus_lang_calls == [False], (
            "codex #640 R3 P1:主库收窄不得外溢到挂载引用库的语言闸判定,"
            f"实际 {corpus_lang_calls}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6f. codex #640 R3(P2):漂移探针(``_lexical_gate_drift_probe``)本身失败必须
#     fail-open——它的两个调用点(``_retrieve_chunks_multi`` 的一次性现探、
#     ``_keyword_chunk_candidates`` 的唯一一次现探)都在各自的 fail-open
#     ``try/except`` 之外调用它,探针异常若不在 wrapper 内部接住就会直接炸穿
#     整条检索臂,甚至整个 Ask。
# ═══════════════════════════════════════════════════════════════════════════

def test_lexical_gate_drift_probe_fails_open_on_probe_exception(repo, monkeypatch):
    """``_unsafe_source_scope_restricted`` 抛异常时,``_lexical_gate_drift_probe``
    必须吞掉它、发 ``lexical_gate_probe_failed`` 诊断事件(不带查询文本/凭据)、
    返回 False(无漂移=安全路由裁决——语言闸的路由判定失灵只会选错词项集,
    来源谓词本身仍无条件下推,见该函数 docstring)。

    **变异锚点**:把 ``_lexical_gate_drift_probe`` 里包住 ``probe(notebook_id)``
    的 ``try/except`` 去掉 → 本条在 probe 抛出处直接冒泡,报红。
    """
    from app.services.retrieval_candidates import _lexical_gate_drift_probe

    events = _capture_events(repo, monkeypatch)

    def _boom(notebook_id):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(
        repo.retrieval.candidates, "_unsafe_source_scope_restricted", _boom
    )

    verdict = _lexical_gate_drift_probe(repo.retrieval.candidates, "nb-x")

    assert verdict is False, f"探针失败必须 fail-open 到 False,实际 {verdict}"
    failed = [e for e in events if e.get("kind") == "lexical_gate_probe_failed"]
    assert len(failed) == 1, f"探针失败必须发恰好一条诊断事件,实际 {failed}"
    assert failed[0]["notebook_id"] == "nb-x"
    assert failed[0]["error_type"] == "RuntimeError"
    # 诊断事件不带查询文本/凭据——只有定位所需的三个字段。
    assert set(failed[0]) == {"kind", "notebook_id", "error_type"}


def test_chunk_multi_and_keyword_arms_survive_a_failing_drift_probe(repo, monkeypatch):
    """探针失败不得炸穿调用它的检索臂——``_retrieve_chunks_multi``(子查询扇出
    前的一次现探)与 ``_keyword_chunk_candidates``(唯一一次现探)都必须靠
    ``_lexical_gate_drift_probe`` 自己的 fail-open 兜底完成检索,而不是依赖
    调用点各自的 ``try/except``(它们的现探恰好都在各自块之外)。

    **变异锚点**:同上一条——去掉 wrapper 内的 ``try/except`` → 两个调用都
    直接抛出 ``RuntimeError``,本条报红。
    """
    nb, sid = _seed_chunks(
        repo, [f"bandgap reference topic {i} body detail " * 5 for i in range(3)]
    )
    repo.backfill_chunk_fts(nb.id)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda nb_id: {"copyable": False, "size": {}})

    def _boom(notebook_id):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(
        repo.retrieval.candidates, "_unsafe_source_scope_restricted", _boom
    )

    # 不得抛出——多子查询扇出前的一次现探(_CHUNK_ARM_DRIFTED 下传前)。
    collected, per_query, ids, mat = repo.retrieval.candidates._retrieve_chunks_multi(
        nb.id, ["bandgap", "reference"]
    )
    assert isinstance(collected, dict)
    assert len(collected) >= 1, "探针失败不得让子查询扇出本身也丢候选"

    # 不得抛出——keyword 臂唯一一次现探。
    scored = repo.retrieval.candidates._keyword_chunk_candidates(
        nb.id, "bandgap reference"
    )
    assert isinstance(scored, list)
    assert len(scored) >= 1, "探针失败不得让 keyword 臂丢候选"


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

    ann_calls = []

    def _ann_none(
        notebook_id, query, query_vector, idx_, recall, *,
        allowed_source_ids=None, source_restricted=False,
    ):
        ann_calls.append((notebook_id, allowed_source_ids, source_restricted))
        return None                                   # fail-open

    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks_ann", _ann_none)

    gather_calls = {"n": 0}
    orig_gather = repo.retrieval.candidates._gather_chunks

    def _spy_gather(db, notebook_id):
        gather_calls["n"] += 1
        return orig_gather(db, notebook_id)

    monkeypatch.setattr(repo.retrieval.candidates, "_gather_chunks", _spy_gather)

    scored, ids, mat = repo.retrieval.candidates._retrieve_chunks(nb.id, "deepseek moe routing")

    assert ann_calls == [(nb.id, None, False)], "ANN 须按未收窄的来源范围调用一次并 fail-open"
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
