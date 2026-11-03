"""一本笔记本对一个用户的访问级别 —— 后端中性的值类型(分享编辑 P1,PD-5)。

两个后端的 `sharing_store.notebook_access_level` 用同一条
`access_sql.NOTEBOOK_ACCESS_LEVEL_SQL` 答出 `(owner_id, level)`,在这里整形成同一种值;
放在中性层的理由与 `group_rows.py` 同款:两侧各定义一份,某天它们的字段或取值就会
分叉,而分叉不报错。
"""
from __future__ import annotations

from typing import Literal, NamedTuple

#: 访问级别,从高到低。`owner` 是库主本人;`admin` 是管理级授权边(组管理员);`edit`
#: 是编辑级授权边(编辑者);`read` 是任何其余读权来源(只读成员、viewer 边、everyone)。
AccessLevel = Literal["owner", "admin", "edit", "read"]


class NotebookAccess(NamedTuple):
    """`level` 是调用者的最高档;`owner_id` 是这本库的 `created_by`(同一条语句读出,
    省掉体内判定为了知道库主是谁而发的第二条语句)。无权或库不存在时整个值是 None,
    不构造 level 为空的实例。"""

    level: AccessLevel
    owner_id: str


def notebook_access_from_row(row) -> "NotebookAccess | None":
    """`NOTEBOOK_ACCESS_LEVEL_SQL` 的一行(或无行)→ 值。level 为 NULL(无权)与
    无行(库不存在/非存活)都收成 None,与另外几条谓词「不泄露存在性」同口径。"""
    if row is None or row["level"] is None:
        return None
    return NotebookAccess(level=str(row["level"]), owner_id=str(row["owner_id"] or ""))
