"""P1-B:quota 收尾复用初检索打分。等价性=reuse on/off 的 top_hits 逐位一致;
效率=reuse on 时收尾不再触发新的 federated_retrieve。"""
from dataclasses import replace

from app.services.reasoning_retrieval import ReasoningRetriever, SubQuery, ReflectDecision
from app.services.retrieval import RetrievedKnowledge


class _StubSettings:
    retrieval_top_n = 4
    # cap=4 钉住旧总预算(自适应下 retrieval_top_n 只是 floor,复合题会被
    # per_query×n 抬高;本测试的 reuse⇔rerun 等价性要在固定预算下对比)。
    reasoning_top_n_per_query = 3
    reasoning_top_n_cap = 4
    reasoning_max_steps = 5
    reasoning_max_subqueries = 3
    reasoning_stale_limit = 3
    reasoning_max_element_searches = 2
    reasoning_quota_enabled = True
    reasoning_quota_reuse_enabled = True
    graph_ppr_enabled = False
    reasoning_ppr_prefetch = False
    # 与真实 Settings 默认一致(精确查找 seed pass 也读它们)。本文件的问题串
    # 「问题A」「问题B」不含标识符,seed 通道零 I/O,reuse⇔rerun 的逐位等价不受影响。
    exact_lookup_enabled = True
    exact_lookup_max_identifiers = 3
    reasoning_timeout_seconds = 5
    reasoning_max_retries = 0
    community_peers_topk = 4
    community_rerank_candidates = 20


def _hit(oid, rel):
    return RetrievedKnowledge(object_id=oid, object_type="claim",
                              payload={"name": oid}, relevance=rel, score=rel)


class _StubRetrieval:
    """两个子查询各自的确定性全量打分表;记录 federated_retrieve 调用次数。"""
    TABLE = {
        "问题A": [_hit("o1", 0.9), _hit("o2", 0.6), _hit("o3", 0.3)],
        "问题B": [_hit("o4", 0.8), _hit("o2", 0.7)],
    }

    def __init__(self):
        self.calls = []

    def federated_retrieve(self, nb, q, types=None, w_keyword=0.4, w_semantic=0.6):
        self.calls.append(q)
        return [replace(h) for h in self.TABLE.get(q, [])]

    def retrieve_scored(self, nb, q):
        return []

    def exact_lookup_chunks(self, nb, q):
        return []

    def ppr_retrieve(self, nb, q):
        return []


class _StubRepo:
    def __init__(self):
        self.retrieval = _StubRetrieval()

    def chat(self, workload_id):
        return type("C", (), {"configured": False})()


class _StubCommunities:
    pass


def _retriever(repo, settings):
    return ReasoningRetriever(
        retrieval=repo.retrieval,
        model_clients=repo,
        communities=_StubCommunities(),
        settings=settings,
    )


def _run(reuse: bool):
    s = _StubSettings()
    s.reasoning_quota_reuse_enabled = reuse
    repo = _StubRepo()
    r = _retriever(repo, s)
    r.plan = lambda question, history="": [SubQuery(query="问题A"), SubQuery(query="问题B")]
    r.reflect = lambda question, sm: ReflectDecision(sufficient=True, next_action="answer")
    res = r.run("nb1", "总问题")
    return res, repo.retrieval.calls


def test_reuse_matches_rerun_bit_for_bit():
    res_on, _ = _run(True)
    res_off, _ = _run(False)
    on = [(h.object_id, round(h.relevance, 9), round(h.score, 9)) for h in res_on.top_hits]
    off = [(h.object_id, round(h.relevance, 9), round(h.score, 9)) for h in res_off.top_hits]
    assert on == off


def test_reuse_skips_final_rerun():
    _, calls_on = _run(True)
    _, calls_off = _run(False)
    # off:初检索 2 次 + 收尾重跑 2 次;on:只有初检索 2 次
    assert len(calls_off) == 4
    assert len(calls_on) == 2


