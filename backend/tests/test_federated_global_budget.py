"""D1-3 -- 一次全局 run 把自己的检索预算借给联邦 chunk 通道之后的合同。

PR-A 在 ``global_ask`` 里攒出的那套有界并行(进程级共享执行器、公平窗口、逐库
预算、四种跳过原因码、逐库回执),在 D1-4 切引擎之后必须原封不动地在
``chunk_federation`` 的对等分支里成立——本文件钉的就是「搬过来了,而且搬的是同一套
语义」,不是「新写了一套看起来像的」。

三条贯穿全文的判据:

* **没有 ``FederatedRunPlan`` 时一个字节都不许变。** 今天生产上的每一次 run 都没有
  计划,所以 ``_run_tasks`` 仍然自建 ``ThreadPoolExecutor``、事件里仍然没有 ``reason``
  字段——``test_without_a_plan_the_module_still_owns_its_own_pool`` 是那条对照臂,
  其余由既有的 ``test_chunk_federation*.py`` 零改动全绿承担。
* **并发一律靠握手。** ``threading.Barrier`` / ``Event`` / 计数信号量决定顺序;
  等待一律是「等某个标志置位」并带一个纯安全网的上限,断言落在因果关系与计数上,
  从不落在「多久之内完成」。
* **预算算术用注入的时钟,断言零容差。** 假钟以真实单调钟为基准、只按测试的指令
  跳进(``_Clock``),所以 ``read_budget`` 内部那只真钟看到的 deadline 仍在真实的
  未来——这是仓库踩过的坑:假钟必须贴着真钟。于是「deadline 减起跑时刻」可以断言成
  精确等式,不留 0.2 秒那种会在 CI 上抖的容差。
"""
from __future__ import annotations

import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

from app.domain.retrieval import RetrievedChunk
from app.domain.retrieval_control import RetrievalControlError
from app.repositories.read_budget import ReadBudgetExceeded, current_read_budget
from app.services import chunk_federation as cf
from app.services.cancellation import AskCancelled
from app.services.federated_run import DetachedAskTurn, FederatedRunPlan
from app.services.global_run import global_ask_run
from app.services.retrieval_participants import (
    ParticipantOverride, participant_override,
)
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import source_scope_context


_ACTOR = "user-global-budget"
# 够宽,宽到「逐库预算」与「阶段预算」哪个在起作用是可以分开断言的。
_PHASE = 60.0
_NOTEBOOK = 5.0


# 纯安全网:握手永远等的是某个标志置位,这个上限只保证用例失败时是断言失败而不是
# 整轮测试挂死。它不是任何断言的依据。
_HANDSHAKE_LIMIT = 10.0


def wait_until_set(token, limit: float = _HANDSHAKE_LIMIT) -> bool:
    """等一个 ``is_set()`` 标志置位;返回是否等到。"""
    if hasattr(token, "wait"):
        return bool(token.wait(limit))
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if token.is_set():
            return True
        time.sleep(0.005)
    return False


class _Clock:
    """贴着真实单调钟的注入时钟:基准取自真钟,只按测试指令跳进。

    ``read_budget`` 内部读的是真钟,所以假钟一旦漂离真钟,被测代码算出来的
    deadline 就会落在真钟的过去或遥远的未来,断言随之变成自说自话。以真钟为基准、
    只做显式跳进,既让预算算术可以零容差断言,又让真钟那一侧仍然成立。
    """

    def __init__(self):
        self.base = time.monotonic()
        self.offset = 0.0

    def now(self) -> float:
        return self.base + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


@pytest.fixture
def clock(monkeypatch):
    injected = _Clock()
    monkeypatch.setattr(cf, "time", SimpleNamespace(
        monotonic=injected.now, perf_counter=time.perf_counter,
    ))
    return injected


def make_chunk(chunk_id: str, relevance: float = 0.9, *,
               elements=()) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=f"src-{chunk_id}", source_title="t",
        section_path="", text=f"text-{chunk_id}",
        element_ids=list(elements), score=relevance, relevance=relevance,
    )


