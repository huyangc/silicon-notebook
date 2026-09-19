"""联邦 chunk 通道(chunk_federation)的合并/扇出策略,脱库单测。

这里只测 `chunk_federation.py` 自己的策略:短路、任务表构成、扇出上限、按参与
集聚池、矩阵拼接与掩码。producer 侧(warm-peek lane、天花板在 LIMIT 之前生效)
由 `test_chunk_federation_peek.py` 覆盖。
"""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.domain.retrieval import RetrievedChunk
from app.services import chunk_federation as cf
from app.services.cancellation import AskCancelled
from app.services.retrieval_candidates import (
    _CHUNK_PEEK_ONLY, CandidateRetrievalService,
)


# 握手超时:只是死锁守卫,绝不是任何断言的判据。
_HANDSHAKE_TIMEOUT = 30.0


def make_chunk(chunk_id: str, relevance: float, text: str = "") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=f"src-{chunk_id}", source_title="t",
        section_path="", text=text or f"text-{chunk_id}",
        score=relevance, relevance=relevance,
    )


class FakeCandidates:
    """`CandidateRetrievalService` 的最小替身:不起数据库、不碰 scale runtime。

    `_mask_vector_matrix` 借用生产实现本身(纯静态方法),因为掩码语义正是本模块
    依赖的契约,复制一份反而会掩盖它的漂移。
    """

    _mask_vector_matrix = staticmethod(CandidateRetrievalService._mask_vector_matrix)

    def __init__(self, participants, *, retrieve=None, recall=200,
                 workers=8, max_participants=8, enabled=True,
                 visible=None, copyable=None, multi_result=None):
        self._participants = tuple(participants)
        self._retrieve = retrieve or (lambda nid, query: ([], [], None))
        self._visible = visible or {}
        self._copyable = copyable or {}
        self.multi_result = multi_result
        self.settings = SimpleNamespace(
            chunk_federation_enabled=enabled,
            chunk_federation_max_participants=max_participants,
            chunk_fanout_max_workers=workers,
            chunk_recall=recall,
        )
        self.events = []
        self.event_log = SimpleNamespace(emit=self.events.append)
        self.calls = []
        self.multi_calls = []
        self.participant_reads = 0
        self.visible_reads = []
        self.peek_seen = []
        self.sources = SimpleNamespace(
            all_visible_source_ids=self._all_visible_source_ids,
        )
        self._lock = threading.Lock()

    # --- 参与集座位 -----------------------------------------------------
    def _retrieval_participants(self, active_notebook_id: str):
        self.participant_reads += 1
        return self._participants

    def notebook_copy_stats(self, notebook_id: str) -> dict:
        return {"copyable": self._copyable.get(notebook_id, True)}

    def _all_visible_source_ids(self, notebook_id: str):
        self.visible_reads.append(notebook_id)
        return self._visible.get(notebook_id, ())

    # --- producer -------------------------------------------------------
    def _retrieve_chunks(self, notebook_id, query, recall=0, *,
                         allowed_source_ids=None, producer_explicit=False,
                         drifted=None):
        with self._lock:
            self.calls.append({
                "notebook_id": notebook_id, "query": query,
                "allowed_source_ids": allowed_source_ids,
                "producer_explicit": producer_explicit,
            })
            self.peek_seen.append((notebook_id, _CHUNK_PEEK_ONLY.get()))
        return self._retrieve(notebook_id, query)

    def _retrieve_chunks_multi(self, notebook_id, sub_queries, *, drifted=None):
        self.multi_calls.append((notebook_id, list(sub_queries)))
        return self.multi_result


def federated(candidates, active="a", sub_queries=("q1",), **kwargs):
    return cf.federated_chunk_candidates(
        candidates, active, list(sub_queries), **kwargs
    )


# --------------------------------------------------------------------------
# 1. 参与集 ≤ 1 时完全短路
# --------------------------------------------------------------------------

