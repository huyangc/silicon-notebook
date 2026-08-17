"""群组与授权边的 HTTP 面(群组知识共享 P1-T3)。

本文件是**策略**的所在地;行持久化在 `repositories/*/group_store.py`。三条口径必须
一起读才完整:

1. **群组的可见性口径是 404,不是 403**。非组成员访问一个群组 → 404,与「这个组不
   存在」完全一样。与 `require_notebook_read` / `require_notebook_write` 的既有惯例
   同款:403 会确认「这个 id 确实存在」,而群组名本身就是可探测的信息(哪个部门在
   用这个系统、有没有某个项目组)。

2. **授权边的创建是双重条件**(设计文档决策 9):请求者既要对这本笔记本有管理权
   (由 `require_notebook_capability("notebook:manage")` 在依赖层挡住),又要是目标
   群组的组管理员(在路由体内查)。少任何一半都不许发边,而且两半的失败形态要说清
   缺的是哪一半——「403」本身不告诉用户该去找谁。这里的 403 不退化成 404:能走到
   这一步说明请求者对这本库有管理权,库的存在性对他本来就不是秘密。

   ⚠ 这一处是群组 404 口径的**唯一例外**:「组不存在」给 404、「组存在但你不是它的
   管理员」给 403,两者可区分。这是刻意的取舍——组 id 是 128 位随机 uuid,能走到这
   一步的人手上必须已经有那个 id(猜不出来),所以可区分不构成枚举通道;换来的是
   「你把组 id 填错了」与「你没有这个组的管理权」两条完全不同的错误有各自的文案。
   浏览群组的那几个端点没有这个前提(id 从列表里来),所以它们仍统一 404,见
   `_require_group_admin`。

3. **撤销是不对称的**(设计文档决策 9):库的管理者可以从笔记本维度删任意一条边
   (`DELETE /notebooks/{id}/grants/{grant_id}`),组管理员可以从群组维度删掉指向本组
   的全部边(`DELETE /groups/{gid}/shared-notebooks/{nb}`)。两个入口各自只需要自己
   那一半权限——组管理员管理共享给本组的全部内容,库主随时可以收回自己的库。

`principal_type` 只收两个**群组**主体。`user` 主体继续走既有只读共享(share_token)
流程、`everyone` 继续走 `POST /notebooks/{id}/tier`,两者都不经此端点:同一件事有两个
写入口,迟早会有一个入口漏掉另一个入口的某条校验。
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    get_current_user,
    group_repository,
    require_notebook_capability,
    user_error,
)
from app.models.groups import (
    GRANT_ROLES,
    GRANTABLE_PRINCIPAL_TYPES,
    GROUP_KINDS,
    GROUP_ROLES,
    OPEN_GROUP_KINDS,
    GrantCreate,
    GroupCreate,
    GroupDetail,
    GroupMemberItem,
    GroupMemberRoleRequest,
    GroupSharedNotebookItem,
    GroupSummary,
    GroupUpdate,
    NotebookGrantItem,
    UserRef,
)
from app.models.identity import UserProfile
from app.repositories.ports import GroupGrantAlreadyExists, LastGroupAdminError


router = APIRouter()

# 群组名/说明的长度上限。约束的是**用户编辑的数据**,所以按 CLAUDE.md 的「数值上限
# 与截断」红线:超限**明确拒绝**,绝不静默截断。精确数值只登记在
# docs/product-and-api*.md 的群组章节。
_MAX_GROUP_NAME_CHARS = 120
_MAX_GROUP_DESCRIPTION_CHARS = 1000


def _group_not_found() -> HTTPException:
    """群组维度的统一「看不见」响应。

    非成员、不存在、已被删除三种情形返回**同一个** 404——这正是不泄露存在性的
    含义:能区分出来的任何差别都是一个可探测信号。
    """
    return user_error(404, "群组不存在")


def _require_membership(group_id: str, user: UserProfile) -> str:
    """我在这个组里的角色;不是成员 → 404(不泄露存在性)。"""
    role = group_repository().user_group_role(group_id, user.id)
    if role is None:
        raise _group_not_found()
    return role


def _require_group_admin(group_id: str, user: UserProfile) -> None:
    """组管理员守卫。**不是**组管理员与**组不存在**同为 404。

    普通成员在这里也拿 404 而不是 403:他知道组存在,但「你不是管理员」这句话对他
    没有可操作性(能给他授权的正是管理员,界面本来就不该给他这个入口),而统一成
    404 让这道守卫只有一种响应形态,不必在每个调用点判「这次该露多少」。
    """
    if _require_membership(group_id, user) != "admin":
        raise _group_not_found()


def _validated_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise user_error(400, "群组名称不能为空")
    if len(name) > _MAX_GROUP_NAME_CHARS:
        raise user_error(400, "群组名称过长,请精简后重试")
    return name


def _validated_description(raw: str) -> str:
    if len(raw) > _MAX_GROUP_DESCRIPTION_CHARS:
        raise user_error(400, "群组说明过长,请精简后重试")
    return raw


# --------------------------------------------------------------------- 群组


@router.post("/groups", response_model=GroupDetail)
def create_group_route(
    payload: GroupCreate, user: UserProfile = Depends(get_current_user)
) -> GroupDetail:
    """建组。项目组人人可建;部门/领域组仅系统管理员可建(已定决策 2)。

    管理员闸内联在这里而不做成能力守卫,与 `set_notebook_tier` 同一形态:它判的是
    **全局角色**,没有 notebook 可挂。
    """
    kind = payload.kind.strip().lower()
    if kind not in GROUP_KINDS:
        raise user_error(422, "群组类型只能是项目、部门或领域")
    if kind not in OPEN_GROUP_KINDS and user.role != "admin":
        raise user_error(403, "仅管理员可创建部门或领域群组")
    group = group_repository().create_group(
        name=_validated_name(payload.name),
        kind=kind,
        description=_validated_description(payload.description),
        created_by=user.id,
    )
    # 创建者已在同一写事务里成为组管理员,所以这里的成员清单必定非空。
    return GroupDetail(
        **group,
        members=[
            GroupMemberItem(**m) for m in group_repository().list_members(group["id"])
        ],
    )


@router.get("/groups", response_model=List[GroupSummary])
def list_groups_route(
    scope: str = Query("mine"),
    user: UserProfile = Depends(get_current_user),
) -> List[GroupSummary]:
    """我所在的群组;`?scope=all` 是系统管理员的全局管理面。"""
    if scope == "all":
        if user.role != "admin":
            raise user_error(403, "仅管理员可查看全部群组")
        return [GroupSummary(**g) for g in group_repository().list_all_groups(user_id=user.id)]
    return [GroupSummary(**g) for g in group_repository().list_groups_for_user(user.id)]


# 静态段路由必须在 /users/{...} 形态之前;此处没有同前缀的动态路由,但保持与
# notebook_routes 一致的写法顺序,免得日后新增 /users/{id} 时被静默抢匹配。
@router.get("/users/resolve", response_model=UserRef)
def resolve_user_route(
    username: str = Query(...),
    user: UserProfile = Depends(get_current_user),
) -> UserRef:
    """按用户名**精确**查一个用户,供组管理员加人。

    任何登录用户可调。这在内部部署下是**已登记接受**的取舍:它让用户名可被逐个探测
    (「这个人有没有账号」)。替代方案(只允许组管理员调、或做成模糊搜索)都更糟——
    前者要先有组才能加第一个人,后者把逐个探测换成批量枚举。返回面刻意只有 id /
    用户名 / 显示名,不含邮箱、角色、用量等任何额外身份信息。
    """
    del user
    found = group_repository().find_user_by_username(username.strip())
    if found is None:
        raise user_error(404, "没有找到该用户名对应的用户")
    return UserRef(**found)


@router.get("/groups/{group_id}", response_model=GroupDetail)
def get_group_route(
    group_id: str, user: UserProfile = Depends(get_current_user)
) -> GroupDetail:
    """组详情 + 成员清单。仅组成员可见,非成员 404。"""
    _require_membership(group_id, user)
    groups = group_repository()
    group = groups.get_group(group_id, user_id=user.id)
    if group is None:
        raise _group_not_found()
    return GroupDetail(
        **group, members=[GroupMemberItem(**m) for m in groups.list_members(group_id)]
    )


@router.patch("/groups/{group_id}", response_model=GroupDetail)
def update_group_route(
    group_id: str,
    payload: GroupUpdate,
    user: UserProfile = Depends(get_current_user),
) -> GroupDetail:
    _require_group_admin(group_id, user)
    groups = group_repository()
    updated = groups.update_group(
        group_id,
        name=None if payload.name is None else _validated_name(payload.name),
        description=(
            None
            if payload.description is None
            else _validated_description(payload.description)
        ),
    )
    group = groups.get_group(group_id, user_id=user.id) if updated else None
    if group is None:
        raise _group_not_found()
    return GroupDetail(
        **group, members=[GroupMemberItem(**m) for m in groups.list_members(group_id)]
    )


@router.delete("/groups/{group_id}", status_code=204)
def delete_group_route(
    group_id: str, user: UserProfile = Depends(get_current_user)
) -> None:
    """删组。指向本组的授权边在**同一个写事务**里一并清掉(已定裁决 3)。

    不清就是孤儿授权边:谓词侧因 join 不到组成员而失效(不会越权),但共享管理列表
    会永远挂着一条指向已不存在的组的边——`principal_id` 是刻意无 FK 的多态列,
    数据库替不了这件事。
    """
    _require_group_admin(group_id, user)
    if not group_repository().delete_group(group_id):
        raise _group_not_found()


# ------------------------------------------------------------------- 组成员


@router.put("/groups/{group_id}/members/{user_id}", response_model=GroupDetail)
def put_group_member_route(
    group_id: str,
    user_id: str,
    payload: GroupMemberRoleRequest,
    user: UserProfile = Depends(get_current_user),
) -> GroupDetail:
    """加人 / 改角色。同一个端点做两件事:目标用户在不在组里,调用方本来就不必先查。"""
    _require_group_admin(group_id, user)
    role = payload.role.strip().lower()
    if role not in GROUP_ROLES:
        raise user_error(422, "群组角色只能是成员或组管理员")
    groups = group_repository()
    if groups.find_user_by_id(user_id) is None:
        raise user_error(404, "没有找到要添加的用户")
    try:
        groups.upsert_member(group_id, user_id, role=role, added_by=user.id)
    except LastGroupAdminError:
        raise user_error(409, "群组至少要保留一名组管理员")
    group = groups.get_group(group_id, user_id=user.id)
    if group is None:
        raise _group_not_found()
    return GroupDetail(
        **group, members=[GroupMemberItem(**m) for m in groups.list_members(group_id)]
    )


@router.delete("/groups/{group_id}/members/{user_id}", status_code=204)
def remove_group_member_route(
    group_id: str, user_id: str, user: UserProfile = Depends(get_current_user)
) -> None:
    _require_group_admin(group_id, user)
    try:
        removed = group_repository().remove_member(group_id, user_id)
    except LastGroupAdminError:
        raise user_error(409, "群组至少要保留一名组管理员")
    if not removed:
        raise user_error(404, "该用户不是这个群组的成员")


@router.delete("/groups/{group_id}/membership", status_code=204)
def leave_group_route(
    group_id: str, user: UserProfile = Depends(get_current_user)
) -> None:
    """自助退出。最后一名组管理员不得退出——否则组就没人能管了。"""
    _require_membership(group_id, user)
    try:
        group_repository().remove_member(group_id, user.id)
    except LastGroupAdminError:
        raise user_error(409, "你是这个群组唯一的组管理员,请先指定其他组管理员再退出")


# ------------------------------------------------------------------- 授权边


@router.get(
    "/notebooks/{notebook_id}/grants",
    response_model=List[NotebookGrantItem],
    dependencies=[Depends(require_notebook_capability("notebook:manage"))],
)
def list_notebook_grants_route(notebook_id: str) -> List[NotebookGrantItem]:
    """这本库上的全部授权边。

    四类主体都如实返回(含本端点不允许创建的 `user` / `everyone`):这个清单是库主
    「谁能看我的库」的完整答案,按主体类型过滤掉一半会让它变成一份骗人的清单。
    """
    return [NotebookGrantItem(**g) for g in group_repository().list_grants(notebook_id)]


@router.post(
    "/notebooks/{notebook_id}/grants",
    response_model=NotebookGrantItem,
    dependencies=[Depends(require_notebook_capability("notebook:manage"))],
)
def create_notebook_grant_route(
    notebook_id: str,
    payload: GrantCreate,
    user: UserProfile = Depends(get_current_user),
) -> NotebookGrantItem:
    """共享给群组。**双重条件**:对库有管理权(依赖层已挡)+ 是目标组的组管理员。"""
    principal_type = payload.principal_type.strip().lower()
    if principal_type not in GRANTABLE_PRINCIPAL_TYPES:
        raise user_error(422, "这里只能共享给群组;共享给个人请用只读共享链接")
    role = payload.role.strip().lower()
    if role not in GRANT_ROLES:
        raise user_error(422, "共享权限只能是只读或管理")
    groups = group_repository()
    group_id = payload.principal_id.strip()
    if groups.get_group(group_id) is None:
        raise _group_not_found()
    if groups.user_group_role(group_id, user.id) != "admin":
        raise user_error(403, "你不是这个群组的组管理员,无法把知识库共享给它")
    try:
        grant = groups.create_grant(
            notebook_id,
            principal_type=principal_type,
            principal_id=group_id,
            role=role,
            created_by=user.id,
        )
    except GroupGrantAlreadyExists:
        raise user_error(409, "这本知识库已经共享给该群组了")
    return NotebookGrantItem(**grant)


@router.delete(
    "/notebooks/{notebook_id}/grants/{grant_id}",
    status_code=204,
    dependencies=[Depends(require_notebook_capability("notebook:manage"))],
)
def delete_notebook_grant_route(notebook_id: str, grant_id: str) -> None:
    """从笔记本维度撤销一条边。库的管理者即可,不要求他也是那个组的管理员。

    `grant_id` 与 `notebook_id` 一起验(store 侧的 WHERE 带两列):只按 grant_id 删,
    等于让「我有一本自己的库的管理权」变成「我能删任何库上的授权边」。
    """
    if not group_repository().delete_grant(notebook_id, grant_id):
        raise user_error(404, "该共享记录不存在")


@router.get(
    "/groups/{group_id}/shared-notebooks",
    response_model=List[GroupSharedNotebookItem],
)
def list_group_shared_notebooks_route(
    group_id: str, user: UserProfile = Depends(get_current_user)
) -> List[GroupSharedNotebookItem]:
    """组管理员视角:共享给本组的知识库清单。"""
    _require_group_admin(group_id, user)
    return [
        GroupSharedNotebookItem(**item)
        for item in group_repository().list_group_shared_notebooks(group_id)
    ]


@router.delete(
    "/groups/{group_id}/shared-notebooks/{notebook_id}", status_code=204
)
def delete_group_shared_notebook_route(
    group_id: str, notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> None:
    """撤销不对称的组维度入口:删掉这本库上指向本组的**全部**边。

    只要组管理员这一半权限——他管理共享给本组的全部内容,不必也是那本库的管理者。
    """
    _require_group_admin(group_id, user)
    if not group_repository().delete_group_grants_for_notebook(group_id, notebook_id):
        raise user_error(404, "这本知识库没有共享给该群组")
