"""联邦 chunk 通道(chunk_federation)的合并/扇出策略,脱库单测。

这里只测 `chunk_federation.py` 自己的策略:短路、任务表构成、扇出上限、按参与
集聚池、矩阵拼接与掩码。producer 侧(warm-peek lane、天花板在 LIMIT 之前生效)
由 `test_chunk_federation_peek.py` 覆盖。
"""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.event_logging import get_log_owner, reset_log_owner, set_log_owner
from app.domain.retrieval import RetrievedChunk
from app.services import chunk_federation as cf
from app.services.cancellation import AskCancelled
from app.services.retrieval import RetrievalSupport
from app.services.retrieval_candidates import (
    _CHUNK_ARM_DRIFTED, _CHUNK_PEEK_ONLY, CandidateRetrievalService,
)


# 握手超时:只是死锁守卫,绝不是任何断言的判据。
_HANDSHAKE_TIMEOUT = 30.0


def make_chunk(chunk_id: str, relevance: float, text: str = "",
               source_id: str = "", supports=()) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=source_id or f"src-{chunk_id}",
        source_title="t",
        section_path="", text=text or f"text-{chunk_id}",
        score=relevance, relevance=relevance,
        retrieval_supports=tuple(supports),
    )


class FakeCandidates:
    """`CandidateRetrievalService` 的最小替身:不起数据库、不碰 scale runtime。"""

    def __init__(self, participants, *, retrieve=None, recall=200,
                 workers=8, max_participants=8, enabled=True,
                 visible=None, copyable=None, multi_result=None,
                 peer_floor=0.0, restricted=False):
        self._participants = tuple(participants)
        self._retrieve = retrieve or (lambda nid, query: ([], [], None))
        self._visible = visible or {}
        self._copyable = copyable or {}
        self._restricted = restricted
        self.multi_result = multi_result
        self.settings = SimpleNamespace(
            chunk_federation_enabled=enabled,
            chunk_federation_max_participants=max_participants,
            chunk_fanout_max_workers=workers,
            chunk_recall=recall,
            chunk_federation_peer_floor=peer_floor,
            embed_runtime_dim=0,
        )
        self.events = []
        self.event_log = SimpleNamespace(emit=self._emit)
        self.calls = []
        self.multi_calls = []
        self.participant_reads = 0
        self.visible_reads = []
        self.peek_seen = []
        self.drift_seen = []
        self.drift_probes = []
        self.event_owners = []
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
        with self._lock:
            self.visible_reads.append(notebook_id)
        return self._visible.get(notebook_id, ())

    # 真实的 ``_lexical_gate_drift_probe`` 走这个属性,所以"现探了几次"可数。
    def _unsafe_source_scope_restricted(self, notebook_id: str) -> bool:
        with self._lock:
            self.drift_probes.append(notebook_id)
        return self._restricted

    def _emit(self, event, **_kwargs):
        with self._lock:
            self.events.append(event)
            self.event_owners.append(get_log_owner())

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
            self.drift_seen.append((notebook_id, _CHUNK_ARM_DRIFTED.get()))
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


def test_no_sub_query_answers_before_reading_the_seat():
    """空子查询 = 什么都没搜,不该为此读座位、更不该为一次空操作发截断事件。"""
    seat = tuple((f"n{i}", "base") for i in range(1, 6))
    candidates = FakeCandidates((("a", "personal"), *seat), max_participants=3)

    result = federated(candidates, sub_queries=())

    assert (result.collected, result.per_query) == ({}, [])
    assert (result.ids, result.matrix) == ([], None)
    assert result.participants == (), "没搜过任何库,participants 必须为空"
    assert candidates.participant_reads == 0
    assert candidates.events == []
    assert candidates.calls == [] and candidates.multi_calls == []


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


