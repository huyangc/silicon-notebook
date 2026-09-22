"""Agentic Memory P1(T6)/P3(T5)/P2(PR-2)——「AI 对这个库的理解」+「Agent 记录」
+「检索经验」API 面。

七个端点全部挂在 ``require_notebook_read`` 之下(owner ∪ 只读成员 ∪ 有效授权边;
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

――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――

P2(PR-2)再加三个端点:``GET``/``POST .../distill``/``DELETE
/notebooks/{id}/understanding/experiences``——这本笔记本自己那一份检索经验
(设计文档 §8 + §13-Q2)。挂在同一个 router 下是刻意的:界面上它就是理解面板
的第四张卡,读权判据、镜像围栏、404 形态全部沿用上面那一套,**不新开鉴权面**。

与上面两组的三处不同,每一处都有理由:

* 这批行**没有 owner**(见 ``RetrievalExperienceStorePort``:一条经验属于一个
  库,不属于任何人),所以读是全体成员的,没有 ``scope`` 这个维度。
* 总闸是**另一把**——``RETRIEVAL_EXPERIENCE_ENABLED``,判据
  ``distillation_wiring_active``,与 ``AGENT_PROFILE_ENABLED`` 相互独立:一个
  部署可以只开理解块、不开经验蒸馏,反之亦然。
* **清空不跟随总闸**(``DELETE`` 关闸照样能删),与 P3 调用记录同一条理由:
  关开关说的是「从现在起不再记」,不是「把记过的藏起来还删不掉」。只有会真的
  排一次蒸馏的 ``POST .../distill`` 在关闸时 409。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    get_current_user,
    mirrored_notebook_error,
    notebook_capability_allowed,
    notebook_mirror_fence,
    repository,
    require_notebook_read,
    retrieval_experience_jobs_service,
    retrieval_experience_store,
    user_error,
)
from app.core.config import get_settings
from app.models.agent_profile import (
    AgentCallOut,
    AgentObservationOut,
    AgentObservationsCleared,
    AgentObservationsResponse,
    ExperienceDistillStarted,
    ExperienceEntryOut,
    ExperiencePartitionCleared,
    ExperiencePartitionResponse,
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
    RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES,
    AgentProfileRevisionConflict,
)
from app.services.agent_profile_block import (
    resolve_agent_profile_names,
    AGENT_PROFILE_VALUE_MAX_CHARS,
    PROFILE_LABEL_ORDER,
)
from app.services.agent_profile_job import BASE_CHAIN_OWNER, BASE_LABELS, OVERLAY_LABELS
from app.services.reasoning_retrieval import profile_wiring_active
from app.services.retrieval_experience_job import (
    MANUAL_DISTILL_BUSY,
    MANUAL_DISTILL_COOLDOWN,
    MANUAL_DISTILL_DISABLED,
    MANUAL_DISTILL_INVALID,
    MANUAL_DISTILL_OUTCOMES,
    MANUAL_DISTILL_STARTED,
    distillation_wiring_active,
)


router = APIRouter()

# 关闸时 PUT/DELETE/rebuild 的统一文案。三个端点共用一句而不是各写各的——「这项
# 功能没开」是同一件事,不该因为动作是编辑还是重建就换一种说法。
_DISABLED_MESSAGE = "这项功能当前未开启，暂时无法编辑"
_REBUILD_BUSY_MESSAGE = "正在整理，请稍候"
_REVISION_CONFLICT_MESSAGE = "这段理解刚被更新过，请刷新后再改"
_SCOPE_LABEL_MISMATCH_MESSAGE = "所选范围与这项内容不匹配，无法编辑"
_VALUE_TOO_LONG_MESSAGE = f"内容过长，最多 {AGENT_PROFILE_VALUE_MAX_CHARS} 字"
_UNKNOWN_KIND_MESSAGE = "要清空的记录种类不对，请刷新页面后重试"
#: 检索经验那把**另外**的总闸关着时,「立即整理」的 409 文案。与上面那句刻意
#: 分开:那一句说的是「不能编辑」,而这个按钮并不编辑任何东西,它排的是一次整理。
_EXPERIENCE_DISABLED_MESSAGE = "这项功能当前未开启，暂时无法整理"
#: 冷却期内的拒绝:刚跑过一批、而且这个库此后再没有新的提问。文案必须同时说清
#: 「刚整理过」与「没有新的提问」两件事——只说前者,用户会以为再等一会就能按;
#: 只说后者,他会以为自己刚才那次提问没被记下。
_EXPERIENCE_COOLDOWN_MESSAGE = "刚整理过，暂时没有新的提问，请稍后再试"
#: ``MANUAL_DISTILL_INVALID`` 的兜底句。这条路由走不到它(分区是路径段,不可能
#: 为空),但 ``distill_now`` 是可以从别处调的,所以这一支仍然要有一句人话。
_EXPERIENCE_UNAVAILABLE_MESSAGE = "无法开始整理，请刷新页面后重试"

#: ``distill_notebook_experiences`` 的分支覆盖到的结果集合,在 import 期与
#: ``distill_now`` 的返回全集对账。一条 ``if/elif`` 链**漏掉**新增的一种结果只
#: 会安静地落到兜底那句——那正是这条断言要挡的:在 job 模块加一种结果而忘了在
#: 这里给它一句话,进程起不来,而不是在生产里说错话。形态与 ``models/
#: agent_profile.py`` 里 ``UnderstandingLabel`` / ``PROFILE_LABEL_ORDER`` 的
#: import 期断言逐字相同。
_HANDLED_DISTILL_OUTCOMES = frozenset({
    MANUAL_DISTILL_STARTED,
    MANUAL_DISTILL_BUSY,
    MANUAL_DISTILL_COOLDOWN,
    MANUAL_DISTILL_DISABLED,
    MANUAL_DISTILL_INVALID,
})
assert _HANDLED_DISTILL_OUTCOMES == MANUAL_DISTILL_OUTCOMES, (
    "distill_notebook_experiences must answer every distill_now outcome"
)

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
        # 镜像写入围栏,与所有装饰器端点同序(权限先判)。今天
        # ``_CAPABILITY_MIRROR_FENCE["agent_profile:write"]`` 是 False——理解底座是
        # 目标端用户自己用出来的,不在同步闭包里——所以这一句恒为 no-op。**刻意写在
        # 这里而不是省掉**:归属只登记在那张表上一处,哪天那一格翻成 True,这条写路径
        # 自动跟随,而不是变成一个「表上说挡、实际放行」的漏点。
        mirrored = notebook_mirror_fence("agent_profile:write", notebook_id)
        if mirrored:
            raise mirrored_notebook_error(mirrored)
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


# --------------------------------------------------------------------------
# P2(PR-2)——「检索经验」:这本笔记本自己那一份经验条目的读 / 整理 / 清空。
#
# ⚠ **这一节的位置是功能性的,不是编排口味**:它必须排在下面
# ``/notebooks/{id}/understanding/{label}`` 那两个端点**之前**。Starlette 按
# **注册顺序**取第一个路径 + 方法都匹配的路由,而 ``{label}`` 的路径参数正则是
# ``[^/]+``——它照样匹配字面量 ``experiences``。顺序反过来时,
# ``DELETE .../understanding/experiences`` 会落进 ``clear_understanding_block``,
# 并因为 ``label`` 不在 ``UnderstandingLabel`` 词表里而回 422:一个看起来像
# 「参数写错了」的错,而不是「清空没接上」。GET/POST 不会撞(``{label}`` 上没有
# GET,而 ``experiences/distill`` 是两段路径),所以这条顺序只由 DELETE 撑着,
# 也因此由 ``test_agent_profile_routes.py`` 的一条回归用例钉住。
# --------------------------------------------------------------------------


def _experiences_wiring_active(store) -> bool:
    """经验库自己的总闸判据——**不是** ``_wiring_active()``。

    两把开关相互独立(``RETRIEVAL_EXPERIENCE_ENABLED`` vs
    ``AGENT_PROFILE_ENABLED``),共用一个判据会让「只开理解块、不开经验蒸馏」
    的部署得到一个半开状态。
    """
    return distillation_wiring_active(get_settings(), store)


#: 解析不出时刻的 ``updated_at`` 用的哨兵:降序排在所有真实时刻之后,并且
#: 永远不会被当成「分区里最新的那一条」。
_UNDATED = float("-inf")


def _experience_instant(value: str) -> float:
    """``updated_at`` 文本 → 可比较的时刻;解析不出的回 ``_UNDATED``。

    ⚠ **不按字符串比大小。** 两个后端写的是带偏移的本地 ISO 文本,而字典序只在
    偏移恒等时才与时间序一致——改时区、夏令时切换、以及一次跨环境同步过来的
    行,都能让「更晚的那一条排在前面」。注入侧的 ``version_signal`` 用字典序
    ``MAX()`` 是另一回事(它只要一个会变的指纹,不要一个真的时刻);这里要排给
    人看,就得是真的时刻。

    天真时间(没有偏移)按**本地**时区解释——store 的 ``now`` 写的就是本地时
    间,缺偏移的只可能是更早版本留下的行,把它当 UTC 会凭空平移几个小时。
    返回 ``float`` 而不是 ``datetime``:aware 与 naive 的 ``datetime`` 相互比较
    会抛 ``TypeError``,而一个分区里两种都有并非不可能,排序不该为此炸掉。
    """
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return _UNDATED
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.timestamp()


def _experience_out(row: dict) -> ExperienceEntryOut:
    return ExperienceEntryOut(
        action=str(row.get("action") or ""),
        polarity=str(row.get("polarity") or ""),
        rationale=str(row.get("rationale") or ""),
        support=int(row.get("support") or 0),
        adopted=int(row.get("adopted") or 0),
        updated_at=str(row.get("updated_at") or ""),
    )


def _ordered_experiences(rows: list[dict]) -> list[ExperienceEntryOut]:
    """按 ``(support desc, updated_at desc, id asc)`` 排序,截到分区上限后投影。

    排序在**这里**而不是在 store 里:``read_partition`` 按 ``id`` 出行是它自己
    的契约(内容哈希序,让注入侧能对两次读做字节级比较并 memo),而这份顺序是
    **界面**的——「最多人验证过的排在前面」。让 store 多一个排序参数会把一个
    展示决定焊进一条注入侧依赖其稳定性的读。

    ⚠ **截断必须发生在排序之后**,这正是调用点多读一倍行的理由。淘汰是在蒸馏
    写完之后才跑的,所以分区可以短暂地多于上限;这时候让 store 按 ``id`` 序
    截到 100,截掉的是内容哈希恰好靠后的那些行,而它们与 support 高低毫无关系
    ——界面会丢掉这个库最被验证的几条经验,还完全看不出来。

    三趟稳定排序而不是一个复合键:``updated_at`` 要降序、``id`` 要升序,而
    ``reverse=True`` 在 Python 的稳定排序里**不会**打乱相等元素的既有次序,所以
    「先按次要键排、再按主要键排」逐字得到那个三级序。对两百行来说,这比给
    字符串键造一个可取负的替身诚实得多。
    """
    ordered = sorted(rows, key=lambda row: str(row.get("id") or ""))
    ordered.sort(
        key=lambda row: _experience_instant(str(row.get("updated_at") or "")),
        reverse=True,
    )
    ordered.sort(key=lambda row: int(row.get("support") or 0), reverse=True)
    return [
        _experience_out(row)
        for row in ordered[:RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES]
    ]


def _newest_updated_at(entries: list[ExperienceEntryOut]) -> "str | None":
    """这份清单里最新的 ``updated_at`` 原文;没有一条解析得出时刻则 ``None``。

    取自**响应真正列出的那些条目**,不是读回来的全部行——``count`` 也是,两者
    因此描述同一批东西。原样回原始字符串而不是回解析后的时刻:时区与精度是
    行自己的,后端没有理由在这里替浏览器重新格式化一遍。
    """
    dated = [entry for entry in entries if _experience_instant(entry.updated_at) > _UNDATED]
    if not dated:
        return None
    return max(dated, key=lambda entry: _experience_instant(entry.updated_at)).updated_at


def _can_manage_experiences(notebook_id: str, user_id: str) -> bool:
    """「立即整理」/「清空」两个按钮该不该出现——与 ``can_edit_base`` 同一口径。

    ⚠ **刻意只判能力,不调 ``notebook_mirror_fence``**,与 ``deps.py`` 那条
    「凡走 ``notebook_capability_allowed`` 的**写**路径在它之后再调一次围栏;
    只读投影不调」逐字一致。这个 bool 答的是「这个人有没有这项能力」——镜像库
    上他的权限一点没少,少的是那个库此刻能不能被写;把两件事塞进一个布尔,
    它们就再也分不开了。围栏该说的话由两个写端点自己说:被它拦下的 409 带着
    ``sync_origin``。⚠ 前端**目前**并不解析那个字段,统一回落到通用 409 文案;
    这是独立于本端点的一项已登记待办(所有走围栏的写端点同此),不是这三个端点
    自己的形态。
    """
    return notebook_capability_allowed("agent_profile:write", notebook_id, user_id)


def _require_experience_manager(notebook_id: str, user: UserProfile) -> None:
    """两个写端点共用的守卫:能力 → 404,镜像围栏 → 409(与 rebuild 同序同形)。"""
    if not notebook_capability_allowed("agent_profile:write", notebook_id, user.id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    mirrored = notebook_mirror_fence("agent_profile:write", notebook_id)
    if mirrored:
        raise mirrored_notebook_error(mirrored)


@router.get(
    "/notebooks/{notebook_id}/understanding/experiences",
    response_model=ExperiencePartitionResponse,
    dependencies=[Depends(require_notebook_read)],
)
def get_notebook_experiences(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> ExperiencePartitionResponse:
    store = retrieval_experience_store()
    if not _experiences_wiring_active(store):
        # 关闸时**不查表也不查能力**——与 ``get_understanding`` 同一条理由:总闸
        # 已经决定了「什么都不可做」,两次查询的结果都无关紧要。
        return ExperiencePartitionResponse(enabled=False)
    # 一次读,三个字段全部由它派生:``count`` 与 ``entries`` 因此不可能自相矛盾
    # (「说有 7 条、只列出 5 条」)。
    #
    # ⚠ 取数宽度是上限的**两倍**,不是上限本身。淘汰跑在蒸馏写完之后,所以分区
    # 可以短暂地多于 100 行;按 ``id``(内容哈希)序只读 100 行,会在排序之前就
    # 把一批与 support 毫无关系的行丢掉。多读一倍再在内存里排序、截断,让「露出
    # 来的是 support 最高的那 100 条」在那个窗口里也成立。两倍是够用的裕量:一次
    # 蒸馏批次最多写进几条,而淘汰紧随其后。
    entries = _ordered_experiences(
        store.read_partition(
            notebook_id, 2 * RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES
        )
    )
    return ExperiencePartitionResponse(
        enabled=True,
        count=len(entries),
        # 空分区(或一条时刻都解析不出来)给 ``None`` 而不是空串:「从来没更新过」
        # 与「更新时间不详」对一个要把它渲染成时间的浏览器是两件事。
        updated_at=_newest_updated_at(entries),
        can_manage=_can_manage_experiences(notebook_id, user.id),
        entries=entries,
    )


@router.post(
    "/notebooks/{notebook_id}/understanding/experiences/distill",
    response_model=ExperienceDistillStarted,
    dependencies=[Depends(require_notebook_read)],
)
def distill_notebook_experiences(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> ExperienceDistillStarted:
    # 权限在总闸**之前**,与 ``rebuild_understanding`` 刻意相反:下面的 DELETE
    # 根本不跟随总闸,两个写端点若一个先判闸、一个先判权限,同一个没有管理权的
    # 成员会因为按了哪个按钮而收到 404 或 409。先判权限让这一对恒为 404。
    _require_experience_manager(notebook_id, user)
    store = retrieval_experience_store()
    if not _experiences_wiring_active(store):
        raise user_error(409, _EXPERIENCE_DISABLED_MESSAGE)
    # 逐支 if 而不是一张查表:每一句 409 的文案在 AST 上都看得见是哪一支给的
    # (``test_user_error`` 的静态扫描读的就是这个),而「漏掉一种结果」由上面那
    # 条 import 期断言挡住,不靠运行时的 KeyError。
    outcome = retrieval_experience_jobs_service().distill_now(notebook_id)
    if outcome == MANUAL_DISTILL_BUSY:
        raise user_error(409, _REBUILD_BUSY_MESSAGE)
    if outcome == MANUAL_DISTILL_COOLDOWN:
        raise user_error(409, _EXPERIENCE_COOLDOWN_MESSAGE)
    if outcome == MANUAL_DISTILL_DISABLED:
        # 到不了(上面那一句闸先拦下),写出来是为了这一串分支对得起
        # ``distill_now`` 的返回集合,而不是只对得起这一个调用点。
        raise user_error(409, _EXPERIENCE_DISABLED_MESSAGE)
    if outcome != MANUAL_DISTILL_STARTED:
        raise user_error(409, _EXPERIENCE_UNAVAILABLE_MESSAGE)
    return ExperienceDistillStarted(started=True)


@router.delete(
    "/notebooks/{notebook_id}/understanding/experiences",
    response_model=ExperiencePartitionCleared,
    dependencies=[Depends(require_notebook_read)],
)
def clear_notebook_experiences(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> ExperiencePartitionCleared:
    _require_experience_manager(notebook_id, user)
    store = retrieval_experience_store()
    # ⚠ 刻意**不**判总闸(模块 docstring 第三条):关开关是「从现在起不再记」,
    # 不是「把记过的藏起来、还删不掉」。
    #
    # ``max_entries=0`` = 把这个分区修剪到零行。``notebook_id`` 恒非空——它是
    # 路径参数,空串匹配不上这条路由——所以这一句碰不到全局分区(``''``),
    # 而那正是 ``evict_to_limit`` 的 ⚠ 里唯一真正危险的误用。
    removed = store.evict_to_limit(0, notebook_id=notebook_id)
    return ExperiencePartitionCleared(removed=removed)


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
        # 镜像写入围栏,与 `_owner_for_scope` 逐字同款(同一条理由写在那里)。
        mirrored = notebook_mirror_fence("agent_profile:write", notebook_id)
        if mirrored:
            raise mirrored_notebook_error(mirrored)
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
    store = repository().agent_observations
    wiring = _wiring_active()
    # 短句那一侧仍然完全跟随总闸(与本端点接入时的契约逐字一致)。
    rows = store.list_observations(notebook_id, user.id, limit=limit) if wiring else []
    # ⚠ 调用记录的**读**不跟随任何一把开关(codex #616 R1 P1/P2)。开关管的是
    # 「从现在起还记不记」,而不是「已经记下的还能不能看见」:今天关掉开关就让
    # 昨天记下的行连同它们的清空入口一起消失,等于把一份用户有权删除的数据变成
    # 他既看不到、也删不掉的东西。查询本身有界(``call_limit`` ≤ 环形上限),
    # 关掉开关后它最多再读到一份不再增长的清单。
    calls_enabled = bool(get_settings().agent_call_log_enabled)
    call_rows = store.list_calls(notebook_id, user.id, limit=call_limit)
    names = _observation_agent_names(user.id)
    return AgentObservationsResponse(
        enabled=wiring,
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
    # 总闸关掉时**只**挡住会碰到短句的那些清空(既有契约,不变):那一侧的写
    # 路径整个不可达,清空作为写路径的一部分一并 409。
    #
    # 专清调用记录(``kind='call'``)是例外,与上面 GET 同一条理由:开关只说
    # 「从现在起不记」,不该让已经记下的行变成删不掉的东西(codex #616 R1 P1)。
    # 这一支不会碰到任何 ``kind='note'`` 的行,所以放行它不等于绕开总闸。
    if clean_kind != AGENT_OBSERVATION_KIND_CALL and not _wiring_active():
        raise user_error(409, _DISABLED_MESSAGE)
    removed = repository().agent_observations.clear_observations(
        notebook_id,
        user.id,
        agent_profile_id=agent_profile_id,
        kind=clean_kind,
    )
    return AgentObservationsCleared(removed=removed)
