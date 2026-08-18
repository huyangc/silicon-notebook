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

#: 「这本笔记本共享给群组了吗」的 SQL 判据 —— 双后端共用**一份文本**。
#:
#: 它没有任何占位符,也没用到方言函数,所以放在中性层是安全的;放在这里而不是各
#: 写一份,是因为同一个判据现在有三个消费点(两条 summary 行查询喂「已分享」徽标、
#: 「已分享」总览的 WHERE),三份手抄迟早会让徽标亮着而总览说「尚未分享」。
#:
#: 谓词只认**两个群组主体**:`user` 主体在 P1 还没有任何写入口(只读共享仍走
#: `notebook_members`,徽标那一半由 `notebooks.is_shared` 负责),`everyone` 是公共
#: 知识库的兼容映射、有自己的 tier 表达,把它算成「已分享」会让每个公共库都多一个
#: 说不清的徽标。
#:
#: 关联列写死 `notebooks.id`:三个消费点的主表都是未起别名的 `notebooks`。
GROUP_GRANT_EXISTS_SQL = (
    "EXISTS(SELECT 1 FROM notebook_grants ng WHERE ng.notebook_id = notebooks.id"
    " AND ng.principal_type IN ('group', 'group_admins'))"
)

#: 上一条的计数形态:这本库共享给了几个**不同的**群组。
#: `DISTINCT principal_id` 而不是 `COUNT(*)`——同一个组可以同时有 `group` 与
#: `group_admins` 两条边(设计文档 §3 的两行编码),按行数会把一个组报成两个。
GROUP_GRANT_COUNT_SQL = (
    "(SELECT COUNT(DISTINCT ng.principal_id) FROM notebook_grants ng"
    " WHERE ng.notebook_id = notebooks.id"
    " AND ng.principal_type IN ('group', 'group_admins'))"
)

#: `NotebookSummary.is_shared` 的第二个来源列(见 `notebook_catalog.from_row`)。
SHARED_TO_GROUPS_COLUMN = GROUP_GRANT_EXISTS_SQL + " AS _shared_to_groups"


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


#: `notebook_grants.role` 里表示「管理级」的取值。与 `models/groups.py::GRANT_ROLES`
#: 的第二项、以及两份 `access_sql._admin_principal_match_expr` 里那个字面量是同一个值。
#: 三处刻意不互相 import(models 是 API 层、access_sql 是 SQL 文本、这里是中性行整形,
#: 互相 import 会成环),由 `test_access_sql_contract` 的一致性断言钉住。
ADMIN_GRANT_ROLE = "admin"


def notebook_grant_confers_admin(row: Any) -> bool:
    """这条授权边行是否**授予内容管理权**(群组知识共享 P2-T2)。

    `granted_notebook_rows` 的行整形,与 `fold_granted_notebook_groups` 同一条理由住在
    中性层:两个后端喂同一份判据,分叉了没有任何测试会自然抓到——它不报错,只是某个
    后端的组管理员在界面上看不到写入口。

    ⚠ 判的是**边自己的 role**(`_grant_role`),不是组成员角色。「你在组里是不是管理员」
    已经由查询的 `gm.role='admin'` 那一半在 `group_admins` 主体上判过了;这里判的是
    「库主把这条边发成了只读还是管理」。两个 role 同名不同轴,混用会让每个 `group_admins`
    边都被当成管理权。

    列缺失(旧行形状/未来的其他行来源)按 **False** 收:方向与
    `NotebookSummary.can_manage_content` 的默认值一致——投影漏判只少画一个按钮,
    多判会让用户点进一个必然 404 的动作。
    """
    try:
        role = row["_grant_role"]
    except (KeyError, IndexError, TypeError):
        return False
    return role == ADMIN_GRANT_ROLE


#: 共享申请的三个合法状态。与 `models/groups.py::SHARE_REQUEST_STATUSES` 同值——
#: 那份是 API 层校验真源,这里是行整形层复用的同一集合(两处不互相 import:models 是
#: API 层、这里是中性行整形,由 `test_group_routes` 的写路径断言与它一致)。
SHARE_REQUEST_DECIDED_STATUSES = ("approved", "rejected")


def assert_share_request_decided_at(status: Any, decided_at: Any) -> None:
    """`notebook_share_requests` 的 `decided_at` 两态不变式(群组知识共享 P2-T3)。

    两侧 store 的行投影都在读到每一行时调它,把「decided_at 该是 NULL 还是 ISO」这条
    纪律做成**always-on 的写路径复核**而不是只靠人工审阅:

    * `status == 'pending'` ⟺ `decided_at is None`。pending 行的 `decided_at` 若不是
      真 NULL(尤其是被某处手滑写成了 `''`),这里当场 `AssertionError` ——那正是 v50
      迁移注释点名要防的:PostgreSQL 的 `timestamptz` 收到空串会类型报错、poison 整条
      shadow 正向复制通道,而空串又刻意**没有**登记进 `POSTGRES_EMPTY_TIME_SENTINELS`,
      不会被哨兵表悄悄兼容掉。所以最省事的防线就是「决不让 `''` 落库」,由这条断言在
      开发/测试期把它顶回去。
    * `status in ('approved','rejected')` ⟹ `decided_at` 是**非空**字符串(datetime
      已在调用点归一成 ISO)。已决定却没有决定时间同样是坏数据。

    只对三个合法状态生效:shadow 停车会给冲突行的 `status` 暂写哨兵串,但那种行只在
    replicator 内部流转、从不经本 store 投影,所以这里遇到未知 `status` 直接放行、不
    误伤(与「status 精确匹配」红线同一方向)。
    """
    if status == "pending":
        assert decided_at is None, (
            "pending share request must have decided_at IS NULL, got "
            f"{decided_at!r} (never write '' — it poisons the shadow timestamptz)"
        )
    elif status in SHARE_REQUEST_DECIDED_STATUSES:
        assert isinstance(decided_at, str) and decided_at, (
            f"decided share request ({status}) must carry an ISO decided_at, got "
            f"{decided_at!r}"
        )


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