def test_keep_ids_narrows_each_part_before_the_concatenation(monkeypatch):
    """收窄必须发生在 ``vstack`` **之前**。

    每个 part 是那一库的**整库**矩阵(unscoped 就是进程缓存那份的引用),先拼
    后掩码会让所有参与库的整库矩阵在内存里汇合一次。这里对 ``np.vstack`` 收到
    的块计行:只要出现过一行没被选中的 chunk,就是先拼后掩。"""
    stacked = []
    original = np.vstack

    def _spy(blocks, *args, **kwargs):
        stacked.extend(int(block.shape[0]) for block in blocks)
        return original(blocks, *args, **kwargs)

    monkeypatch.setattr(np, "vstack", _spy)

    def retrieve(nid, query):
        # 每库返回 40 行整库矩阵,而只有一条候选会被选中。
        ids = [f"{nid}{i}" for i in range(40)]
        return [make_chunk(f"{nid}0", 0.9)], ids, np.eye(40, 3)

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base"), ("c", "base")), retrieve=retrieve,
    )

    result = federated(candidates)

    assert sorted(result.ids) == ["a0", "b0", "c0"]
    assert sum(stacked) == len(result.ids), (
        f"拼接前没有按选中集合收窄:vstack 收到 {sum(stacked)} 行"
    )


def test_dimension_anchor_is_the_majority_not_the_active_library():
    """锚在「第一个被接受的 part」时,一个旧维的 active 会把所有当前维的外库
    矩阵全丢掉,而且零事件。锚改成多数维后,被丢的是它自己,并且说出来。"""
    def retrieve(nid, query):
        if nid == "a":
            return [make_chunk("a1", 0.9)], ["a1"], np.eye(1, 4)
        return [make_chunk(f"{nid}1", 0.8)], [f"{nid}1"], np.eye(1, 3)

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base"), ("c", "base")), retrieve=retrieve,
    )

    result = federated(candidates)

    assert result.ids == ["b1", "c1"]
    assert result.matrix.shape == (2, 3)
    assert candidates.events == [{
        "kind": "chunk_federation_dim_mismatch", "notebook_id": "a",
        "dim": 4, "expected_dim": 3,
    }]


def test_runtime_dimension_wins_over_the_majority():
    """运行时声明了截断维时,它就是锚——哪怕多数库还留着旧维产物。"""
    def retrieve(nid, query):
        if nid == "a":
            return [make_chunk("a1", 0.9)], ["a1"], np.eye(1, 3)
        return [make_chunk(f"{nid}1", 0.8)], [f"{nid}1"], np.eye(1, 4)

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base"), ("c", "base")), retrieve=retrieve,
    )
    candidates.settings.embed_runtime_dim = 3

    result = federated(candidates)

    assert result.ids == ["a1"]
    assert {event["notebook_id"] for event in candidates.events} == {"b", "c"}
    assert all(event["expected_dim"] == 3 for event in candidates.events)


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
    assert len(candidates.events) == 1
    event = dict(candidates.events[0])
    assert isinstance(event.pop("latency_ms"), int)
    assert event == {
        "kind": "chunk_federation_skipped", "notebook_id": "c",
        "error_type": "RuntimeError",
    }


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

def test_executor_is_constructed_with_the_bounded_width(monkeypatch):
    """上界的**确定性**断言:握手只能证下界,池里多出来的线程未必会被调度到。

    直接看执行器的构造参数,把「改回字面量 8」这种变异一次钉死。"""
    seen = []
    original = cf.ThreadPoolExecutor

    def _record(*args, **kwargs):
        seen.append(kwargs.get("max_workers"))
        return original(*args, **kwargs)

    monkeypatch.setattr(cf, "ThreadPoolExecutor", _record)
    participants = (
        ("a", "personal"), ("b", "base"), ("c", "base"), ("d", "base"),
    )
    candidates = FakeCandidates(participants, workers=3)

    federated(candidates, sub_queries=("q1", "q2", "q3", "q4"))

    assert seen == [3], "总工作位必须是 min(任务数, 设定上限)"

    # 任务数少于上限时,取的是任务数。
    seen.clear()
    fewer = FakeCandidates(participants, workers=32)
    federated(fewer, sub_queries=("q1", "q2"))
    assert seen == [len(participants) * 2]


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


# --------------------------------------------------------------------------
# 两个 ContextVar 的 set/reset 守卫:漂移探针一次、peek lane 不外泄
# --------------------------------------------------------------------------

