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

from threading import Event
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
def test_the_fair_window_shrinks_as_jobs_arrive(setup, monkeypatch):
    """共享池 + FIFO:一个作业一次提交全部库就占满槽位,下一个提问者的库排到
    自己的阶段时限到期都起不了跑——零个被搜过的库、一份没有证据的答案。

    份额按**活跃作业数**现算,所以这里用登记状态而不是真跑:两个作业登记之后
    窗口必须是二分之一。
    """
    service, _, _, _ = setup
    assert service._retrieval_window() == 4
    staged = []
    monkeypatch.setattr("app.services.global_ask.threading.Thread.start",
                        _stage_job_threads(staged))
    jobs = [service.start(GlobalAskRequest(question=question), user_id="u")
            for question in ("first question", "second question")]
    assert len(staged) == 2

    assert service._retrieval_window() == 2

    monkeypatch.undo()
    for thread in staged:
        thread.start()
    for job in jobs:
        assert finished(service, job).status == "done"
    # 作业退场,份额还回去。
    assert service._retrieval_window() == 4


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
