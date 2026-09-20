"""全局问答借给联邦通道的**进程级检索预算**,以及参与集上限。

D1-4 之前这个文件钉的是 ``global_ask`` 自己的扇出:并发重叠、worker 继承 run
上下文、逐库预算起算点、槽位释放、``queue_deadline`` 原因码、逐库回执顺序。那条
扇出已经不存在——检索由 ``AskService.ask`` 内部的联邦通道做,上述性质现在由
``tests/test_federated_global_budget.py`` 在它真正的宿主上钉(握手、不看墙钟)。

留在这一层的只有两件仍然由作业层拥有的事:

  ① 参与集上限是产品裁决,不是部署旋钮——超限整条拒绝,不截断;
  ② 一次 run 交给联邦通道的**运行计划**里装的必须是那个**共享**线程池与那个会
     随作业数收缩的公平窗口。这条是连接池上界的唯一真源:每个作业自建一个池,
     4 作业 × 8 worker 就是 32 条数据库连接,PostgreSQL 默认池是 10。
"""
from __future__ import annotations

from threading import Barrier, Event, Thread
from types import SimpleNamespace
import pytest

from app.core.config import Settings
from app.models.global_ask import GlobalAskRequest
from app.services.global_ask import GlobalAskError
from app.services.federated_run import current_federated_run_plan
from tests.test_global_ask import setup, finished, _stage_job_threads


def _libraries(service, readable, names):
    readable.clear()
    readable.update(names)
    return sorted(names)


def test_scope_above_the_product_ceiling_is_rejected_not_truncated(setup):
    service, readable, retrieved, _ = setup
    _libraries(service, readable, {f"library-{index}" for index in range(9)})
    with pytest.raises(GlobalAskError) as error:
        service.start(GlobalAskRequest(question="q"), user_id="u")
    assert error.value.status_code == 422
    assert "8" in error.value.message
    assert not retrieved


@pytest.mark.parametrize("variable,value", [
    ("GLOBAL_ASK_MAX_NOTEBOOKS", "9"),
    ("GLOBAL_ASK_RETRIEVAL_CONCURRENCY", "9"),
    ("GLOBAL_ASK_RETRIEVAL_CONCURRENCY", "0"),
])
def test_over_ceiling_configuration_fails_validation(monkeypatch, variable, value):
    """The participant ceiling is a product decision, not a deployment knob:
    raising it in the environment must fail loudly at startup."""
    monkeypatch.setenv(variable, value)
    with pytest.raises(Exception) as error:
        Settings(_env_file=None)
    assert variable.lower() in str(error.value).lower() or "less than or equal" in str(error.value)


@pytest.mark.parametrize(
    "setup", [{"global_ask_retrieval_concurrency": 4}], indirect=True,
)
def test_the_run_plan_lends_the_shared_pool_and_the_live_window(setup):
    """计划里装的是那**一个**池与那个**会重算**的窗口,不是它们的快照。

    借而不是拥有:联邦通道拿到的是 ``service._retrieval_pool`` 本体,所以
    ``global_ask_retrieval_concurrency`` 仍然是「检索持有的数据库连接」的真实上界。
    窗口是可调用对象而不是一个数,所以别的作业来了、走了,份额跟着变——传一个数
    过去,先起跑的作业就会按「只有我一个」的份额一直占着。
    """
    service, _, _, _ = setup
    seen = {}
    synthesize = service.ask.synthesize

    def capture(question, chunks, names, history, cancel):
        plan = current_federated_run_plan()
        seen["executor"] = plan.executor
        seen["window"] = plan.window
        seen["share_alone"] = plan.window()
        seen["phase"] = plan.phase_timeout_seconds
        seen["notebook"] = plan.notebook_timeout_seconds
        return synthesize(question, chunks, names, history, cancel)

    service.ask.synthesize = capture
    assert finished(
        service, service.start(GlobalAskRequest(question="q"), user_id="u"),
    ).status == "done"

    assert seen["executor"] is service._retrieval_pool
    assert seen["window"] == service._retrieval_window
    # 一个作业在跑 → 全部四个槽位;两个 → 各两个。
    assert seen["share_alone"] == 4
    assert seen["phase"] == service.settings.global_ask_retrieval_timeout_seconds
    assert seen["notebook"] == service.settings.global_ask_notebook_timeout_seconds