# --- prefer 维度回归:留存条件须同时守 prefer=="balanced" ---
# 上面 _StubRetrieval.federated_retrieve 无视 w_keyword/w_semantic,任何 prefer
# 都打同一张表——无法揭穿"留存未过滤非 balanced prefer"这个 bug。这里换一个
# w_keyword 敏感的桩:score 是 w_keyword 的显函数,keyword 偏好(0.7,0.3)与
# balanced 偏好(W_KEYWORD,W_SEMANTIC=0.4,0.6)对同一 object 打分不同,
# 才能让"留存的是哪次调用的打分"这件事外显可测。
class _PreferAwareRetrieval:
    """打分 = base + w_keyword * bonus,故同一 query+object 在不同 prefer 下
    (不同 w_keyword)relevance/score 不同——足以撑起 prefer 回归。"""
    BASE = {
        "问题A": {"o1": 0.5, "o2": 0.2},
        "问题B": {"o2": 0.3, "o3": 0.4},
    }
    BONUS = 0.3

    def __init__(self):
        self.calls = []  # (query, w_keyword) 二元组,便于断言具体用了哪组权重

    def federated_retrieve(self, nb, q, types=None, w_keyword=0.4, w_semantic=0.6):
        self.calls.append((q, w_keyword))
        row = self.BASE.get(q, {})
        return [RetrievedKnowledge(object_id=oid, object_type="claim",
                                    payload={"name": oid},
                                    relevance=base + w_keyword * self.BONUS,
                                    score=base + w_keyword * self.BONUS)
                for oid, base in row.items()]

    def retrieve_scored(self, nb, q):
        return []

    def exact_lookup_chunks(self, nb, q):
        return []

    def ppr_retrieve(self, nb, q):
        return []


class _PreferAwareRepo:
    def __init__(self):
        self.retrieval = _PreferAwareRetrieval()

    def chat(self, workload_id):
        return type("C", (), {"configured": False})()


def _run_prefer(reuse: bool):
    """两个子查询: 问题A 走 balanced(默认权重), 问题B 走 keyword(0.7,0.3)——
    非 balanced 分支才是本回归要覆盖的对象。"""
    s = _StubSettings()
    s.reasoning_quota_reuse_enabled = reuse
    repo = _PreferAwareRepo()
    r = _retriever(repo, s)
    r.plan = lambda question, history="": [
        SubQuery(query="问题A", prefer="balanced"),
        SubQuery(query="问题B", prefer="keyword"),
    ]
    r.reflect = lambda question, sm: ReflectDecision(sufficient=True, next_action="answer")
    res = r.run("nb1", "总问题")
    return res


def test_reuse_matches_rerun_for_non_balanced_prefer():
    """核心回归: 问题B 以 prefer="keyword" 完成初检索并被 quota 收尾消费。
    若留存条件只看 not types(旧/错误版本), search() 会把 keyword 权重下的打分
    存下来直接复用于收尾, 而 reuse=False 时收尾走 _quota_rerank 的兜底重跑—— 该
    重跑固定调用 self.search(nb, q)(prefer 默认 "balanced"), 权重不同 ⇒ 打分不同
    ⇒ quota_fuse 分组/排序不同 ⇒ top_hits 不同, on/off 不再逐位等价。
    修复后(留存须同时满足 prefer=="balanced"): 问题B 的 keyword 打分不会被存,
    reuse=True 时也回退重跑 self.search(nb, "问题B")(隐式 balanced), 与
    reuse=False 完全同路径 ⇒ top_hits 逐位一致。"""
    res_on = _run_prefer(True)
    res_off = _run_prefer(False)
    on = [(h.object_id, round(h.relevance, 9), round(h.score, 9)) for h in res_on.top_hits]
    off = [(h.object_id, round(h.relevance, 9), round(h.score, 9)) for h in res_off.top_hits]
    assert on == off
