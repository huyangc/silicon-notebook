"""D1-4 -- 全局问答改由单库 ``AskService.ask`` 作答。

全局问答从此**没有自己的检索与合成链路**:它建一次参与集覆盖 + 逐库冻结来源
天花板 + detached turn + 联邦运行计划,然后调同一个引擎。本文件钉的是那条接缝
上真正会出错的东西,不是「调用发生了」:

  · 标准 ``AskResponse`` 进 ``job.answer``、trace 边跑边可见;
  · 答案**绝不**写进任何笔记本的 ``answers`` 表(桩 ``ask_state`` 任一写方法被
    调即 fail);
  · 权限在五个点复核,其中逐库那一点在回执回调里,任何一点失败整条请求失败——
    包括回调的异常被检索层 fail-soft 吞掉的情形;
  · 逐库回执跨**多次**联邦调用聚合(reasoning 每轮一次):从未成功 = 跳过,
    部分失败 = 降级;一次联邦调用都没有的 run 不诬告任何库;
  · 引用冻结复核改吃 ``response.citations`` + ``anchors`` + 累积指纹表,逐条判定、
    部分失败照常交付(失效引用带原因标记 + ``citation_check`` 汇总),从不整份作废;
  · 入口校验:未知 mode / 扩展引擎 422,``deep`` 档位压成 ``standard``,
    升级前写下的旧请求串仍然幂等。

多数用例用一个**脚本化的假引擎**:它读 detached turn、按脚本发回执与指纹、返回
一个 ``AskResponse``,这样「接缝上的约定」可以逐条钉死而不必让八个库真的跑一遍
检索。最后一条是真库真引擎的端到端。
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from app.core.config import Settings
from app.models.ask import AskResponse, Citation, TraceStep
from app.models.global_ask import GlobalAskRequest
from app.services.ask_followup import FollowupResolution
from app.services.ask_modes import resolve_mode
from app.services.cancellation import AskCancelled
from app.services.federated_run import (
    LibraryOutcome, current_detached_ask_turn, current_federated_run_plan,
)
from app.services.global_ask import GlobalAskService, GlobalAskError
from app.services.retrieval_participants import (
    assert_override_matches_run, current_participant_override,
)
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import (
    current_source_scope, subjectless_run_active,
)
from app.services.sqlite_repository import SQLiteRepository


_LIBRARIES = ("a", "b", "c")


# ---------------------------------------------------------------------------
# 脚本化的假引擎
# ---------------------------------------------------------------------------

class _FakeAsk:
    """``AskService`` 在这条接缝上的形状,一行生产代码都不替。

    它做的事与真引擎在这条接缝上做的完全同形:建一次 retrieval run(覆盖的身份
    复核就是对着它跑的)、读 detached turn、经 ``plan`` 回传逐库回执与检索时刻的
    指纹、把 trace 推给 ``on_trace``、返回一个标准 ``AskResponse``。
    """

    def __init__(self, *, rounds=((),), citations=(), evidence=None,
                 evidence_groups=None, trace=(), extension_modes=(),
                 on_run=None, anchors=(), evidence_level=None):
        # 每一轮 = 一次联邦调用,内容是 ``[(notebook_id, LibraryOutcome), ...]``。
        self.rounds = [list(row) for row in rounds]
        self.citations = list(citations)
        self.anchors = list(anchors)
        # 真引擎有引用时给 ``grounded`` 档;部分失败必须把它封顶到 ``overview``。
        self.evidence_level = evidence_level or (
            "grounded" if self.citations or self.anchors else "inferred"
        )
        self.evidence_rounds = evidence
        # 每一轮入选的多元素段落:``[(element_id, ...), ...]``。一条引用只带
        # 段落的**首个** element,这一列才说出「这条引用背后还靠着哪些」。
        self.evidence_group_rounds = evidence_groups
        self.trace = list(trace)
        self.extension_modes = tuple(extension_modes)
        self.on_run = on_run
        self.calls: list = []
        self.seen_turn = None
        self.seen_override = None
        self.seen_scope = None
        self.seen_thread = ""

    # -- the two entry-point preflights the service calls on ``start`` -----
    def _resolve_ask_mode(self, mode):
        return resolve_mode(mode, self.extension_modes)

    def validate_reasoning_submission(self, notebook_id, payload):
        return None

    def resolve_reasoning_followup(self, notebook_id, payload, history=None):
        self.calls.append(("followup", notebook_id, history))
        question = payload.question.strip()
        return FollowupResolution(
            question=question, resolved_question=question,
            rewrite_ms=None, gate_message="",
        )

    # -- the engine ---------------------------------------------------------
    def ask(self, notebook_id, payload, *, user_id, job_id="",
            cancel_event=None, on_trace=None):
        self.calls.append(("ask", notebook_id, payload.mode))
        self.seen_thread = threading.current_thread().name
        with retrieval_run(
            run_kind=f"ask_{payload.mode}", actor_id=user_id,
            correlation_id=job_id, cancel_event=cancel_event,
        ):
            self.seen_turn = current_detached_ask_turn()
            self.seen_override = current_participant_override()
            self.seen_scope = current_source_scope()
            assert_override_matches_run()
            plan = current_federated_run_plan()
            for step in self.trace:
                if on_trace is not None:
                    on_trace(step)
            for index, receipts in enumerate(self.rounds):
                if cancel_event is not None and cancel_event.is_set():
                    raise AskCancelled()
                if not receipts:
                    continue
                for notebook_id_, outcome in receipts:
                    plan.on_library(notebook_id_, outcome)
                # 与生产者同序:分组先于指纹(``_report_evidence``)。
                plan.on_evidence_groups(self._groups_for(index))
                plan.on_evidence(self._evidence_for(index))
            if self.on_run is not None:
                self.on_run(self)
            if cancel_event is not None and cancel_event.is_set():
                raise AskCancelled()
            return AskResponse(
                answer_id="", conclusion="结论", answer="答案",
                grounded=bool(self.citations), mode=payload.mode,
                evidence_level=self.evidence_level,
                citations=[row.model_copy() for row in self.citations],
                anchors=[row.model_copy() for row in self.anchors],
                conversation_id=self.seen_turn.conversation_id,
                reasoning_trace=list(self.trace) or None,
            )

    def _evidence_for(self, index):
        if self.evidence_rounds is None:
            return {}
        if isinstance(self.evidence_rounds, dict):
            return self.evidence_rounds if index == 0 else {}
        return self.evidence_rounds[index]

    def _groups_for(self, index):
        if self.evidence_group_rounds is None:
            return ()
        return self.evidence_group_rounds[index] if index < len(
            self.evidence_group_rounds
        ) else ()


def _ok(*notebook_ids):
    return [(nid, LibraryOutcome(status="ok", candidate_count=1))
            for nid in notebook_ids]


def _skipped(notebook_id, reason="timeout"):
    return (notebook_id, LibraryOutcome(status="skipped", reason=reason))


def _degraded(notebook_id):
    return (notebook_id, LibraryOutcome(status="ok", degraded=True,
                                        candidate_count=1))


def _citation(notebook_id, index=1):
    return Citation(
        label=f"来源 {notebook_id}", source_id=f"s-{notebook_id}",
        element_id=f"e-{notebook_id}-{index}", location_label="第 1 节",
        quoted_span="原文", notebook_id=notebook_id,
    )


class _WriteTrap:
    """任何 ``ask_state`` 方法被调用就是一次越界写。

    不做白名单:detached 模式下引擎压根不该碰这个 store——``_prepare_turn`` 直接
    回传交下来的 turn,``_save_answer`` 在落库前返回。哪怕是一次「无害的读」也
    值得报红,因为它意味着有一条路径又开始假设「全局提问属于某个笔记本」。
    """

    def __init__(self):
        self.touched: list = []

    def __getattr__(self, name):
        def _fail(*args, **kwargs):
            self.touched.append(name)
            raise AssertionError(f"detached run 调用了 ask_state.{name}")
        return _fail


@pytest.fixture
def service(tmp_path):
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'global.db'}",
        storage_dir=str(tmp_path / "storage"),
    )
    repo = SQLiteRepository(settings)
    readable = set(_LIBRARIES)
    visible = {nid: {f"s-{nid}"} for nid in _LIBRARIES}
    # 名义 active 之外还有一个**可见来源为空**的库:天花板必须写成
    # ``frozenset()`` 而不是被跳过。
    visible["c"] = set()
    fingerprints: dict = {}
    sources = SimpleNamespace(
        all_visible_source_ids=lambda nb: sorted(visible.get(nb, ())),
        evidence_elements=lambda ids: {},
        evidence_fingerprints=lambda ids: {
            key: fingerprints[key] for key in ids if key in fingerprints
        },
    )
    sources.visible_source_ids_by_notebook = lambda ids: {
        nb: sorted(visible.get(nb, ())) for nb in ids
    }
    events: list = []
    built = GlobalAskService(
        store=repo._runtime.global_ask_store,
        notebooks=lambda user: [SimpleNamespace(id=nb, name=nb.upper())
                                for nb in sorted(readable)],
        can_read=lambda nb, user: nb in readable and user == "u",
        can_read_many=lambda ids, user: [nb for nb in ids if nb in readable],
        sources=sources, settings=settings, ask=_FakeAsk(),
        event_log=SimpleNamespace(emit=events.append),
    )
    built.test_visible = visible
    built.test_fingerprints = fingerprints
    built.test_readable = readable
    built.test_events = events
    yield built
    built.close()
    repo.close()


def _finished(service, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = service.store.job(job.job_id, "u")
        if row.status != "running" and job.job_id not in service._events:
            return row
        threading.Event().wait(0.01)
    pytest.fail("global worker did not settle")


def _run(service, **kwargs):
    job = service.start(GlobalAskRequest(**{"question": "比较一下", **kwargs}),
                        user_id="u")
    return _finished(service, job)


# ---------------------------------------------------------------------------
# 1. 标准应答与 trace
# ---------------------------------------------------------------------------

def test_chunk_mode_returns_standard_ask_response(service):
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES)], citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "fp")},
    )
    service.test_fingerprints["e-b-1"] = ("s-b", "fp")
    service.test_visible["b"] = {"s-b"}

    result = _run(service)

    assert result.status == "done"
    assert result.answer is not None and isinstance(result.answer, AskResponse)
    assert result.answer.answer == "答案"
    assert result.mode == "chunk"
    # 旧的全局专属应答形状不再被写入。
    assert result.response is None
    assert result.cited_notebook_ids == ["b"]
    assert result.searched_notebook_ids == list(_LIBRARIES)
    # 完成时刻由本服务补盖:detached 轮次绕过了单库那条 ``save_answer``(平时是它盖
    # 的),不补的话界面会把**提交**时刻显示成回答时刻。
    assert result.answer.answered_at
    assert result.answer.answered_at >= result.created_at
    # 同一处绕过的另一半:``answer_id`` 也是那条 ``save_answer`` 铸的。共享的回答视图
    # 以它为键(按回答重置引用小卡片、只在它非空时才给出「分享」),全局回答的身份
    # 就是它的作业。
    assert result.answer.answer_id == result.job_id


def test_reasoning_mode_streams_trace_into_job(service):
    """边跑边可见:引擎还没返回,轮询就已经读得到已推送的步骤。

    步数闸(``_TRACE_SAVE_STEPS``)是这里真正被触发的那一道——时间闸在一次快到
    测不出来的假引擎里恒不成立,所以用步数把「写出去了」这件事钉死,而不是靠
    跑得够慢。
    """
    steps = [TraceStep(step_type="plan", summary=f"第 {index} 步")
             for index in range(6)]
    seen: list = []
    service.ask = _FakeAsk(
        rounds=[[]], trace=steps,
        on_run=lambda fake: seen.append(
            [row.summary for row in
             service.store.job(_current_job_id(service), "u").trace]
        ),
    )

    result = _run(service, mode="reasoning")

    assert result.status == "done"
    # 第 5 步触发步数闸,所以引擎返回之前至少看得到前 5 步。
    assert seen[0][:5] == [f"第 {index} 步" for index in range(5)]
    # 终态清空,权威在 ``answer.reasoning_trace``,避免 payload 翻倍。
    assert result.trace == []
    assert [row.summary for row in result.answer.reasoning_trace] == [
        f"第 {index} 步" for index in range(6)
    ]


def test_the_trace_write_is_throttled_and_the_terminal_state_is_complete(service):
    """每一步都整行重写 = O(n²) 字节(全局作业的 trace 在同一份 JSON payload 里,
    不像单库那样是子表的一行 INSERT)。

    注入时钟,不看墙钟:前四步只写一次(第一步那次),第五步触发步数闸,时间推
    过阈值再来一步触发时间闸。终态无论如何都是完整的。
    """
    writes: list = []
    original = service.store.save_progress
    service.store.save_progress = lambda job, user_id: (
        writes.append(len(job.trace)), original(job, user_id),
    )[1]
    clock = [1000.0]
    service._clock = lambda: clock[0]

    def push(fake):
        plan = current_federated_run_plan()
        assert plan is not None

    steps = [TraceStep(step_type="plan", summary=f"s{index}")
             for index in range(7)]
    service.ask = _FakeAsk(rounds=[[]], trace=steps, on_run=push)

    result = _run(service, mode="reasoning")

    assert result.status == "done"
    # 第 1 步(距上次写 ∞)写一次,之后要再攒够 ``_TRACE_SAVE_STEPS`` 步才写第
    # 二次(第 6 步);第 2~5 步一次都不落库。终态那次是收尾的进度写。
    assert writes[:2] == [1, 6], writes
    assert not {2, 3, 4, 5} & set(writes), writes
    assert [row.summary for row in result.answer.reasoning_trace] == [
        f"s{index}" for index in range(7)
    ]


def test_the_trace_time_bound_also_fires(service):
    """时间闸单独有用例,否则它是一条永不触发的死代码。"""
    writes: list = []
    original = service.store.save_progress
    service.store.save_progress = lambda job, user_id: (
        writes.append(len(job.trace)), original(job, user_id),
    )[1]
    clock = [1000.0]
    service._clock = lambda: clock[0]

    def tick(step):
        clock[0] += 10.0

    steps = [TraceStep(step_type="plan", summary=f"s{index}")
             for index in range(3)]

    class _TickingAsk(_FakeAsk):
        def ask(self, notebook_id, payload, **kwargs):
            on_trace = kwargs.get("on_trace")
            kwargs["on_trace"] = lambda step: (tick(step), on_trace(step))[1]
            return super().ask(notebook_id, payload, **kwargs)

    service.ask = _TickingAsk(rounds=[[]], trace=steps)

    assert _run(service, mode="reasoning").status == "done"

    # 每一步之间时钟都跳过阈值,所以三步各写一次。
    assert writes[:3] == [1, 2, 3], writes


def _current_job_id(service):
    with service._lock:
        return next(iter(service._events))


# ---------------------------------------------------------------------------
# 2. detached:答案绝不进任何笔记本的 answers 表
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["chunk", "reasoning"])
def test_answers_table_is_never_written(tmp_path, monkeypatch, mode):
    """真 ``AskService``,桩 ``ask_state``:引擎走完一整轮也不碰它。

    这条用例故意不用假引擎——它要证明的正是**生产的** ``_prepare_turn`` /
    ``_save_answer`` 在 detached turn 下的行为,假引擎证明不了。

    两个断言缺一不可:作业必须 ``done``(不是「随便什么终态」——一次在检索之前
    就炸掉的 run 同样不会碰 store,那样这条守卫就恒真了),而且 ``_save_answer``
    必须**真的被调用过**并交回 detached 的空 answer id。
    """
    repo, notebooks, user_id = _e2e_repo(
        tmp_path, monkeypatch, answer="这些库里有三篇手册。",
    )
    try:
        service = repo._runtime.global_ask_service()
        trap = _WriteTrap()
        service.ask.ask_state = trap
        saved: list = []
        original_save = service.ask._save_answer

        def spy(*args, **kwargs):
            answer_id = original_save(*args, **kwargs)
            saved.append(answer_id)
            return answer_id

        monkeypatch.setattr(service.ask, "_save_answer", spy)

        result = _e2e_answer(
            repo, notebooks, user_id, "介绍一下这个notebook中的文章", mode=mode,
        )

        assert result.status == "done", result.error
        assert trap.touched == []
        # 走到了持久化收口,并且那一步返回了 detached 的空值。
        assert saved == [""], saved
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# 3. 安装形状
# ---------------------------------------------------------------------------

def test_participant_override_installed_with_attested_actor(service):
    fake = _FakeAsk(rounds=[_ok(*_LIBRARIES)])
    service.ask = fake

    assert _run(service).status == "done"

    assert fake.seen_override.notebook_ids == _LIBRARIES
    assert fake.seen_override.attested_actor_id == "u"
    assert fake.seen_override.nominal_active_id == _LIBRARIES[0]
    assert fake.seen_turn.conversation_id.startswith("gconv-")


def test_source_ceilings_cover_every_resolved_notebook(service):
    """全映射,含名义 active,含可见来源为空的库。

    少一个键就是 ``_peer_ceiling_participants``(fail-closed)与
    ``ActiveSourceScope.allows``(fail-open)对同一事实给出两个答案;名义 active
    少了自己的那一项,``filter_retrieval_items`` 会落到「这里没有天花板」而对它
    完全不设防。
    """
    fake = _FakeAsk(rounds=[_ok(*_LIBRARIES)])
    service.ask = fake

    assert _run(service).status == "done"

    scope = fake.seen_scope
    assert subjectless_run_active.__module__  # 读口存在,见下一断言的语义
    assert [scope.source_ceiling_for(nid) for nid in _LIBRARIES] == [
        frozenset({"s-a"}), frozenset({"s-b"}), frozenset(),
    ]
    # 提交的两个维度必须缺席,否则 ``covers_notebook`` 会开始排除参与库。
    assert scope.source_ceiling_for("nb-outsider") is None


def test_no_base_scope_is_submitted():
    """静态:全局入口不构造 ``SourceScope`` / ``BaseNotebookScope``。

    一次没有主体库的 run 没有自己的勾选清单;凭空造一个会被冻进 scope 的两份
    payload,并让参与库从 ``covers_notebook`` 里掉出去。
    """
    path = Path(__file__).resolve().parents[1] / "app/services/global_ask.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    built = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not built & {"SourceScope", "BaseNotebookScope"}, built


def test_job_thread_is_not_a_retrieval_pool_thread(service):
    """作业线程不能是共享检索池的 worker,否则是教科书式的池死锁。

    联邦调用在**调用线程**上 ``wait()`` 等池里的任务;作业线程自己占着池里一个
    槽位去等池里的槽位,并发到池容量就必然挂死。
    """
    fake = _FakeAsk(rounds=[_ok(*_LIBRARIES)])
    service.ask = fake

    assert _run(service).status == "done"

    pool_prefix = service._retrieval_pool._thread_name_prefix
    assert not fake.seen_thread.startswith(pool_prefix), fake.seen_thread
    assert fake.seen_thread.startswith("global-ask")


# ---------------------------------------------------------------------------
# 4. 权限中途变化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "where", ["receipt", "after-answer", "after-citation-recheck"],
)
def test_authority_revoked_mid_run_fails_the_whole_request(service, where):
    """三个注入点各一次,三次都是整条请求失败,且不落答案。

    ``receipt`` 那一支额外证明一件事:回调抛出的异常即使被检索层的 fail-soft
    handler 吞掉,run 仍然失败——服务把首个异常记了下来,在引擎返回之后重抛。
    """
    allowed = [list(_LIBRARIES)]

    if where == "receipt":
        # 回执回调抛出之后**把权限还回去**,这样唯一还能让这条 run 失败的东西就是
        # 「服务记下了首个异常并在引擎返回后重抛」。不还回去的话,引擎返回之后那
        # 一次复核会顺手把它拦下来,这条分支就证明不了自己要证明的事。
        class _SwallowingAsk(_FakeAsk):
            def ask(self, notebook_id, payload, **kwargs):
                allowed[0] = []
                try:
                    return super().ask(notebook_id, payload, **kwargs)
                except GlobalAskError:
                    # 检索层的 fail-soft 把它吞掉,照常交出一个答案——这正是要被
                    # 拦下的形态。
                    allowed[0] = list(_LIBRARIES)
                    return AskResponse(
                        answer_id="", conclusion="结论", answer="答案",
                        mode="chunk", conversation_id="gconv-x",
                    )

        service.ask = _SwallowingAsk(rounds=[_ok(*_LIBRARIES)])
    elif where == "after-answer":
        service.ask = _FakeAsk(
            rounds=[_ok(*_LIBRARIES)],
            on_run=lambda fake: allowed.__setitem__(0, []),
        )
    else:
        service.ask = _FakeAsk(
            rounds=[_ok(*_LIBRARIES)], citations=[_citation("b")],
            evidence={"e-b-1": ("s-b", "fp")},
            on_run=lambda fake: None,
        )
        service.test_fingerprints["e-b-1"] = ("s-b", "fp")
        service.test_visible["b"] = {"s-b"}
        # 引用复核跑完之后再撤权:最后一个 ``_check`` 必须仍然拦得住。
        original_check = service._check_citations

        def revoke_after(*args, **kwargs):
            outcome = original_check(*args, **kwargs)
            allowed[0] = []
            return outcome

        service._check_citations = revoke_after

    job = service.start(
        GlobalAskRequest(question="比较一下"), user_id="u",
        allowed_notebook_ids=list(_LIBRARIES),
        authority_check=lambda: allowed[0],
    )
    result = _finished(service, job)

    assert result.status == "failed"
    assert result.answer is None and result.response is None
    assert result.error == "部分笔记本已无法访问，请重新选择范围。"


def test_cancellation_stops_before_synthesis(service):
    """检索中取消 → 作业置取消态,不落答案,合成不发生。

    握手而不是 sleep:引擎在第一轮回执之后停在栅栏上,测试在那一刻按下取消,
    再放行;引擎的下一轮看到取消位就抛 ``AskCancelled``。
    """
    reached, release = threading.Event(), threading.Event()
    synthesized = []

    class _PausingAsk(_FakeAsk):
        def ask(self, notebook_id, payload, **kwargs):
            cancel_event = kwargs.get("cancel_event")
            plan = None
            with retrieval_run(
                run_kind="ask_chunk", actor_id=kwargs["user_id"],
                cancel_event=cancel_event,
            ):
                plan = current_federated_run_plan()
                for nid, outcome in _ok(*_LIBRARIES):
                    plan.on_library(nid, outcome)
                plan.on_evidence({})
                reached.set()
                assert release.wait(5)
                if cancel_event.is_set():
                    raise AskCancelled()
                synthesized.append(1)
                return AskResponse(
                    answer_id="", conclusion="结论", answer="答案",
                    mode="chunk", conversation_id="gconv-x",
                )

    service.ask = _PausingAsk()
    job = service.start(GlobalAskRequest(question="比较一下"), user_id="u")
    assert reached.wait(5)
    assert service.cancel(job.job_id, user_id="u").status == "cancelled"
    release.set()
    result = _finished(service, job)

    assert result.status == "cancelled"
    assert result.answer is None and result.response is None
    assert synthesized == []


# ---------------------------------------------------------------------------
# 5. 跨调用回执聚合
# ---------------------------------------------------------------------------

def test_library_never_succeeded_is_skipped_partial_is_degraded(service):
    """两轮联邦调用:b 两轮都失败,c 只失败一轮。

    reasoning 每轮检索发一次回执,所以「这一库这次 run 到底搜到没有」只有作业
    这一层知道。
    """
    service.ask = _FakeAsk(rounds=[
        [*_ok("a"), _skipped("b", "timeout"), *_ok("c")],
        [*_ok("a"), _skipped("b", "saturated"), _degraded("c")],
    ])

    result = _run(service, mode="reasoning")

    assert result.searched_notebook_ids == ["a", "c"]
    assert result.degraded_notebook_ids == ["c"]
    assert [row.notebook_id for row in result.skipped_notebooks] == ["b"]
    # 最近一轮的原因码,翻成文案。
    assert result.skipped_notebooks[0].reason == "检索未完成，请稍后重试。"


def test_a_library_that_answers_a_later_round_is_not_skipped(service):
    service.ask = _FakeAsk(rounds=[
        [_skipped("a"), *_ok("b", "c")],
        _ok("a", "b", "c"),
    ])

    result = _run(service, mode="reasoning")

    assert result.searched_notebook_ids == list(_LIBRARIES)
    assert result.skipped_notebooks == []
    # 一轮输过 → 降级,不是跳过。
    assert result.degraded_notebook_ids == ["a"]


def test_concurrent_receipt_callbacks_do_not_lose_coverage(service):
    """两条线程同时回执:聚合与持久化都不能丢账。

    reasoning 把子查询扇到引擎自己的池上,每条线程各自进一次联邦通道,所以
    ``on_library`` / ``on_evidence`` 是**并发**回调。握手(``Barrier``)构造出
    「两条回调同时在 flight」这个事实,而不是靠 sleep 等它发生。
    """
    from threading import Barrier, Thread

    ready = Barrier(2, timeout=5)

    class _ConcurrentAsk(_FakeAsk):
        def ask(self, notebook_id, payload, **kwargs):
            with retrieval_run(
                run_kind="ask_reasoning", actor_id=kwargs["user_id"],
            ):
                plan = current_federated_run_plan()
                errors: list = []

                def leg(library, element_id):
                    try:
                        ready.wait()
                        plan.on_library(
                            library,
                            LibraryOutcome(status="ok", candidate_count=1),
                        )
                        plan.on_evidence({element_id: (f"s-{library}", "fp")})
                    except BaseException as exc:  # noqa: BLE001
                        errors.append(exc)

                threads = [
                    Thread(target=leg, args=("a", "e-a-1")),
                    Thread(target=leg, args=("b", "e-b-1")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                assert not errors, errors
                plan.on_library(
                    "c", LibraryOutcome(status="skipped", reason="timeout"),
                )
                return AskResponse(
                    answer_id="", conclusion="结论", answer="答案",
                    mode="reasoning", conversation_id="gconv-x",
                )

    service.ask = _ConcurrentAsk()

    result = _run(service, mode="reasoning")

    assert result.status == "done"
    # 两条并发回执都记住了,顺序仍然是解析序。
    assert result.searched_notebook_ids == ["a", "b"]
    assert [row.notebook_id for row in result.skipped_notebooks] == ["c"]


def test_a_stale_coverage_snapshot_is_never_published_after_a_fresher_one(service):
    """序号闸:拿着旧快照的写者在新快照落地之后不许再写。

    没有这道闸,两条并发回调会以「都读、都通过、旧的最后写」收场,持久化的覆盖
    列表于是往回走——一个库从 ``searched`` 里消失又出现,轮询读到的是一次在丢
    地盘的 run。
    """
    from app.services.global_ask import _RunState

    state = _RunState(["a", "b"])
    first, *_ = state.coverage()
    second, *_ = state.coverage()

    assert state.claim_publish(second) is True
    assert state.claim_publish(first) is False
    assert state.claim_publish(second) is False


def test_a_trace_write_cannot_commit_a_stale_coverage_list(service):
    """trace 写与 coverage 发布走同一条发布路径:持久化的覆盖列表不许倒退。

    ``save_progress`` 写的是**整行**,coverage 字段在内。reasoning 一边从引擎自己
    的线程推 trace、一边从别的线程发回执,所以一次 trace 写会在 coverage 发布的
    正中间把整行序列化出去。它若不走 ``publish_lock``,就成了 coverage 字段的第二
    个、没有序号的发布者:拿到旧 coverage、卡住、在更新的那份提交之后才落库——
    轮询看到 run 在丢地盘,随后的取消还会把这份陈旧列表永久留下。

    握手不靠计时:trace 写在**进入 store 之后**卡住(这时它手上的序列化快照已经
    取好了),主线程这才去发回执。修好之后这次发布根本进不来,``coverage_persisted``
    的等待必然走到上限——上限只决定用例多慢,不决定结论;结论落在持久化顺序上。
    """
    from threading import Event, Thread

    trace_idents: list = []
    trace_writing, coverage_persisted = Event(), Event()
    persisted: list = []
    recorder = threading.Lock()
    original = service.store.save_progress

    def probing_save(job, user_id):
        # 真 store 也是在写之前把整行(含 coverage)序列化出来的。
        snapshot = list(job.searched_notebook_ids)
        if threading.get_ident() in trace_idents:
            trace_writing.set()
            coverage_persisted.wait(0.5)
        result = original(job, user_id)
        with recorder:
            persisted.append(snapshot)
        if snapshot:
            coverage_persisted.set()
        return result

    service.store.save_progress = probing_save

    class _TraceRacingAsk(_FakeAsk):
        def ask(self, notebook_id, payload, *, user_id, job_id="",
                cancel_event=None, on_trace=None):
            with retrieval_run(
                run_kind="ask_reasoning", actor_id=user_id,
                correlation_id=job_id,
            ):
                plan = current_federated_run_plan()

                def push():
                    trace_idents.append(threading.get_ident())
                    on_trace(TraceStep(step_type="plan", summary="第一步"))

                tracer = Thread(target=push)
                tracer.start()
                assert trace_writing.wait(5), "trace 写没有进到 store"
                plan.on_library(
                    "a", LibraryOutcome(status="ok", candidate_count=1),
                )
                tracer.join(5)
                assert not tracer.is_alive()
                return AskResponse(
                    answer_id="", conclusion="结论", answer="答案",
                    mode="reasoning",
                    conversation_id=current_detached_ask_turn().conversation_id,
                )

    service.ask = _TraceRacingAsk()

    result = _run(service, mode="reasoning")

    assert result.status == "done", result.error
    # trace 写先提交它那份(当时还空的)覆盖列表,回执那份**之后**才落库。
    assert persisted[:2] == [[], ["a"]], persisted
    # 任何一次写都不得比前一次少:那正是轮询读到的「丢地盘」。
    assert all(
        set(earlier) <= set(later)
        for earlier, later in zip(persisted, persisted[1:])
    ), persisted
    assert result.searched_notebook_ids == ["a"]


def test_receipt_order_follows_resolved_ids(service):
    """回执按解析序,不按完成序:轮询不能看到收据来回跳。"""
    service.ask = _FakeAsk(rounds=[[
        *_ok("c"), _skipped("b"), *_ok("a"),
    ]])

    result = _run(service)

    assert result.searched_notebook_ids == ["a", "c"]


def test_a_run_without_any_federated_call_blames_no_library(service):
    """文档概述 / 集合枚举这类直接作答的路径:一次联邦调用都没有。

    它们走的是参与集感知的枚举腿,整个参与集都被读过,所以把任何一个库记成
    「跳过」都是在让用户去缩小一个其实读全了的范围——而 ``queue_deadline``
    这类原因码描述的是一次**发出过的**查询,这里一次都没发。
    """
    service.ask = _FakeAsk(rounds=[[]])

    result = _run(service)

    assert result.searched_notebook_ids == list(_LIBRARIES)
    assert result.skipped_notebooks == []
    assert result.degraded_notebook_ids == []


# ---------------------------------------------------------------------------
# 6. 引用冻结复核:逐条判定,部分失败照常交付(Q3)
# ---------------------------------------------------------------------------
#
# 终态复核只判引用**完整性**(原文已改动 / 资料已删除 / 无法核对),从不作废整份
# 答案、从不改动正文;权限与来源范围只归检索层管(2026-09-29 用户裁决)。

_ANSWER_TEXT = "答案"


def _grounded_service(service, *, citations, evidence, fingerprints, **fake):
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES)], citations=citations, evidence=evidence,
        **fake,
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints.clear()
    service.test_fingerprints.update(fingerprints)


def _events(service, kind):
    return [row for row in service.test_events if row.get("kind") == kind]


def _check(result):
    """The wire summary exactly as the answer serialises it (``None`` = absent)."""
    return result.answer.model_dump(mode="json").get("citation_check")


def _assert_delivered(result):
    """Q3: the answer text is the engine's, never a replacement sentence."""
    assert result.status == "done"
    assert result.answer.answer == _ANSWER_TEXT
    assert result.answer.conclusion == "结论"
    assert "引用原文在回答期间发生了变化" not in result.answer.answer


