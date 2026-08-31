"""Z2 补测:PostgreSQL 崩溃恢复 ``recover_interrupted_jobs`` 的逐语句故障隔离。

纯 Python + fake db,镜像 ``test_postgres_knowledge_counts_cache.py`` 的 fake connection
形态,无需真 PostgreSQL 服务器——故放在主测试根,不进 ``backend/tests/postgres/``(那目录
整体在无服务器时 skip)。

验证 ``PostgresMaintenanceAdapter.recover_interrupted_jobs`` 改造后的核心不变量:12 条
结算步骤各自独立事务、任一条失败只记账并跳过,绝不阻断其余步骤,也绝不向上抛出。
"""
from __future__ import annotations

from contextlib import contextmanager
import logging

import pytest

from app.repositories.postgres.maintenance import PostgresMaintenanceAdapter


class _FakeCursor:
    def __init__(self, rowcount: int = 0):
        self.rowcount = rowcount


class _FakeWriteConnection:
    """``database.write()`` 每次进入返回的连接。按 SQL 关键字分流:命中
    ``fail_marker`` 的那一条抛错,其余记进共享的 ``executed`` 列表(记录顺序,
    用来断言"其余步骤仍全部执行")。"""

    def __init__(self, executed: list[str], fail_marker: str):
        self._executed = executed
        self._fail_marker = fail_marker

    def execute(self, sql: str, params: tuple = ()) -> _FakeCursor:
        if self._fail_marker in sql:
            raise RuntimeError(f"boom: injected failure for {self._fail_marker!r}")
        self._executed.append(sql)
        return _FakeCursor(rowcount=0)


class _FakeDatabase:
    def __init__(self, executed: list[str], fail_marker: str):
        self._executed = executed
        self._fail_marker = fail_marker

    @contextmanager
    def write(self):
        yield _FakeWriteConnection(self._executed, self._fail_marker)


class _FakeSeams:
    @staticmethod
    def now() -> str:
        return "2026-01-01T00:00:00+00:00"


class _FakeRuntime:
    def __init__(self, executed: list[str], fail_marker: str):
        self.database = _FakeDatabase(executed, fail_marker)
        self.seams = _FakeSeams()


# 12 条结算步骤的标签,与 recover_interrupted_jobs 里 _settle(...) 调用顺序逐字一致。
_ALL_STEP_LABELS = (
    "retained_user_activity",
    "merge_review_jobs",
    "ask_jobs",
    "knowhow_rows",
    "sources(extracting)",
    "sources(queued/parsing)",
    "extraction_runs",
    "kg_build_jobs",
    "indexing_pipeline_stages",
    "catalog_jobs",
    "kg_cluster_scratch",
    "kg_canonical_scratch",
)


def test_first_statement_failure_does_not_block_the_remaining_eleven(caplog):
    """第一条(retained_user_activity 的 DELETE)抛错:其余 11 条仍必须全部执行、函数
    本身不向上抛、失败标签进最终汇总日志。"""
    executed: list[str] = []
    runtime = _FakeRuntime(executed, fail_marker="retained_user_activity")
    adapter = PostgresMaintenanceAdapter(runtime)

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.postgres.maintenance"):
        adapter.recover_interrupted_jobs()  # ① 必须不向上抛(否则本调用本身就会让测试出错)

    # ② 其余 11 条步骤仍全部执行:除了被注入失败的那条(retained_user_activity 的 SQL
    # 从未进 executed),其余每条语句的 SQL 都真的跑到了 fake 连接上。
    assert len(executed) == 11
    assert not any("retained_user_activity" in sql for sql in executed)
    assert any("merge_review_jobs" in sql for sql in executed)
    assert any("ask_jobs" in sql for sql in executed)
    assert any("kg_build_jobs" in sql for sql in executed)
    assert any("TRUNCATE kg_cluster_scratch" in sql for sql in executed)
    assert any("TRUNCATE kg_canonical_scratch" in sql for sql in executed)  # 排在最后,证明真跑到底

    # ③ failed_steps 里出现该标签(通过收尾 logger.error 汇总的消息内容间接验证——
    # failed_steps 是函数局部变量,不对外暴露,消息文本就是它唯一的可观察出口)。
    summary_records = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and r.getMessage().startswith(
            "recover_interrupted_jobs: 1 条结算语句失败并被跳过"
        )
    ]
    assert len(summary_records) == 1
    assert "retained_user_activity" in summary_records[0].getMessage()

    # 逐语句的失败也各自记了一条 exception 日志(与汇总日志分开的两条独立证据)。
    per_step_records = [
        r for r in caplog.records
        if r.levelno == logging.ERROR
        and "结算 retained_user_activity 失败" in r.getMessage()
    ]
    assert len(per_step_records) == 1