def _big_peer_last():
    """参与集最后一个是大外库——泄漏时 ``_CHUNK_PEEK_ONLY`` 恰好留下 True。"""
    return (
        (("a", "personal"), ("b", "base"), ("z", "base")),
        {"a": True, "b": True, "z": False},
    )


@pytest.mark.parametrize("passed, restricted, expected, probes", [
    (True, False, True, 0),     # 调用方显式传入 → 一次都不现探
    (None, True, True, 1),      # 未传 → 整条臂现探一次,答案下传给每个任务
    (None, False, False, 1),
])
def test_drift_verdict_reaches_every_task_and_is_probed_once(
    passed, restricted, expected, probes,
):
    participants, copyable = _big_peer_last()
    candidates = FakeCandidates(
        participants, copyable=copyable, restricted=restricted,
    )

    federated(candidates, sub_queries=("q1", "q2"), drifted=passed)

    assert len(candidates.drift_seen) == len(participants) * 2
    assert {verdict for _nid, verdict in candidates.drift_seen} == {expected}
    assert len(candidates.drift_probes) == probes, (
        "探针次数必须是每臂一次,不是「库 × 子查询」次"
    )


def test_context_variables_never_leak_back_to_the_caller():
    participants, copyable = _big_peer_last()
    candidates = FakeCandidates(participants, copyable=copyable)

    federated(candidates, sub_queries=("q1", "q2"), drifted=True)

    # 泄漏的 peek lane 会让本次 ask 之后的向量通道静默消失。
    assert dict(candidates.peek_seen)["z"] is True, "前提:最后一个确实是大外库"
    assert _CHUNK_PEEK_ONLY.get() is False
    assert _CHUNK_ARM_DRIFTED.get() is None


def test_context_variables_are_restored_on_cancellation():
    participants, copyable = _big_peer_last()

    def retrieve(nid, query):
        if nid == "z":
            raise AskCancelled()
        return [], [], None

    candidates = FakeCandidates(
        participants, copyable=copyable, retrieve=retrieve,
    )

    with pytest.raises(AskCancelled):
        federated(candidates, sub_queries=("q1", "q2"), drifted=True)

    assert _CHUNK_PEEK_ONLY.get() is False
    assert _CHUNK_ARM_DRIFTED.get() is None


# --------------------------------------------------------------------------
# 跳过事件写进调用方的日志归属,不是工作线程的空 context
# --------------------------------------------------------------------------

def test_skipped_event_is_emitted_in_the_callers_log_owner():
    """events logger 是 per-user 的,目录取自 ``_log_owner`` ContextVar;工作
    线程起始 context 为空,在 ``ctx.run`` **之外**发就恒写进 ``user-local``。"""
    def retrieve(nid, query):
        if nid == "b":
            raise RuntimeError("peer exploded")
        return [], [], None

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base")), retrieve=retrieve,
    )
    token = set_log_owner("user-deadbeef")
    try:
        federated(candidates)
    finally:
        reset_log_owner(token)

    assert candidates.event_owners == ["user-deadbeef"]
    assert candidates.events[0]["kind"] == "chunk_federation_skipped"
    assert isinstance(candidates.events[0]["latency_ms"], int)


# --------------------------------------------------------------------------
# 跨库合并:可比模式下弱库不再挤掉强库
# --------------------------------------------------------------------------

def _strong_active_and_weak_peers(peer_floor):
    strong = [make_chunk(f"a{i}", 0.9 - i * 0.001) for i in range(40)]
    weak = {f"n{i}": [make_chunk(f"n{i}-1", 0.05)] for i in range(1, 8)}

    def retrieve(nid, query):
        return (strong if nid == "a" else weak[nid]), [], None

    return FakeCandidates(
        (("a", "personal"), *((nid, "base") for nid in weak)),
        retrieve=retrieve, recall=8, peer_floor=peer_floor,
    )