def test_changed_evidence_is_marked_and_the_answer_is_delivered(service):
    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")},
        fingerprints={"e-b-1": ("s-b", "改过的原文")},
    )

    result = _run(service)

    _assert_delivered(result)
    assert [row.verification for row in result.answer.citations] == ["changed"]
    assert _check(result) == {
        "outcome": "partial", "checked": 1, "failed": 1,
        "changed": 1, "source_gone": 0, "unverifiable": 0,
    }
    assert result.answer.grounded is False
    # 徽章封顶「概述」,不再自称有据。
    assert result.answer.evidence_level == "overview"
    # 引用还在,归属照常统计,学习链照常能拿到它。
    assert result.cited_notebook_ids == ["b"]
    [partial] = _events(service, "global_ask_citations_partial")
    assert partial == {
        "kind": "global_ask_citations_partial", "libraries": 3,
        "checked": 1, "failed": 1, "changed": 1, "source_gone": 0,
        "unverifiable": 0, "reasons": {"changed": 1},
    }
    assert not _events(service, "global_ask_citations_void")
    assert not _events(service, "global_ask_citation_scope_diagnostic")


def test_the_partial_answer_persists_in_the_payload(service):
    """落库随 ``payload_json``,重开这一轮拿到的是同一份标记与汇总。"""
    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")}, fingerprints={},
    )

    result = _run(service)
    stored = service.store.job(result.job_id, "u")

    assert stored.answer.citations[0].verification == "source_gone"
    assert stored.answer.citation_check.model_dump() == _check(result)
    from app.services.global_citation_check import global_answer_check
    assert global_answer_check(stored) == _check(result)
    assert global_answer_check(
        {"payload": {"answer": stored.answer.model_dump(mode="json")}}
    ) == _check(result)


