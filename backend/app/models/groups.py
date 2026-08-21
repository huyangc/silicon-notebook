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
# `GET /groups` 的 `scope` 取值。非法值 422 而不是静默落回 `mine`。
GROUP_LIST_SCOPES = ("mine", "all")

# 授权边主体。四值白名单,判定谓词按精确匹配消费(已定裁决 1b)。
PRINCIPAL_TYPES = ("user", "group", "group_admins", "everyone")
# 本端点允许**创建**的主体:只有两种群组主体。
# `user` 主体继续走既有只读共享(share_token)流程,`everyone` 继续走
# `POST /notebooks/{id}/tier`;两者都不经本端点,免得同一件事有两个写入口。
GRANTABLE_PRINCIPAL_TYPES = ("group", "group_admins")
# 授权角色。P1 只消费它的**读**含义(任何有效授权边都 ≥ viewer),`admin` 的写权
# 翻转是 P2 的事;枚举现在就收下,免得 P2 再动一次 schema 与前端。
GRANT_ROLES = ("viewer", "admin")

# 成员贡献审批流的状态(群组知识共享 P2-T3)。状态机 **pending → approved/rejected 单向**,
# 撤回是申请者删整行、不是第四个状态(裁决 P2-2)。取值一律**精确匹配**消费——绝不用
# `status != 'pending'` 当「已决定」判据(v50 迁移注释里点名的红线:shadow 停车会给冲突行的
# `status` 暂写哨兵串,否定式判据会把停车行误判成正常状态)。这份元组是那份校验的真源。
SHARE_REQUEST_STATUSES = ("pending", "approved", "rejected")


class GroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str = "project"
    description: str = ""


class GroupUpdate(BaseModel):
    """只改可编辑的展示字段。`kind` 刻意**不可改**,由 `extra="forbid"` 兜住(传了
    `kind` 直接 422,而不是安静地忽略它)。

    理由不是权限:`kind` 只是分类标签,不影响任何权限机制(设计文档 §3)。放开它的
    后果是**标签失真**——普通用户能把自己建的项目组改标成「部门」,于是目录里出现一
    个谁都能建的「部门」,而建组时那道「部门/领域仅系统管理员」的闸正是为了让这个
    标签可信。要改分类就重建一个组,让那道闸重新判一次。"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    description: Optional[str] = None


class GroupMemberRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = "member"


class GroupOwnerTransferRequest(BaseModel):
    """Transfer the one live group-owner authority to an existing member."""

    model_config = ConfigDict(extra="forbid")

    new_owner_id: str


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
    # Immutable creation audit remains in storage as created_by. owner_id is
    # the current, transferable authority and always names a group member.
    owner_id: str
    # 请求者本人在该组里的角色;不是成员时为空串(`?scope=all` 的管理面会出现)。
    my_role: str = ""
    member_count: int = 0
    created_at: str = ""


class GroupDetail(GroupSummary):
    members: List[GroupMemberItem] = Field(default_factory=list)


class GroupInviteState(BaseModel):
    """Current reusable invitation capability, visible only to group admins."""

    active: bool = False
    token: str = ""
    created_at: Optional[str] = None


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


class ShareRequestCreate(BaseModel):
    """普通成员发起「把这本库共享给这个组」的申请(群组知识共享 P2-T3)。

    只带 `group_id`——`notebook_id` 在 URL 路径里,请求者是当前登录用户,状态恒 `pending`
    (不由客户端指定)。`extra="forbid"` 兜住:传别的字段直接 422,而不是安静忽略。
    """

    model_config = ConfigDict(extra="forbid")

    group_id: str


class ShareRequestItem(BaseModel):
    """一条共享申请的只读投影。

    `decided_by` / `decided_at` 只在**已决定**(approved/rejected)的行上有值——组管理员
    做出决定时才写。`decided_at` 是可空 ISO 时间戳:pending 时为 `None`,绝不是空串
    (v50 迁移注释里点名的纪律,store 层有两态断言把关)。
    """

    id: str
    notebook_id: str
    notebook_name: str = ""
    group_id: str
    group_name: str = ""
    requested_by: str = ""
    requested_by_username: str = ""
    status: str = "pending"
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    created_at: str = ""
