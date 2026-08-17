"""群组与授权边的 API 模型(群组知识共享 P1-T3)。

取值枚举(`kind` / 组内 `role` / `principal_type` / 授权 `role`)一律**在应用层**校验
——schema 侧刻意不加 CHECK(设计文档已定裁决 2:正向 shadow 的 UNIQUE 停车方案依赖
`notebook_grants.principal_type` 保持一个既无 CHECK 也无 FK 的裸列)。所以这几个常量
元组就是那份校验的真源,路由与 store 都从这里取,不要在别处再抄一份字面量。
"""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


# 群组分类。**只影响谁能建组与界面文案,不影响任何权限机制**(设计文档 §3)。
GROUP_KINDS = ("project", "department", "domain")
# 「人人可建」的那一类;其余两类仅系统管理员可建(已定决策 2)。
OPEN_GROUP_KINDS = ("project",)
# 组内角色。两级,不引入 editor(已定决策 4)。
GROUP_ROLES = ("member", "admin")

# 授权边主体。四值白名单,判定谓词按精确匹配消费(已定裁决 1b)。
PRINCIPAL_TYPES = ("user", "group", "group_admins", "everyone")
# 本端点允许**创建**的主体:只有两种群组主体。
# `user` 主体继续走既有只读共享(share_token)流程,`everyone` 继续走
# `POST /notebooks/{id}/tier`;两者都不经本端点,免得同一件事有两个写入口。
GRANTABLE_PRINCIPAL_TYPES = ("group", "group_admins")
# 授权角色。P1 只消费它的**读**含义(任何有效授权边都 ≥ viewer),`admin` 的写权
# 翻转是 P2 的事;枚举现在就收下,免得 P2 再动一次 schema 与前端。
GRANT_ROLES = ("viewer", "admin")


class GroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str = "project"
    description: str = ""


class GroupUpdate(BaseModel):
    """只改可编辑的展示字段。`kind` 刻意不可改:它决定了「谁能建这个组」,
    改它等于让普通用户把自己建的项目组升格成部门/领域组,绕开建组时的管理员闸。"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    description: Optional[str] = None


class GroupMemberRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = "member"


class UserRef(BaseModel):
    """按用户名精确查到的用户。供组管理员加人;不含邮箱等任何额外身份信息。"""

    id: str
    username: str
    display_name: str = ""


class GroupMemberItem(UserRef):
    role: str = "member"
    added_at: str = ""


class GroupSummary(BaseModel):
    id: str
    name: str
    kind: str = "project"
    description: str = ""
    # 请求者本人在该组里的角色;不是成员时为空串(`?scope=all` 的管理面会出现)。
    my_role: str = ""
    member_count: int = 0
    created_at: str = ""


class GroupDetail(GroupSummary):
    members: List[GroupMemberItem] = Field(default_factory=list)


class GrantedGroupRef(BaseModel):
    """`NotebookSummary.granted_via` 的元素:这本笔记本是**经哪个群组**共享给我的。"""

    group_id: str
    group_name: str = ""
    kind: str = "project"


class NotebookGrantItem(BaseModel):
    """一条授权边的只读投影。

    `principal_name` / `principal_kind` 只在群组主体上有值——`user` 与 `everyone`
    行如实返回、由 `principal_type` 自我标注,不在这里替它们编一个名字。
    """

    id: str
    principal_type: str
    principal_id: str = ""
    role: str = "viewer"
    principal_name: str = ""
    principal_kind: str = ""
    created_at: str = ""


class GrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_type: str
    principal_id: str
    role: str = "viewer"


class GroupSharedNotebookItem(BaseModel):
    """组管理员视角:共享给本组的一本笔记本(同组两条边折成一项)。"""

    notebook_id: str
    name: str = ""
    owner_username: str = ""
    roles: List[str] = Field(default_factory=list)