def test_single_participant_is_byte_identical(monkeypatch):
    expected = (
        {"c1": make_chunk("c1", 0.9)},
        [{"c1": make_chunk("c1", 0.9)}],
        ["c1"],
        np.eye(1, 3),
    )
    candidates = FakeCandidates((("a", "personal"),), multi_result=expected)
    entered = []
    monkeypatch.setattr(
        cf, "peer_evidence", lambda *a, **k: entered.append(1) or [],
    )

    result = federated(candidates, sub_queries=("q1", "q2"))

    assert result.collected is expected[0]
    assert result.per_query is expected[1]
    assert result.ids is expected[2]
    assert result.matrix is expected[3]
    assert result.participants == ("a",)
    assert candidates.multi_calls == [("a", ["q1", "q2"])]
    assert candidates.calls == []
    assert entered == []


def test_single_participant_single_query_uses_the_plain_lane(monkeypatch):
    hit = make_chunk("c1", 0.9)
    candidates = FakeCandidates(
        (("a", "personal"),),
        retrieve=lambda nid, q: ([hit], ["c1"], np.eye(1, 3)),
    )
    entered = []
    monkeypatch.setattr(
        cf, "peer_evidence", lambda *a, **k: entered.append(1) or [],
    )

    result = federated(candidates, sub_queries=("q1",))

    assert result.collected == {"c1": hit}
    assert result.per_query == [{"c1": hit}]
    assert result.ids == ["c1"]
    assert candidates.multi_calls == []
    # 活动库位置参数调用,一个关键字都不传(既有测试替身签名更窄)。
    assert candidates.calls == [{
        "notebook_id": "a", "query": "q1",
        "allowed_source_ids": None, "producer_explicit": False,
    }]
    assert entered == []


def test_feature_flag_off_returns_active_only_without_reading_the_seat():
    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base")), enabled=False,
    )

    assert cf.federation_participants(candidates, "a") == (("a", "personal"),)
    assert candidates.participant_reads == 0


def test_participants_are_truncated_deterministically_with_an_event():
    seat = tuple((f"n{i}", "base") for i in range(1, 5))
    candidates = FakeCandidates(
        (("a", "personal"), *seat), max_participants=3,
    )

    assert cf.federation_participants(candidates, "a") == (
        ("a", "personal"), ("n1", "base"), ("n2", "base"),
    )
    assert candidates.events == [{
        "kind": "chunk_federation_truncated", "notebook_id": "a",
        "participants": 5, "kept": 3,
    }]


def test_seat_without_the_active_notebook_falls_back_to_one_library():
    candidates = FakeCandidates((("b", "base"),))

    assert cf.federation_participants(candidates, "a") == (("a", "personal"),)


# --------------------------------------------------------------------------
# 2. 大库不得通吃
# --------------------------------------------------------------------------

def test_big_library_cannot_monopolize():
    big = [make_chunk(f"a{i}", 0.9 - i * 0.004) for i in range(100)]
    small = [make_chunk("b1", 0.45)]

    def retrieve(nid, query):
        return (big if nid == "a" else small), [], None

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base")), retrieve=retrieve, recall=10,
    )

    result = federated(candidates)

    assert len(result.collected) == 10
    assert "b1" in result.collected


# --------------------------------------------------------------------------
# 3. 矩阵拼接
# --------------------------------------------------------------------------

def test_merge_matrices_preserves_rows_and_drops_dim_mismatch():
    first = (["a1", "a2"], np.eye(2, 3))
    second = (["b1"], np.eye(1, 3))

    ids, matrix = cf.merge_chunk_matrices([first, second])

    assert ids == ["a1", "a2", "b1"]
    assert matrix.shape == (3, 3)

    stale = (["c1"], np.eye(1, 4))
    ids, matrix = cf.merge_chunk_matrices([first, stale, second])

    assert ids == ["a1", "a2", "b1"]
    assert matrix.shape == (3, 3)