class FakeCandidates:
    """``CandidateRetrievalService`` 的最小替身,外加一把读预算探针。

    ``_retrieve_chunks`` 记录的是**起跑时刻**与那一刻 ``current_read_budget()``
    里的 deadline:逐库预算到底从排队算起还是从起跑算起,只有在工作线程里、在生产者
    真正被调用的那一刻读,才是可观测的。
    """

    def __init__(self, participants, *, retrieve=None, visible=None,
                 fingerprints=None, recall=200):
        self._participants = tuple(participants)
        self._retrieve = retrieve or (
            lambda nid, query: (
                [make_chunk(f"c-{nid}-{query}", elements=[f"e-{nid}"])], [], None
            )
        )
        self._visible = visible or {}
        self._fingerprints = fingerprints or {}
        self.settings = SimpleNamespace(
            chunk_federation_enabled=True,
            chunk_federation_max_participants=8,
            chunk_fanout_max_workers=4,
            chunk_recall=recall,
            chunk_federation_peer_floor=0.5,
            chunk_federation_active_reserve=0.0,
            chunk_mmr_k=16,
            embed_runtime_dim=0,
            global_ask_min_relevance=0.0,
            global_ask_relative_relevance=0.0,
            global_ask_candidate_limit=64,
        )
        self.events: list = []
        self.event_log = SimpleNamespace(emit=self.events.append)
        self.calls: list = []
        self.budgets: dict = {}
        self.fingerprint_reads: list = []
        self.sources = SimpleNamespace(
            all_visible_source_ids=self._all_visible_source_ids,
            evidence_fingerprints=self._evidence_fingerprints,
        )
        self._lock = threading.Lock()

    # --- 参与集座位 -----------------------------------------------------
    def _retrieval_participants(self, active_notebook_id: str):
        return self._participants

    def notebook_copy_stats(self, notebook_id: str) -> dict:
        return {"copyable": True}

    def _all_visible_source_ids(self, notebook_id: str):
        value = self._visible.get(notebook_id, ())
        if isinstance(value, Exception):
            raise value
        return value

    def _evidence_fingerprints(self, element_ids):
        with self._lock:
            self.fingerprint_reads.append(list(element_ids))
            read_index = len(self.fingerprint_reads)
        if callable(self._fingerprints):
            return self._fingerprints(read_index, list(element_ids))
        if isinstance(self._fingerprints, Exception):
            raise self._fingerprints
        return {
            element_id: self._fingerprints.get(element_id, ("src", "fp"))
            for element_id in element_ids
        }

    def _unsafe_source_scope_restricted(self, notebook_id: str) -> bool:
        return False

    def _scale_index(self, notebook_id: str, **kwargs):
        return None

    def _peek_warm_chunk_index(self, notebook_id: str):
        return None

    # --- producer -------------------------------------------------------
    def _retrieve_chunks(self, notebook_id, query, recall=0, *,
                         allowed_source_ids=None, producer_explicit=False,
                         drifted=None):
        budget = current_read_budget()
        with self._lock:
            self.calls.append((notebook_id, query))
            # ``cf.time`` 而不是模块级 ``time``:注入时钟是打在被测模块上的,
            # 记账必须用同一只钟,否则「起跑时刻」与「deadline」不在一个刻度上。
            self.budgets.setdefault(notebook_id, []).append((
                cf.time.monotonic(),
                None if budget is None else budget.deadline,
            ))
        return self._retrieve(notebook_id, query)

    def _retrieve_chunks_multi(self, notebook_id, sub_queries, *, drifted=None):
        raise AssertionError("single-library short circuit was taken")

    def started_at(self, notebook_id: str) -> float:
        return self.budgets[notebook_id][0][0]

    def deadline_of(self, notebook_id: str) -> float:
        return self.budgets[notebook_id][0][1]


class Receipts:
    """两条回传接缝的记录器,连调用线程一起记。"""

    def __init__(self):
        self.libraries: list = []
        self.threads: set = set()
        self.evidence: list = []

    def on_library(self, notebook_id, outcome):
        self.threads.add(threading.get_ident())
        self.libraries.append((notebook_id, outcome))

    def on_evidence(self, fingerprints):
        self.threads.add(threading.get_ident())
        self.evidence.append(dict(fingerprints))

    def outcome(self, notebook_id):
        matched = [row for nid, row in self.libraries if nid == notebook_id]
        assert matched, f"{notebook_id} has no receipt: {self.libraries}"
        return matched[0]

    @property
    def order(self) -> list:
        return [notebook_id for notebook_id, _outcome in self.libraries]


class _global_run:
    """生产上的安装形状:``global_ask_run`` 一次装齐四件事。

    刻意不手工装计划——D1-2 的管理器是唯一写入方,测试绕过它就证明不了「检索层读到
    的计划来自那个管理器」。
    """

    def __init__(self, notebook_ids, plan, *, actor=_ACTOR, fanout_limit=None):
        self.notebook_ids = tuple(notebook_ids)
        self.plan = plan
        self.actor = actor
        self.fanout_limit = fanout_limit
        self._stack: list = []

    def __enter__(self):
        override = ParticipantOverride(
            notebook_ids=self.notebook_ids, tiers={},
            attested_actor_id=self.actor,
        )
        managers = [
            retrieval_run(
                run_kind="ask_chunk", actor_id=self.actor,
                fanout_limit=self.fanout_limit,
            ),
            global_ask_run(
                override,
                {nid: {f"src-{nid}"} for nid in self.notebook_ids},
                DetachedAskTurn(conversation_id="conv-1"),
                self.plan,
                nominal_active=self.notebook_ids[0],
            ),
        ]
        for manager in managers:
            manager.__enter__()
            self._stack.append(manager)
        return self

    def __exit__(self, *exc):
        for manager in reversed(self._stack):
            manager.__exit__(*exc)
        self._stack.clear()
        return False


def _participants(notebook_ids):
    return tuple((notebook_id, "personal") for notebook_id in notebook_ids)


@pytest.fixture
def pool():
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="d13-test")
    try:
        yield executor
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _plan(pool, receipts, *, window=4, phase=_PHASE, notebook=_NOTEBOOK,
          cancel=None) -> FederatedRunPlan:
    return FederatedRunPlan(
        phase_timeout_seconds=phase,
        notebook_timeout_seconds=notebook,
        executor=pool,
        window=(window if callable(window) else (lambda: window)),
        cancel=cancel if cancel is not None else threading.Event(),
        on_library=receipts.on_library,
        on_evidence=receipts.on_evidence,
    )


# --------------------------------------------------------------------------
# 逐库预算:从起跑算起,不从排队算起
# --------------------------------------------------------------------------