def test_every_citation_is_judged_not_only_the_first_failure(service):
    """不在第一条失败时退出:每一条引用都拿到自己的原因,计数逐类如实。"""
    _grounded_service(
        service,
        citations=[_citation("b", 1), _citation("b", 2), _citation("b", 3)],
        evidence={
            "e-b-1": ("s-b", "一"), "e-b-2": ("s-b", "二"), "e-b-3": ("s-b", "三"),
        },
        fingerprints={"e-b-1": ("s-b", "一改"), "e-b-3": ("s-b", "三")},
    )

    result = _run(service)

    _assert_delivered(result)
    assert [row.verification for row in result.answer.citations] == [
        "changed", "source_gone", None,
    ]
    assert _check(result) == {
        "outcome": "partial", "checked": 3, "failed": 2,
        "changed": 1, "source_gone": 1, "unverifiable": 0,
    }


def test_answer_anchors_are_validated_too(service):
    """reasoning 的权威显示路径是锚点:只复核 citations 等于放过主路径。"""
    from app.models.ask import AnswerAnchor

    anchor = AnswerAnchor(
        key="k1", object_id="e-b-2", object_type="element", label="来源 b",
        source_id="s-b", element_id="e-b-2", notebook_id="b",
    )
    _grounded_service(
        service, citations=[_citation("b", 1)], anchors=[anchor],
        evidence={"e-b-1": ("s-b", "一"), "e-b-2": ("s-b", "二")},
        fingerprints={"e-b-1": ("s-b", "一"), "e-b-2": ("s-b", "二改")},
    )

    result = _run(service)

    _assert_delivered(result)
    assert result.answer.citations[0].verification is None
    assert result.answer.anchors[0].verification == "changed"
    assert _check(result)["changed"] == 1