def test_merge_matrices_skips_absent_matrices_and_repeated_rows():
    ids, matrix = cf.merge_chunk_matrices([
        (["a1"], np.eye(1, 3)), (["b1", "b2"], None), (["a1"], np.eye(1, 3)),
    ])

    assert ids == ["a1"]
    assert matrix.shape == (1, 3)

    assert cf.merge_chunk_matrices([([], None), (["x"], None)]) == ([], None)


def test_merged_matrix_is_masked_down_to_the_selected_chunks():
    def retrieve(nid, query):
        if nid == "a":
            return [make_chunk("a1", 0.9)], ["a1", "a2"], np.eye(2, 3)
        return [make_chunk("b1", 0.8)], ["b1"], np.eye(1, 3)

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base")), retrieve=retrieve,
    )

    result = federated(candidates)

    # a2 从未被任何子查询选中,它的行不得作为 MMR 的隐藏多样性对照留下来。
    assert result.ids == ["a1", "b1"]
    assert result.matrix.shape == (2, 3)


# --------------------------------------------------------------------------
# 4. 任务表确定性:库为主序、子查询为次序;聚池按参与集序不按完成序
# --------------------------------------------------------------------------

def test_task_order_is_participant_major_and_deterministic():
    def retrieve(nid, query):
        cid = f"{nid}-{query}"
        return [make_chunk(cid, 0.5)], [], None

    def run_once():
        candidates = FakeCandidates(
            (("a", "personal"), ("b", "base")), retrieve=retrieve,
        )
        return federated(candidates, sub_queries=("q1", "q2"))

    first, second = run_once(), run_once()

    assert [sorted(group) for group in first.per_query] == [
        ["a-q1"], ["a-q2"], ["b-q1"], ["b-q2"],
    ]
    assert list(first.collected) == list(second.collected)
    assert [sorted(g) for g in first.per_query] == [
        sorted(g) for g in second.per_query
    ]


def test_pools_follow_participant_order_not_completion_order():
    finished_peer = threading.Event()

    def retrieve(nid, query):
        if nid == "b":
            hits = [make_chunk("b1", 0.99)]
            finished_peer.set()
            return hits, [], None
        assert finished_peer.wait(_HANDSHAKE_TIMEOUT), "peer task never ran"
        return [make_chunk("a1", 0.10)], [], None

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base")), retrieve=retrieve, workers=4,
    )

    result = federated(candidates)

    # 完成序是 b→a,但保底名额按参与集序发放:active 的那条先进 collected。
    assert list(result.collected) == ["a1", "b1"]


# --------------------------------------------------------------------------
# 5. 单库失败被隔离
# --------------------------------------------------------------------------

def test_worker_failure_is_isolated():
    barrier = threading.Barrier(3, timeout=_HANDSHAKE_TIMEOUT)

    def retrieve(nid, query):
        barrier.wait()
        if nid == "c":
            raise RuntimeError("peer exploded")
        return [make_chunk(f"{nid}1", 0.5)], [], None

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base"), ("c", "base")), retrieve=retrieve,
    )

    result = federated(candidates)

    assert sorted(result.collected) == ["a1", "b1"]
    assert candidates.events == [{
        "kind": "chunk_federation_skipped", "notebook_id": "c",
        "error_type": "RuntimeError",
    }]


def test_skipped_event_never_carries_the_exception_message():
    def retrieve(nid, query):
        if nid == "b":
            raise RuntimeError("secret source title leaked here")
        return [], [], None

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base")), retrieve=retrieve,
    )

    federated(candidates)

    assert "secret" not in repr(candidates.events)


# --------------------------------------------------------------------------
# 6. 取消不得被吞
# --------------------------------------------------------------------------

def test_cancellation_is_not_swallowed():
    def retrieve(nid, query):
        if nid == "b":
            raise AskCancelled()
        return [make_chunk("a1", 0.5)], [], None

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base")), retrieve=retrieve,
    )

    with pytest.raises(AskCancelled):
        federated(candidates)
    assert candidates.events == []


# --------------------------------------------------------------------------
# 风险清单:连接池打穿 / MMR 退化
# --------------------------------------------------------------------------

