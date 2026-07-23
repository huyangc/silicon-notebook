"""P0-C:PPR seed pass 预取。等价性=预取 on/off 的 ReasoningResult 逐位一致;
并发正确性=ppr_retrieve 在后台线程仍能读到请求用户 ContextVar。"""
import contextvars
import threading

import pytest

from app.services.reasoning_retrieval import ReasoningRetriever, SubQuery, ReflectDecision


class _StubSettings:
    retrieval_top_n = 12
    reasoning_top_n_per_query = 3
    reasoning_top_n_cap = 36
    reasoning_max_steps = 5
    reasoning_max_subqueries = 3
    reasoning_stale_limit = 3
    reasoning_max_element_searches = 2
    reasoning_quota_enabled = False
    graph_ppr_enabled = True
    reasoning_ppr_prefetch = True
    reasoning_timeout_seconds = 5
    reasoning_max_retries = 0
    community_peers_topk = 4
    community_rerank_candidates = 20


class _StubRetrieval:
    """检索原语 stub:确定性返回,并记录 ppr 调用发生的线程。"""
    def __init__(self):
        self.ppr_threads = []

    def federated_retrieve(self, nb, q, types=None, w_keyword=0.4, w_semantic=0.6):
        return []

    def retrieve_scored(self, nb, q):
        return []

    def ppr_retrieve(self, nb, q):
        self.ppr_threads.append(threading.current_thread().name)
        from app.services.retrieval import RetrievedChunk
        return [RetrievedChunk(chunk_id=f"ch-{q[:4]}", source_id="s1",
                               source_title="t", section_path="p",
                               text="正文", relevance=0.9, score=0.9)]


class _StubRepo:
    def __init__(self):
        self.retrieval = _StubRetrieval()

    def chat(self, workload_id):
        return type("C", (), {"configured": False})()


class _StubCommunities:
    pass


def _mk(settings=None):
    repo = _StubRepo()
    r = ReasoningRetriever(
        retrieval=repo.retrieval,
        model_clients=repo,
        communities=_StubCommunities(),
        settings=settings or _StubSettings(),
    )
    # plan/reflect 固定:1 个子查询,反思立即 answer
    r.plan = lambda question, history="": [SubQuery(query=question)]
    r.reflect = lambda question, s: ReflectDecision(sufficient=True, next_action="answer")
    return repo, r


def test_prefetch_result_identical_to_serial():
    s_on = _StubSettings()
    s_off = _StubSettings()
    s_off.reasoning_ppr_prefetch = False
    _, r_on = _mk(s_on)
    _, r_off = _mk(s_off)
    res_on = r_on.run("nb1", "带隙基准的启动电路?")
    res_off = r_off.run("nb1", "带隙基准的启动电路?")
    assert [c.chunk_id for c in res_on.chunks] == [c.chunk_id for c in res_off.chunks]
    assert [t.step_type for t in res_on.trace] == [t.step_type for t in res_off.trace]


def test_prefetch_propagates_contextvar():
    cv = contextvars.ContextVar("probe", default="unset")
    cv.set("user-42")
    repo, r = _mk()
    seen = {}
    orig = repo.retrieval.ppr_retrieve
    repo.retrieval.ppr_retrieve = lambda nb, q: (seen.setdefault("v", cv.get()), orig(nb, q))[1]
    r.run("nb1", "问题")
    assert seen["v"] == "user-42"


def test_prefetch_runs_ppr_on_background_thread():
    """钉住行为:prefetch on 时 ppr_retrieve 实际在后台线程执行(非仅结果碰巧一致)。"""
    repo, r = _mk()
    r.run("nb1", "问题")
    assert repo.retrieval.ppr_threads
    assert repo.retrieval.ppr_threads[0] != threading.main_thread().name


def test_prefetch_pool_shutdown_when_plan_raises():
    """异常安全:plan() 抛错(seed pass 执行不到)时,已 submit 的线程池必须仍被关闭,
    不能出现"submit 了却无人 join 且池未关闭"的线程泄漏。"""
    import app.services.reasoning_retrieval as mod

    created_pools = []
    real_pool_cls = mod.ThreadPoolExecutor

    class _TrackingPool(real_pool_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created_pools.append(self)

    repo, r = _mk()

    def _boom(question, history=""):
        raise RuntimeError("plan failed")
    r.plan = _boom

    mod.ThreadPoolExecutor = _TrackingPool
    try:
        with pytest.raises(RuntimeError, match="plan failed"):
            r.run("nb1", "问题")
    finally:
        mod.ThreadPoolExecutor = real_pool_cls

    # max_workers=1 的 ppr 预取池必然先于子查询并发池创建 —— 取第一个。
    assert created_pools, "预期至少创建了 PPR 预取线程池"
    ppr_pool = created_pools[0]
    assert ppr_pool._shutdown is True


def test_prefetch_pool_shutdown_when_ppr_future_raises():
    """异常安全:plan/初检索都正常走到 seed pass,但后台 ppr_retrieve 本身抛错时,
    ppr_future.result() 在 seed pass 处重抛——异常必须原样传出 run(),且线程池
    仍必须被关闭一次(治 finally 被误删/误挪导致的线程泄漏)。"""
    import app.services.reasoning_retrieval as mod

    created_pools = []
    real_pool_cls = mod.ThreadPoolExecutor

    class _TrackingPool(real_pool_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created_pools.append(self)

    repo, r = _mk()

    def _boom(nb, q):
        raise RuntimeError("ppr failed")
    repo.retrieval.ppr_retrieve = _boom

    mod.ThreadPoolExecutor = _TrackingPool
    try:
        with pytest.raises(RuntimeError, match="ppr failed"):
            r.run("nb1", "问题")
    finally:
        mod.ThreadPoolExecutor = real_pool_cls

    # max_workers=1 的 ppr 预取池必然先于子查询并发池创建 —— 取第一个。
    assert created_pools, "预期至少创建了 PPR 预取线程池"
    ppr_pool = created_pools[0]
    assert ppr_pool._shutdown is True