def test_an_anchor_and_a_citation_on_one_element_count_once(service):
    from app.models.ask import AnswerAnchor

    anchor = AnswerAnchor(
        key="k1", object_id="e-b-1", object_type="element", label="来源 b",
        source_id="s-b", element_id="e-b-1", notebook_id="b",
    )
    _grounded_service(
        service, citations=[_citation("b")], anchors=[anchor],
        evidence={"e-b-1": ("s-b", "原文")}, fingerprints={},
    )

    result = _run(service)

    assert result.answer.citations[0].verification == "source_gone"
    assert result.answer.anchors[0].verification == "source_gone"
    assert _check(result)["checked"] == 1 and _check(result)["failed"] == 1


def test_nothing_failed_serialises_no_check_and_no_marks(service):
    """零失败时汇总与标记整体缺席:库外读者看到的形状与改动之前逐字节相同。"""
    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")},
        fingerprints={"e-b-1": ("s-b", "原文")},
    )

    result = _run(service)

    dumped = result.answer.model_dump(mode="json")
    assert "citation_check" not in dumped
    assert all("verification" not in row for row in dumped["citations"])
    assert result.answer.grounded is True
    assert result.answer.evidence_level == "grounded"
    from app.services.global_citation_check import global_answer_check
    assert global_answer_check(result) is None
    assert not _events(service, "global_ask_citations_partial")


def test_an_inferred_answer_is_not_raised_by_the_cap(service):
    _grounded_service(
        service, citations=[_citation("b")], evidence_level="inferred",
        evidence={"e-b-1": ("s-b", "原文")}, fingerprints={},
    )

    result = _run(service)

    assert result.answer.evidence_level == "inferred"
    assert result.answer.grounded is False


def test_an_unreadable_fingerprint_is_unverifiable_not_accepted(service):
    """第 2 轮指纹读失败 → 那一轮的 element 被**明确**标成 ``None`` → 无法核对。

    契约的三态在这里起作用:``None`` 是「走过生产者但读不到指纹」,是一条**声明
    出来的**「无法核对」;它不是「通过」。
    """
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES), _ok(*_LIBRARIES)],
        citations=[_citation("b", 2)],
        evidence=[{"e-b-1": ("s-b", "原文")}, {"e-b-2": None}],
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints.update({
        "e-b-1": ("s-b", "原文"), "e-b-2": ("s-b", "第二轮原文"),
    })

    result = _run(service, mode="reasoning")

    _assert_delivered(result)
    assert result.answer.citations[0].verification == "unverifiable"
    assert _events(service, "global_ask_citations_partial")[-1]["reasons"] == {
        "unreadable": 1,
    }


