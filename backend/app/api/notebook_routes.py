from typing import List

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import (
    get_current_user,
    notebook_catalog_repository,
    notebook_delete_repository,
    notebook_sharing_repository,
    repository,
    require_notebook_capability,
    require_notebook_delete,
    require_notebook_read,
    user_error,
)
from app.core.audit_actor import session_audit_principal
from app.domain.indexing_pipeline import (
    IndexingPipelineLargeLibraryError,
    IndexingPipelineRebuildActiveError,
    IndexingPipelineStalePlanError,
    IndexingPipelineRebuildFailedError,
    IndexingPipelineUnavailableError,
)
from app.models.identity import UserProfile
from app.models.notebooks import (
    MountableNotebook,
    MountedBase,
    MountedByCount,
    IndexingPipelineResponse,
    NotebookAnalytics,
    NotebookCreate,
    NotebookDeleteResponse,
    NotebookSummary,
    NotebookUpdate,
    SetIndexingPipelineRequest,
    SetBasesRequest,
    SetTierRequest,
    ShareResponse,
    ShareState,
    SharedByMeItem,
    SharedPreview,
)
from app.repositories.ports import (
    KgBuildAlreadyRunning,
    KgMaintenanceAlreadyRunning,
    NotebookAlreadyDeletingError,
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


@router.get(
    "/notebooks/{notebook_id}/indexing-pipeline",
    response_model=IndexingPipelineResponse,
    dependencies=[Depends(require_notebook_read)],
)
def get_indexing_pipeline(notebook_id: str) -> IndexingPipelineResponse:
    """Return only the sanitized frozen registry projection for this notebook."""
    try:
        return IndexingPipelineResponse(**repository().indexing_pipeline_options(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.patch(
    "/notebooks/{notebook_id}/indexing-pipeline",
    response_model=IndexingPipelineResponse,
    dependencies=[Depends(require_notebook_capability("kg:write"))],
)
def set_indexing_pipeline(
    notebook_id: str,
    payload: SetIndexingPipelineRequest,
) -> IndexingPipelineResponse:
    """Build all source chunks first, then atomically publish the whole notebook."""
    try:
        return IndexingPipelineResponse(
            **repository().set_indexing_pipeline(notebook_id, payload.pipeline_id)
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except IndexingPipelineUnavailableError:
        raise user_error(
            409,
            "所选索引管线当前不可用；旧索引仍可读取。请切回内建管线后重试。",
        )
    except IndexingPipelineRebuildActiveError:
        # begin() 在改 desired 之前就拒绝了——什么都没保存,正在跑的重建不受影响。
        raise user_error(409, "索引重建正在进行；等它完成后再调整索引管线。")
    except IndexingPipelineLargeLibraryError:
        # 批 3·W3(D3):同样在改 desired 之前拒绝——什么都没保存,内建管线
        # 继续生效。切换的整库重建在此规模上事实不可完成,先显式禁用。
        raise user_error(409, "这本笔记本规模较大，暂不支持切换索引管线；当前索引不受影响。")
    except IndexingPipelineStalePlanError:
        raise user_error(409, "索引内容在重建期间发生变化，请重试切换。")
    except IndexingPipelineRebuildFailedError:
        raise user_error(409, "索引管线已保存但重建未完成；旧索引仍可读取，请重试或切回内建管线。")
    except KgBuildAlreadyRunning:
        raise user_error(409, "索引管线已保存，但另一项知识图谱任务正在运行；请稍后在设置中点「重试重建」。")
    except KgMaintenanceAlreadyRunning:
        # 批 3·W2 §2.1:同上一支的语义——desired 已落库、重建作业没起来,
        # 占槽的是维护动作(重新合并/补上关联)。文案同款,不落 500。
        raise user_error(409, "索引管线已保存，但另一项知识图谱任务正在运行；请稍后在设置中点「重试重建」。")


@router.patch("/notebooks/{notebook_id}", response_model=NotebookSummary, dependencies=[Depends(require_notebook_capability("notebook:manage"))])
def update_notebook(
    notebook_id: str,
    payload: NotebookUpdate,
) -> NotebookSummary:
    try:
        return notebook_catalog_repository().update_notebook(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.delete(
    "/notebooks/{notebook_id}",
    status_code=202,
    response_model=NotebookDeleteResponse,
    dependencies=[Depends(require_notebook_delete)],
)
def delete_notebook(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> NotebookDeleteResponse:
    """批 3·W1 PR-3 §T-2/§5:CAS tombstone 提交后立即返回 202——实际清理由
    后台删除作业异步完成(六相位,见 `services/notebook_delete.py`)。前端
    零改动:`requestVoid` 只看 2xx 就丢弃 body(`frontend/app/api-client.ts`),
    202 与之前的 204 对它完全透明。

    守卫改用 `require_notebook_delete`（codex #659 R6 P2）而不是
    `require_notebook_capability("notebook:delete")`：后者的
    NOTEBOOK_WRITE_SQL 带生命周期过滤，会把重复/重试的 DELETE 挡成假 404；
    这道守卫仍是 owner-only，只是放行 owner 自己正在删除/拷贝中的库，让下面
    这段 `except NotebookAlreadyDeletingError` 真正有机会跑到。"""
    try:
        return NotebookDeleteResponse(
            **notebook_delete_repository().request(notebook_id, user.id)
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except NotebookAlreadyDeletingError:
        raise user_error(409, "该笔记本正在被拷贝或已经在删除中，请稍后刷新查看。")


@router.post("/notebooks/{notebook_id}/tier", response_model=NotebookSummary, dependencies=[Depends(require_notebook_capability("notebook:manage"))])
def set_notebook_tier(notebook_id: str, payload: SetTierRequest, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    """Set a notebook's federation tier: 'base'(发布为公共知识库,可被任何笔记本
    挂载为参考库) 或 'personal'(撤回发布)。**不再全局唯一** —— 每个领域可以有自己
    的公共知识库。

    ⚠ 能力档留在 `notebook:manage`(P2-T2 后 = admin,组管理员可过守卫),但体内
    还有一道**系统管理员**门(`user.role != "admin"`):发布公共库始终只有系统
    管理员能做,组管理员过了能力守卫也会被这道 403 挡住。它不与 `notebook:configure`
    (挂载/链接分享,恒 owner)同轴——那批是 owner 对自己库的配置,这里是系统级
    发布,两道门的判据不同,刻意不合并。"""
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
            dependencies=[Depends(require_notebook_capability("notebook:configure"))])
def list_notebook_bases_route(notebook_id: str) -> List[MountedBase]:
    """本 notebook 挂载的参考库。含 active=False 的失效边(被挂库易主 / 公共库被
    降级),前端置灰展示——边保留是为了对方恢复后自动生效。"""
    return [MountedBase(**edge) for edge in repository().list_notebook_bases(notebook_id)]


@router.put("/notebooks/{notebook_id}/bases", response_model=List[MountedBase],
            dependencies=[Depends(require_notebook_capability("notebook:configure"))])
def set_notebook_bases_route(
    notebook_id: str, payload: SetBasesRequest,
    user: UserProfile = Depends(get_current_user),
) -> List[MountedBase]:
    """全量替换挂载集合。只接受本 notebook 的可挂候选 ∪ 当前已挂载的 id(含失效边),
    其余一律 400 —— 挂载边不是授权凭证,写入侧也要挡。可挂候选共四支:公共知识库、
    同 owner 的库、被挂库上有「全员可读」授权的库,以及本笔记本 owner 有受限读权
    (只读共享或点名/群组授权)且**本笔记本自身尚未被共享**的库。最后那道未共享门
    堵的是转手再分享:借来的参考库不能随着本笔记本再被共享出去。
    写权限本身由 require_notebook_capability("notebook:configure")(**恒 owner**,
    404 on denial)在依赖层挡;这里只做候选集校验,不重复手工判断写权限。
    ⚠ 挂载配置刻意**不随内容管理权翻给组管理员**(P2-T2 评审 P0):候选集
    (mountable_notebooks)按**被挂库 owner** `a.created_by` 解析,含「同 owner」支——
    组管理员若能改挂载,就能把库主从未共享的私有库挂进来经代理端点读全文。
    见 deps.py `_CAPABILITY_LEVELS` 上 notebook:configure 那条注释。

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
            dependencies=[Depends(require_notebook_capability("notebook:configure"))])
def mountable_notebooks_route(notebook_id: str) -> List[MountableNotebook]:
    """可挂候选 = 公共知识库 ∪ 同 owner 的库 ∪ 有「全员可读」授权的库 ∪
    (本库 owner 有受限读权的库,且仅当本笔记本自身尚未被共享)。

    最后一支的未共享门堵的是转手再分享:借来的参考库不能随着本笔记本再被共享出去。

    ⚠ 恒 owner(notebook:configure):候选按**被挂库 owner** `a.created_by` 解析,
    「同 owner」支会列出库主的**全部私有库**(含从未共享的)。组管理员若能调它,就拿到
    了库主全部私有库名的枚举——所以这条与 PUT bases 一起是 owner-only(P2-T2 评审 P0)。

    响应模型是 `MountableNotebook` 而不是 `NotebookRef`:每个候选还带一个 `origin`
    (base / mine / shared),让挂载选择器能如实分组——群组共享放开之后,别人 owner
    的库也会出现在这份候选里,只按 `tier` 分组会把它们标成「我的笔记本」。

    刻意挂在 {notebook_id} 下而非 /notebooks/mountable —— 后者会与既有的
    /notebooks/{notebook_id} 争路由匹配(FastAPI 按声明序,静态段必须先注册)。"""
    return [MountableNotebook(**n) for n in repository().mountable_notebooks(notebook_id)]


@router.get("/notebooks/{notebook_id}/mounted-by-count", response_model=MountedByCount,
            dependencies=[Depends(require_notebook_capability("notebook:configure"))])
def mounted_by_count_route(notebook_id: str) -> MountedByCount:
    """删除确认弹窗专用(spec §6):有多少笔记本正在把本 notebook 挂为参考库——
    ON DELETE CASCADE 会连同这些边一起清空且不可撤销,用户点删除前必须看到影响面。
    它只喂 owner-only 的删除确认流,自身语义是 owner-scoped,故归 notebook:configure
    (恒 owner)——与 DELETE 同为 owner-only,不随内容管理权翻给组管理员(P2-T2 评审)。
    计数本身不泄露库名/token,归 configure 是为了让这个「与 DELETE 同权」的旧承诺在
    翻格后仍然成立,而不是留在 notebook:manage 上被顺带翻成 admin。"""
    return MountedByCount(count=repository().mounted_by_count(notebook_id))


@router.get("/notebooks/{notebook_id}/share", response_model=ShareState,
            dependencies=[Depends(require_notebook_capability("notebook:configure"))])
def share_state_route(notebook_id: str) -> ShareState:
    """当前的分享链接状态。**只读**——打开分享弹窗不该铸出一条链接。

    与 POST/DELETE 同为 owner-only(notebook:configure):它回传 `share_token`——一条
    只要拿到就能整本 copy/join 的持有型凭证。组管理员即便有内容管理权也不该读到它
    (会把库主对外处置的链接转手泄露出去,P2-T2 评审 P0)。「共享给群组」那一节的
    授权边管理走独立的 notebook:manage(GET/POST/DELETE /grants),前端因此对组管理员
    跳过这条 GET、只渲染群组授权区(见 page.tsx openShareModal)。

    没有 token 时返回空串且**不计算**规模统计。理由见 `ShareState` 的模型注释
    (「只想共享给群组」的用户不该因为打开了一次弹窗就被发一条分享链接)。
    """
    try:
        return ShareState(**notebook_sharing_repository().share_state(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/share", response_model=ShareResponse,
             dependencies=[Depends(require_notebook_capability("notebook:configure"))])
def share_notebook_route(notebook_id: str) -> ShareResponse:
    """铸/取回本库的公开分享链接。**恒 owner**(notebook:configure):链接是库主对外
    处置(拿到链接的任何登录用户可整本 copy 或只读 join),不随内容管理权转移
    ——组管理员替库主铸链接、组外任意人整本 copy 正是 P2-T2 评审复现的 P0。"""
    try:
        return ShareResponse(**notebook_sharing_repository().share_notebook(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.delete("/notebooks/{notebook_id}/share", status_code=204,
               dependencies=[Depends(require_notebook_capability("notebook:configure"))])
def unshare_notebook_route(notebook_id: str) -> None:
    """撤销分享链接。**恒 owner**(notebook:configure):撤销会连带**踢掉全部只读
    成员**(unshare_notebook 清 notebook_members),爆炸半径超出内容管理,不给组管理员。"""
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
    # codex #659 R11 P1: find_notebook_by_share_token now filters out a
    # non-live notebook (NOTEBOOK_LIVE_SQL), but a narrower TOCTOU race
    # remains — the delete job's tombstone could land in the instant between
    # that resolution and this call. shared_preview() reaches
    # NotebookCatalogService.get_notebook() internally, which raises
    # KeyError for exactly that case; map it to the same 404 rather than
    # letting it surface as a bare 500.
    try:
        return SharedPreview(**sharing.shared_preview(nb_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Shared notebook not found")


@router.post("/shared/{token}/copy", response_model=NotebookSummary)
def copy_shared_route(token: str, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    sharing = notebook_sharing_repository()
    nb_id = sharing.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    # codex #659 R11 P1: same TOCTOU race as shared_preview_route above —
    # notebook_copy_stats()/copy_notebook() can raise KeyError if the
    # tombstone lands after find_notebook_by_share_token resolved but before
    # this call runs; map it to 404 instead of a bare 500.
    try:
        if not sharing.notebook_copy_stats(nb_id)["copyable"]:
            # codex #659 R23 P2: the verdict above can come from the
            # KG-version-cached copy_stats without touching a single
            # live-filtered query, so a tombstone that landed after token
            # resolution would surface as this 409 — a deleting notebook
            # staying observable through the share endpoint. Re-resolve the
            # token (live-filtered, R11) immediately before the size-based
            # early response; gone → the documented 404.
            if sharing.find_notebook_by_share_token(token) is None:
                raise HTTPException(status_code=404, detail="Shared notebook not found")
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
    except KeyError:
        raise HTTPException(status_code=404, detail="Shared notebook not found")


@router.post("/shared/{token}/join", response_model=NotebookSummary)
def join_shared_route(token: str, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    """大库只读加入:凭 share_token 成为只读成员。小库应走 copy 而非 join。"""
    sharing = notebook_sharing_repository()
    nb_id = sharing.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    # codex #659 R11 P1: same TOCTOU race as the two routes above — map a
    # race-induced KeyError to 404 instead of a bare 500.
    try:
        if sharing.notebook_copy_stats(nb_id)["copyable"]:
            # codex #659 R23 P2: same cached-verdict TOCTOU as
            # copy_shared_route — recheck liveness before the size-based
            # early response so a deleting notebook 404s instead of 400.
            if sharing.find_notebook_by_share_token(token) is None:
                raise HTTPException(status_code=404, detail="Shared notebook not found")
            raise HTTPException(status_code=400, detail="small notebook — use copy, not join")
        return sharing.join_shared(nb_id, user.id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Shared notebook not found")


@router.delete("/notebooks/{notebook_id}/membership", status_code=204)
def leave_notebook_route(notebook_id: str, user: UserProfile = Depends(get_current_user)) -> None:
    """退出只读共享:只删自己的成员记录(幂等,不影响他人)。"""
    notebook_sharing_repository().leave_notebook(notebook_id, user.id)
