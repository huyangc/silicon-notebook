"""`batch_ingest backfill-images` 的跨层稳定值。

放在 `app.domain` 是因为**两侧都要用而它们互相 import 不了**：抛出方是两个仓储
适配器（`app/repositories/{sqlite,postgres}/maintenance.py`），捕获方是离线阶段
（`app/services/image_backfill_phase.py`）。先例是
`app/domain/indexing_pipeline.py` 的那组 stale/busy 异常。
"""
from __future__ import annotations


class ImageBackfillConcurrentChange(RuntimeError):
    """计划阶段的来源快照在写事务里被证明已经过期。

    本阶段是**离线**工具，但 SQLite 后端没有 PostgreSQL 那种"构造 repository 前
    强制 `--confirm-service-stopped`"的 preflight——所以在 SQLite 上它可能与一个
    仍在跑的服务并发。若此时同一个来源正在被重新解析（`replace_elements` +
    重新分块），计划阶段读到的元素/chunk 快照就已作废，而按它写下去会：

    * 把快照里的旧 ``element_ids`` 整份盖回一个已经换代的 chunk 行；
    * 把新图元素挂到一个已经不存在的锚点 id 上（外键指向已删元素的 id 段）；
    * 让本趟的孤儿清扫把并发重解析刚建出来的资产当成"本趟新出现且无人引用"删掉
      （差集判据只能证明"这一趟之后出现的"，证明不了"是我写的"）。

    三种都不报错、只是把库写脏。所以每源写事务的**第一件事**是重读代次信号做
    CAS，不一致就抛这个异常让整个事务回滚。

    它**不是**失败：语义是"这个来源此刻正被别人改，稍后重跑即可"。调用方按单源
    收编（记 reason ``concurrent_change``、清扫本趟资产、继续下一个来源），且不计
    入进程退出码——否则活服务上的正常运维跑会恒定非零。
    """