def test_last_statement_failure_still_reports_only_that_one_label(caplog):
    """再钉一次最后一条(kg_canonical_scratch 的 TRUNCATE)失败的对称场景:前面
    11 条必须已经执行完,不能因为最后一条失败而被追溯性影响。"""
    executed: list[str] = []
    runtime = _FakeRuntime(executed, fail_marker="kg_canonical_scratch")
    adapter = PostgresMaintenanceAdapter(runtime)

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.postgres.maintenance"):
        adapter.recover_interrupted_jobs()

    assert len(executed) == 11
    assert any("merge_review_jobs" in sql for sql in executed)
    assert any("TRUNCATE kg_cluster_scratch" in sql for sql in executed)
    assert not any("TRUNCATE kg_canonical_scratch" in sql for sql in executed)

    summary_records = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and r.getMessage().startswith(
            "recover_interrupted_jobs: 1 条结算语句失败并被跳过"
        )
    ]
    assert len(summary_records) == 1
    assert "kg_canonical_scratch" in summary_records[0].getMessage()


class _FakeWriteConnectionByIndex:
    """Fails on exactly the ``fail_index``-th ``execute()`` call across the
    whole session (1-indexed, by **call order**, not by SQL text). Needed for
    ``test_each_step_failure_in_call_order_is_isolated`` below: two of the
    twelve ``_ALL_STEP_LABELS`` — ``"sources(extracting)"`` and
    ``"sources(queued/parsing)"`` — annotate the step with the *status
    values* it targets, which never appear verbatim in that step's actual
    UPDATE SQL (e.g. ``WHERE parse_status='extracting'``, not
    ``sources(extracting)``); a substring-matching fake like
    ``_FakeWriteConnection`` above would silently never trigger a failure for
    either of those two, so a full ``_ALL_STEP_LABELS`` sweep needs
    order-based injection instead."""

    def __init__(self, counter: list[int], executed: list[str], fail_index: int):
        self._counter = counter
        self._executed = executed
        self._fail_index = fail_index

    def execute(self, sql: str, params: tuple = ()) -> _FakeCursor:
        self._counter[0] += 1
        if self._counter[0] == self._fail_index:
            raise RuntimeError(f"boom: injected failure at call #{self._fail_index}")
        self._executed.append(sql)
        return _FakeCursor(rowcount=0)


class _FakeDatabaseByIndex:
    def __init__(self, counter: list[int], executed: list[str], fail_index: int):
        self._counter = counter
        self._executed = executed
        self._fail_index = fail_index

    @contextmanager
    def write(self):
        yield _FakeWriteConnectionByIndex(self._counter, self._executed, self._fail_index)


class _FakeRuntimeByIndex:
    def __init__(self, counter: list[int], executed: list[str], fail_index: int):
        self.database = _FakeDatabaseByIndex(counter, executed, fail_index)
        self.seams = _FakeSeams()


@pytest.mark.parametrize(
    "step_index,expected_label", list(enumerate(_ALL_STEP_LABELS, start=1))
)
def test_each_step_failure_in_call_order_is_isolated(caplog, step_index, expected_label):
    """把 ``_ALL_STEP_LABELS`` 用起来(此前定义了却从未被任何用例引用的死代码):按
    ``recover_interrupted_jobs`` 里 ``_settle(...)`` 的调用顺序,把全部 12 条逐一钉一遍
    ——不只是 Z2 补测已经手工挑的头(``merge_review_jobs``)尾
    (``kg_canonical_scratch``)两条。用**第 N 次 execute() 调用**注入失败(而不是按 SQL
    文本里能不能找到标签字符串——见 ``_FakeWriteConnectionByIndex`` 的 docstring:两个
    ``sources(...)`` 标签按文本匹配会完全触发不了,静默漏测)。同时这份参数化本身核对了
    ``_ALL_STEP_LABELS`` 的顺序与源码 ``_settle()`` 调用顺序逐字一致——顺序一旦漂移,
    汇总日志里报出的标签就会与 ``expected_label`` 对不上而炸。"""
    counter = [0]
    executed: list[str] = []
    runtime = _FakeRuntimeByIndex(counter, executed, fail_index=step_index)
    adapter = PostgresMaintenanceAdapter(runtime)

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.postgres.maintenance"):
        adapter.recover_interrupted_jobs()  # 必须不向上抛

    assert len(executed) == 11  # 其余 11 条仍全部执行
    summary_records = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and r.getMessage().startswith(
            "recover_interrupted_jobs: 1 条结算语句失败并被跳过"
        )
    ]
    assert len(summary_records) == 1
    assert expected_label in summary_records[0].getMessage()


def test_no_failure_means_no_summary_log_and_all_twelve_run(caplog):
    """健康路径的对照组:零失败时不发汇总 error 日志,12 条全部执行。"""
    executed: list[str] = []
    runtime = _FakeRuntime(executed, fail_marker="__never_matches__")
    adapter = PostgresMaintenanceAdapter(runtime)

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.postgres.maintenance"):
        adapter.recover_interrupted_jobs()

    assert len(executed) == 12
    assert not any(
        r.levelno == logging.ERROR for r in caplog.records
    )
