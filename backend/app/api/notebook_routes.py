from typing import List

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import (
    get_current_user,
    notebook_catalog_repository,
    notebook_sharing_repository,
    repository,
    require_notebook_capability,
    require_notebook_read,
    user_error,
)
from app.core.audit_actor import session_audit_principal
from app.models.identity import UserProfile
from app.models.notebooks import (
    MountableNotebook,
    MountedBase,
    MountedByCount,
    NotebookAnalytics,
    NotebookCreate,
    NotebookSummary,
    NotebookUpdate,
    SetBasesRequest,
    SetTierRequest,
    ShareResponse,
    ShareState,
    SharedByMeItem,
    SharedPreview,
)


router = APIRouter()


@router.get("/notebooks", response_model=List[NotebookSummary])
def list_notebooks() -> List[NotebookSummary]:
    return notebook_catalog_repository().list_notebooks()


# 注意:静态段路由必须在 /notebooks/{notebook_id} 之前注册,否则 "shared-by-me" 被当作 {notebook_id}。
@router.get("/notebooks/shared-by-me", response_model=List[SharedByMeItem])
def shared_by_me_route(user: UserProfile = Depends(get_current_user)) -> List[SharedByMeItem]:
    return [SharedByMeItem(**it) for it in notebook_sharing_repository().shared_by_me(user.id)]


@router.post("/notebooks", response_model=NotebookSummary)
def create_notebook(payload: NotebookCreate) -> NotebookSummary:
    return notebook_catalog_repository().create_notebook(payload)


@router.get("/notebooks/{notebook_id}", response_model=NotebookSummary, dependencies=[Depends(require_notebook_read)])
def get_notebook(notebook_id: str) -> NotebookSummary:
    try:
        return notebook_catalog_repository().get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/analytics", response_model=NotebookAnalytics, dependencies=[Depends(require_notebook_read)])
