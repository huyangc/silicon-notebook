"""群组与授权边的 HTTP 面(群组知识共享 P1-T3)。

本文件是**策略**的所在地;行持久化在 `repositories/*/group_store.py`。三条口径必须
一起读才完整:

1. **群组的可见性口径是 404,不是 403**。非组成员访问一个群组 → 404,与「这个组不
   存在」完全一样。与 `require_notebook_read` / `require_notebook_write` 的既有惯例
   同款:403 会确认「这个 id 确实存在」,而群组名本身就是可探测的信息(哪个部门在
   用这个系统、有没有某个项目组)。

   ⚠ **系统管理员(`user.role == "admin"`)绕过这一层**——可读任意组详情、可管理
   任意组。这是**运维旁路**,与「系统管理员可转移 `notebooks.created_by`」同性质
   (设计文档 §4 已登记)。少了它有两处直接坏掉:①「最后一名组管理员保护」在
   理论并发窗口下仍可能留下一个零管理员的组,而所有管理端点对系统管理员也 404,
   那个组在 API 里**永远不可恢复**,只能改数据库;②`GET /groups?scope=all` 是给
   系统管理员的全局管理面,没有旁路它就是一张每一行都点不开的表。旁路只作用于
   群组维度,笔记本的读写守卫一个字不动。

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
from app.core.capability_tokens import new_capability_token
from app.models.groups import (
    GRANT_ROLES,
    GRANTABLE_PRINCIPAL_TYPES,
    GROUP_KINDS,
    GROUP_LIST_SCOPES,
    GROUP_ROLES,
    OPEN_GROUP_KINDS,
    GrantCreate,
    GroupCreate,
    GroupDetail,
    GroupInviteState,
    GroupMemberItem,
    GroupMemberRoleRequest,
    GroupOwnerTransferRequest,
    GroupSharedNotebookItem,
    GroupSummary,
    GroupUpdate,
    NotebookGrantItem,
    ShareRequestCreate,
    ShareRequestItem,
    UserRef,
)
from app.models.identity import UserProfile
from app.services.pending_bus import publish_snapshot
from app.repositories.ports import (
    GroupAdminRequiredError,
    GroupAdminShouldShareDirectlyError,
    GroupGrantAlreadyExists,
    GroupMembershipRequiredError,
    GroupNotFoundError,
    GroupOwnerProtectedError,
    GroupOwnerRequiredError,
    GroupOwnerTransferTargetError,
    LastGroupAdminError,
    NotebookManageRequiredError,
    NotebookNotFoundError,
    ShareRequestAlreadyPendingError,
    ShareRequestNotPendingError,
    ShareRequesterUnauthorizedError,
)


router = APIRouter()

# 群组名/说明的长度上限。约束的是**用户编辑的数据**,所以按 CLAUDE.md 的「数值上限
# 与截断」红线:超限**明确拒绝**,绝不静默截断。
# ⚠ 精确数值**待 T5 登记**进 docs/product-and-api*.md 的群组章节(本轮只落代码,
# 文档批次在 T5;别把这条注释读成「已经登记过了」)。
_MAX_GROUP_NAME_CHARS = 120
_MAX_GROUP_DESCRIPTION_CHARS = 1000


def _group_not_found() -> HTTPException:
    """群组维度的统一「看不见」响应。

    非成员、不存在、已被删除三种情形返回**同一个** 404——这正是不泄露存在性的
    含义:能区分出来的任何差别都是一个可探测信号。
    """
    return user_error(404, "群组不存在")


def _share_request_not_for_group_admins() -> HTTPException:
    """组管理员走错了入口:审批流不是给他准备的(codex #519 R8 P2)。

    ⚠ 刻意**不**套 `_group_not_found()` 那套 404 遮蔽:能走到这里说明他是这个组的管理员,
    组的存在性对他根本不是秘密,回一句「群组不存在」只会让他去查一个没有问题的组。文案
    给**可操作**的出口——他手上本来就有更直接的那条路。
    """
    return user_error(
        403, "你是这个群组的组管理员,直接共享给它即可,不必提交申请"
    )


def _notify_group_admins(group_id: str) -> None:
    """该组的待审批数变了 → 给它的**全部组管理员**各推一次待办快照(codex #519 R12 P1)。

    铃铛里 `share_request` 那一项的判据是「我是这个组的 admin」+「申请仍 pending」
    (见 `query_store` 的投影),所以受影响的正是**这个组的全部管理员**——不是只有动手的
    那一个:A 批准之后,B 的队列也少了一条。反过来也不能全量广播:别的组的管理员这一刻
    什么都没变。

    ⚠ 三条硬性:
    * **在写事务之外调**(全部调用点都在 store 返回之后),绝不持锁做 I/O;
    * **fail-open**:总线发不出去绝不能让已经提交成功的改动报失败——那会把「通知没送到」
      变成「批准失败」,正是 codex #519 R11 P2 刚修的那类混淆。`publish_snapshot` 自身已
      fail-open,这里再包一层是因为**解析管理员名单**这一步也可能抛;
    * **一次改动只解析一次名单**(一条 `list_members` 查询),不逐个管理员回查。
    """
    try:
        for member in group_repository().list_members(group_id):
            if member["role"] == "admin":   # 精确匹配,与投影里的 `gm.role='admin'` 同口径
                publish_snapshot(member["id"])
    except Exception:  # noqa: BLE001 - 通知是尽力而为,绝不影响已提交的改动
        pass


def _is_system_admin(user: UserProfile) -> bool:
    return user.role == "admin"


def _require_own_membership(group_id: str, user: UserProfile) -> str:
    """我**本人**在这个组里的角色;不是成员 → 404。

    刻意**不带**系统管理员旁路:唯一的调用点是自助退出,而「退出」的前提是本人真的
    在组里。给它旁路只会让一个不是成员的系统管理员拿到 204 却什么也没发生。
    """
    role = group_repository().user_group_role(group_id, user.id)
    if role is None:
        raise _group_not_found()
    return role


def _require_group_admin(group_id: str, user: UserProfile) -> None:
    """组管理员守卫。**不是**组管理员与**组不存在**同为 404。

    普通成员在这里也拿 404 而不是 403:他知道组存在,但「你不是管理员」这句话对他
    没有可操作性(能给他授权的正是管理员,界面本来就不该给他这个入口),而统一成
    404 让这道守卫只有一种响应形态,不必在每个调用点判「这次该露多少」。

    系统管理员经运维旁路放行(见模块 docstring),但**仍要求组真的存在**:旁路给的
    是「谁能管」,不是「管一个不存在的东西」。存在性那次额外查询只发生在旁路这条
    分支上,普通组管理员仍是一次查询。
    """
    groups = group_repository()
    if groups.user_group_role(group_id, user.id) == "admin":
        return
    if _is_system_admin(user) and groups.get_group(group_id) is not None:
        return
    raise _group_not_found()


def _require_group_owner(group_id: str, user: UserProfile) -> None:
    """Owner-only group actions, with the existing system-admin recovery bypass."""
    groups = group_repository()
    group = groups.get_group(group_id, user_id=user.id)
    if group is None or (not group["my_role"] and not _is_system_admin(user)):
        raise _group_not_found()
    if group["owner_id"] == user.id or _is_system_admin(user):
        return
    # The caller can only reach an owner action through a visible group. Say
    # which authority is missing instead of pretending the known group vanished.
    raise user_error(403, "只有群组所有者可以执行此操作")


def _require_group_visible(group_id: str, user: UserProfile) -> dict:
    """Any member may read the group workspace; system admins retain recovery access."""
    group = group_repository().get_group(group_id, user_id=user.id)
    if group is None or (not group["my_role"] and not _is_system_admin(user)):
        raise _group_not_found()
    return group


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
    """我所在的群组;`?scope=all` 是系统管理员的全局管理面。

    非法 `scope` 明确 422,**不静默当成 `mine`**:那会让一个拼错的 `?scope=al` 安静地
    返回一份收窄过的清单,调用方看到 200 就以为拿到了全部。
    """
    if scope not in GROUP_LIST_SCOPES:
        raise user_error(422, "查看范围只能是我所在的群组或全部群组")
    if scope == "all":
        if not _is_system_admin(user):
            raise user_error(403, "仅管理员可查看全部群组")
        return [GroupSummary(**g) for g in group_repository().list_all_groups(user_id=user.id)]
    return [GroupSummary(**g) for g in group_repository().list_groups_for_user(user.id)]


# This static prefix must be registered before ``/groups/{group_id}``: an
# invitation token is a bearer capability, not a group id and not a route that
# requires pre-existing group visibility.
@router.post("/group-invites/{token}/join", response_model=GroupDetail)
def join_group_invite_route(
    token: str, user: UserProfile = Depends(get_current_user)
) -> GroupDetail:
    """Join as a plain member. Unknown/revoked/deleted links share one 404."""
    groups = group_repository()
    joined = groups.join_by_invite(token, user_id=user.id)
    if joined is None:
        raise user_error(404, "群组邀请链接无效或已撤销")
    return GroupDetail(
        **joined,
        members=[GroupMemberItem(**m) for m in groups.list_members(joined["id"])],
    )


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
    """组详情 + 成员清单。仅组成员可见(系统管理员经运维旁路可见),非成员 404。

    可见性判定直接读 `get_group` 已经算好的 `my_role`,不再单发一次成员查询:非成员
    的 `my_role` 恒为空串,这与「不是成员」是同一件事。
    """
    groups = group_repository()
    group = groups.get_group(group_id, user_id=user.id)
    if group is None or (not group["my_role"] and not _is_system_admin(user)):
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
    _require_group_owner(group_id, user)
    try:
        deleted = group_repository().delete_group(
            group_id,
            actor_id=user.id,
            actor_is_system_admin=_is_system_admin(user),
        )
    except GroupOwnerRequiredError:
        raise user_error(403, "你已不再是该群组的所有者")
    if not deleted:
        raise _group_not_found()


@router.post("/groups/{group_id}/transfer", response_model=GroupDetail)
def transfer_group_owner_route(
    group_id: str,
    payload: GroupOwnerTransferRequest,
    user: UserProfile = Depends(get_current_user),
) -> GroupDetail:
    """Transfer the unique owner to an existing member; the old owner stays admin."""
    _require_group_owner(group_id, user)
    groups = group_repository()
    target_id = payload.new_owner_id.strip()
    if not target_id:
        raise user_error(400, "请选择新的群组所有者")
    try:
        group = groups.transfer_group_owner(
            group_id,
            new_owner_id=target_id,
            actor_id=user.id,
            actor_is_system_admin=_is_system_admin(user),
        )
    except GroupNotFoundError:
        raise _group_not_found()
    except GroupOwnerRequiredError:
        raise user_error(403, "你已不再是该群组的所有者")
    except GroupOwnerTransferTargetError:
        raise user_error(409, "新的群组所有者必须是当前群组成员")
    # A member promoted to admin gains this group's pending approvals in the
    # account badge. The ownership write has already committed, so notification
    # stays fail-open just like the ordinary member-role route below.
    publish_snapshot(target_id)
    return GroupDetail(
        **group, members=[GroupMemberItem(**m) for m in groups.list_members(group_id)]
    )


# ------------------------------------------------------------------- 组成员


@router.get("/groups/{group_id}/invite-link", response_model=GroupInviteState)
def get_group_invite_route(
    group_id: str, user: UserProfile = Depends(get_current_user)
) -> GroupInviteState:
    """Read the current link without creating one as a side effect."""
    _require_group_admin(group_id, user)
    try:
        return GroupInviteState(**group_repository().get_invite_state(
            group_id,
            actor_id=user.id,
            actor_is_system_admin=_is_system_admin(user),
        ))
    except (GroupNotFoundError, GroupAdminRequiredError):
        raise _group_not_found()


def _issue_group_invite(
    group_id: str, user: UserProfile, *, rotate: bool
) -> GroupInviteState:
    _require_group_admin(group_id, user)
    try:
        state = group_repository().issue_invite(
            group_id,
            token=new_capability_token("gri"),
            actor_id=user.id,
            actor_is_system_admin=_is_system_admin(user),
            rotate=rotate,
        )
    except (GroupNotFoundError, GroupAdminRequiredError):
        # The group may have disappeared, or the actor may have been demoted,
        # between the friendly preflight and the authoritative write lock.
        raise _group_not_found()
    return GroupInviteState(**state)


@router.post("/groups/{group_id}/invite-link", response_model=GroupInviteState)
def create_group_invite_route(
    group_id: str, user: UserProfile = Depends(get_current_user)
) -> GroupInviteState:
    """Create the link once; repeated calls return the same live capability."""
    return _issue_group_invite(group_id, user, rotate=False)


@router.post("/groups/{group_id}/invite-link/rotate", response_model=GroupInviteState)
def rotate_group_invite_route(
    group_id: str, user: UserProfile = Depends(get_current_user)
) -> GroupInviteState:
    """Atomically replace the current link, invalidating the old token."""
    return _issue_group_invite(group_id, user, rotate=True)


@router.delete("/groups/{group_id}/invite-link", status_code=204)
def revoke_group_invite_route(
    group_id: str, user: UserProfile = Depends(get_current_user)
) -> None:
    _require_group_admin(group_id, user)
    try:
        group_repository().revoke_invite(
            group_id,
            actor_id=user.id,
            actor_is_system_admin=_is_system_admin(user),
        )
    except (GroupNotFoundError, GroupAdminRequiredError):
        raise _group_not_found()


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
    except GroupOwnerProtectedError:
        raise user_error(409, "群组所有者不能被降级，请先转让群组")
    except LastGroupAdminError:
        raise user_error(409, "群组至少要保留一名组管理员")
    except GroupNotFoundError:
        # 组在守卫通过之后、写事务落库之前被并发删掉。少了这一支,SQLite 会撞
        # `group_members.group_id` 的外键抛 IntegrityError,用户拿到 500。
        raise _group_not_found()
    # 角色变了 → **被改的那个人**的铃铛要变(升成组管理员就多出这个组的待审批数,
    # 降级就少掉)。只推他一个:别人的判定一个字都没动。
    publish_snapshot(user_id)
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
    except GroupOwnerProtectedError:
        raise user_error(409, "群组所有者不能被移出，请先转让群组")
    except LastGroupAdminError:
        raise user_error(409, "群组至少要保留一名组管理员")
    except GroupNotFoundError:
        raise _group_not_found()
    if not removed:
        raise user_error(404, "该用户不是这个群组的成员")
    # 被移出组的人如果原来是组管理员,他的铃铛里这个组的待审批数要消失。
    publish_snapshot(user_id)


@router.delete("/groups/{group_id}/membership", status_code=204)
def leave_group_route(
    group_id: str, user: UserProfile = Depends(get_current_user)
) -> None:
    """自助退出。Owner 必须先转让；最后一名普通管理员也不得直接退出。"""
    _require_own_membership(group_id, user)
    try:
        group_repository().remove_member(group_id, user.id)
    except GroupOwnerProtectedError:
        raise user_error(409, "群组所有者必须先转让群组才能退出")
    except LastGroupAdminError:
        raise user_error(409, "你是这个群组唯一的组管理员,请先指定其他组管理员再退出")
    except GroupNotFoundError:
        raise _group_not_found()
    # 自助退出:退出者自己的铃铛要去掉这个组的待审批数。
    publish_snapshot(user.id)


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

    指向已不存在的群组的**孤儿边**带 `principal_kind="missing"` 标注(删组事务会清掉
    这类边,但 `principal_id` 没有外键,合库仍可能复活它们)。不标注的话它在界面上与
    正常条目长得一模一样,只是没有名字——库主既看不懂那是什么,也不知道能不能删。
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
    """共享给群组。**双重条件**:对库有管理权(依赖层已挡)+ 是目标组的组管理员。

    ⚠ 群组那一半的**权威判定在 store 的写事务里**(`create_grant` 同事务复核组存在
    与发起者的组管理员身份),这里不做前置查询。理由是这条边一落库就**立刻**给整组
    人读权:在路由层查一次、再在另一个事务里插入,中间那个窗口足够让组被删、或让
    发起者被移出/降级,而边照发不误。两个异常各自映射,文案仍能说清缺的是哪一半。

    ⚠ **笔记本那个外键父行同样在事务内复核** → `NotebookNotFoundError` → 404
    (codex #519 R7 存疑项收口,与 `create_share_request_route` 同款)。它复核的是
    **存在性**而不是权限:能力守卫放行之后、写事务之前库被并发删掉,PG 侧那条 INSERT
    会撞 `notebook_id` 外键变成 500。
    """
    principal_type = payload.principal_type.strip().lower()
    if principal_type not in GRANTABLE_PRINCIPAL_TYPES:
        raise user_error(422, "这里只能共享给群组;共享给个人请用只读共享链接")
    role = payload.role.strip().lower()
    if role not in GRANT_ROLES:
        raise user_error(422, "共享权限只能是只读或管理")
    try:
        grant = group_repository().create_grant(
            notebook_id,
            principal_type=principal_type,
            principal_id=payload.principal_id.strip(),
            role=role,
            created_by=user.id,
            admin_user_id=user.id,
        )
    except GroupNotFoundError:
        raise _group_not_found()
    except NotebookNotFoundError:
        # 与 `create_share_request_route` 逐字同一个响应:笔记本维度的「看不见」是 404,
        # 与能力守卫拒绝时同一个状态码。不复用 `_group_not_found()`——组没有问题。
        raise user_error(404, "笔记本不存在")
    except GroupAdminRequiredError:
        raise user_error(403, "你不是这个群组的组管理员,无法把知识库共享给它")
    except NotebookManageRequiredError:
        # 能力守卫放行之后、写事务之前被撤销了管理边(codex #519 R6 P1)。403 而不是
        # 404:能走到这一步说明他刚刚还有管理权,库的存在性对他不是秘密。
        raise user_error(403, "你已不再拥有这本笔记本的管理权")
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
    """Group workspace inventory. Members may view; admins manage it."""
    group = _require_group_visible(group_id, user)
    return [
        GroupSharedNotebookItem(**item)
        for item in group_repository().list_group_shared_notebooks(
            group_id,
            include_admin_only=(group["my_role"] == "admin" or _is_system_admin(user)),
        )
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


# --------------------------------------------------------------- 成员贡献审批流
#
# 授权边的两个写入口(库主/组管理员直接发边)覆盖的是「发起者本人就是目标组的组
# 管理员」那一半;这一节覆盖**另一半**:一个对某本库有管理权、但对目标组只是普通成员
# 的人,想把库贡献给那个组——他没有直接发边的权限,只能**申请**,由组管理员审批
# (设计文档 §4 决策 9)。状态机 **pending → approved / rejected 单向**(裁决 P2-2):
# 撤回是申请者删整行、不是第四个状态,所以 `decided_by`/`decided_at` 保持「组管理员做出
# 的决定」这一纯粹语义。批准把 `(group, viewer)` 边与状态更新放在**同一写事务**里——
# 批准这件事要么整体生效、要么整体不生效,不能出现「边发了、申请还挂着 pending」或
# 反过来的半成品。
#
# ⚠ 组管理员分享进**自己管理**的组永远不走这张表:他直接写 `notebook_grants` 行。


@router.post(
    "/notebooks/{notebook_id}/share-requests",
    response_model=ShareRequestItem,
    dependencies=[Depends(require_notebook_capability("notebook:manage"))],
)
def create_share_request_route(
    notebook_id: str,
    payload: ShareRequestCreate,
    user: UserProfile = Depends(get_current_user),
) -> ShareRequestItem:
    """提交共享申请。**双重条件**:对库有管理权(依赖层已挡)+ 是目标组的**普通成员**。

    组成员那一半在这里查。判据是 `role == 'member'` 的**正向精确匹配**,不是「有没有
    成员行」:审批流覆盖的是**另一半**入口——组管理员分享进自己管理的组永远走
    `POST /notebooks/{id}/grants`、**不经这张表**(设计 §4 决策 9)。放宽成「是成员即可」,
    组管理员就能建出一条 pending 申请再**自己批自己**(codex #519 R8 P2)。非成员与
    「组不存在」同为 **404**(群组的可见性口径):库主固然知道自己的库在,但他不知道那个
    组存不存在,统一 404 不泄露组的存在性;而组管理员拿的是 **403 + 可操作说明**,
    见 `_share_request_not_for_group_admins`。

    幂等:同库同组已有一条待审批申请时,`create_share_request` **返回既有那条**而不是
    报 409——申请者刷新页面重复点提交是常见操作,不该弹错误(裁决 P2-5)。但幂等**只对
    本人成立**:一本库可以有多个管理权持有者(owner + 组管理员),别人已经提过时给
    409 而不是把他的申请行返回给你——那会让界面报成功、自查列表里查不到、也撤不掉
    (codex #519 R3)。冲突文案刻意**不点名申请者**。

    ⚠ 这里的前置查询**只用来给出友好文案**,承重的复核在 store 的写事务里:组在守卫通过
    之后被删 → `GroupNotFoundError`;申请人在那之后被移出组 → `GroupMembershipRequiredError`
    (codex #519 R2 P2-1)。两者都映射成同一个 404 —— 与上面那次前置检查逐字同一个响应,
    群组维度的「看不见」口径不因为走到了哪一层而变。

    ⚠ **笔记本那个外键父行同样要复核** → `NotebookNotFoundError` → 404(codex #519 R7 P2)。
    这一条与上面两条不是同一类:它复核的是**存在性**而不是权限——能力守卫放行之后、写事务
    开始之前,这本库可以被并发删掉,而 `INSERT` 会撞 `notebook_id` 的外键,用户拿到的是 500。
    一次插入有几个外键,守卫与写事务之间就有几个父行可能消失,只堵组那一个不算堵住。
    """
    groups = group_repository()
    group_id = payload.group_id.strip()
    role = groups.user_group_role(group_id, user.id)
    if role is None:
        raise _group_not_found()
    if role != "member":
        # ⚠ 与下面那个 except 分支**同一个响应**,因此单独摘掉它不会改变任何可观测行为
        # (变异验证实测:摘掉后全绿)。它不是守卫,是与紧邻的 `role is None` 同形的
        # **友好前置检查**——承重判定在 store 的写事务里,那半有两条用例钉着。留着它是
        # 为了「不为一个必然失败的请求开写事务」,不是为了拦住谁。
        raise _share_request_not_for_group_admins()
    try:
        request = groups.create_share_request(
            notebook_id, group_id=group_id, requested_by=user.id
        )
    except (GroupNotFoundError, GroupMembershipRequiredError):
        raise _group_not_found()
    except GroupAdminShouldShareDirectlyError:
        # 前置检查读到 "member" 之后、写事务之前被提升成组管理员(codex #519 R8 P2)。
        # 与上面那一支**同一个响应**:承重判定在 store 的写事务里,路由这次只给文案。
        raise _share_request_not_for_group_admins()
    except NotebookNotFoundError:
        # 笔记本维度的「看不见」:与能力守卫拒绝时**同一个状态码**(404,不泄露存在性),
        # 只是文案走 `user_error()` 好让浏览器能直接上屏。刻意不复用 `_group_not_found()`
        # ——那句「群组不存在」在这里是错的,用户会去查一个根本没问题的组。
        raise user_error(404, "笔记本不存在")
    except ShareRequestAlreadyPendingError:
        raise user_error(409, "这本笔记本已有一条待该群组审批的申请")
    # 队列里多了一条 → 该组的管理员铃铛要立刻更新(事务已提交,fail-open)。
    _notify_group_admins(group_id)
    return ShareRequestItem(**request)


@router.get(
    "/notebooks/{notebook_id}/share-requests",
    response_model=List[ShareRequestItem],
    dependencies=[Depends(require_notebook_capability("notebook:manage"))],
)
def list_my_share_requests_route(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> List[ShareRequestItem]:
    """请求者本人对这本库发起过的申请(弹窗回显待审批 / 已驳回状态)。

    只回**本人**发起的(`requested_by = 当前用户`):这是「我提过哪些申请」的自查,不是
    库上全部申请的管理面。管理权那一半仍在依赖层挡住(不能替别人查他对别的组的申请)。
    """
    return [
        ShareRequestItem(**item)
        for item in group_repository().list_my_share_requests(
            notebook_id, requested_by=user.id
        )
    ]


@router.get("/me/share-requests", response_model=List[ShareRequestItem])
def list_my_pending_share_requests_route(
    user: UserProfile = Depends(get_current_user),
) -> List[ShareRequestItem]:
    """我发起的、仍**待审批**的全部共享申请 —— 跨笔记本,只要求登录。

    ⚠ **路径刻意在 `/me` 下而不是 `/notebooks/{id}/...`**:授权轴是**申请归属**,不是笔记本
    权限。挂成 notebook 维度再把守卫放宽,等于让「谁能列这本库的申请」有两套口径;放在 `/me`
    下,主体是「我」这件事从路径就读得出来。既有的
    `GET /notebooks/{id}/share-requests`(笔记本维度、`notebook:manage` 门)一个字不动。

    存在的理由是裁决 P2-7 的另一半(codex #519 R11 P1):撤回只认「这条申请是你提的」,
    因为 P2-6 让批准会拒绝已失权的申请人——两边都要管理权,这类申请就**既批不了也撤不掉**。
    但申请人一失权就打不开那本笔记本,唯一列出申请、拿到 id 的入口也跟着消失,于是那个刻意
    留下的口子在**它唯一存在意义的场景**里够不着。这条端点就是给他的全局入口。

    **披露面是有意选定的**,不是顺手把 store 的行倒出来(每一项都对照「他原本知不知道」):
    * `id` / `notebook_id` —— 撤回请求要用;两者都是他自己提交时就持有的。
    * `notebook_name` —— **带**。他提交申请时对这本库有管理权(创建端点要求 `notebook:manage`),
      库名是他早就知道的;而 `notebooks.created_by` 全仓只在建库与深拷贝时写入(无转让功能,
      `NotebookUpdate` 里也没有这一列,有护栏钉着),所以不存在「库易主后拿旧 id 探测新主人」
      这条通道。不带的话清单只能显示「某个知识库」,有多条待审批时根本分不清撤哪个。
    * `group_id` / `group_name` —— 他自己选的目标组,提交时是其成员。
    * `requested_by` / `requested_by_username` —— 他自己。
    * `status` 恒 `pending`;`decided_by` / `decided_at` 因此恒为 null —— 审批者身份**不**经这条
      路泄露(已决定的申请压根不在结果里)。
    * 库的其它任何状态(来源数、是否仍共享、现任成员……)一律不带。
    """
    return [
        ShareRequestItem(**item)
        for item in group_repository().list_pending_share_requests_by_requester(user.id)
    ]


@router.delete(
    "/notebooks/{notebook_id}/share-requests/{request_id}",
    status_code=204,
)
def delete_share_request_route(
    notebook_id: str,
    request_id: str,
    user: UserProfile = Depends(get_current_user),
) -> None:
    """撤回一条**待审批**申请(申请者自己的动作)。

    撤回不是第三个状态,是删整行(裁决 P2-2)。只有 `status='pending'` 可撤:已批准/已驳回
    是既成的决定,撤回它没有意义——store 在写事务里按精确状态判定,已决定的申请撤回请求
    映射成 **409**;根本没有这条申请(或不属于这本库、或不是本人提交的)则 404。

    ⚠ **本端点刻意没有 notebook 能力依赖,只要求登录**(codex #519 R6 P2)。授权轴是
    **申请归属**而不是当前笔记本权限:store 按三列谓词
    (`notebook_id` + `request_id` + `requested_by`)兜底,非本人一律 `not_found` → 404,
    与「这条申请不存在」不可区分(无存在性泄露),跨库拼 URL 因为 `notebook_id` 仍在
    WHERE 里同样 404。

    挂 `notebook:manage` 是**用错了轴**,而且在 R4 之后会造出死锁形态:R4 让批准拒绝
    「申请人已失去管理权」的申请,若撤回也要求管理权,这类申请就**既批不了也撤不掉**,
    永远卡在组管理员的队列里。也**不能**换成 `require_notebook_read`——读权同样可能随那条
    授权边一起没了。两条裁决是互补的:R4 防止陈旧授权被兑现,本条保证申请人**始终**能收回
    自己的提议。

    ⚠ `user.id` 必须传进 store:它现在是唯一的授权判据(此前还有能力守卫兜底)。同一本库
    可以有多个管理权持有者(owner + 组管理员),丢掉这个参数就等于让他们互相撤回对方的
    待审批申请(codex #519 R1 P1)。
    """
    groups = group_repository()
    # 撤回之后这一行就没了,拿不到它的 group_id 了,所以**先**读出来(只为通知用)。
    # 刻意不改 `delete_share_request` 的返回契约:它的 `"deleted"`/`"not_found"` 被十来处
    # 断言钉着,为一次尽力而为的通知去动它不划算。这是一次有界的单人查询(我在这本库上
    # 的申请,至多几条),不是 N+1;读在写之前的 TOCTOU 也无害——申请的 `group_id` 不可变。
    target_group = next(
        (
            item["group_id"]
            for item in groups.list_my_share_requests(notebook_id, requested_by=user.id)
            if item["id"] == request_id
        ),
        "",
    )
    try:
        outcome = groups.delete_share_request(notebook_id, request_id, user.id)
    except ShareRequestNotPendingError:
        raise user_error(409, "这条共享申请已经处理过了,无法撤回")
    if outcome == "not_found":
        raise user_error(404, "这条共享申请不存在")
    # 队列里少了一条 → 该组管理员的铃铛要更新。
    if target_group:
        _notify_group_admins(target_group)


@router.get(
    "/groups/{group_id}/share-requests",
    response_model=List[ShareRequestItem],
)
def list_group_share_requests_route(
    group_id: str, user: UserProfile = Depends(get_current_user)
) -> List[ShareRequestItem]:
    """组管理员的审核队列:共享给本组的**待审批**申请清单。"""
    _require_group_admin(group_id, user)
    return [
        ShareRequestItem(**item)
        for item in group_repository().list_pending_share_requests(group_id)
    ]


@router.post(
    "/groups/{group_id}/share-requests/{request_id}/approve",
    response_model=ShareRequestItem,
)
def approve_share_request_route(
    group_id: str,
    request_id: str,
    user: UserProfile = Depends(get_current_user),
) -> ShareRequestItem:
    """批准:同一写事务写 `(group, viewer)` 授权边并把申请标 approved。

    已共享(同库同组已有边)时幂等——不因「已经共享过」让批准失败,那会留下一条永远批
    不掉的申请。申请不存在或已被其他管理员决定 → 404(`approve_share_request` 返回
    `None`,并发双审由 store 的行锁 + 精确状态匹配挡住)。

    ⚠ 下面这道守卫**不是**最终判定:它与写事务之间有一个窗口,足够让这个人被降级或移出
    组,而批准会把**整组**的读权放出去(codex #519 R2 P1)。store 因此在同一写事务里再
    复核一次审批资格,不成立抛 `GroupAdminRequiredError` → **403**。这里的 403 不退化成
    404:能走到这一步说明他刚刚还是这个组的管理员,组的存在性对他本来就不是秘密——与
    `create_notebook_grant_route` 的同款取舍一致。系统管理员的运维旁路必须一路传到 store
    (他不必是组成员),所以显式把 `_is_system_admin(user)` 交给它,而不是让 store 去读
    `users.role`(store 不做身份解析)。

    ⚠ store 还会复核**申请人**此刻仍对这本库有管理权,不成立 → **409**(申请行保留,
    组管理员可自行驳回)。这条**推翻**了此前登记的「不复检申请人当前 manage 权是刻意
    设计」:授权在生效时刻实时判定、绝不缓存,而批准正是把一次陈旧检查兑现成一条活的
    授权边——库主可能早已撤掉申请人的管理权,却没有任何一方在这一刻验它。完整理由见
    `ShareRequesterUnauthorizedError`。
    """
    _require_group_admin(group_id, user)
    try:
        decided = group_repository().approve_share_request(
            group_id,
            request_id,
            decided_by=user.id,
            decided_by_is_system_admin=_is_system_admin(user),
        )
    except GroupAdminRequiredError:
        raise user_error(403, "你不是这个群组的组管理员,无法审批共享申请")
    except ShareRequesterUnauthorizedError:
        raise user_error(
            409, "申请人已不再拥有这本笔记本的管理权,无法批准这条申请"
        )
    if decided is None:
        raise user_error(404, "这条待审批的共享申请不存在或已被处理")
    # 队列里少了一条 —— 对**全组**管理员都成立,不只是动手的这一个。
    _notify_group_admins(group_id)
    return ShareRequestItem(**decided)


@router.post(
    "/groups/{group_id}/share-requests/{request_id}/reject",
    response_model=ShareRequestItem,
)
def reject_share_request_route(
    group_id: str,
    request_id: str,
    user: UserProfile = Depends(get_current_user),
) -> ShareRequestItem:
    """驳回:把申请标 rejected,不写任何授权边。

    申请不存在或已被决定 → 404,与批准同一口径。驳回后申请者可看到「已驳回」,并可对同
    库同组重新发起(rejected 不占那条部分唯一索引,`WHERE status='pending'` 才占)。

    审批资格的事务内复核与系统管理员旁路同 `approve_share_request_route`:驳回虽然不发
    授权边,但它同样是一个终态决定(把别人的申请判死),被降级的人不该还能做。
    """
    _require_group_admin(group_id, user)
    try:
        decided = group_repository().reject_share_request(
            group_id,
            request_id,
            decided_by=user.id,
            decided_by_is_system_admin=_is_system_admin(user),
        )
    except GroupAdminRequiredError:
        raise user_error(403, "你不是这个群组的组管理员,无法审批共享申请")
    if decided is None:
        raise user_error(404, "这条待审批的共享申请不存在或已被处理")
    # 同 approve:队列少一条对全组管理员都成立。
    _notify_group_admins(group_id)
    return ShareRequestItem(**decided)
