"""P1-B:quota 收尾复用初检索打分。等价性=reuse on/off 的 top_hits 逐位一致;
效率=reuse on 时收尾不再触发新的 federated_retrieve。"""
from dataclasses import replace

from app.services.reasoning_retrieval import ReasoningRetriever, SubQuery, ReflectDecision
from app.services.retrieval import RetrievedKnowledge


class _StubSettings:
    retrieval_top_n = 4
    reasoning_max_steps = 5
    reasoning_max_subqueries = 3
    reasoning_stale_limit = 3
    reasoning_max_element_searches = 2
    reasoning_quota_enabled = True
    reasoning_quota_reuse_enabled = True
    graph_ppr_enabled = False
    reasoning_ppr_prefetch = False
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

    def ppr_retrieve(self, nb, q):
        return []


class _StubRepo:
    def __init__(self):
        self.retrieval = _StubRetrieval()
        self.reasoning_llm_client = type("C", (), {"configured": False})()


def _run(reuse: bool):
    s = _StubSettings()
    s.reasoning_quota_reuse_enabled = reuse
    repo = _StubRepo()
    r = ReasoningRetriever(repo, s)
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
