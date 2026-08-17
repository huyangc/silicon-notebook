"""群组授权边的后端中性行折叠 —— 两个后端共用一份,不各折一遍。

放在中性层的理由与 `like_pattern.py` / `text_whitespace.py` 同款:这是纯 Python 的
行整形,不含任何 SQL 方言;两侧各写一份的唯一结果就是某天它们折得不一样,而「同库
两条边折成一项」这种事分叉了没有任何测试会自然抓到——它不报错,只是某个后端的
共享清单里同一本库出现两次。
"""
from __future__ import annotations

from typing import Any, Sequence


#: 群组主体的两个 `principal_type`。授权边清单要按它判「这条边该不该解析出组名」。
GROUP_PRINCIPAL_TYPES = ("group", "group_admins")

#: 群组主体解析不出组时,`principal_kind` 上的失效标注。
#:
#: 它不是「这个组恰好没填 kind」——`groups.kind` 是 `NOT NULL DEFAULT 'project'`,
#: 有组就必有 kind。所以「群组主体 + 空 kind」只可能是**孤儿边**:指向的组已经
#: 不存在了。删组事务会清掉这类边,但 `principal_id` 没有外键,合库(`merge_dbs`)
#: 的并集仍可能复活它们,而它们在界面上原本长得和正常条目一模一样——库主看到一条
#: 没有名字的共享记录,既不知道它是什么,也不知道该不该删。标注出来让他看得懂。
MISSING_PRINCIPAL_KIND = "missing"


def resolve_grant_principal(
    principal_type: Any, group_name: Any, group_kind: Any
) -> tuple[str, str]:
    """一条授权边的 ``(principal_name, principal_kind)`` 展示投影 —— 双后端共用。

    * 群组主体 + 解析到组 → ``(组名, 组分类)``;
    * 群组主体 + 解析不到组 → ``("", "missing")``,即孤儿边;
    * `user` / `everyone` 主体 → ``("", "")``:它们本来就没有组可解析,由
      `principal_type` 自我标注,不在这里替它们编一个名字,更**不能**误标成
      `missing`(那会把两条完全正常的边说成坏数据)。
    """
    if str(principal_type or "") not in GROUP_PRINCIPAL_TYPES:
        return ("", "")
    name = str(group_name or "")
    kind = str(group_kind or "")
    return (name, kind or MISSING_PRINCIPAL_KIND)


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