def test_total_workers_never_exceed_setting():
    workers = 4
    participants = (
        ("a", "personal"), ("b", "base"), ("c", "base"), ("d", "base"),
    )
    sub_queries = ("q1", "q2", "q3", "q4")
    # 16 个任务对 4 个工作位。让恰好 `workers` 个任务互相握手后才放行:并发若
    # 低于 workers 会死锁(超时暴露),高于 workers 会被 peak 断言抓住。
    barrier = threading.Barrier(workers, timeout=_HANDSHAKE_TIMEOUT)
    lock = threading.Lock()
    state = {"live": 0, "peak": 0}

    def retrieve(nid, query):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        barrier.wait()
        with lock:
            state["live"] -= 1
        return [make_chunk(f"{nid}-{query}", 0.5)], [], None

    candidates = FakeCandidates(
        participants, retrieve=retrieve, workers=workers,
    )

    result = federated(candidates, sub_queries=sub_queries)

    assert state["peak"] == workers
    assert len(result.per_query) == len(participants) * len(sub_queries)


def test_mmr_sees_cross_library_similarity():
    near = np.array([[0.99995, 0.01, 0.0]])
    near = near / np.linalg.norm(near)
    local_ids, local_matrix = ["a1", "c1"], np.eye(2, 3)

    def retrieve(nid, query):
        if nid == "a":
            return (
                [make_chunk("a1", 0.9), make_chunk("c1", 0.7)],
                local_ids, local_matrix,
            )
        return [make_chunk("b1", 0.8)], ["b1"], near

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base")), retrieve=retrieve,
    )

    result = federated(candidates)
    scored = sorted(result.collected.values(), key=lambda c: -c.relevance)
    row = {cid: index for index, cid in enumerate(result.ids)}

    # 拼接后两库那两条近乎相同的向量同在一份矩阵里,余弦≈1。
    assert set(result.ids) == {"a1", "b1", "c1"}
    assert float(result.matrix[row["a1"]] @ result.matrix[row["b1"]]) > 0.99

    chosen = CandidateRetrievalService._mmr_select_chunks(
        None, scored, result.ids, result.matrix, 2, 0.5,
    )
    assert [c.chunk_id for c in chosen] == ["a1", "c1"]

    # 对照臂:只带 active 一本的矩阵时,外库那条被判"完全不相似",MMR 系统性
    # 超选它——正是拼接矩阵要消除的质量缺陷。
    unmerged = CandidateRetrievalService._mmr_select_chunks(
        None, scored, local_ids, local_matrix, 2, 0.5,
    )
    assert [c.chunk_id for c in unmerged] == ["a1", "b1"]


# --------------------------------------------------------------------------
# 调用形状:active 不传关键字,外库带 visible 天花板 + producer_explicit=False
# --------------------------------------------------------------------------

def test_peer_library_gets_visible_ceiling_and_never_attests_explicit():
    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base")),
        visible={"b": ("s1", "s2")},
    )

    federated(candidates)

    calls = {call["notebook_id"]: call for call in candidates.calls}
    assert calls["a"]["allowed_source_ids"] is None
    assert calls["a"]["producer_explicit"] is False
    assert calls["b"]["allowed_source_ids"] == ("s1", "s2")
    assert calls["b"]["producer_explicit"] is False
    assert candidates.visible_reads == ["b"]


def test_peek_only_is_set_for_large_peer_libraries_only():
    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base"), ("c", "base")),
        copyable={"a": False, "b": False, "c": True},
    )

    federated(candidates)

    assert dict(candidates.peek_seen) == {"a": False, "b": True, "c": False}


def test_unreadable_copy_stats_never_cold_loads():
    class Exploding(FakeCandidates):
        def notebook_copy_stats(self, notebook_id):
            raise RuntimeError("stats unavailable")

    candidates = Exploding((("a", "personal"), ("b", "base")))

    federated(candidates)

    assert dict(candidates.peek_seen) == {"a": False, "b": True}
