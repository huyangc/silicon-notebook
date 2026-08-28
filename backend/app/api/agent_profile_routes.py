"""Agentic Memory P1(T6)/P3(T5)——「AI 对这个库的理解」+「Agent 记录」API 面。

四个端点全部挂在 ``require_notebook_read`` 之下(owner ∪ 只读成员 ∪ 有效授权边;
非授权一律 404,不泄露存在性)。写操作再按 ``scope`` 分两条口径:

* ``scope="shared"``(共享底座,``owner_id=''``)——过
  ``notebook_capability_allowed("agent_profile:write", ...)``——与
  ``knowhow:write`` 同格,P2-T2 之后是 **admin 档**(owner ∪ ``role='admin'`` 的
  有效授权边),随 ``deps.py::_CAPABILITY_LEVELS`` 自动跟随,见该表上方第 6 条
  消费点登记;拒绝一律 404(与 ``require_notebook_write`` 同一形态)。
* ``scope="mine"``(该成员自己的覆盖层,``owner_id=<调用者自己的 user id>``)——
  只需读权。``owner_id`` **永远**由服务端从已认证的调用者身份派生,请求体里没有
  任何字段能指向别的成员,所以这一侧不需要额外的行级归属校验——它天然只能是
  调用者自己。

总闸(``AGENT_PROFILE_ENABLED``)的唯一判据是
``reasoning_retrieval.profile_wiring_active``——与注入侧、巡固触发侧共用同一个
函数,这里是第三个调用方。``GET`` 关闸时回 ``enabled=false`` + 空列表(**不是**
404:前端要能分清「关了」与「开着但还没写过」);``PUT``/``DELETE``/``POST
rebuild`` 关闸时统一 409(判定在计划里明确要求的是 rebuild,PUT/DELETE 按同一
口径处理,理由是关闸即「完全回到接入前」——写路径不应该在入口按钮消失之后仍然
可达)。

路由体保持薄:参数解析 + 守卫 + 一跳编排,实际读写全部委托给
``repository().agent_profile``(store)与 ``repository().agent_profile_jobs``
(巡固触发 service,``start_base``/``start_overlay`` 是 T6 手动重建复用的同一条
claim→submit 路径,见 ``agent_profile_job.py`` 的模块 docstring)。

――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――

P3(T5)在同一 router 里再加两个端点:``GET``/``DELETE
/notebooks/{id}/agent-observations``——外部 Agent 经 ``add_observation`` MCP
工具(T3)写下的短句,读/清界面。两个端点都是上面 ``scope="mine"`` 的那种
"mine"——``owner_id`` 永远是已认证调用者自己的 id,不接受任何请求字段指向别
的成员,所以**都只需要 ``require_notebook_read``,不需要**
``agent_profile:write``:这批行是调用者自己写给自己看的东西,与共享底座的
admin 档写权限无关。总闸判据同一个 ``_wiring_active()``——GET 关闸回
``enabled=false`` + 空列表,DELETE 关闸走 ``_DISABLED_MESSAGE`` 同款 409。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    get_current_user,
    notebook_capability_allowed,
    repository,
    require_notebook_read,
    user_error,
)
from app.core.config import get_settings
from app.models.agent_profile import (
    AgentCallOut,
    AgentObservationOut,
    AgentObservationsCleared,
    AgentObservationsResponse,
    UnderstandingBlockOut,
    UnderstandingBlockUpdate,
    UnderstandingJobs,
    UnderstandingJobStatus,
    UnderstandingLabel,
    UnderstandingRebuildRequest,
    UnderstandingRebuildResponse,
    UnderstandingResponse,
    UnderstandingScope,
)
from app.models.identity import UserProfile
from app.repositories.ports import (
    AGENT_CALL_RING_MAX,
    AGENT_CALL_SAMPLE_MAX,
    AGENT_OBSERVATION_KIND_CALL,
    AGENT_OBSERVATION_KIND_NOTE,
    AGENT_OBSERVATION_RING_MAX,
    AGENT_OBSERVATION_SAMPLE_MAX,
    AgentProfileRevisionConflict,
)
from app.services.agent_profile_block import (
    resolve_agent_profile_names,
    AGENT_PROFILE_VALUE_MAX_CHARS,
    PROFILE_LABEL_ORDER,
)
from app.services.agent_profile_job import BASE_CHAIN_OWNER, BASE_LABELS, OVERLAY_LABELS
from app.services.reasoning_retrieval import profile_wiring_active


router = APIRouter()

# 关闸时 PUT/DELETE/rebuild 的统一文案。三个端点共用一句而不是各写各的——「这项
# 功能没开」是同一件事,不该因为动作是编辑还是重建就换一种说法。
_DISABLED_MESSAGE = "这项功能当前未开启，暂时无法编辑"
_REBUILD_BUSY_MESSAGE = "正在整理，请稍候"
_REVISION_CONFLICT_MESSAGE = "这段理解刚被更新过，请刷新后再改"
_SCOPE_LABEL_MISMATCH_MESSAGE = "所选范围与这项内容不匹配，无法编辑"
_VALUE_TOO_LONG_MESSAGE = f"内容过长，最多 {AGENT_PROFILE_VALUE_MAX_CHARS} 字"
_UNKNOWN_KIND_MESSAGE = "要清空的记录种类不对，请刷新页面后重试"

#: 允许出现在 ``DELETE .../agent-observations?kind=`` 上的两个值。空串(清全部)
#: 不在其中——它是**缺省**,不是一个要被认出来的值,见该端点的说明。
_CLEARABLE_KINDS = frozenset(
    {AGENT_OBSERVATION_KIND_NOTE, AGENT_OBSERVATION_KIND_CALL}
)


def _profile_store():
    return repository().agent_profile


def _wiring_active() -> bool:
    return profile_wiring_active(get_settings(), _profile_store())


def _label_allowed_for_scope(scope: str, label: str) -> bool:
    allowed = BASE_LABELS if scope == "shared" else OVERLAY_LABELS
    return label in allowed


def _owner_for_scope(scope: str, notebook_id: str, user: UserProfile) -> str:
    """``scope="shared"`` resolves to the base sentinel and requires the
    ``agent_profile:write`` capability (404 on denial — existence is never
    disclosed to a caller who cannot write). ``scope="mine"`` resolves to the
    caller's OWN id, unconditionally: nothing here ever takes an owner id
    from the request, so there is no separate authorization check to make —
    a reader editing "mine" can only ever be editing their own row."""
    if scope == "shared":
        if not notebook_capability_allowed(
            "agent_profile:write", notebook_id, user.id
        ):
            raise HTTPException(status_code=404, detail="Notebook not found")
        return BASE_CHAIN_OWNER
    return user.id


def _block_out(row: dict) -> UnderstandingBlockOut:
    return UnderstandingBlockOut(
        label=str(row.get("label") or ""),
        value=str(row.get("value") or ""),
        evidence=list(row.get("evidence") or []),
        revision=int(row.get("revision") or 0),
        updated_at=str(row.get("updated_at") or ""),
        updated_origin=str(row.get("updated_origin") or ""),
    )


def _empty_block_out(label: str) -> UnderstandingBlockOut:
    """A block that has never been written. ``revision=0`` mirrors
    ``write_block``'s own "row doesn't exist yet" convention (``
    expected_revision=0`` creates it), so a client that reads this back and
    immediately PUTs is using the exact revision the store expects."""
    return UnderstandingBlockOut(
        label=label, value="", evidence=[], revision=0, updated_at="", updated_origin=""
    )


def _job_status(row: dict | None) -> UnderstandingJobStatus | None:
    if row is None:
        return None
    return UnderstandingJobStatus(
        status=str(row.get("status") or "idle"),
        pending=int(row.get("pending_signal") or 0),
        updated_at=str(row.get("updated_at") or ""),
        failure_reason=str(row.get("failure_reason") or ""),
    )


def _ordered_blocks(rows: list[dict], owner_id: str) -> list[UnderstandingBlockOut]:
    by_label = {
        str(row.get("label") or ""): row
        for row in rows
        if str(row.get("owner_id") or "") == owner_id
    }
    return [
        _block_out(by_label[label]) for label in PROFILE_LABEL_ORDER if label in by_label
    ]


@router.get(
    "/notebooks/{notebook_id}/understanding",
    response_model=UnderstandingResponse,
    dependencies=[Depends(require_notebook_read)],
)
def get_understanding(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> UnderstandingResponse:
    if not _wiring_active():
        # 关闸时不查这项能力——总闸已经决定了「不可编辑」，能力查询的结果无关
        # 紧要，白付一次查询没有意义；响应固定给 False。
        return UnderstandingResponse(
            enabled=False, base=[], mine=[], job=UnderstandingJobs(), can_edit_base=False
        )
    can_edit_base = notebook_capability_allowed(
        "agent_profile:write", notebook_id, user.id
    )
    store = _profile_store()
    # codex R7 P2:读序是契约——**先读两条 job 行,再读块**。反过来(先块后 job)
    # 时,一次巡固恰好在两读之间写块并 settle,响应就是「done + 旧块」:前端据此
    # 解除忙碌位、停止轮询,旧文本一直挂到重开面板。job 先走后,同一交错最坏是
    # 「running + 新块」——继续轮询,下一拍自然收敛到 done。
    #
    # ⚠ Registered residual (codex #520 R8 P2, accepted): job 读之后、块读之前
    # 被一次**新** run 认领的交错,返回的是「上一轮终态 + 旧块」——那是认领前一
    # 刻的**一致**快照,不是撕裂读。用单快照读法也关不掉它的用户可见形态:哪怕
    # 完美一致的响应,发出 1ms 后开始的 run 同样不在里面,前端同样按「无忙碌」停
    # 止轮询。轮询的本质就是快照会过期;R7 修掉的是「终态与它代表的写入不配对」
    # 这个撕裂形态,剩下的陈旧性由下一次交互/重开面板自愈,不再为它引入快照事务。
    base_job = store.job_row(notebook_id, BASE_CHAIN_OWNER)
    mine_job = store.job_row(notebook_id, user.id)
    rows = store.read_blocks(notebook_id, user.id)
    return UnderstandingResponse(
        enabled=True,
        base=_ordered_blocks(rows, BASE_CHAIN_OWNER),
        mine=_ordered_blocks(rows, user.id),
        job=UnderstandingJobs(base=_job_status(base_job), mine=_job_status(mine_job)),
        can_edit_base=can_edit_base,
    )


@router.put(
    "/notebooks/{notebook_id}/understanding/{label}",
    response_model=UnderstandingBlockOut,
    dependencies=[Depends(require_notebook_read)],
)
def update_understanding_block(
    notebook_id: str,
    label: UnderstandingLabel,
    payload: UnderstandingBlockUpdate,
    user: UserProfile = Depends(get_current_user),
) -> UnderstandingBlockOut:
    if not _wiring_active():
        raise user_error(409, _DISABLED_MESSAGE)
    if not _label_allowed_for_scope(payload.scope, label):
        raise user_error(422, _SCOPE_LABEL_MISMATCH_MESSAGE)
    if len(payload.value) > AGENT_PROFILE_VALUE_MAX_CHARS:
        raise user_error(422, _VALUE_TOO_LONG_MESSAGE)
    owner_id = _owner_for_scope(payload.scope, notebook_id, user)
    try:
        row = _profile_store().write_block(
            notebook_id,
            owner_id,
            label,
            value=payload.value,
            # 人写没有服务端计算过的证据——那个字段只属于巡固任务自己产出的
            # value(design §5.1),用户编辑覆盖的是断言本身,不是伪造一份证据。
            evidence=[],
            expected_revision=payload.expected_revision,
            origin="user",
            actor=user.id,
        )
    except AgentProfileRevisionConflict:
        raise user_error(409, _REVISION_CONFLICT_MESSAGE)
    return _block_out(row)


@router.delete(
    "/notebooks/{notebook_id}/understanding/{label}",
    response_model=UnderstandingBlockOut,
    dependencies=[Depends(require_notebook_read)],
)
def clear_understanding_block(
    notebook_id: str,
    label: UnderstandingLabel,
    scope: UnderstandingScope = Query(...),
    expected_revision: int = Query(..., ge=0),
    user: UserProfile = Depends(get_current_user),
) -> UnderstandingBlockOut:
    # codex R1 P2: DELETE 与 PUT 同享乐观并发——``expected_revision`` 是浏览器
    # **看到过**的那个版本,必填。服务端自读当前 revision 再清空会把「加载后被
    # 巡固/他人更新过」的未见内容清掉,恰恰绕开了 PUT 的保护。
    if not _wiring_active():
        raise user_error(409, _DISABLED_MESSAGE)
    if not _label_allowed_for_scope(scope, label):
        raise user_error(422, _SCOPE_LABEL_MISMATCH_MESSAGE)
    owner_id = _owner_for_scope(scope, notebook_id, user)
    store = _profile_store()
    current = store.read_block(notebook_id, owner_id, label)
    if current is None:
        # 从没写过的块:expected_revision==0(浏览器看到的就是空块)按幂等清空;
        # 非 0 说明浏览器看过的那行已被整块删掉(clear_all)——按冲突报,让前端
        # 重取后再决定。
        if expected_revision == 0:
            return _empty_block_out(label)
        raise user_error(409, _REVISION_CONFLICT_MESSAGE)
    try:
        row = store.clear_block(
            notebook_id,
            owner_id,
            label,
            expected_revision=expected_revision,
            actor=user.id,
        )
    except AgentProfileRevisionConflict:
        raise user_error(409, _REVISION_CONFLICT_MESSAGE)
    except KeyError:
        # 读到行之后、清空之前被别的请求整块删掉(``clear_all``)——浏览器看到的
        # 值确实已经不在了,按幂等空块收。
        return _empty_block_out(label)
    return _block_out(row)


@router.post(
    "/notebooks/{notebook_id}/understanding/rebuild",
    response_model=UnderstandingRebuildResponse,
    dependencies=[Depends(require_notebook_read)],
)
def rebuild_understanding(
    notebook_id: str,
    payload: UnderstandingRebuildRequest,
    user: UserProfile = Depends(get_current_user),
) -> UnderstandingRebuildResponse:
    if not _wiring_active():
        raise user_error(409, _DISABLED_MESSAGE)
    jobs = repository().agent_profile_jobs
    if payload.scope == "shared":
        if not notebook_capability_allowed(
            "agent_profile:write", notebook_id, user.id
        ):
            raise HTTPException(status_code=404, detail="Notebook not found")
        started = jobs.start_base(notebook_id)
    else:
        started = jobs.start_overlay(notebook_id, user.id)
    if not started:
        raise user_error(409, _REBUILD_BUSY_MESSAGE)
    return UnderstandingRebuildResponse(started=True)


# --------------------------------------------------------------------------
# P3(T5)——「Agent 记录」:调用者自己的 observation 日志读/清。
# --------------------------------------------------------------------------


def _observation_agent_names(owner_id: str) -> dict[str, str]:
    """Agent id → 显示名,只解析调用者**自己**名下的 Agent。

    与 ``mcp_server.py`` 的 ``_profile_names`` 共用同一个分页 helper
    ``resolve_agent_profile_names``(codex #535 R2 P2:原先各自只读第一页
    100 条,超过一页的 owner 会让老 profile 的记录落到「该 Agent」兜底名;
    现在按 roster 翻到尽头,两侧按构造是同一个查找)。
    """
    return resolve_agent_profile_names(
        repository().list_agent_profiles, owner_id
    )


@router.get(
    "/notebooks/{notebook_id}/agent-observations",
    response_model=AgentObservationsResponse,
    dependencies=[Depends(require_notebook_read)],
)
def get_agent_observations(
    notebook_id: str,
    limit: int = Query(
        AGENT_OBSERVATION_SAMPLE_MAX, ge=1, le=AGENT_OBSERVATION_RING_MAX
    ),
    # 调用记录有**自己**的取数宽度,不共用上面那个 ``limit``:两份清单的行来自
    # 两个独立的环,写入速率差一个数量级,共用一个参数就等于让调用侧的取数上限
    # 替 Agent 手写的短句做决定。
    call_limit: int = Query(AGENT_CALL_SAMPLE_MAX, ge=1, le=AGENT_CALL_RING_MAX),
    user: UserProfile = Depends(get_current_user),
) -> AgentObservationsResponse:
    # ``owner_id`` 是本端点的隔离层三:永远是已认证调用者自己的 id,从不取自
    # 请求(路径/查询/body 都没有任何字段能指向别的成员)——与 ``mine`` scope
    # 的 ``_owner_for_scope`` 同一条不变式,只是这里连分支都不需要,唯一的
    # owner 就是 ``user.id``。请求形状本身(路径/查询参数的存在与类型)由
    # `api_contract` 架构守卫冻结兜底,这里不重复断言(T3-T5 修复轮质量评审
    # 变异 ③ 的结论)。
    if not _wiring_active():
        return AgentObservationsResponse(enabled=False, items=[], calls=[])
    store = repository().agent_observations
    rows = store.list_observations(notebook_id, user.id, limit=limit)
    # 调用记录跟随它**自己**的开关。关掉时不发这次查询,也不把空清单说成
    # 「没有 Agent 来过」——前端据 ``calls_enabled`` 分辨「关了」与「开着但还
    # 没人调用过」,与总闸那一层 ``enabled`` 的分辨方式逐字相同。
    calls_enabled = bool(get_settings().agent_call_log_enabled)
    call_rows = (
        store.list_calls(notebook_id, user.id, limit=call_limit)
        if calls_enabled
        else []
    )
    names = _observation_agent_names(user.id)
    return AgentObservationsResponse(
        enabled=True,
        items=[
            AgentObservationOut(
                id=str(row.get("id") or ""),
                agent_profile_id=str(row.get("agent_profile_id") or ""),
                agent_name=names.get(str(row.get("agent_profile_id") or ""), ""),
                text=str(row.get("text") or ""),
                created_at=str(row.get("created_at") or ""),
            )
            for row in rows
        ],
        calls_enabled=calls_enabled,
        calls=[
            AgentCallOut(
                id=str(row.get("id") or ""),
                agent_profile_id=str(row.get("agent_profile_id") or ""),
                agent_name=names.get(str(row.get("agent_profile_id") or ""), ""),
                capability=str(row.get("capability") or ""),
                created_at=str(row.get("created_at") or ""),
            )
            for row in call_rows
        ],
    )


@router.delete(
    "/notebooks/{notebook_id}/agent-observations",
    response_model=AgentObservationsCleared,
    dependencies=[Depends(require_notebook_read)],
)
def clear_agent_observations(
    notebook_id: str,
    agent_profile_id: str = Query(""),
    kind: str = Query(""),
    user: UserProfile = Depends(get_current_user),
) -> AgentObservationsCleared:
    # 同一条不变式:``owner_id`` 只能是 ``user.id``。``agent_profile_id`` 与
    # ``kind`` 都只收窄「清哪些行」,从不改「清谁的」。请求形状本身同样由
    # `api_contract` 架构守卫冻结兜底(T3-T5 修复轮质量评审变异 ③ 的结论)。
    #
    # ``kind`` 走**白名单**而不是原样下传:它最终会进 SQL 的等值谓词,而一个
    # 拼错的值(或者一个存心试探的值)静默匹配零行、回 ``removed=0``,看起来
    # 与「本来就没有」一模一样——用户会以为清过了。不认识的值直接回 400。
    clean_kind = str(kind or "")
    if clean_kind and clean_kind not in _CLEARABLE_KINDS:
        raise user_error(400, _UNKNOWN_KIND_MESSAGE)
    if not _wiring_active():
        raise user_error(409, _DISABLED_MESSAGE)
    removed = repository().agent_observations.clear_observations(
        notebook_id,
        user.id,
        agent_profile_id=agent_profile_id,
        kind=clean_kind,
    )
    return AgentObservationsCleared(removed=removed)
