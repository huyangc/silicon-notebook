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
  · 引用冻结复核改吃 ``response.citations`` + 累积指纹表,失效时整份作废;
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
                 trace=(), extension_modes=(), on_run=None):
        # 每一轮 = 一次联邦调用,内容是 ``[(notebook_id, LibraryOutcome), ...]``。
        self.rounds = [list(row) for row in rounds]
        self.citations = list(citations)
        self.evidence_rounds = evidence
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
                plan.on_evidence(self._evidence_for(index))
            if self.on_run is not None:
                self.on_run(self)
            if cancel_event is not None and cancel_event.is_set():
                raise AskCancelled()
            return AskResponse(
                answer_id="", conclusion="结论", answer="答案",
                grounded=bool(self.citations), mode=payload.mode,
                citations=list(self.citations),
                conversation_id=self.seen_turn.conversation_id,
                reasoning_trace=list(self.trace) or None,
            )

    def _evidence_for(self, index):
        if self.evidence_rounds is None:
            return {}
        if isinstance(self.evidence_rounds, dict):
            return self.evidence_rounds if index == 0 else {}
        return self.evidence_rounds[index]


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
    built = GlobalAskService(
        store=repo._runtime.global_ask_store,
        notebooks=lambda user: [SimpleNamespace(id=nb, name=nb.upper())
                                for nb in sorted(readable)],
        can_read=lambda nb, user: nb in readable and user == "u",
        can_read_many=lambda ids, user: [nb for nb in ids if nb in readable],
        sources=sources, settings=settings, ask=_FakeAsk(),
    )
    built.test_visible = visible
    built.test_fingerprints = fingerprints
    built.test_readable = readable
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


def test_reasoning_mode_streams_trace_into_job(service):
    steps = [TraceStep(step_type="plan", summary="拆解问题"),
             TraceStep(step_type="search", summary="检索")]
    seen: list = []
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES)], trace=steps,
        on_run=lambda fake: seen.append(
            [row.summary for row in
             service.store.job(_current_job_id(service), "u").trace]
        ),
    )

    result = _run(service, mode="reasoning")

    assert result.status == "done"
    # 边跑边可见:引擎还没返回时,轮询就已经读得到两步。
    assert seen == [["拆解问题", "检索"]]
    # 终态清空,权威在 ``answer.reasoning_trace``,避免 payload 翻倍。
    assert result.trace == []
    assert [row.summary for row in result.answer.reasoning_trace] == [
        "拆解问题", "检索",
    ]


def _current_job_id(service):
    with service._lock:
        return next(iter(service._events))


# ---------------------------------------------------------------------------
# 2. detached:答案绝不进任何笔记本的 answers 表
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["chunk", "reasoning"])
def test_answers_table_is_never_written(tmp_path, mode):
    """真 ``AskService``,桩 ``ask_state``:引擎走完一整轮也不碰它。

    这条用例故意不用假引擎——它要证明的正是**生产的** ``_prepare_turn`` /
    ``_save_answer`` 在 detached turn 下的行为,假引擎证明不了。
    """
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'g.db'}",
        storage_dir=str(tmp_path / "s"),
    )
    repo = SQLiteRepository(settings)
    try:
        from app.models.notebooks import NotebookCreate

        notebooks = [repo.create_notebook(NotebookCreate(name=f"库{i}"))
                     for i in range(2)]
        user_id = repo.current_user().id
        service = repo._runtime.global_ask_service()
        trap = _WriteTrap()
        service.ask.ask_state = trap
        job = service.start(
            GlobalAskRequest(
                question="低温性能在这几个库里分别是怎么写的", mode=mode,
                notebook_scope={"mode": "include",
                                "notebook_ids": [nb.id for nb in notebooks]},
            ),
            user_id=user_id,
        )
        deadline = time.monotonic() + 10
        while job.job_id in service._events and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        result = service.get_job(job.job_id, user_id=user_id)
        assert result.status in ("done", "failed")
        assert trap.touched == []
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
        original_validate = service._validate_citations

        def revoke_after(*args, **kwargs):
            outcome = original_validate(*args, **kwargs)
            allowed[0] = []
            return outcome

        service._validate_citations = revoke_after

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
# 6. 引用冻结复核
# ---------------------------------------------------------------------------