def test_per_library_budget_starts_when_task_runs(clock):
    """排队等待不计入该库预算。

    窗口开到 4 让两条腿**同时提交**,而共享池只有一个座位,所以第二条腿真的在
    执行器队列里等着——这正是生产里「窗口比池宽、或别的作业占着座位」的形状,也是
    唯一能把「提交时刻」与「起跑时刻」分开的形状。等待本身用注入时钟跳过,所以这里
    既不睡觉也没有容差:三条断言都是精确等式。

    第一条腿先等「两条都已提交」这个握手再让时钟跳进:``submit`` 会让出 GIL,一条
    瞬时完成的腿能在主线程提交第二条之前就跑完,那样两次提交仍在同一刻,用例就悄悄
    测不到排队了。
    """
    ids = ("nb-slow", "nb-queued")
    submitted = threading.Event()

    class CountingPool(ThreadPoolExecutor):
        def __init__(self):
            super().__init__(max_workers=1)
            self.submits = 0

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            self.submits += 1
            if self.submits == len(ids):
                submitted.set()
            return future

    def retrieve(notebook_id, query):
        if notebook_id == "nb-slow":
            assert wait_until_set(submitted)
            clock.advance(10.0)
        return [make_chunk(f"c-{notebook_id}")], [], None

    candidates = FakeCandidates(_participants(ids), retrieve=retrieve)
    receipts = Receipts()
    executor = CountingPool()
    try:
        with _global_run(ids, _plan(executor, receipts, window=4)):
            cf.federated_chunk_candidates(candidates, ids[0], ["q"])
    finally:
        executor.shutdown(wait=True)

    assert executor.submits == 2

    for notebook_id in ids:
        assert (
            candidates.deadline_of(notebook_id)
            - candidates.started_at(notebook_id)
        ) == _NOTEBOOK
    # 排队的那 10 秒整整齐齐地推后了第二条腿的预算起点;若预算在提交时刻起算,
    # 两个 deadline 会完全相等。
    assert (
        candidates.deadline_of("nb-queued") - candidates.deadline_of("nb-slow")
    ) == 10.0


def test_phase_deadline_is_per_call(clock, pool):
    """两次联邦调用各有各的时限,不共用一个 run 起点起算的绝对时刻。

    推理模式一次 run 会多轮调用本模块,中间夹着模型调用——用注入时钟把那段模型
    时间跳过去。绝对时刻会让第二轮开始就整集 ``queue_deadline``。逐库预算调到远大于
    阶段预算,于是 ``min`` 取到的是阶段这一侧,阶段时限因此可观测。
    """
    ids = ("nb-a",)
    candidates = FakeCandidates(_participants(ids))
    receipts = Receipts()
    phase = 3.0

    with _global_run(ids, _plan(pool, receipts, phase=phase, notebook=1000.0)):
        first_at = clock.now()
        cf.federated_chunk_candidates(candidates, ids[0], ["q1"])
        clock.advance(10.0)
        second_at = clock.now()
        cf.federated_chunk_candidates(candidates, ids[0], ["q2"])

    first, second = (row[1] for row in candidates.budgets["nb-a"])
    assert first == first_at + phase
    assert second == second_at + phase
    assert second - first == 10.0


# --------------------------------------------------------------------------
# 四种原因码
# --------------------------------------------------------------------------

def test_queued_library_reports_queue_deadline(pool):
    """排到阶段时限仍未起跑的库不再提交,回执是 ``queue_deadline``。

    它对数据库一个字都没问,所以文案不许说「请缩小范围」——原因码与
    ``timeout`` 分开的全部意义就在这里。断言落在「生产者从未被调用」,不是计数。

    时限怎么在「第一条腿已完成、余下的腿还没提交」这个空档里走完:公平窗口是
    计划自己的回调、跑在父线程上,让它在第二次被问到时慢一拍,就精确地制造出那个
    空档,而不必去赌某条腿的耗时与时限谁先到。已经在途的腿是另一件事(它会被记成
    ``timeout``),由 ``test_expired_phase_does_not_wait_for_a_running_leg`` 钉。
    """
    ids = ("nb-first", "nb-late-1", "nb-late-2")
    candidates = FakeCandidates(_participants(ids))
    receipts = Receipts()
    asked: list = []

    def window() -> int:
        asked.append(len(asked))
        if len(asked) == 2:
            time.sleep(0.7)
        return 1

    with _global_run(ids, _plan(pool, receipts, window=window, phase=0.5)):
        result = cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert [notebook_id for notebook_id, _query in candidates.calls] == ["nb-first"]
    assert receipts.outcome("nb-first").status == "ok"
    for notebook_id in ("nb-late-1", "nb-late-2"):
        outcome = receipts.outcome(notebook_id)
        assert (outcome.status, outcome.reason) == ("skipped", "queue_deadline")
    assert [
        event["reason"] for event in candidates.events
        if event["kind"] == "chunk_federation_skipped"
    ] == ["queue_deadline", "queue_deadline"]
    assert list(result.collected) == ["c-nb-first-q"]