def test_a_real_snapshot_is_never_overwritten_by_a_later_unreadable_one(service):
    """第 1 轮读到了真快照,第 2 轮同一个 element 读失败 → 仍按真快照判。"""
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES), _ok(*_LIBRARIES)],
        citations=[_citation("b")],
        evidence=[{"e-b-1": ("s-b", "原文")}, {"e-b-1": None}],
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints["e-b-1"] = ("s-b", "原文")

    result = _run(service, mode="reasoning")

    assert [row.element_id for row in result.answer.citations] == ["e-b-1"]
    assert result.answer.citations[0].verification is None
    assert result.answer.grounded is True


def test_the_first_real_snapshot_wins_over_a_later_one(service):
    """首个真实快照获胜:后到的指针读可能晚于一次编辑,不许把它盖掉。

    第 1 轮登记的是检索时刻的原文;第 2 轮(例如一次 ``attest_pointers`` 的按 id
    读)读到的已经是改过的文字,现读也是改过的文字。后者若覆盖前者,终态复核
    就是拿新文字比新文字,一次真实的改动被判成通过。
    """
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES), _ok(*_LIBRARIES)],
        citations=[_citation("b")],
        evidence=[{"e-b-1": ("s-b", "原文")}, {"e-b-1": ("s-b", "改过")}],
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints["e-b-1"] = ("s-b", "改过")

    result = _run(service, mode="reasoning")

    assert result.answer.citations[0].verification == "changed"


def test_a_later_real_snapshot_replaces_an_unreadable_one(service):
    """反向:先 ``None``、后真快照 → 用真快照,不停留在「无法核对」。"""
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES), _ok(*_LIBRARIES)],
        citations=[_citation("b")],
        evidence=[{"e-b-1": None}, {"e-b-1": ("s-b", "原文")}],
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints["e-b-1"] = ("s-b", "原文")

    result = _run(service, mode="reasoning")

    assert [row.element_id for row in result.answer.citations] == ["e-b-1"]
    assert result.answer.citations[0].verification is None


def test_a_deleted_source_is_marked_source_gone(service):
    """天花板在 start 时冻结;之后来源被删(全局问答里来源离开可见集只能是被删)。"""
    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")},
        fingerprints={"e-b-1": ("s-b", "原文")},
    )
    service.ask.on_run = lambda fake: service.test_visible.__setitem__("b", set())

    result = _run(service)

    _assert_delivered(result)
    assert result.answer.citations[0].verification == "source_gone"


def test_a_source_outside_the_frozen_ceiling_is_unverifiable_and_diagnosed(service):
    """原先会整份作废的 ``out_of_ceiling``:现在只标「无法核对」+ 诊断事件。

    走到这里说明某条检索通道漏过了来源天花板——那是检索层的 bug,要能定位;
    但终态复核不是权限闸,不因此扣下正文(2026-09-29 裁决)。
    """
    stray = _citation("b").model_copy(update={"source_id": "s-elsewhere"})
    _grounded_service(
        service, citations=[stray],
        evidence={"e-b-1": ("s-elsewhere", "原文")},
        fingerprints={"e-b-1": ("s-elsewhere", "原文")},
    )

    result = _run(service)

    _assert_delivered(result)
    assert result.answer.citations == [stray.model_copy(update={
        "verification": "unverifiable",
    })]
    assert _check(result)["unverifiable"] == 1
    [diagnostic] = _events(service, "global_ask_citation_scope_diagnostic")
    assert diagnostic == {
        "kind": "global_ask_citation_scope_diagnostic", "libraries": 3,
        "out_of_ceiling": 1,
    }


def test_an_unattributed_citation_is_unverifiable_and_diagnosed(service):
    """空归属是 D0 归一漏改的告警面:内部原因码单独进诊断事件,线上只说无法核对。"""
    blank = _citation("b").model_copy(update={"notebook_id": ""})
    service.ask = _FakeAsk(rounds=[_ok(*_LIBRARIES)], citations=[blank])
    service.test_visible["b"] = {"s-b"}

    result = _run(service)

    _assert_delivered(result)
    assert result.answer.citations[0].verification == "unverifiable"
    assert _events(service, "global_ask_citations_partial")[-1]["reasons"] == {
        "unattributed": 1,
    }
    [diagnostic] = _events(service, "global_ask_citation_scope_diagnostic")
    assert diagnostic["unattributed"] == 1


def test_nobody_registered_is_unverifiable_and_not_the_changed_copy(service):
    """集合枚举 / 文档概览形状:引用的是真的 ``source_elements`` 行,但没有任何生产者
    登记过检索时刻快照 → 「无法核对」,文案不是「原文发生了变化」。

    只有真实快照不一致才说「发生了变化」(J1)。
    """
    from app.services.global_citation_check import citation_check_notice

    service.ask = _FakeAsk(
        rounds=[[]], citations=[_citation("b")], evidence=None,
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints["e-b-1"] = ("s-b", "原文")

    result = _run(service)

    _assert_delivered(result)
    assert result.answer.citations[0].verification == "unverifiable"
    assert [row.notebook_id for row in result.answer.citations] == ["b"]
    notice = citation_check_notice(_check(result))
    assert notice == (
        "本次回答有部分引用未通过核对：1 条无法核对。"
        "回答内容照常保留，带标记的引用可点开查看原因。"
    )
    assert "改动" not in notice and "变化" not in notice
    assert _events(service, "global_ask_citations_partial")[-1]["reasons"] == {
        "unattested": 1,
    }


def test_an_unregistered_element_under_another_source_is_not_called_changed(service):
    """没有快照就没有「之前」:现读挂到了别的来源下也不说「原文已改动」。"""
    service.ask = _FakeAsk(
        rounds=[[]], citations=[_citation("b")], evidence=None,
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints["e-b-1"] = ("s-other", "原文")

    result = _run(service)

    assert result.answer.citations[0].verification == "unverifiable"


def _external_citation(**updates):
    return Citation(
        label="arXiv · 某篇论文", source_id="", element_id="",
        location_label="摘要", quoted_span="库外原文", tier="external",
        url="https://arxiv.org/abs/2401.00001",
    ).model_copy(update=updates)


def test_an_external_citation_is_not_checked(service):
    """reflect 插件动作带回的库外证据不属于任何一个库,不参与冻结复核,也不计数。"""
    service.test_visible["b"] = {"s-b"}
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES)],
        citations=[_citation("b"), _external_citation()],
        evidence={"e-b-1": ("s-b", "原文")},
    )
    service.test_fingerprints["e-b-1"] = ("s-b", "原文")

    result = _run(service)

    assert result.status == "done"
    assert [row.tier for row in result.answer.citations] == ["personal", "external"]
    assert [row.verification for row in result.answer.citations] == [None, None]
    assert result.cited_notebook_ids == ["b"]
    assert _check(result) is None


@pytest.mark.parametrize("updates", [
    {"source_id": "s-b"},            # 自称库外,却指着一个库内来源
    {"element_id": "e-b-1"},         # ……或一个库内 element
], ids=["names-a-source", "names-an-element"])
def test_the_external_tier_alone_does_not_exempt_a_citation(service, updates):
    """豁免是严格的:只认「tier=external + 有 URL + 不指向任何库内行」。"""
    service.test_visible["b"] = {"s-b"}
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES)],
        citations=[_external_citation(**updates)], evidence={},
    )

    result = _run(service)

    _assert_delivered(result)
    assert result.answer.citations[0].verification == "unverifiable"
    assert _events(service, "global_ask_citations_partial")[-1]["reasons"] == {
        "unattributed": 1,
    }


def test_a_reference_naming_no_library_row_has_nothing_to_check(service):
    """既无来源也无 element(无 URL 的「库外」条目、裸图节点):没有可核对的完整性。"""
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES)],
        citations=[_external_citation(url="")], evidence={},
    )

    result = _run(service)

    assert result.answer.citations[0].verification is None
    assert _check(result) is None


def test_a_deleted_element_is_marked_source_gone(service):
    """检索时刻有快照、现读没有 → 资料已删除。"""
    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")}, fingerprints={},
    )

    result = _run(service)

    _assert_delivered(result)
    assert result.answer.citations[0].verification == "source_gone"
    assert _check(result)["source_gone"] == 1


def test_same_text_reinserted_under_the_same_id_does_not_fail(service):
    """删除后以同一 id、同一文字重新入库(确定性 id)→ 现读与快照相同 → 通过。"""
    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")}, fingerprints={},
    )
    service.ask.on_run = lambda fake: service.test_fingerprints.update(
        {"e-b-1": ("s-b", "原文")}
    )

    result = _run(service)

    assert result.answer.citations[0].verification is None
    assert _check(result) is None


def test_a_terminal_read_failure_marks_unverifiable_and_still_delivers(service):
    def broken(ids):
        raise RuntimeError("database went away")

    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")}, fingerprints={},
    )
    service.sources.evidence_fingerprints = broken

    result = _run(service)

    _assert_delivered(result)
    assert result.answer.citations[0].verification == "unverifiable"
    [failure] = _events(service, "global_ask_citation_check_read_failed")
    assert failure == {
        "kind": "global_ask_citation_check_read_failed",
        "read": "fingerprints", "error_type": "RuntimeError",
    }


def _passage_service(service, *, evidence, fingerprints):
    """被引段落跨两个 element:引用只带首个,分组说出第二个。

    ``build_chunks`` 把碎元素合成 ~600 字,所以多数 chunk 跨多个元素;
    ``evidence_context.chunk_citations`` 只把 ``element_ids[0]`` 写进
    ``Citation.element_id``。第二个 element 的原文同样进了提示词、同样支撑了
    答案,复核却看不见它——这正是分组接缝要补的那一半。
    """
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES)], citations=[_citation("b")],
        evidence=evidence, evidence_groups=[[("e-b-1", "e-b-2")]],
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints.clear()
    service.test_fingerprints.update(fingerprints)