def _grounded_service(service, *, citations, evidence, fingerprints):
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES)], citations=citations, evidence=evidence,
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints.clear()
    service.test_fingerprints.update(fingerprints)


def test_changed_evidence_returns_retry_copy(service):
    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")},
        fingerprints={"e-b-1": ("s-b", "改过的原文")},
    )

    result = _run(service)

    assert result.status == "done"
    assert result.answer.grounded is False
    assert result.answer.citations == []
    assert result.answer.answer == (
        "引用原文在回答期间发生了变化，暂时无法提供可靠结论，请重新提问。"
    )
    assert result.answer.conclusion == result.answer.answer
    # 读者看到的徽章是 ``evidence_level``,不是 ``grounded``。
    assert result.answer.evidence_level == "inferred"
    assert result.answer.anchors == []
    assert result.cited_notebook_ids == []


def test_a_missing_fingerprint_is_refused_not_accepted(service):
    """第 2 轮指纹读失败 → 只在那一轮出现的 element 没有「之前」 → 整份作废。

    ``_report_evidence`` 读失败时按契约回传**空表**,正是为了让复核拒绝而不是
    放行未经核对的引用。所以「累积表非空」不能当作放行理由:判据是**这一条**
    引用的 element 在不在表里。
    """
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES), _ok(*_LIBRARIES)],
        citations=[_citation("b", 2)],
        evidence=[{"e-b-1": ("s-b", "原文")}, {}],
    )
    service.test_visible["b"] = {"s-b"}
    service.test_fingerprints.update({
        "e-b-1": ("s-b", "原文"), "e-b-2": ("s-b", "第二轮原文"),
    })

    result = _run(service, mode="reasoning")

    assert result.answer.citations == []
    assert "变化" in result.answer.answer


def test_a_source_that_left_the_frozen_ceiling_voids_the_answer(service):
    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")},
        fingerprints={"e-b-1": ("s-b", "原文")},
    )
    # 天花板在 start 时按当时可见来源冻结;之后把来源藏起来。
    service.test_visible["b"] = set()

    result = _run(service)

    assert result.answer.citations == []
    assert "变化" in result.answer.answer


def test_a_citation_without_a_source_element_is_held_to_the_ceiling_only(service):
    """KG 对象 / 文档概述锚点没有 element 指纹,只过「来源仍在天花板且可见」。

    它们的 element id 不是 ``source_elements`` 的行,活体读回来也是空——正因为
    如此,判「是不是段落引用」用的是**活体读**而不是快照:一个检索时存在、现在
    被删掉的 element 在快照里有、活体读没有,于是被拒绝,而不是被误认成图对象。
    """
    _grounded_service(
        service, citations=[_citation("b")], evidence={}, fingerprints={},
    )

    result = _run(service)

    assert result.answer.grounded is True
    assert [row.notebook_id for row in result.answer.citations] == ["b"]


def test_a_deleted_element_is_refused_not_mistaken_for_a_graph_object(service):
    _grounded_service(
        service, citations=[_citation("b")],
        evidence={"e-b-1": ("s-b", "原文")}, fingerprints={},
    )

    result = _run(service)

    assert result.answer.citations == []
    assert "变化" in result.answer.answer


def test_every_citation_names_its_notebook(service):
    """含名义 active:对等模式下每条引用都带真实归属。

    归属为空意味着某个归一点漏改了,而一条无归属的引用没法对任何一个库的天花板
    复核——所以它是 fail-closed 的整份作废,不是「少显示一个徽章」。
    """
    for nid in _LIBRARIES:
        service.test_visible[nid] = {f"s-{nid}"}
    service.ask = _FakeAsk(
        rounds=[_ok(*_LIBRARIES)],
        citations=[_citation(nid) for nid in _LIBRARIES], evidence={},
    )

    result = _run(service)

    assert [row.notebook_id for row in result.answer.citations] == list(_LIBRARIES)
    assert result.cited_notebook_ids == list(_LIBRARIES)


def test_a_blank_citation_origin_voids_the_answer(service):
    blank = _citation("b").model_copy(update={"notebook_id": ""})
    _grounded_service(
        service, citations=[blank], evidence={}, fingerprints={},
    )

    result = _run(service)

    assert result.answer.citations == []
    assert "变化" in result.answer.answer


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