def test_slow_library_is_skipped_others_survive(pool):
    """一个库的预算耗尽不影响其余库,原因码走 ``classify_read_failure``。"""
    ids = ("nb-ok", "nb-expired")
    candidates = FakeCandidates(
        _participants(ids),
        retrieve=lambda nid, q: (
            (_ for _ in ()).throw(ReadBudgetExceeded("read budget exhausted"))
            if nid == "nb-expired"
            else ([make_chunk(f"c-{nid}")], [], None)
        ),
    )
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts)):
        result = cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert list(result.collected) == ["c-nb-ok"]
    assert receipts.outcome("nb-ok").status == "ok"
    expired = receipts.outcome("nb-expired")
    assert (expired.status, expired.reason) == ("skipped", "timeout")
    skipped = [
        event for event in candidates.events
        if event["kind"] == "chunk_federation_skipped"
    ]
    assert [(event["notebook_id"], event["reason"], event["error_type"])
            for event in skipped] == [
        ("nb-expired", "timeout", "ReadBudgetExceeded"),
    ]


def test_pool_exhaustion_reports_saturated(pool):
    """连接池租不到连接是 ``saturated``,不是 ``timeout``。

    这条判据只有驱动自己知道(查询压根没跑,池满了),所以它必须来自
    ``classify_read_failure`` 而不是本模块对时钟的猜测——用真实的
    ``psycopg_pool.PoolTimeout`` 而不是替身异常,正是为了证明走的是那条判据。
    """
    psycopg_pool = pytest.importorskip("psycopg_pool")
    ids = ("nb-a", "nb-full")
    candidates = FakeCandidates(
        _participants(ids),
        retrieve=lambda nid, q: (
            (_ for _ in ()).throw(psycopg_pool.PoolTimeout("pool is full"))
            if nid == "nb-full"
            else ([make_chunk(f"c-{nid}")], [], None)
        ),
    )
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts)):
        cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    full = receipts.outcome("nb-full")
    assert (full.status, full.reason) == ("skipped", "saturated")
    # 时钟远未到点:这条原因码不可能是本模块猜出来的。
    assert any(
        event.get("reason") == "saturated" for event in candidates.events
    )


def test_unclassifiable_failure_before_the_deadline_is_unavailable(pool):
    """驱动说不出所以然、且时限未到 → ``unavailable``,不是 ``timeout``。"""
    ids = ("nb-a", "nb-broken")
    candidates = FakeCandidates(
        _participants(ids),
        retrieve=lambda nid, q: (
            (_ for _ in ()).throw(RuntimeError("index is broken"))
            if nid == "nb-broken"
            else ([make_chunk(f"c-{nid}")], [], None)
        ),
    )
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts)):
        cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    broken = receipts.outcome("nb-broken")
    assert (broken.status, broken.reason) == ("skipped", "unavailable")


def test_worst_reason_prefers_the_code_that_guesses_nothing():
    """同一个库的多条腿败因不同时,取「文案不猜原因」的那一个。

    ``timeout`` 的中文是「请缩小范围」,而真实原因若是连接池满,那句话就是在替
    别人的负载给建议——这正是 ``read_budget`` 把两者分开的理由。
    """
    assert cf._worst_reason(["timeout", "saturated"]) == "saturated"
    assert cf._worst_reason(["unavailable", "timeout"]) == "timeout"
    assert cf._worst_reason(["unavailable", "queue_deadline"]) == "queue_deadline"
    assert cf._worst_reason(["unavailable"]) == "unavailable"


# --------------------------------------------------------------------------
# 借来的执行器:上界、不关停、公平
# --------------------------------------------------------------------------

def test_total_workers_never_exceed_shared_executor(monkeypatch):
    """并发上界是共享执行器,不是 ``chunk_fanout_max_workers``。

    窗口开到 8、任务 8 条,但池只有 2 个座位。计数用信号量式的峰值记录 + 一个
    ``Barrier(2)`` 握手:两条腿必须真的同时在跑,否则 barrier 永远凑不齐——所以
    「峰值==2」既不是墙钟巧合,也不是「其实只跑了一条」。
    """
    monkeypatch.setattr(cf, "ThreadPoolExecutor", _forbidden_pool)
    ids = tuple(f"nb-{index}" for index in range(4))
    live = [0]
    peak = [0]
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=10)

    def retrieve(notebook_id, query):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        barrier.wait()
        with lock:
            live[0] -= 1
        return [make_chunk(f"c-{notebook_id}-{query}")], [], None

    candidates = FakeCandidates(_participants(ids), retrieve=retrieve)
    receipts = Receipts()
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        with _global_run(ids, _plan(executor, receipts, window=8)):
            cf.federated_chunk_candidates(candidates, ids[0], ["q1", "q2"])
    finally:
        executor.shutdown(wait=True)

    assert len(candidates.calls) == 8
    assert peak[0] == 2


def _forbidden_pool(*args, **kwargs):
    raise AssertionError(
        "the federation built its own pool while a run plan was installed"
    )


def test_shared_executor_is_never_shut_down(pool):
    """借来的池不许被 ``with`` 进去:关掉它就是关掉下一次作业的池。"""
    shutdowns: list = []

    class WatchedPool(ThreadPoolExecutor):
        def shutdown(self, *args, **kwargs):
            shutdowns.append((args, kwargs))
            raise AssertionError("the shared executor was shut down")

    ids = ("nb-a", "nb-b")
    candidates = FakeCandidates(_participants(ids))
    receipts = Receipts()
    executor = WatchedPool(max_workers=2)
    try:
        with _global_run(ids, _plan(executor, receipts)):
            cf.federated_chunk_candidates(candidates, ids[0], ["q"])
        assert shutdowns == []
        # 还活着:下一次作业照样能用。
        assert executor.submit(lambda: "alive").result(timeout=5) == "alive"
    finally:
        ThreadPoolExecutor.shutdown(executor, wait=True)