def test_irrelevant_libraries_do_not_reserve_slots_in_comparable_mode():
    result = _federate_merge(_strong_active_and_weak_peers(0.5))

    assert len(result) == 8
    assert all(hit.notebook_id == "a" for hit in result), (
        "峰值 0.05 的无关库不得从 0.9 的强库手里各拿走一个保底名额"
    )


def test_peer_floor_zero_keeps_the_guaranteed_slot_per_library():
    """对照臂:设 0 就是今天的行为,每个库一条保底。"""
    result = _federate_merge(_strong_active_and_weak_peers(0.0))

    assert sum(hit.notebook_id != "a" for hit in result) == 7


def test_a_genuinely_relevant_base_still_reserves_its_slot():
    strong = [make_chunk(f"a{i}", 0.9 - i * 0.001) for i in range(40)]
    relevant = [make_chunk("b1", 0.6)]
    noise = {f"n{i}": [make_chunk(f"n{i}-1", 0.05)] for i in range(1, 6)}

    def retrieve(nid, query):
        if nid == "a":
            return strong, [], None
        return (relevant if nid == "b" else noise[nid]), [], None

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base"),
         *((nid, "base") for nid in noise)),
        retrieve=retrieve, recall=4, peer_floor=0.5,
    )

    result = _federate_merge(candidates)

    assert "b1" in {hit.chunk_id for hit in result}, (
        "peak 0.6 ≥ 0.9×0.5 的库仍要有保底名额,不能被大库挤掉"
    )
    assert all(hit.chunk_id.startswith(("a", "b")) for hit in result)


def _federate_merge(candidates):
    return list(federated(candidates).collected.values())


# --------------------------------------------------------------------------
# 去重:库内先按 content_key 折叠,才交给 peer_evidence 做跨库选择
# --------------------------------------------------------------------------

def test_double_hit_keeps_the_semantic_representative_and_unions_supports():
    """同一段正文既被语义臂又被生成问题索引命中时,代表必须是带语义 support
    的那条——只按 ``hit.text`` 去重会让 generated-question-only 那条当选,
    下游 quota fuse 于是把这段正文整段判成 supplemental。"""
    semantic = make_chunk(
        "c-semantic", 0.88, text="同一段正文", source_id="s1",
        supports=(RetrievalSupport("semantic", "chunk", "c-semantic", 0.88),),
    )
    question_only = make_chunk(
        "c-question", 0.95, text="同一段正文", source_id="s1",
        supports=(
            RetrievalSupport("generated_question", "chunk", "c-question", 0.95),
        ),
    )

    def retrieve(nid, query):
        if nid != "a":
            return [make_chunk("b1", 0.5)], [], None
        return ([semantic] if query == "q1" else [question_only]), [], None

    candidates = FakeCandidates(
        (("a", "personal"), ("b", "base")), retrieve=retrieve,
    )

    result = federated(candidates, sub_queries=("q1", "q2"))

    chosen = [
        hit for hit in result.collected.values() if hit.text == "同一段正文"
    ]
    assert len(chosen) == 1, "同库同源同文只占一个名额"
    origins = {support.origin for support in chosen[0].retrieval_supports}
    assert origins == {"semantic", "generated_question"}, (
        "support 必须取并集,而不是被替换成补充臂那一份"
    )
    assert chosen[0].chunk_id == semantic.chunk_id


# --------------------------------------------------------------------------
# 每库来源清单在父线程取一次,不随子查询放大
# --------------------------------------------------------------------------

def test_peer_source_ceiling_is_read_once_per_library_without_a_run():
    """无 ambient ``retrieval_run`` 时 memo 退化成直通,读在任务体里就是
    「库 × 子查询」次 DB 查询。"""
    peers = tuple((f"n{i}", "base") for i in range(1, 8))
    candidates = FakeCandidates(
        (("a", "personal"), *peers),
        visible={nid: (f"s-{nid}",) for nid, _ in peers},
    )

    federated(candidates, sub_queries=("q1", "q2", "q3", "q4"))

    assert sorted(candidates.visible_reads) == sorted(nid for nid, _ in peers)
    assert len(candidates.calls) == 8 * 4, "前提:任务表确实是 8 库 × 4 子查询"