def notebook_analytics(notebook_id: str) -> NotebookAnalytics:
    try:
        return notebook_catalog_repository().notebook_analytics(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.patch("/notebooks/{notebook_id}", response_model=NotebookSummary, dependencies=[Depends(require_notebook_capability("notebook:manage"))])
def update_notebook(
    notebook_id: str,
    payload: NotebookUpdate,
) -> NotebookSummary:
    try:
        return notebook_catalog_repository().update_notebook(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.delete("/notebooks/{notebook_id}", status_code=204, dependencies=[Depends(require_notebook_capability("notebook:delete"))])
def delete_notebook(notebook_id: str) -> None:
    try:
        notebook_catalog_repository().delete_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/tier", response_model=NotebookSummary, dependencies=[Depends(require_notebook_capability("notebook:manage"))])
def set_notebook_tier(notebook_id: str, payload: SetTierRequest, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    """Set a notebook's federation tier: 'base'(发布为公共知识库,可被任何笔记本
    挂载为参考库) 或 'personal'(撤回发布)。**不再全局唯一** —— 每个领域可以有自己
    的公共知识库。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可设为公共知识库")
    tier = payload.tier.strip().lower()
    if tier not in {"base", "personal"}:
        raise HTTPException(status_code=400, detail="tier must be 'base' or 'personal'")
    try:
        catalog = notebook_catalog_repository()
        if tier == "base":
            catalog.mark_notebook_base(notebook_id)
        else:
            catalog.set_notebook_personal(notebook_id)
        return catalog.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/bases", response_model=List[MountedBase],
            dependencies=[Depends(require_notebook_capability("notebook:manage"))])
def list_notebook_bases_route(notebook_id: str) -> List[MountedBase]:
    """本 notebook 挂载的参考库。含 active=False 的失效边(被挂库易主 / 公共库被
    降级),前端置灰展示——边保留是为了对方恢复后自动生效。"""
    return [MountedBase(**edge) for edge in repository().list_notebook_bases(notebook_id)]


@router.put("/notebooks/{notebook_id}/bases", response_model=List[MountedBase],
            dependencies=[Depends(require_notebook_capability("notebook:manage"))])
def set_notebook_bases_route(
    notebook_id: str, payload: SetBasesRequest,
    user: UserProfile = Depends(get_current_user),
) -> List[MountedBase]:
    """全量替换挂载集合。只接受本 notebook 的可挂候选 ∪ 当前已挂载的 id(含失效边),
    其余一律 400 —— 挂载边不是授权凭证,写入侧也要挡。可挂候选共四支:公共知识库、
    同 owner 的库、被挂库上有「全员可读」授权的库,以及本笔记本 owner 有受限读权
    (只读共享或点名/群组授权)且**本笔记本自身尚未被共享**的库。最后那道未共享门
    堵的是转手再分享:借来的参考库不能随着本笔记本再被共享出去。
    写权限本身由 require_notebook_capability("notebook:manage")(P0 阶段解析到
    owner-only,404 on denial)在依赖层挡;
    这里只做候选集校验,不重复手工判断写权限。

    并入"当前已挂载的 id"是刻意的:mountable_notebooks 与失效边的判定谓词
    (MOUNT_VALID_EXPR)是同一个表达式,所以一条失效边(被挂库降级/易主、共享被撤销,
    或本笔记本自身被共享而关闭了借入边之后)永远不会出现在 mountable 里。前端编辑表单原样重新提交"保留这条失效边不变"的挂载集合
    (不做任何前端过滤 —— 那会静默删掉设计上刻意保留、等对方重新发布后自动恢复的边)
    时,若只拿 mountable 当白名单会把这个合法保留动作也 400 掉,导致表单永久存不了。
    并入的是"已挂载"而非"任意 id",所以仍然拒绝新挂一个从未属于本笔记本的无效 id。"""
    repo = repository()
    allowed = {n["id"] for n in repo.mountable_notebooks(notebook_id)}
    allowed |= {edge["id"] for edge in repo.list_notebook_bases(notebook_id)}
    wanted = [nb_id for nb_id in dict.fromkeys(payload.base_notebook_ids) if nb_id]
    if any(nb_id not in allowed for nb_id in wanted):
        raise user_error(400, "选择里包含不能作为参考库的知识库")
    repo.replace_notebook_bases(notebook_id, wanted, user.id)
    return [MountedBase(**edge) for edge in repo.list_notebook_bases(notebook_id)]


@router.get("/notebooks/{notebook_id}/mountable", response_model=List[MountableNotebook],
            dependencies=[Depends(require_notebook_capability("notebook:manage"))])
def mountable_notebooks_route(notebook_id: str) -> List[MountableNotebook]:
    """可挂候选 = 公共知识库 ∪ 同 owner 的库 ∪ 有「全员可读」授权的库 ∪
    (本库 owner 有受限读权的库,且仅当本笔记本自身尚未被共享)。

    最后一支的未共享门堵的是转手再分享:借来的参考库不能随着本笔记本再被共享出去。

    响应模型是 `MountableNotebook` 而不是 `NotebookRef`:每个候选还带一个 `origin`
    (base / mine / shared),让挂载选择器能如实分组——群组共享放开之后,别人 owner
    的库也会出现在这份候选里,只按 `tier` 分组会把它们标成「我的笔记本」。

    刻意挂在 {notebook_id} 下而非 /notebooks/mountable —— 后者会与既有的
    /notebooks/{notebook_id} 争路由匹配(FastAPI 按声明序,静态段必须先注册)。"""
    return [MountableNotebook(**n) for n in repository().mountable_notebooks(notebook_id)]


@router.get("/notebooks/{notebook_id}/mounted-by-count", response_model=MountedByCount,
            dependencies=[Depends(require_notebook_capability("notebook:manage"))])
def mounted_by_count_route(notebook_id: str) -> MountedByCount:
    """删除确认弹窗专用(spec §6):有多少笔记本正在把本 notebook 挂为参考库——
    ON DELETE CASCADE 会连同这些边一起清空且不可撤销,用户点删除前必须看到影响面。
    与 DELETE 端点用同一个 owner-only 依赖(不新开一套权限判断)。"""
    return MountedByCount(count=repository().mounted_by_count(notebook_id))


@router.get("/notebooks/{notebook_id}/share", response_model=ShareState,
            dependencies=[Depends(require_notebook_capability("notebook:manage"))])
def share_state_route(notebook_id: str) -> ShareState:
    """当前的分享链接状态。**只读**——打开分享弹窗不该铸出一条链接。

    与 POST 同路径同守卫,只是这一条没有副作用:没有 token 时返回空串且**不计算**
    规模统计。理由见 `ShareState` 的模型注释(「只想共享给群组」的用户不该因为打开
    了一次弹窗就被发一条分享链接)。
    """
    try:
        return ShareState(**notebook_sharing_repository().share_state(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/share", response_model=ShareResponse,
             dependencies=[Depends(require_notebook_capability("notebook:manage"))])
def share_notebook_route(notebook_id: str) -> ShareResponse:
    try:
        return ShareResponse(**notebook_sharing_repository().share_notebook(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.delete("/notebooks/{notebook_id}/share", status_code=204,
               dependencies=[Depends(require_notebook_capability("notebook:manage"))])
def unshare_notebook_route(notebook_id: str) -> None:
    try:
        notebook_sharing_repository().unshare_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/shared/{token}", response_model=SharedPreview)
def shared_preview_route(token: str, user: UserProfile = Depends(get_current_user)) -> SharedPreview:
    sharing = notebook_sharing_repository()
    nb_id = sharing.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    return SharedPreview(**sharing.shared_preview(nb_id))


@router.post("/shared/{token}/copy", response_model=NotebookSummary)
def copy_shared_route(token: str, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    sharing = notebook_sharing_repository()
    nb_id = sharing.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    if not sharing.notebook_copy_stats(nb_id)["copyable"]:
        raise HTTPException(status_code=409, detail="notebook too large to copy")
    from app.services.notebook_sharing import NotebookTooLargeToCopyError
    try:
        principal = session_audit_principal(user)
        return sharing.copy_notebook(
            nb_id, new_owner_id=principal.identity_id,
            actor_label=principal.audit_label,
        )
    except NotebookTooLargeToCopyError:
        # If ingestion pushed the notebook past the limit after the pre-check
        # above, copy_notebook's atomic within_copy_row_limit() bound (checked on
        # the snapshot's own connection) refuses the copy — map that to the
        # documented 409, not a 500.
        raise HTTPException(status_code=409, detail="notebook too large to copy")


@router.post("/shared/{token}/join", response_model=NotebookSummary)
def join_shared_route(token: str, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    """大库只读加入:凭 share_token 成为只读成员。小库应走 copy 而非 join。"""
    sharing = notebook_sharing_repository()
    nb_id = sharing.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    if sharing.notebook_copy_stats(nb_id)["copyable"]:
        raise HTTPException(status_code=400, detail="small notebook — use copy, not join")
    return sharing.join_shared(nb_id, user.id)


@router.delete("/notebooks/{notebook_id}/membership", status_code=204)
def leave_notebook_route(notebook_id: str, user: UserProfile = Depends(get_current_user)) -> None:
    """退出只读共享:只删自己的成员记录(幂等,不影响他人)。"""
    notebook_sharing_repository().leave_notebook(notebook_id, user.id)