def test_window_is_shared_round_robin_across_libraries(pool):
    """窗口按库轮转发放,不按任务表 FIFO。

    任务表是库主序,照它提交会让一个库的两条子查询占满窗口=2 的全部座位,其余
    七个库干等。窗口=1 时执行顺序是确定的,所以这条断言不需要任何计时。
    """
    ids = ("nb-a", "nb-b")
    candidates = FakeCandidates(_participants(ids))
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts, window=1)):
        cf.federated_chunk_candidates(candidates, ids[0], ["q1", "q2"])

    assert candidates.calls == [
        ("nb-a", "q1"), ("nb-b", "q1"), ("nb-a", "q2"), ("nb-b", "q2"),
    ]


def test_expired_phase_abandons_and_releases_a_running_leg(pool):
    """阶段到点时,在途的腿按 ``timeout`` 记账、被放弃,并且**收到取消信号**。

    调用方再等下去花的是合成的预算,所以它不等;但代价不能是那条腿继续攥着共享
    执行器的座位和一条数据库连接跑完自己的逐库预算——并发的其它作业正按这个池算
    自己的公平窗口。所以离开时必须置位阶段信号。腿自己等的就是那个信号(握手),
    不是一段时长。
    """
    ids = ("nb-fast", "nb-stuck")
    entered = threading.Event()
    finished = threading.Event()
    observed: dict = {}

    def retrieve(notebook_id, query):
        if notebook_id == "nb-stuck":
            entered.set()
            observed["released"] = wait_until_set(
                current_read_budget().cancel_event
            )
            finished.set()
        return [make_chunk(f"c-{notebook_id}")], [], None

    candidates = FakeCandidates(_participants(ids), retrieve=retrieve)
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts, window=2, phase=0.4)):
        result = cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert entered.is_set(), "被放弃的那条腿从未起跑,这条用例没测到东西"
    assert wait_until_set(finished)
    assert observed["released"] is True, "在途腿没收到阶段结束信号"
    assert list(result.collected) == ["c-nb-fast"]
    stuck = receipts.outcome("nb-stuck")
    assert (stuck.status, stuck.reason) == ("skipped", "timeout")
    assert [
        event.get("lane") for event in candidates.events
        if event["kind"] == "chunk_federation_skipped"
    ] == ["abandoned"]


def test_a_hard_failure_releases_the_legs_still_running(pool):
    """一条腿硬失败 → 父线程立刻返回,其余在途腿必须当场收到取消信号。

    这是比到点更尖锐的那一种:父线程是**抛着**离开的,没有任何一轮记账。少了阶段
    信号,其余腿会按自己的逐库预算继续跑满,占着共享池的座位与连接,而这次问答
    早已失败。腿等的是 token 置位,不是时长。
    """
    ids = ("nb-watch", "nb-boom")
    watching = threading.Event()
    finished = threading.Event()
    observed: dict = {}

    def retrieve(notebook_id, query):
        if notebook_id == "nb-watch":
            watching.set()
            observed["released"] = wait_until_set(
                current_read_budget().cancel_event
            )
            finished.set()
            return [make_chunk("c-nb-watch")], [], None
        assert wait_until_set(watching)
        raise RetrievalControlError("attestation failed")

    candidates = FakeCandidates(_participants(ids), retrieve=retrieve)
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts, window=2)):
        with pytest.raises(RetrievalControlError):
            cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert wait_until_set(finished)
    assert observed["released"] is True, "硬失败没有释放其余在途腿"
    assert receipts.libraries == []


def test_a_leg_parked_on_the_fanout_slot_reports_queue_deadline(pool):
    """卡在 run 的 ``fanout_limit`` 信号量上的腿是 ``queue_deadline``,不是超时。

    它一个字也没问数据库——等的是这次 run 自己的扇出闸。记成 ``timeout`` 会让用户
    读到「请缩小范围」,而范围压根没被搜过,正是两个原因码分家要避免的那种误导。
    窗口比扇出闸宽,所以两条腿都提交了,第二条必然停在信号量上。
    """
    ids = ("nb-holder", "nb-parked")
    holding = threading.Event()
    finished = threading.Event()

    def retrieve(notebook_id, query):
        if notebook_id == "nb-holder":
            holding.set()
            wait_until_set(current_read_budget().cancel_event)
            finished.set()
        return [make_chunk(f"c-{notebook_id}")], [], None

    candidates = FakeCandidates(_participants(ids), retrieve=retrieve)
    receipts = Receipts()

    with _global_run(
        ids, _plan(pool, receipts, window=2, phase=0.4), fanout_limit=1,
    ):
        cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert holding.is_set()
    assert wait_until_set(finished)
    holder = receipts.outcome("nb-holder")
    assert (holder.status, holder.reason) == ("skipped", "timeout")
    parked = receipts.outcome("nb-parked")
    assert (parked.status, parked.reason) == ("skipped", "queue_deadline")