def test_a_passage_whose_second_element_changed_is_marked_changed(service):
    """被引段落的第二个 element 在答案产生后被改 → 这条引用「原文已改动」。"""
    _passage_service(
        service,
        evidence={"e-b-1": ("s-b", "前半"), "e-b-2": ("s-b", "后半")},
        fingerprints={"e-b-1": ("s-b", "前半"), "e-b-2": ("s-b", "改过的后半")},
    )

    result = _run(service)

    _assert_delivered(result)
    assert result.answer.citations[0].verification == "changed"


def test_a_passage_whose_second_element_was_deleted_is_marked_changed(service):
    """段落的一部分没了、被引元素还在:读者看到的那段原文变了。"""
    _passage_service(
        service,
        evidence={"e-b-1": ("s-b", "前半"), "e-b-2": ("s-b", "后半")},
        fingerprints={"e-b-1": ("s-b", "前半")},
    )

    result = _run(service)

    assert result.answer.citations[0].verification == "changed"


def test_a_passage_whose_second_element_is_unreadable_is_unverifiable(service):
    _passage_service(
        service,
        evidence={"e-b-1": ("s-b", "前半"), "e-b-2": None},
        fingerprints={"e-b-1": ("s-b", "前半"), "e-b-2": ("s-b", "后半")},
    )

    result = _run(service)

    assert result.answer.citations[0].verification == "unverifiable"
    assert _events(service, "global_ask_citations_partial")[-1]["reasons"] == {
        "unreadable": 1,
    }


def test_a_passage_whose_elements_all_still_hold_is_grounded(service):
    """对照臂:整段每一个 element 都没动 → 照常交付、无标记。"""
    _passage_service(
        service,
        evidence={"e-b-1": ("s-b", "前半"), "e-b-2": ("s-b", "后半")},
        fingerprints={"e-b-1": ("s-b", "前半"), "e-b-2": ("s-b", "后半")},
    )

    result = _run(service)

    assert result.answer.grounded is True
    assert [row.element_id for row in result.answer.citations] == ["e-b-1"]
    assert _check(result) is None


def test_a_single_element_passage_is_checked_exactly_as_before(service):
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES)], citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")}, evidence_groups=[[("e-b-1",)]],
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints.clear()
    service.test_fingerprints["e-b-1"] = ("s-b", "原文")

    result = _run(service)

    assert result.answer.grounded is True
    assert [row.element_id for row in result.answer.citations] == ["e-b-1"]


def test_an_unregistered_dangling_id_reaching_the_check_is_unverifiable(service):
    """J2 is applied by the PRODUCERS: an element id already dangling before the
    question produces no card (``attest_pointers`` answers ``dead``; the
    single-notebook half is ``reference_liveness``). This pins the consumer's
    side for an id that reached it anyway, e.g. from a producer that registers
    nothing: with no retrieval-time snapshot it is not "deleted during the
    answer", so the check says ``unverifiable`` -- never ``source_gone`` or
    ``changed`` -- and the answer text is untouched.
    """
    service.ask = _FakeAsk(
        rounds=[[]], citations=[_citation("b")], evidence=None,
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints.clear()

    result = _run(service)

    _assert_delivered(result)
    assert [row.element_id for row in result.answer.citations] == ["e-b-1"]
    assert result.answer.citations[0].verification == "unverifiable"


def test_every_citation_names_its_notebook(service):
    """含名义 active:对等模式下每条引用都带真实归属。"""
    for nid in _LIBRARIES:
        service.test_visible[nid] = {f"s-{nid}"}
        service.test_fingerprints[f"e-{nid}-1"] = (f"s-{nid}", "原文")
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES)],
        citations=[_citation(nid) for nid in _LIBRARIES],
        evidence={f"e-{nid}-1": (f"s-{nid}", "原文") for nid in _LIBRARIES},
    )

    result = _run(service)

    assert [row.notebook_id for row in result.answer.citations] == list(_LIBRARIES)
    assert result.cited_notebook_ids == list(_LIBRARIES)
    assert _check(result) is None


def test_a_pointer_producer_attests_through_the_run_seat(service):
    """``GlobalAskService`` installs the attestation seat around the engine with
    the wired reader: a producer deep inside the run registers a pointer with
    ONE read, and that snapshot is what the terminal check compares against."""
    from app.services.evidence_attestation import attest_pointers

    seen: list = []

    def produce(fake):
        seen.append(attest_pointers("kg_objects", ["e-b-1", "e-b-gone"]))
        # Edited after retrieval, before the terminal read.
        service.test_fingerprints["e-b-1"] = ("s-b", "改过")

    _grounded_service(
        service, citations=[_citation("b")], evidence=None,
        fingerprints={"e-b-1": ("s-b", "原文")},
    )
    service.ask.on_run = produce
    reads: list = []
    reader = service.sources.evidence_fingerprints
    service.evidence_reader = SimpleNamespace(
        evidence_fingerprints=lambda ids: reads.append(tuple(ids)) or reader(ids),
    )

    result = _run(service)

    assert seen == [{"e-b-1": "live", "e-b-gone": "dead"}]
    assert reads == [("e-b-1", "e-b-gone")]
    assert result.answer.citations[0].verification == "changed"
    [attested] = _events(service, "producer_evidence_attested")
    assert attested["producer"] == "kg_objects" and attested["live"] == 1


def test_a_blind_pointer_read_cannot_launder_a_declared_failure(service):
    """The federated channel read E, then the source was re-ingested (same
    element id, new text): its snapshot read declared E ``None``. A producer
    later attests E by pointer and reads the NEW text. That snapshot must not
    replace the ``None`` -- otherwise the terminal check compares new with new
    and passes while the card shows the old excerpt. Two guards hold here:
    the run already registered E, so the pointer producer is told ``attested``
    and reads nothing; and even a pointer snapshot could not replace the
    ``None`` (``test_a_pointer_read_does_not_replace_a_declared_none`` pins that
    merge rule alone). The verdict is
    ``unverifiable`` (internal ``unreadable``), not ``changed``: there is no
    trustworthy "before" to compare, and only a real snapshot mismatch may be
    called a change (J1)."""
    from app.services.evidence_attestation import attest_pointers

    def produce(fake):
        attest_pointers("kg_objects", ["e-b-1"])

    _grounded_service(
        service, citations=[_citation("b")], evidence={"e-b-1": None},
        fingerprints={"e-b-1": ("s-b", "重新入库后的新文字")},
    )
    service.ask.on_run = produce

    result = _run(service)

    _assert_delivered(result)
    assert result.answer.citations[0].verification == "unverifiable"
    assert _events(service, "global_ask_citations_partial")[-1]["reasons"] == {
        "unreadable": 1,
    }


def test_a_flagged_citation_is_refused_by_the_service_with_one_job_read(service):
    """One gate, one read: ``cited_element`` refuses a flagged citation right
    after its own ``get_job``; the API layers only translate."""
    from app.models.global_ask import FLAGGED_CITATION_MESSAGE

    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")}, fingerprints={},
    )
    result = _run(service)
    assert result.answer.citations[0].verification == "source_gone"
    reads: list = []
    original = service.store.job
    service.store.job = lambda *args: reads.append(args) or original(*args)

    with pytest.raises(GlobalAskError) as refused:
        service.cited_element(result.job_id, "e-b-1", user_id="u")

    assert refused.value.status_code == 404
    assert refused.value.message == FLAGGED_CITATION_MESSAGE
    assert len(reads) == 1


def test_reasoning_appends_a_truthful_check_step(service):
    """reasoning 轨迹末尾追加「核对」一步,如实写核对条数与未通过条数。"""
    _grounded_service(
        service, citations=[_citation("b", 1), _citation("b", 2)],
        evidence={"e-b-1": ("s-b", "一"), "e-b-2": ("s-b", "二")},
        fingerprints={"e-b-1": ("s-b", "一")},
        trace=[TraceStep(step_type="plan", summary="规划")],
    )

    result = _run(service, mode="reasoning")

    step = result.answer.reasoning_trace[-1]
    assert step.step_type == "citation_check"
    assert step.summary == "核对引用：共核对 2 条，1 条未通过（1 条资料已删除）"
    assert step.detail == {
        "checked": 2, "failed": 1, "changed": 0, "source_gone": 1,
        "unverifiable": 0,
    }


def test_a_chunk_run_gets_no_check_step(service):
    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")}, fingerprints={},
    )

    result = _run(service)

    assert not result.answer.reasoning_trace


def test_a_partially_failed_answer_still_feeds_the_learning_chain(service):
    notes: list = []
    service.note_ask_completed = lambda ids, user, mode, **kw: notes.append(ids)
    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")}, fingerprints={},
    )

    result = _run(service)

    assert _check(result)["failed"] == 1
    deadline = time.monotonic() + 5
    while not notes and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert notes and "b" in notes[0]


# ---------------------------------------------------------------------------
# 7. 入口校验
# ---------------------------------------------------------------------------

def test_unknown_mode_is_422(service):
    with pytest.raises(GlobalAskError) as error:
        service.start(GlobalAskRequest(question="q", mode="telepathy"),
                      user_id="u")
    assert error.value.status_code == 422
    assert error.value.message == "不支持的问答引擎，请刷新页面后重试。"


def test_plugin_engine_mode_is_rejected(service):
    from app.domain.ask import AskMode

    service.ask = _FakeAsk(extension_modes=(
        AskMode("vendor", "ask_plugin_engine", "general", False, False, True),
    ))
    with pytest.raises(GlobalAskError) as error:
        service.start(GlobalAskRequest(question="q", mode="vendor"),
                      user_id="u")
    assert error.value.status_code == 422
    assert "全局问答暂不支持该引擎" in error.value.message