@pytest.mark.parametrize(
    "setup", [{"global_ask_retrieval_concurrency": 4}], indirect=True,
)
def test_the_fair_window_shrinks_per_federated_call_not_per_job(setup):
    """份额的分母是**在途联邦调用数**,不是作业数。

    按作业分的版本在 reasoning 上是错的:一个作业把子查询扇到引擎自己的池上,
    每条线程各自进一次联邦通道,1 作业 × 8 子查询 × 8 库 = 最多 64 条腿排在 4
    个 worker 后面。按作业分会把整池发给那一个作业,剩下六十条腿在阶段时限上
    过期,用户读到的是「检索未开始」——把一次争用说成了范围问题。
    """
    service, _, _, _ = setup
    assert service._retrieval_window() == 4

    with service._federated_call():
        assert service._retrieval_window() == 4
        with service._federated_call():
            # 同一个作业的两次并发联邦调用,各拿一半。
            assert service._retrieval_window() == 2
            with service._federated_call(), service._federated_call():
                assert service._retrieval_window() == 1
        assert service._retrieval_window() == 4

    assert service._retrieval_window() == 4


@pytest.mark.parametrize(
    "setup", [{"global_ask_retrieval_concurrency": 4}], indirect=True,
)
def test_a_federated_call_that_dies_gives_its_share_back(setup):
    """异常退出也要还份额,否则窗口对这个进程的余生永久收窄。"""
    service, _, _, _ = setup
    with pytest.raises(RuntimeError):
        with service._federated_call():
            raise RuntimeError("phase gave up")
    assert service._retrieval_window() == 4


def test_two_concurrent_federated_calls_each_get_half_the_window(setup):
    """握手:两条线程同时在联邦调用里,各自读到的窗口都是一半。

    不用墙钟——两条线程在 ``Barrier`` 上碰头之后才读窗口,所以「同时在途」是
    构造出来的事实,不是等出来的。
    """
    service, _, _, _ = setup
    service.settings.global_ask_retrieval_concurrency = 4
    ready, read = Barrier(2, timeout=5), Barrier(2, timeout=5)
    seen: list = []

    def call():
        with service._federated_call():
            ready.wait()
            seen.append(service._retrieval_window())
            # 第二道栅栏:两条线程都读完之前谁也不许退出自己的 scope,否则先
            # 读完的那条会把份额还回去,后读的那条读到的就是「只有我一个」。
            read.wait()

    threads = [Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert seen == [2, 2]
    assert service._retrieval_window() == 4


def test_the_federation_enters_the_call_scope_for_a_whole_fan_out(setup):
    """联邦通道必须真的进 ``plan.call_scope``,否则计数永远是 0、窗口永远是满池。

    直接驱动 ``_run_planned_tasks``(任务表为空,这条用例问的是 scope 有没有被
    进入,不是腿跑得对不对),并在 scope 内部读计数——``finally`` 释放之后再读就
    分不出「进过」和「没进过」。
    """
    from app.services import chunk_federation as cf

    from contextlib import contextmanager

    service, _, _, _ = setup
    inside: list = []

    @contextmanager
    def counting_scope():
        with service._federated_call():
            inside.append(service._federated_calls)
            yield

    plan = SimpleNamespace(
        call_scope=counting_scope,
        executor=service._retrieval_pool,
        window=lambda: 4,
        cancel=Event(),
    )

    results, reasons = cf._run_planned_tasks(
        SimpleNamespace(), [], plan, deadline=1e18,
    )

    assert (results, reasons) == ([], [])
    # 进过 scope(计数在里面是 1)……
    assert inside == [1]
    # ……出来之后还回去了。
    assert service._federated_calls == 0


def test_a_plan_without_a_call_scope_is_a_no_op(setup):
    """对照臂:普通笔记本路径没有 plan(或 plan 没有 scope),计数一动不动。"""
    from app.services import chunk_federation as cf

    service, _, _, _ = setup
    with cf._call_scope(None):
        assert service._federated_calls == 0
    with cf._call_scope(SimpleNamespace(call_scope=None)):
        assert service._federated_calls == 0
    assert service._federated_calls == 0


def test_a_cancelled_job_stops_the_libraries_already_executing(setup):
    """取消令牌是**复合**的:用户按停止,或这次 run 自己放弃,联邦腿都只需问
    ``is_set()``。这是一个库的硬失败怎么触达其它正在执行的库的唯一通道。
    """
    service, _, _, _ = setup
    seen, entered, release = {}, Event(), Event()
    synthesize = service.ask.synthesize

    def capture(question, chunks, names, history, cancel):
        seen["cancel"] = current_federated_run_plan().cancel
        entered.set()
        assert release.wait(5)
        return synthesize(question, chunks, names, history, cancel)

    service.ask.synthesize = capture
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert entered.wait(5)
    assert not seen["cancel"].is_set()
    service.cancel(job.job_id, user_id="u")

    assert seen["cancel"].is_set()
    release.set()
    assert finished(service, job).status == "cancelled"