def test_cancel_after_a_leg_started_fails_the_whole_call(pool):
    """取消在腿起跑之后到达 → 抛 ``AskCancelled``,不写出「全部超时」的回执。

    中途取消对腿来说就是预算被掐断,``classify_read_failure`` 会把它读成
    ``timeout``。若不在发回执之前复核 run 自己的取消令牌,一次用户主动停止就会被
    持久化成一份「每个库都超时了」的覆盖列表。只认 ``plan.cancel``:本次调用自己的
    阶段信号不是取消。
    """
    ids = ("nb-a",)
    cancel = threading.Event()
    candidates = FakeCandidates(
        _participants(ids),
        retrieve=lambda nid, q: (
            cancel.set(), ([make_chunk(f"c-{nid}")], [], None),
        )[1],
    )
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts, cancel=cancel)):
        with pytest.raises(AskCancelled):
            cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert candidates.calls == [("nb-a", "q")], "这条腿本来就该跑过一次"
    assert receipts.libraries == []
    assert receipts.evidence == []


def test_fanning_out_from_the_pools_own_thread_is_refused():
    """在借来的池自己的线程上扇出 = 等自己排在后面的任务,经典自死锁。"""
    ids = ("nb-a",)
    candidates = FakeCandidates(_participants(ids))
    receipts = Receipts()
    executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="d13-selfdeadlock",
    )
    try:
        with _global_run(ids, _plan(executor, receipts)):
            context = contextvars.copy_context()
            future = executor.submit(
                context.run,
                lambda: cf.federated_chunk_candidates(candidates, ids[0], ["q"]),
            )
            with pytest.raises(RuntimeError, match="own executor"):
                future.result(timeout=_HANDSHAKE_LIMIT)
    finally:
        executor.shutdown(wait=True)

    assert candidates.calls == []


def test_a_leg_that_finished_at_the_deadline_keeps_its_result():
    """到点那一刻已经干净完成的腿保留结果,不被无条件记成超时。

    直接测这个纯函数:它要覆盖的是「``wait`` 空手返回与时限检查之间那一瞬完成」的
    竞态,用真实线程去撞那一瞬既不确定也不可复现。它同时钉住另一半——以异常结束的
    腿必须原样抛出,``_run_one`` 只会放行取消与身份复核两种,都不许被时钟降级成
    逐库跳过。
    """
    finished = _FakeFuture(value=((["hit"], ["id"], None), ""))
    running = _FakeFuture(running=True)
    futures = {finished: 0, running: 1}
    results = [cf._empty_leg(), cf._empty_leg()]
    reasons = ["", ""]

    cf._harvest_finished(futures, results, reasons)

    assert results[0] == (["hit"], ["id"], None)
    assert list(futures.values()) == [1]

    control = _FakeFuture(error=RetrievalControlError("attestation failed"))
    with pytest.raises(RetrievalControlError):
        cf._harvest_finished({control: 0}, [cf._empty_leg()], [""])


class _FakeFuture:
    """``done``/``cancelled``/``exception``/``result`` 四件套,够 ``_harvest_finished`` 用。"""

    def __init__(self, *, value=None, error=None, running=False):
        self._value = value
        self._error = error
        self._running = running

    def done(self) -> bool:
        return not self._running

    def cancelled(self) -> bool:
        return False

    def exception(self):
        return self._error

    def result(self):
        if self._error is not None:
            raise self._error
        return self._value


# --------------------------------------------------------------------------
# 逐库回执
# --------------------------------------------------------------------------

def test_receipts_are_called_on_parent_thread_in_participant_order(pool):
    """回执在调用线程上、按参与集顺序发;工作线程只 return 值。

    作业线程是持久回执的唯一写入方,这是 ``_ordered_insert`` 那套确定性的前提:
    完成顺序是调度噪声,回执顺序不许是。
    """
    ids = ("nb-a", "nb-b", "nb-c", "nb-d")
    candidates = FakeCandidates(_participants(ids))
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts, window=4)):
        cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert receipts.order == list(ids)
    assert receipts.threads == {threading.get_ident()}
    assert all(outcome.status == "ok" for _nid, outcome in receipts.libraries)
    assert all(outcome.candidate_count == 1
               for _nid, outcome in receipts.libraries)


def test_partial_failure_marks_library_degraded(pool):
    """一个库两条子查询,败一条:仍算 ``ok``,但打 ``degraded``。

    它确实贡献了证据,所以覆盖列表不许说它被跳过;而答案是用比它实际拥有的更少的
    东西拼出来的,这正是 ``degraded`` 对读者的意思。它**不带**原因码:
    ``LibraryOutcome`` 拒绝一个答了题却背着跳过原因的库,否则用户会在一段被引用的
    原文旁边读到「检索未完成」。
    """
    ids = ("nb-half",)
    candidates = FakeCandidates(
        _participants(ids),
        retrieve=lambda nid, q: (
            (_ for _ in ()).throw(RuntimeError("one leg is down")) if q == "q2"
            else ([make_chunk(f"c-{nid}-{q}")], [], None)
        ),
    )
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts)):
        cf.federated_chunk_candidates(candidates, ids[0], ["q1", "q2"])

    outcome = receipts.outcome("nb-half")
    assert (outcome.status, outcome.reason) == ("ok", "")
    assert outcome.degraded is True
    assert outcome.candidate_count == 1


