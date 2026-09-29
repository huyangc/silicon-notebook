"""原文检索工具(`ReasoningRetriever.search_chunks`)内部的 cross-encoder 精排。

红线:
* 精排可用时,入选由精排序决定(取前 k),本库保底席位照旧生效;
* 基线按融合分取前 `REASONING_CHUNK_RERANK_CANDIDATES` 条交精排,生成问题补充
  候选单独精排、排在基线之后;
* 精排关闭 / 未配置 / 没有 `.rerank` 的 model_clients / 调用失败时回到 MMR,
  结果与改动前逐字节相同,失败不进本次提问的模型错误集合(不上横幅)、只记一条
  不含内容的事件;
* `relevance` 不被改写(它参与接地阈值)。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.ask_context import _ASK_MODEL_ERRORS
from app.core.config import Settings
from app.domain.retrieval import RetrievalSupport
from app.services.cancellation import AskCancelled
from app.services.retrieval import RetrievedChunk
from tests.model_testkit import bind_chat_client, bind_rerank_client
from tests.test_reasoning_retrieval import (  # noqa: F401 — rrepo 是夹具
    _SeqLLM, _seed_notebook_without_kg, rrepo,
)


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #

def _hit(chunk_id, relevance, *, notebook_id="", generated=False):
    supports = (
        (RetrievalSupport(origin="generated_question",
                          support_kind="chunk", support_id=chunk_id),)
        if generated else ()
    )
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=f"s-{chunk_id}", source_title="Doc",
        section_path=f"章节 {chunk_id}", text=f"{chunk_id} 的原文段落正文",
        relevance=relevance, score=relevance, notebook_id=notebook_id,
        retrieval_supports=supports)


class _Retrieval:
    """召回交回固定候选;`select_chunk_candidates` 是可辨认的 MMR 哨兵。"""

    def __init__(self, scored):
        self.scored = list(scored)
        self.select_calls = []

    def retrieve_chunk_candidates(self, notebook_id, query):
        return list(self.scored), [c.chunk_id for c in self.scored], None

    def select_chunk_candidates(self, scored, ids, matrix, k, lambda_):
        self.select_calls.append((k, lambda_))
        return [c for c in scored if c.chunk_id.endswith("-mmr")] or list(scored[-k:])


class _Rerank:
    """记录每次调用的文档;缺省返回逆序,也可指定 order 函数 / 失败方式。"""

    def __init__(self, *, configured=True, order=None, fail=None):
        self.configured = configured
        self.calls = []
        self._order = order or (lambda n: list(reversed(range(n))))
        self._fail = fail

    def rerank(self, query, documents, on_error=None):
        self.calls.append((query, list(documents)))
        if self._fail == "raise":
            raise RuntimeError("rerank upstream down")
        if self._fail == "on_error":
            on_error(RuntimeError("rerank batch failed"))
            return list(range(len(documents)))
        return self._order(len(documents))


class _EventLog:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(dict(event))


class _Models:
    def __init__(self, client):
        self.client = client
        self.requested = []
        self.event_log = _EventLog()

    def rerank(self, workload_id):
        self.requested.append(workload_id)
        return self.client


def _retriever(scored, model_clients, **overrides):
    from app.services.reasoning_retrieval import ReasoningRetriever

    settings = Settings()
    for key, value in overrides.items():
        setattr(settings, key, value)
    retrieval = _Retrieval(scored)
    rr = ReasoningRetriever(
        retrieval=retrieval, model_clients=model_clients, communities=None,
        settings=settings)
    return rr, retrieval


def _ids(hits):
    return [h.chunk_id for h in hits]


def _descending(n, prefix="b", start=0.95, step=0.01, **kw):
    return [_hit(f"{prefix}{i:02d}", round(start - i * step, 4), **kw)
            for i in range(n)]


# --------------------------------------------------------------------------- #
# 1. 精排决定入选;本库保底仍生效
# --------------------------------------------------------------------------- #

def test_reverse_rerank_picks_the_rerank_head_not_mmr():
    scored = _descending(6)
    models = _Models(_Rerank())
    rr, retrieval = _retriever(scored, models)

    selected = rr.search_chunks("nb", "布局布线", k=3)

    assert _ids(selected) == ["b05", "b04", "b03"]
    assert retrieval.select_calls == []          # MMR 没被调用
    assert models.requested == ["retrieval_rerank"]
    assert [q for q, _ in models.client.calls] == ["布局布线"]
    # relevance 原样保留(接地阈值读它)。
    assert [h.relevance for h in selected] == [0.9, 0.91, 0.92]


def test_action_default_k_is_chunk_mmr_k():
    scored = _descending(30, step=0.001)
    rr, _ = _retriever(scored, _Models(_Rerank()))
    assert len(rr.search_chunks("nb", "q")) == rr.settings.chunk_mmr_k


def test_active_reserve_still_pulls_an_active_passage_back():
    # 参考库 6 段强命中 + 本库 2 段弱命中;精排给出融合分原序(恒等序不是失败),
    # 本库两段排在最末 → 前 4 段全是参考库,保底(ceil(4 × 0.25)=1 席)把本库
    # 最强那段换进来。
    peers = _descending(6, prefix="p", notebook_id="nb-peer")
    active = [_hit("a00", 0.30), _hit("a01", 0.20)]
    models = _Models(_Rerank(order=lambda n: list(range(n))))
    rr, retrieval = _retriever(peers + active, models)

    selected = rr.search_chunks("nb", "q", k=4)

    assert len(selected) == 4
    assert "a00" in _ids(selected)
    assert _ids(selected)[:3] == ["p00", "p01", "p02"]
    assert retrieval.select_calls == []


# --------------------------------------------------------------------------- #
# 2. 候选窗口
# --------------------------------------------------------------------------- #

def test_only_the_top_window_by_fused_score_goes_to_the_reranker():
    scored = _descending(80, step=0.001)
    shuffled = scored[1::2] + scored[0::2]       # 召回序与融合分序无关
    models = _Models(_Rerank())
    rr, _ = _retriever(shuffled, models, reasoning_chunk_rerank_candidates=50)

    selected = rr.search_chunks("nb", "q", k=16)

    assert len(models.client.calls) == 1
    docs = models.client.calls[0][1]
    assert len(docs) == 50
    assert docs == [c.text for c in scored[:50]]
    # 逆序精排 → 取窗口里融合分最低的 16 段。
    assert _ids(selected) == _ids(list(reversed(scored[:50]))[:16])


def test_window_smaller_than_k_tops_up_with_fused_order():
    scored = _descending(10)
    models = _Models(_Rerank())
    rr, _ = _retriever(scored, models, reasoning_chunk_rerank_candidates=3)

    selected = rr.search_chunks("nb", "q", k=5)

    assert len(models.client.calls[0][1]) == 3
    assert _ids(selected) == ["b02", "b01", "b00", "b03", "b04"]


# --------------------------------------------------------------------------- #
# 3. 生成问题补充候选:单独精排、排在基线之后
# --------------------------------------------------------------------------- #

def test_supplemental_candidates_are_reranked_separately_after_the_baseline():
    baseline = _descending(3)
    supplemental = _descending(5, prefix="g", start=0.99, generated=True)
    models = _Models(_Rerank())
    rr, _ = _retriever(supplemental + baseline, models)

    selected = rr.search_chunks("nb", "q", k=5)

    docs = [d for _, d in models.client.calls]
    assert docs == [[c.text for c in baseline], [c.text for c in supplemental]]
    # 补充候选融合分更高也排在基线之后。
    assert _ids(selected) == ["b02", "b01", "b00", "g04", "g03"]


def test_supplemental_is_not_reranked_when_the_baseline_fills_k():
    baseline = _descending(6)
    supplemental = _descending(3, prefix="g", start=0.99, generated=True)
    models = _Models(_Rerank())
    rr, _ = _retriever(baseline + supplemental, models)

    selected = rr.search_chunks("nb", "q", k=4)

    assert len(models.client.calls) == 1
    assert all(not h.chunk_id.startswith("g") for h in selected)


# --------------------------------------------------------------------------- #
# 4. 回退到 MMR:逐字节不变,不上横幅
# --------------------------------------------------------------------------- #

def _mmr_baseline(scored, k):
    rr, retrieval = _retriever(scored, SimpleNamespace())
    return rr.search_chunks("nb", "q", k=k), retrieval


@pytest.mark.parametrize("models", [
    _Models(_Rerank(configured=False)),
    SimpleNamespace(),                                   # from_repository 形态
    _Models(SimpleNamespace(rerank=lambda *a, **kw: [])),  # 客户端不自报 configured
])
def test_unavailable_rerank_is_the_mmr_path(models):
    scored = _descending(6)
    expected, _ = _mmr_baseline(scored, 3)
    rr, retrieval = _retriever(scored, models)

    selected = rr.search_chunks("nb", "q", k=3)

    assert selected == expected
    assert retrieval.select_calls == [(3, rr.settings.chunk_mmr_lambda)]
    client = getattr(models, "client", None)
    assert not getattr(client, "calls", [])


@pytest.mark.parametrize("fail", ["raise", "on_error"])
def test_rerank_failure_falls_back_to_mmr_and_records_an_event(fail):
    scored = _descending(6)
    expected, _ = _mmr_baseline(scored, 3)
    models = _Models(_Rerank(fail=fail))
    rr, retrieval = _retriever(scored, models)
    sink: list = []
    token = _ASK_MODEL_ERRORS.set(sink)
    try:
        selected = rr.search_chunks("nb", "q", k=3)
    finally:
        _ASK_MODEL_ERRORS.reset(token)

    assert selected == expected
    assert retrieval.select_calls == [(3, rr.settings.chunk_mmr_lambda)]
    assert sink == []                              # 不进横幅集合
    assert models.event_log.events == [{
        "kind": "reasoning_chunk_rerank_fallback",
        "reason": "rerank_failed",
        "error_type": "RuntimeError",
        "candidates": 6,
    }]


def test_supplemental_rerank_failure_also_falls_back_to_mmr():
    baseline = _descending(2)
    supplemental = _descending(4, prefix="g", generated=True)
    calls = {"n": 0}

    class _SecondFails(_Rerank):
        def rerank(self, query, documents, on_error=None):
            calls["n"] += 1
            if calls["n"] == 2:
                on_error(RuntimeError("second batch"))
                return list(range(len(documents)))
            return super().rerank(query, documents, on_error)

    models = _Models(_SecondFails())
    rr, retrieval = _retriever(baseline + supplemental, models)
    rr.search_chunks("nb", "q", k=4)
    assert retrieval.select_calls == [(4, rr.settings.chunk_mmr_lambda)]


def test_failure_does_not_reach_the_banner_through_the_real_provider(rrepo):
    """真 provider(带 `note_model_error`)+ 抛错的精排:横幅集合仍为空。"""
    from app.services.reasoning_retrieval import ReasoningRetriever

    client = _Rerank(fail="on_error")
    bind_rerank_client(rrepo, client)
    scored = _descending(6)
    retrieval = _Retrieval(scored)
    rr = ReasoningRetriever(
        retrieval=retrieval, model_clients=rrepo._runtime.models,
        communities=None, settings=rrepo.settings)
    sink: list = []
    token = _ASK_MODEL_ERRORS.set(sink)
    try:
        rr.search_chunks("nb", "q", k=3)
    finally:
        _ASK_MODEL_ERRORS.reset(token)
    assert client.calls and sink == []
    assert retrieval.select_calls


# --------------------------------------------------------------------------- #
# 5. 开关:零精排调用
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("overrides", [
    {"reasoning_chunk_rerank_enabled": False},
    {"reasoning_chunk_rerank_candidates": 0},
])
def test_switches_off_mean_zero_rerank_calls(overrides):
    scored = _descending(6)
    expected, _ = _mmr_baseline(scored, 3)
    models = _Models(_Rerank())
    rr, _ = _retriever(scored, models, **overrides)
    assert rr.search_chunks("nb", "q", k=3) == expected
    assert models.client.calls == [] and models.requested == []


def test_env_aliases(monkeypatch):
    assert Settings().reasoning_chunk_rerank_enabled is True
    assert Settings().reasoning_chunk_rerank_candidates == 50
    monkeypatch.setenv("REASONING_CHUNK_RERANK_ENABLED", "false")
    monkeypatch.setenv("REASONING_CHUNK_RERANK_CANDIDATES", "7")
    settings = Settings()
    assert settings.reasoning_chunk_rerank_enabled is False
    assert settings.reasoning_chunk_rerank_candidates == 7


def test_pool_no_larger_than_k_needs_no_rerank():
    scored = _descending(3)
    models = _Models(_Rerank())
    rr, retrieval = _retriever(scored, models)
    rr.search_chunks("nb", "q", k=3)
    assert models.client.calls == [] and retrieval.select_calls


def test_cancellation_is_not_swallowed_into_a_fallback():
    import threading

    scored = _descending(6)
    cancel = threading.Event()

    class _CancelDuring(_Rerank):
        def rerank(self, query, documents, on_error=None):
            cancel.set()
            on_error(AskCancelled())
            return list(range(len(documents)))

    models = _Models(_CancelDuring())
    rr, retrieval = _retriever(scored, models)
    rr.cancel_event = cancel
    with pytest.raises(AskCancelled):
        rr.search_chunks("nb", "q", k=3)
    assert retrieval.select_calls == [] and models.event_log.events == []


# --------------------------------------------------------------------------- #
# 6. 端到端:真仓库、真召回,首轮播种每条子查询一次精排
# --------------------------------------------------------------------------- #

_TEXTS = tuple(
    f"布局布线第 {i} 步:{topic}。" + "细节说明。" * 40
    for i, topic in enumerate((
        "全局布局", "详细布线", "时钟树综合", "静态时序分析", "功耗优化",
        "拥塞分析", "天线效应修复", "填充单元插入",
    ))
)


def test_first_round_seed_reranks_once_per_subquery(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_notebook_without_kg(rrepo, texts=_TEXTS)
    rrepo.settings.graph_ppr_enabled = False
    rrepo.settings.reasoning_per_query_limit = 1
    subqueries = ["布局布线", "时钟树综合"]
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": q} for q in subqueries]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    client = _Rerank()
    bind_rerank_client(rrepo, client)
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    # 生产 Ask/报告路径的 model_clients 是带 `.rerank` 的 provider。
    rr.model_clients = rrepo._runtime.models
    assert len(rrepo.retrieval.retrieve_chunk_candidates(nb.id, "布局布线")[0]) > 1

    res = rr.run(nb.id, "布局布线怎么做", "")

    assert sorted(q for q, _ in client.calls) == sorted(subqueries)
    seed = next(t for t in res.trace if t.step_type == "search_chunks")
    assert seed.detail["phase"] == "seed" and seed.detail["found"] >= 1


def test_real_path_unconfigured_rerank_is_byte_identical_to_mmr(rrepo):
    """真召回 + 真 MMR:未配置精排的 provider 与没有 `.rerank` 的 facade 逐字节相同。"""
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_notebook_without_kg(rrepo, texts=_TEXTS)
    before = ReasoningRetriever.from_repository(
        rrepo, rrepo.settings).search_chunks(nb.id, "布局布线", k=2)
    client = _Rerank(configured=False)
    bind_rerank_client(rrepo, client)
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    rr.model_clients = rrepo._runtime.models
    after = rr.search_chunks(nb.id, "布局布线", k=2)
    assert client.calls == []
    assert json.dumps([vars(h) for h in after], default=repr, ensure_ascii=False) == \
        json.dumps([vars(h) for h in before], default=repr, ensure_ascii=False)