def test_a_retired_mode_alias_still_resolves(service):
    """退役别名归一,不 422:旧标签页/旧书签提交的 ``global`` 仍然能作答。"""
    service.ask = _FakeAsk(rounds=[_ok(*_LIBRARIES)])
    result = _run(service, mode="global")
    assert result.status == "done" and result.mode == "chunk"


def test_deep_effort_is_clamped_to_standard(service):
    """``deep`` 的轮数上限 × 8 库放不进阶段时限,所以 v1 按 ``standard`` 作答。"""
    fake = _FakeAsk(rounds=[_ok(*_LIBRARIES)])
    service.ask = fake

    result = _run(service, mode="reasoning", retrieval_effort="deep")

    assert result.retrieval_effort == "standard"
    assert result.answer.retrieval_effort == "standard"


def test_a_confirmed_intent_reaches_the_engine(service):
    """``intent`` 是 run 输入,不是作业字段,但它必须真的到引擎手里。

    不落进 ``GlobalAskJob``:那是响应体,而同一个契约已经由
    ``answer.intent`` 发布;重复一份只会把 ``AskIntentConfirmation`` 在整份公开
    OpenAPI 里劈成 Input/Output 两个 schema 名。所以这条用例钉的是另一半——
    「不落库」不等于「被丢掉」。
    """
    from app.models.ask import AskIntentConfirmation

    payloads: list = []
    fake = _FakeAsk(rounds=[_ok(*_LIBRARIES)])
    original = fake.ask

    def capture(notebook_id, payload, **kwargs):
        payloads.append(payload)
        return original(notebook_id, payload, **kwargs)

    fake.ask = capture
    service.ask = fake
    confirmed = AskIntentConfirmation(
        contract={
            "objective": "低温性能如何", "resolved_question": "低温性能如何",
            "topics": [], "ambiguities": [],
            "needs_clarification": False, "confirmed": True,
        },
        resolved_question="低温性能如何",
    )

    result = _run(service, question="低温性能如何", mode="reasoning",
                  intent=confirmed)

    assert result.status == "done"
    assert payloads[0].intent is not None
    assert payloads[0].intent.resolved_question == "低温性能如何"
    # 作业行本身不带这个字段。
    assert "intent" not in result.model_dump()


def test_retry_with_legacy_request_json_is_idempotent(service):
    """升级前写下的请求串(没有 mode/intent/retrieval_effort)仍然命中幂等。

    逐字比原始串会让**每一次加字段**都变成一次追溯性的幂等失效:客户端用同一个
    ``client_request_id`` 诚实重试同一个问题,却被告知这个 id 属于别的问题,而
    它自己修不了——差的是一个它当初根本没有的字段。
    """
    service.ask = _FakeAsk(rounds=[_ok(*_LIBRARIES)])
    payload = GlobalAskRequest(question="比较一下", client_request_id="once")
    job = _finished(service, service.start(payload, user_id="u"))
    # 把库里的请求串改写成升级前的形状。
    _rewrite_stored_request(
        service, "once", '{"question":"比较一下","notebook_scope":null,'
        '"conversation_id":null,"client_request_id":"once"}',
    )

    again = service.start(payload, user_id="u")

    assert again.job_id == job.job_id


def test_a_retry_with_a_regenerated_intent_is_the_same_request(service):
    """确认过的 ``intent`` 不属于请求身份。

    浏览器与 MCP 的每一次重试都会重新跑一遍问题理解,两次确认至少
    ``understanding_ms`` 不同;把它算进身份,幂等要保护的那次重试就成了永久 409
    (codex #755 第 1 轮 P2 ×2)。
    """
    from app.models.ask import AskIntentConfirmation, QueryIntentContract

    def _confirmed(ms):
        return AskIntentConfirmation(
            contract=QueryIntentContract(
                objective="比较一下", resolved_question="比较一下",
                needs_clarification=False,
            ),
            resolved_question="比较一下", answers=[], understanding_ms=ms,
        )

    service.ask = _FakeAsk(rounds=[_ok(*_LIBRARIES)])
    first = GlobalAskRequest(
        question="比较一下", client_request_id="once", mode="reasoning",
        intent=_confirmed(1200),
    )
    job = _finished(service, service.start(first, user_id="u"))

    again = service.start(
        first.model_copy(update={"intent": _confirmed(3400)}), user_id="u",
    )
    assert again.job_id == job.job_id
    # 对照臂:引擎是用户选的,属于身份。
    with pytest.raises(GlobalAskError) as error:
        service.start(first.model_copy(update={"mode": "chunk", "intent": None}),
                      user_id="u")
    assert error.value.status_code == 409


def test_a_genuinely_different_question_still_conflicts(service):
    """对照臂:归一不是放水——同一个 id 换个问题仍然 409。"""
    service.ask = _FakeAsk(rounds=[_ok(*_LIBRARIES)])
    payload = GlobalAskRequest(question="比较一下", client_request_id="once")
    _finished(service, service.start(payload, user_id="u"))

    with pytest.raises(GlobalAskError) as error:
        service.start(payload.model_copy(update={"question": "别的问题"}),
                      user_id="u")
    assert error.value.status_code == 409


def _rewrite_stored_request(service, client_request_id, raw):
    database = service.store.database
    with database.write() as db:
        db.execute(
            "UPDATE global_ask_jobs SET request_json=? "
            "WHERE client_request_id=?",
            (raw, client_request_id),
        )


# ---------------------------------------------------------------------------
# 8. 端到端:真库、真引擎、真联邦检索
# ---------------------------------------------------------------------------

def test_end_to_end_answer_cites_more_than_one_library(tmp_path, monkeypatch):
    """三个有内容的库、真 ``AskService``、真联邦检索:引用跨库。

    这是整条接缝唯一一条不替任何生产件的用例:模型是假的(仓库既有的测试件),
    检索、参与集覆盖、逐库天花板、合并与引用装配全是生产代码。
    """
    import json

    from app.models.notebooks import NotebookCreate
    from app.repositories.ports import UploadedSourceFile
    from app.services.embedding import FakeEmbedder
    from tests.model_testkit import (
        RecordingModelProvider, bind_all_embedding_clients, bind_chat_client,
    )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'e2e.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    # Every participant counts as a LARGE library, which is the shape the
    # cold-load promise is about: ``_peek_only`` must hold for all of them --
    # the nominal active included, because a peer run has no "the library the
    # user is actually using" to make an exception for.
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "0")
    repo = SQLiteRepository(Settings(), model_provider=RecordingModelProvider())
    try:
        bind_all_embedding_clients(repo, FakeEmbedder(dim=16))

        class _Answering:
            configured = True
            model = "fake"

            def chat_json(self, messages, schema_hint, **kwargs):
                if "sub_queries" in schema_hint:
                    return json.dumps({"sub_queries": [{"query": "低温性能"}]})
                prompt = "\n".join(
                    str(row.get("content", "")) for row in messages
                )
                keys = sorted(set(
                    part.split("]")[0]
                    for part in prompt.split("[k")[1:] if "]" in part
                ))
                return json.dumps({
                    "conclusion": "三个库都提到了低温性能。",
                    "answer": "三个库都提到了低温性能。" + "".join(
                        f"[k{key}]" for key in keys[:3]
                    ),
                    "anchors": [], "grounded": True,
                })

        for workload in ("ask_answer", "query_rewrite", "reasoning_agent"):
            bind_chat_client(repo, workload, _Answering())

        notebooks = []
        for index in range(3):
            notebook = repo.create_notebook(NotebookCreate(name=f"库{index}"))
            source = repo.upload_sources(notebook.id, [UploadedSourceFile(
                file_name=f"手册{index}.md",
                content_type="text/markdown",
                content=(
                    f"# 手册{index}\n\n"
                    f"## 低温性能\n\n"
                    f"本器件在低温下的增益随温度下降而上升，实测在零下四十度"
                    f"仍然满足指标{index}。低温性能是第{index}章的主题。"
                ).encode("utf-8"),
            )], scheduler=lambda _source_id: None)[0]
            repo.process_source(source.id)
            notebooks.append(notebook)
        user_id = repo.current_user().id

        candidates = repo._runtime.ask_service().candidates.candidates
        loads: list = []
        original_scale_index = candidates._scale_index

        def _record_scale_index(notebook_id, allow_stale=False):
            loads.append(notebook_id)
            return original_scale_index(notebook_id, allow_stale=allow_stale)

        monkeypatch.setattr(candidates, "_scale_index", _record_scale_index)

        service = repo._runtime.global_ask_service()
        job = service.start(
            GlobalAskRequest(
                question="低温性能如何",
                notebook_scope={"mode": "include",
                                "notebook_ids": [nb.id for nb in notebooks]},
            ),
            user_id=user_id,
        )
        deadline = time.monotonic() + 30
        while job.job_id in service._events and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        result = service.get_job(job.job_id, user_id=user_id)

        assert result.status == "done", result.error
        assert result.answer is not None
        origins = {row.notebook_id for row in result.answer.citations}
        assert len(origins) > 1, [row.model_dump() for row in result.answer.citations]
        assert origins <= {nb.id for nb in notebooks}
        # 每条引用都带归属,含名义 active。
        assert all(row.notebook_id for row in result.answer.citations)
        assert set(result.cited_notebook_ids) == origins
        assert result.searched_notebook_ids == [nb.id for nb in notebooks]
        # 八库 run 不得冷加载任何共享 scale 索引(``_peek_only`` 对全部腿生效)。
        assert loads == []
    finally:
        repo.close()