def test_candidate_count_is_the_pool_that_entered_the_merge(pool):
    """``candidate_count`` = 库内折叠之后、跨库合并之前的命中数。

    两条子查询都命中同一段原文时只算一次(折叠之后),而被 ``peer_evidence`` 挤掉
    的那条仍然算(合并之前)——它度量的是这个库**拿出了**多少,不是赢了多少席位。
    """
    ids = ("nb-a",)
    candidates = FakeCandidates(
        _participants(ids), recall=1,
        retrieve=lambda nid, q: (
            [make_chunk("c-same", 0.9), make_chunk(f"c-{q}", 0.5)], [], None,
        ),
    )
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts)):
        result = cf.federated_chunk_candidates(candidates, ids[0], ["q1", "q2"])

    # 合并预算是 ``global_ask_candidate_limit``,所以三条都进得去;折叠把两次
    # 命中的 ``c-same`` 收成一条。
    assert receipts.outcome("nb-a").candidate_count == 3
    assert len(result.collected) == 3


def test_a_library_whose_ceiling_cannot_be_read_still_gets_a_receipt(pool):
    """准备阶段就掉队的库也要有回执,否则它既不在已搜也不在跳过。

    来源枚举跑在父线程、任何任务存在之前,所以工作线程那把 handler 够不着它。
    """
    ids = ("nb-a", "nb-unreadable")
    candidates = FakeCandidates(
        _participants(ids),
        visible={"nb-unreadable": RuntimeError("source list is unreadable")},
    )
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts)):
        result = cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert result.participants == ("nb-a",)
    assert receipts.order == ["nb-a", "nb-unreadable"]
    dropped = receipts.outcome("nb-unreadable")
    assert (dropped.status, dropped.reason) == ("skipped", "unavailable")


# --------------------------------------------------------------------------
# 证据指纹
# --------------------------------------------------------------------------

def test_evidence_fingerprints_are_read_once(pool):
    """入选证据的指纹整次调用**只读一次**,不是逐库读。"""
    ids = ("nb-a", "nb-b", "nb-c")
    candidates = FakeCandidates(
        _participants(ids),
        retrieve=lambda nid, q: (
            [make_chunk(f"c-{nid}", elements=[f"e-{nid}", "e-shared"])], [], None,
        ),
    )
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts)):
        cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert len(candidates.fingerprint_reads) == 1
    # 去重之后的一份有界清单,不是每条命中各带一份。
    assert sorted(candidates.fingerprint_reads[0]) == [
        "e-nb-a", "e-nb-b", "e-nb-c", "e-shared",
    ]
    assert len(receipts.evidence) == 1
    assert sorted(receipts.evidence[0]) == [
        "e-nb-a", "e-nb-b", "e-nb-c", "e-shared",
    ]
    assert receipts.threads == {threading.get_ident()}


def test_evidence_read_has_its_own_budget_not_the_spent_phase(clock, pool):
    """阶段被慢库耗尽时,答完的库的入选证据仍要被佐证。

    阶段恰恰是在「某个库慢」时走完的,而逐库跳过就是为这种情形存在的。指纹读若
    以阶段剩余为上界,一个慢库就会让**整份答案**作废(缺席即拒绝)——于是它有自己
    的、逐库大小的预算,从读的那一刻起算。用注入时钟在生产者里把阶段走完:断言
    落在读那一刻 ``current_read_budget()`` 的 deadline,而不是读有没有发生。
    """
    ids = ("nb-a",)
    phase, notebook = 3.0, 5.0
    seen: dict = {}

    def retrieve(nid, query):
        clock.advance(phase + 1.0)
        return [make_chunk(f"c-{nid}", elements=[f"e-{nid}"])], [], None

    def fingerprints(read_index, element_ids):
        seen["deadline"] = current_read_budget().deadline
        seen["now"] = clock.now()
        return {element_id: ("src", "fp") for element_id in element_ids}

    candidates = FakeCandidates(
        _participants(ids), retrieve=retrieve, fingerprints=fingerprints,
    )
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts, phase=phase, notebook=notebook)):
        started = clock.now()
        cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert seen["now"] > started + phase          # 阶段确已走完
    assert seen["deadline"] == seen["now"] + notebook
    assert sorted(receipts.evidence[0]) == ["e-nb-a"]


def test_an_element_missing_from_a_successful_read_is_stated_as_none(pool):
    """读成功、但某个入选 element 没有行(检索之后被重新入库删掉)→ 回传 ``None``。

    缺席的含义是「从未走过本通道」,复核只按来源天花板判;一个走过本通道、却在
    指纹读之前被删掉的 element 若被留成缺席,它的引用就会被不加核验地放行。它也
    不进已见集合,下一轮照常重试(codex #755 第 1 轮 P2)。
    """
    ids = ("nb-a",)
    candidates = FakeCandidates(
        _participants(ids),
        retrieve=lambda nid, q: (
            [make_chunk(f"c-{nid}", elements=["e-live", "e-deleted"])], [], None,
        ),
        fingerprints=lambda read_index, element_ids: {
            element_id: ("src", "fp") for element_id in element_ids
            if element_id != "e-deleted"
        },
    )
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts)):
        cf.federated_chunk_candidates(candidates, ids[0], ["q"])
        cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert receipts.evidence[0] == {"e-live": ("src", "fp"), "e-deleted": None}
    # 真快照已结清、不再读;缺行的那个下一轮重试。
    assert candidates.fingerprint_reads == [["e-live", "e-deleted"], ["e-deleted"]]


