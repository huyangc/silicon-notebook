"""群组授权边的后端中性行折叠 —— 两个后端共用一份,不各折一遍。

放在中性层的理由与 `like_pattern.py` / `text_whitespace.py` 同款:这是纯 Python 的
行整形,不含任何 SQL 方言;两侧各写一份的唯一结果就是某天它们折得不一样,而「同库
两条边折成一项」这种事分叉了没有任何测试会自然抓到——它不报错,只是某个后端的
共享清单里同一本库出现两次。
"""
from __future__ import annotations

from typing import Any, Sequence


def fold_shared_notebooks(rows: Sequence[Any]) -> list[dict]:
    """``(notebook_id, role, name, owner_username)`` 行 → 每库一项、`roles` 去重。

    输入必须已按稳定顺序排好(两个 store 都按 ``notebook.created_at, id, grant.id``
    排),本函数保持首次出现序,不再排一次。
    """
    folded: dict[str, dict] = {}
    for row in rows:
        notebook_id = row["notebook_id"]
        item = folded.get(notebook_id)
        if item is None:
            item = {
                "notebook_id": notebook_id,
                "name": row["name"] or "",
                "owner_username": row["owner_username"] or "",
                "roles": [],
            }
            folded[notebook_id] = item
        role = row["role"]
        if role not in item["roles"]:
            item["roles"].append(role)
    return list(folded.values())


def fold_granted_notebook_groups(rows: Sequence[Any]) -> dict[str, list[dict]]:
    """授权边列表投影的第二半:``notebook_id -> [{group_id, group_name, kind}]``。

    与 `fold_shared_notebooks` 同一条理由住在中性层。同一本库可能同时经
    `group` 与 `group_admins` 两条边到达同一个组,所以按 group_id 去重。
    """
    out: dict[str, list[dict]] = {}
    for row in rows:
        bucket = out.setdefault(row["notebook_id"], [])
        group_id = row["group_id"]
        if any(existing["group_id"] == group_id for existing in bucket):
            continue
        bucket.append(
            {
                "group_id": group_id,
                "group_name": row["group_name"] or "",
                "kind": row["group_kind"] or "project",
            }
        )
    return out