def _e2e_repo(tmp_path, monkeypatch, *, answer):
    """真仓库 + 假模型,三个有内容的库。返回 ``(repo, notebooks, user_id)``。"""
    import json

    from app.models.notebooks import NotebookCreate
    from app.repositories.ports import UploadedSourceFile
    from app.services.embedding import FakeEmbedder
    from tests.model_testkit import (
        RecordingModelProvider, bind_all_embedding_clients, bind_chat_client,
    )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'e2e.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(), model_provider=RecordingModelProvider())
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))

    class _Answering:
        configured = True
        model = "fake"

        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "文档"}]})
            prompt = "\n".join(str(row.get("content", "")) for row in messages)
            keys = sorted(set(
                part.split("]")[0]
                for part in prompt.split("[k")[1:] if "]" in part
            ))
            return json.dumps({
                "conclusion": answer,
                "answer": answer + "".join(f"[k{key}]" for key in keys[:4]),
                "anchors": [], "grounded": True,
                "summary": answer, "sections": [],
            })

    for workload in ("ask_answer", "query_rewrite", "reasoning_agent",
                     "evidence_refine", "source_summary"):
        bind_chat_client(repo, workload, _Answering())

    notebooks = []
    for index in range(3):
        notebook = repo.create_notebook(NotebookCreate(name=f"库{index}"))
        source = repo.upload_sources(notebook.id, [UploadedSourceFile(
            file_name=f"手册{index}.md",
            content_type="text/markdown",
            content=(
                f"# 手册{index}\n\n## 低温性能\n\n"
                f"本器件在低温下的增益随温度下降而上升，实测在零下四十度"
                f"仍然满足指标{index}。低温性能是第{index}章的主题。"
            ).encode("utf-8"),
        )], scheduler=lambda _source_id: None)[0]
        repo.process_source(source.id)
        notebooks.append(notebook)
    return repo, notebooks, repo.current_user().id


def _e2e_answer(repo, notebooks, user_id, question, **kwargs):
    service = repo._runtime.global_ask_service()
    job = service.start(
        GlobalAskRequest(
            question=question,
            notebook_scope={"mode": "include",
                            "notebook_ids": [nb.id for nb in notebooks]},
            **kwargs,
        ),
        user_id=user_id,
    )
    deadline = time.monotonic() + 40
    while job.job_id in service._events and time.monotonic() < deadline:
        threading.Event().wait(0.02)
    return service.get_job(job.job_id, user_id=user_id)


def test_end_to_end_document_overview_is_not_voided(tmp_path, monkeypatch):
    """文档概览:真库、真引擎、一次联邦调用都没有,答案必须**不**被作废。

    这条路的引用是真的 ``source_elements`` 行,却从不经过联邦 chunk 通道,所以
    累积指纹表里没有它们。把缺席当拒绝,就是把「我的库里有哪些文档」这类答案
    100% 判成「引用原文发生了变化」。
    """
    repo, notebooks, user_id = _e2e_repo(
        tmp_path, monkeypatch, answer="这些库里有三篇手册。",
    )
    try:
        result = _e2e_answer(
            repo, notebooks, user_id, "介绍一下这个notebook中的文章",
        )

        assert result.status == "done", result.error
        assert "引用原文在回答期间发生了变化" not in result.answer.answer
        assert result.answer.citations, "文档概览必须交出引用"
        origins = {row.notebook_id for row in result.answer.citations}
        assert all(row.notebook_id for row in result.answer.citations)
        assert origins <= {nb.id for nb in notebooks}
        # 名义 active 自己的引用也带归属。
        assert notebooks[0].id in origins
        # 一次联邦调用都没有的 run 不诬告任何库。
        assert result.searched_notebook_ids == [nb.id for nb in notebooks]
        assert result.skipped_notebooks == []
    finally:
        repo.close()


def test_end_to_end_a_source_reparsed_mid_run_is_unverifiable_not_self_compared(
    tmp_path, monkeypatch,
):
    """真库真引擎:检索腿读完原文、快照读之前来源被重新解析 → 引用「无法核对」。

    元素 id 是确定性复用的(``el-<来源>-<序号>``),所以重新解析之后**同一个 id
    下已经是新文字**。若检索时刻的指纹是按 id 单独读的,读到的就是新文字的指纹,
    终态复核再读一次也是新文字——两份新指纹自比恒等,一份基于已经不存在的原文
    写出来的答案会被当成有据发布。段落快照把原文摘要与元素指纹钉在同一个数据库
    快照里,所以这里必须是 ``unreadable``(线上「无法核对」),而不是 grounded;
    回答本身照常交付(Q3)。

    握手不靠计时:重写就发生在快照读这个调用里,在读之前。
    """
    repo, notebooks, user_id = _e2e_repo(
        tmp_path, monkeypatch, answer="三个库都提到了低温性能。",
    )
    try:
        service = repo._runtime.global_ask_service()
        sources = service.sources
        original = sources.passage_evidence_snapshot
        reparsed: list = []

        def reparse_then_read(chunk_ids):
            # 只在第一次快照读之前动手:模拟「检索腿已经把原文读走了,来源随后被
            # 重新解析」,新文字仍然挂在同一批 id 下。
            if not reparsed:
                reparsed.append(tuple(chunk_ids))
                with sources.database.write() as db:
                    for chunk_id in chunk_ids:
                        db.execute(
                            "UPDATE chunks SET text=? WHERE id=?",
                            (f"重新解析后的正文 {chunk_id}", chunk_id),
                        )
                    db.execute(
                        "UPDATE source_elements SET text=text||'（重新解析）'"
                    )
            return original(chunk_ids)

        monkeypatch.setattr(
            sources, "passage_evidence_snapshot", reparse_then_read,
        )

        result = _e2e_answer(repo, notebooks, user_id, "低温性能如何")

        assert result.status == "done", result.error
        assert reparsed, "这次 run 根本没走联邦原文通道,用例什么也没证明"
        assert result.answer.grounded is False
        assert result.answer.answer.startswith("三个库都提到了低温性能。")
        assert result.answer.citations
        # Every federated citation is refused by name, as unreadable -- none is
        # called "changed", which only a real snapshot mismatch may say.
        assert {row.verification for row in result.answer.citations} == {"unverifiable"}
        check = result.answer.citation_check
        assert check.failed == check.unverifiable == check.checked
        assert result.cited_notebook_ids
    finally:
        repo.close()


@pytest.mark.parametrize("mutation,expected", [
    ("update", "changed"),
    ("delete", "source_gone"),
    ("reinsert", None),
])
def test_end_to_end_federated_passage_race_before_the_terminal_read(
    tmp_path, monkeypatch, mutation, expected,
):
    """真库真引擎、联邦原文通道:答案写完、终态读之前改 / 删 / 同文重插被引元素。

    检索时刻的段落快照是真的(同一条语句里摘要与元素指纹);终态读按 id 现读。
    改 → 原文已改动;删 → 资料已删除;同 id 同文字重插(确定性 id 的重新入库)
    → 不算失败。任何一种都照常交付正文。握手不靠计时:改动就发生在终态读这个
    调用里,在读之前。PostgreSQL 孪生见 ``tests/postgres/test_global_citation_race_pg.py``。
    """
    from app.services import global_citation_check as check_module

    repo, notebooks, user_id = _e2e_repo(
        tmp_path, monkeypatch, answer="三个库都提到了低温性能。",
    )
    try:
        database = repo._runtime.global_ask_service().sources.database
        original = check_module.GlobalCitationCheck._read_current
        touched: list = []

        def mutate_then_read(self, references, siblings, event):
            if not touched:
                touched.extend(sorted({key[2] for key in references if key[2]}))
                marks = ",".join("?" for _ in touched)
                with database.write() as db:
                    if mutation == "update":
                        db.execute(
                            "UPDATE source_elements SET text=text||'（改）' "
                            f"WHERE id IN ({marks})", touched,
                        )
                    else:
                        rows = [dict(row) for row in db.execute(
                            f"SELECT * FROM source_elements WHERE id IN ({marks})",
                            touched,
                        ).fetchall()]
                        db.execute(
                            f"DELETE FROM source_elements WHERE id IN ({marks})",
                            touched,
                        )
                        if mutation == "reinsert":
                            for row in rows:
                                columns = ",".join(row)
                                db.execute(
                                    f"INSERT INTO source_elements({columns}) "
                                    f"VALUES({','.join('?' for _ in row)})",
                                    list(row.values()),
                                )
            return original(self, references, siblings, event)

        monkeypatch.setattr(
            check_module.GlobalCitationCheck, "_read_current", mutate_then_read,
        )

        result = _e2e_answer(repo, notebooks, user_id, "低温性能如何")

        assert result.status == "done", result.error
        assert touched, "终态读没有任何被引元素,用例什么也没证明"
        assert result.answer.answer.startswith("三个库都提到了低温性能。")
        cited = [
            row for row in [*result.answer.citations, *result.answer.anchors]
            if row.element_id in touched
        ]
        assert cited
        assert {row.verification for row in cited} == {expected}
        if expected is None:
            assert result.answer.citation_check is None
        else:
            assert getattr(result.answer.citation_check, expected) == len(touched)
            assert result.answer.grounded is False
    finally:
        repo.close()


def test_end_to_end_reasoning_enumeration_is_not_voided(tmp_path, monkeypatch):
    """集合枚举(reasoning)同形:引用来自枚举行,不经联邦通道,不得被作废。"""
    repo, notebooks, user_id = _e2e_repo(
        tmp_path, monkeypatch, answer="这些库里有三篇手册。",
    )
    try:
        result = _e2e_answer(
            repo, notebooks, user_id, "介绍一下这个notebook中的文章",
            mode="reasoning",
        )

        assert result.status == "done", result.error
        assert "引用原文在回答期间发生了变化" not in result.answer.answer
        assert all(row.notebook_id for row in result.answer.citations)
        # 对等模式关掉了单库元素腿:轨迹里不该出现一次真的 search_elements。
        trace = result.answer.reasoning_trace or []
        assert not [
            step for step in trace
            if step.step_type == "fallback" and "降级查原文" in step.summary
        ]
    finally:
        repo.close()