def test_a_second_round_reads_only_the_new_elements(pool):
    """同一次 run 的第二轮只读新增 element,已回传过的不再读。

    ``on_evidence`` 是**累积**语义:消费者把每轮的表合并成一张,而本次 run 内任何
    一次快照都是合法的「之前」。推理 5 轮各覆盖全部选中元素 = 5 次宽读,而轮与轮
    之间的选择大面积重叠。去重是 run-local 的,所以两次 run 不会互借快照。
    """
    ids = ("nb-a",)

    def retrieve(notebook_id, query):
        hits = [make_chunk("c-1", elements=["e-1"])]
        if query == "q2":
            hits.append(make_chunk("c-2", elements=["e-2"]))
        return hits, [], None

    candidates = FakeCandidates(_participants(ids), retrieve=retrieve)
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts)):
        cf.federated_chunk_candidates(candidates, ids[0], ["q1"])
        cf.federated_chunk_candidates(candidates, ids[0], ["q2"])

    assert candidates.fingerprint_reads == [["e-1"], ["e-2"]]
    assert [sorted(row) for row in receipts.evidence] == [["e-1"], ["e-2"]]


def test_unreadable_fingerprints_are_published_as_none_and_retried(pool):
    """指纹读不到 → 发一条内容无关事件、逐 element 回传 ``None``,检索本身不塌;下一轮重试。

    三态合同:快照 / ``None``(走过本通道但读不到 → 复核拒绝)/ 缺席(从未走过本
    通道:文档概览、集合枚举、图对象 → 复核只按来源天花板判)。读失败若回传空表,
    这批 element 就与「从未走过本通道」无法区分,要么被不加核验地放行、要么连累
    概览类答案整份作废。读失败的 element 不进已见集合,否则一次瞬时故障会让它们在
    整次 run 里永久不可佐证;下一轮读成功的快照取代 ``None``。
    """
    ids = ("nb-a",)
    candidates = FakeCandidates(
        _participants(ids),
        retrieve=lambda nid, q: (
            [make_chunk(f"c-{nid}", elements=["e-1"])], [], None,
        ),
        fingerprints=lambda read_index, element_ids: (
            (_ for _ in ()).throw(RuntimeError("evidence table is unreadable"))
            if read_index == 1
            else {element_id: ("src", "fp") for element_id in element_ids}
        ),
    )
    receipts = Receipts()

    with _global_run(ids, _plan(pool, receipts)):
        result = cf.federated_chunk_candidates(candidates, ids[0], ["q"])
        cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert list(result.collected) == ["c-nb-a"]
    assert candidates.fingerprint_reads == [["e-1"], ["e-1"]]
    assert receipts.evidence == [{"e-1": None}, {"e-1": ("src", "fp")}]
    assert [event for event in candidates.events
            if event["kind"] == "chunk_federation_evidence_unavailable"] == [{
                "kind": "chunk_federation_evidence_unavailable",
                "error_type": "RuntimeError", "elements": 1,
            }]


# --------------------------------------------------------------------------
# 取消
# --------------------------------------------------------------------------

def test_cancel_propagates(pool):
    """计划的取消令牌一旦置位,整条臂抛 ``AskCancelled``,不降级成逐库跳过。

    取消是用户自己的决定,不是某个库的故障;而且已经取消的 run 不许再开连接,
    所以生产者一次都不该被调用,也不该留下任何一条「这个库被跳过了」的事件——
    那正是把取消读成逐库超时的样子。
    """
    ids = ("nb-a", "nb-b")
    candidates = FakeCandidates(_participants(ids))
    receipts = Receipts()
    cancel = threading.Event()
    cancel.set()

    with _global_run(ids, _plan(pool, receipts, cancel=cancel)):
        with pytest.raises(AskCancelled):
            cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert candidates.calls == []
    assert candidates.events == []
    assert receipts.libraries == []
    assert receipts.evidence == []


# --------------------------------------------------------------------------
# 没有计划时:一个字节都不变
# --------------------------------------------------------------------------

def test_without_a_plan_the_module_still_owns_its_own_pool(monkeypatch):
    """对等模式但没装计划 → 仍然自建 ``ThreadPoolExecutor``,事件里没有原因码。

    生产上今天的每一次 run 都是这条路。计划是 D1-4 才接上的,在那之前本任务的
    任何一条新行为都不许提前生效。
    """
    built: list = []
    real = cf.ThreadPoolExecutor

    def _counting(*args, **kwargs):
        built.append(kwargs.get("max_workers"))
        return real(*args, **kwargs)

    monkeypatch.setattr(cf, "ThreadPoolExecutor", _counting)
    ids = ("nb-a", "nb-b")
    candidates = FakeCandidates(
        _participants(ids),
        retrieve=lambda nid, q: (
            (_ for _ in ()).throw(RuntimeError("boom")) if nid == "nb-b"
            else ([make_chunk(f"c-{nid}")], [], None)
        ),
    )

    override = ParticipantOverride(
        notebook_ids=ids, tiers={}, attested_actor_id=_ACTOR,
    )
    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        with source_scope_context(
            ids[0], None, None,
            notebook_source_ceilings={nid: frozenset({f"src-{nid}"})
                                      for nid in ids},
            subjectless=True,
        ):
            with participant_override(override):
                result = cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert built == [2]
    assert list(result.collected) == ["c-nb-a"]
    skipped = [event for event in candidates.events
               if event["kind"] == "chunk_federation_skipped"]
    assert len(skipped) == 1
    assert "reason" not in skipped[0]
    assert "lane" not in skipped[0]
