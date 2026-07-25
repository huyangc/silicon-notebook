"""流水线体检(P2)的 API 响应模型。

`CheckupService`(`app/services/checkup.py`)产出内部 dataclass;这里是它的 pydantic 传输层。
字段是**内部契约**:``code``(H2..H8)与 ``fix``(修复动作枚举)都是内部代号——面向用户的界面词
由前端映射层(T5)负责,后端不出中文用户文案(界面词汇守卫红线)。
"""
from __future__ import annotations

from pydantic import BaseModel


class CheckupItem(BaseModel):
    """单个体检项。``count``>0 即命中;``sample`` 是有界的 source_id 样本(H2/H3 给前端展示,
    H4–H8 是计数型、留空);``fix`` 是修复动作枚举
    (reparse|backfill_vectors|extract_kg|fold_index|rebuild_index)。"""

    code: str
    count: int
    sample: list[str]
    fix: str


class CheckupResponse(BaseModel):
    """一个 notebook 的体检结果聚合。``healthy`` = 所有 check.count 均为 0。"""

    notebook_id: str
    checked_at: str
    healthy: bool
    checks: list[CheckupItem]
