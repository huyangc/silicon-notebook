"""命令目录抽取的七个端点(方案 C·C1b,`dismiss` 是 R7 补的第七个)。

守卫口径与既有来源端点一致:`preview` / `job` / `candidates` 是只读,走
`require_notebook_read`(只读成员也该看得到成本预告与审阅队列);`start` /
`cancel` / `apply` / `dismiss` 会写库或花模型钱,走 `require_notebook_access`
(owner)——`dismiss` 虽然不写 knowhow 表,但改的是候选的持久状态,口径与
`apply` 一致。拒绝一律 404 而不是 403 —— 本仓库「不确认资源存在性」的既有约定。

路由体保持薄:参数解析 + 守卫 + 一跳编排。抽取逻辑在
`app/services/catalog_job.py`,接地校验在 `app/services/command_catalog.py`。
"""
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.api.deps import (
    command_catalog_service,
    get_current_user,
    repository,
    require_notebook_access,
    require_notebook_read,
    source_repository,
    user_error,
)
from app.core.audit_actor import session_audit_principal
from app.models.command_catalog import (
    CommandCatalogApplyRequest,
    CommandCatalogApplyResult,
    CommandCatalogCancelResponse,
    CommandCatalogCandidate,
    CommandCatalogCandidatePage,
    CommandCatalogConflict,
    CommandCatalogDismissRequest,
    CommandCatalogDismissResult,
    CommandCatalogJob,
    CommandCatalogJobResponse,
    CommandCatalogPreview,
    CommandCatalogStartResponse,
)
from app.models.identity import UserProfile
from app.repositories.ports import CATALOG_TERMINAL_STATUSES, CatalogJobAlreadyRunning
from app.services import background_jobs
from app.services.catalog_job import (
    APPLY_TABLE_SHAPE_MESSAGE,
    MAX_APPLY_CANDIDATES,
    MODEL_UNAVAILABLE_MESSAGE,
    SOURCE_BUSY_MESSAGE,
    SOURCE_NOT_PARSED_MESSAGE,
    SOURCE_PARSE_FAILED_MESSAGE,
    SOURCE_REPARSING_MESSAGE,
    SOURCE_STALE_MESSAGE,
    CatalogApplyTargetInvalid,
    CatalogModelUnavailable,
    CatalogPendingCandidates,
    CatalogSourceBusy,
    CatalogSourceChanged,
    CatalogSourceNotParsed,
    CommandCatalogService,
    pending_candidates_message,
)
from app.services.knowhow import api as knowhow_api


router = APIRouter()

_CANDIDATE_PAGE_DEFAULT = 25
_ALREADY_RUNNING_MESSAGE = "该来源已有命令目录识别任务正在运行，请等待或先取消。"
_APPLY_EMPTY_MESSAGE = "请先选择要确认的命令，或勾选“确认全部待审阅”。"
_APPLY_TOO_MANY_MESSAGE = f"一次最多确认 {MAX_APPLY_CANDIDATES} 条，请分批确认。"
# R7: dismiss mirrors apply's own empty/overflow guard copy — same page-scoped
# selection contract, same reason a caller must not be told nothing when the
# request carries zero ids, same reason a silently truncated "跳过了 100 条"
# on a 300-id request would read as done.
_DISMISS_EMPTY_MESSAGE = "请先选择要跳过的命令，或勾选“跳过全部待审阅”。"
_DISMISS_TOO_MANY_MESSAGE = f"一次最多跳过 {MAX_APPLY_CANDIDATES} 条，请分批跳过。"
# R13 (codex PR #412 评审第 13 轮 P2): `all_pending=true` 且 `candidate_ids`
# 非空是一份自相矛盾的载荷——之前静默偏向 `all_pending`(比调用方明确写出的
# 选择更宽的一次写),调用方永远看不出自己传的 `candidate_ids` 被无声吞掉了。
# 两个端点共用同一条常量:两者都是同一个「二选一」传输合同(见
# `CommandCatalogApplyRequest`/`CommandCatalogDismissRequest` 的字段注释),
# 拒绝的理由与措辞不因写不写库而不同。
_DUAL_SCOPE_MESSAGE = "一次只能选择一种确认范围：逐条选择或全部待审阅。"
# R3 (codex PR #494 评审第 3 轮 P2): 审阅一个还在跑的任务会造出一条**永远确认
# 不了**的候选。识别中途确认某条命令 → 后面的窗口把这条命令的续表参数合并写回
# 时发现行已 applied → 按既定的降级路径追加一条同名新候选(那是刻意的:参数看得
# 见比丢掉好);但确认这条替补行时,目标表里已经有同名锚点,`_apply_locked` 会
# 判 `conflict_existing_row` 跳过——迟到发现的参数从此可见而不可落库。
# 闸放在 **API 边界**而不是服务层:①它对齐界面本来的行为(审阅入口只在终态开),
# ②服务层的锁与降级路径因此原样保留为纵深防御——那条路仍会在「用户先取消、
# 恰好与最后一个窗口的写回交错」这类合法竞态里跑到,只是不再被一个界面上做不到
# 的操作源源不断地喂。两条文案分开写而不是共用一条:补救步骤相同,但用户点的是
# 「确认」还是「跳过」不同,措辞跟着动词走(与上面 empty/too-many 两对同一惯例)。
_APPLY_WHILE_RUNNING_MESSAGE = "识别还在进行中，请等它完成或先取消，再确认命令。"
_DISMISS_WHILE_RUNNING_MESSAGE = "识别还在进行中，请等它完成或先取消，再跳过命令。"


def _job_with_pending(service: CommandCatalogService, job: dict) -> CommandCatalogJob:
    """`CommandCatalogJob.of()` plus the one number it cannot derive from the
    job row alone: how many candidates are still `state='candidate'`
    (unreviewed). Computed via the same grouped-by-state count `start()`
    already uses for the 409 guard above — one indexed query, not a scan.

    Every route below that can hand back a job goes through this, so a client
    polling `.../job` sees the same contract regardless of which action
    produced this particular response (`start`'s own response skips the query
    — a job it just created has no candidates yet, so 0 is already correct).
    """
    pending = service.catalog.candidate_counts(job["id"]).get("candidate", 0)
    return CommandCatalogJob.of(job, pending_candidates=pending)


def _not_parsed_error(exc: CatalogSourceNotParsed) -> HTTPException:
    """R8: the two user messages behind one precondition.

    Which one to show is decided by the exception's ``parse_status`` FIELD, not
    by inspecting a message string — same provenance rule the rest of this file
    follows (`CatalogModelUnavailable` → `MODEL_UNAVAILABLE_MESSAGE`), and both
    strings stay curated constants in the service module rather than being
    assembled here. `failed` gets its own copy because the remedy differs: one
    is "wait", the other is "reparse or re-upload".
    """
    if exc.parse_status == "failed":
        return user_error(409, SOURCE_PARSE_FAILED_MESSAGE)
    return user_error(409, SOURCE_NOT_PARSED_MESSAGE)


def _require_settled_job(job: dict, message: str) -> None:
    """Refuse to review a run that is still going. See
    `_APPLY_WHILE_RUNNING_MESSAGE` for the row this prevents from existing.

    The status whitelist is `CATALOG_TERMINAL_STATUSES` — the same frozenset
    `start`'s own restart guard reads, so "finished" has exactly one spelling
    on the backend. A `queued` job counts as running: its worker has not
    started yet, which is the widest window of all.
    """
    if job.get("status") not in CATALOG_TERMINAL_STATUSES:
        raise user_error(409, message)


def _owned_source(notebook_id: str, source_id: str):
    """本 notebook 自己的来源,否则 404。

    刻意**不**接受参与集(挂载参考库)的来源:抽取会花模型钱并写这个库的知识,
    对一个只是被挂载进来的库这么做,授权语义是错的。
    """
    try:
        detail = source_repository().get_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")
    if detail.notebook_id != notebook_id:
        raise HTTPException(status_code=404, detail="Source not found")
    return detail


@router.get(
    "/notebooks/{notebook_id}/sources/{source_id}/command-catalog/preview",
    response_model=CommandCatalogPreview,
    dependencies=[Depends(require_notebook_read)],
)
def command_catalog_preview(
    notebook_id: str, source_id: str
) -> CommandCatalogPreview:
    """零模型调用的成本预告(段数在有界前缀内真跑分段、调用数同样精确测量)。

    R8:来源还没解析完(或解析失败)时同样 409。一份对着还没有元素的来源做的成本
    预告会报出「约 0 个窗口」,读起来像「这份文档没什么可抽的」而不是「过一会
    儿再来」——成本预告唯一不能出的错就是这个方向。

    R4:预告要读的两条语句之间落进一次重解析时(服务层复核来源代次后重读一次仍
    不一致),同样 409。与上面那条是同一类拒绝——「现在给不出可信的数」——只是
    原因从「还没解析」变成「正在重新解析」,所以文案分开。
    """
    _owned_source(notebook_id, source_id)
    try:
        preview = command_catalog_service().preview(notebook_id, source_id)
    except CatalogSourceNotParsed as exc:
        raise _not_parsed_error(exc)
    except CatalogSourceBusy:
        # 同一个异常类型在 apply/dismiss 那里映射成 `SOURCE_BUSY_MESSAGE`(措辞是
        # 「再确认或跳过」)。预告既不确认也不跳过,照搬会点名两个用户没做的动作
        # ——与 empty/too-many 两对按动词分开写同一条惯例。
        raise user_error(409, SOURCE_REPARSING_MESSAGE)
    return CommandCatalogPreview(
        source_id=preview.source_id,
        source_title=preview.source_title,
        estimated_windows=preview.estimated_windows,
        estimated_calls=preview.estimated_calls,
        windows_in_prefix=preview.windows_in_prefix,
        skipped_windows_in_prefix=preview.skipped_windows_in_prefix,
        sampled=preview.sampled,
        element_limit=preview.element_limit,
    )


@router.post(
    "/notebooks/{notebook_id}/sources/{source_id}/command-catalog",
    response_model=CommandCatalogStartResponse,
    dependencies=[Depends(require_notebook_access)],
)
def start_command_catalog(
    notebook_id: str, source_id: str
) -> CommandCatalogStartResponse:
    """发起抽取。重复发起 → 409(用户可读文案;既有任务从 `.../job` 取)。

    R8:来源尚未完成解析(或解析失败)同样 409——对着还没落下元素的来源发起,
    抽出来的是「这份手册的一小段」却会被记成完整的一次识别。
    """
    _owned_source(notebook_id, source_id)
    service = command_catalog_service()
    try:
        job = service.start(notebook_id, source_id)
    except CatalogSourceNotParsed as exc:
        raise _not_parsed_error(exc)
    except CatalogJobAlreadyRunning:
        raise user_error(409, _ALREADY_RUNNING_MESSAGE)
    except CatalogPendingCandidates as exc:
        # Second line of defence behind the frontend's own gating (see
        # command-catalog-panel.tsx): a new run would immediately shadow the
        # previous job's unreviewed candidates (`.../job` only ever returns
        # the latest one). The count is request-specific, so this builds the
        # message via the module's own curated builder rather than a bare
        # constant — still centrally worded, never `str(exc)`.
        raise user_error(409, pending_candidates_message(exc.pending))
    except CatalogModelUnavailable:
        # 用策展常量，不是 `str(exc)`：`user_error` 的契约是「detail 可原样上屏」，
        # 把异常文本喂进去会让将来任何一处带诊断信息的抛出直接带标记上屏。
        raise user_error(409, MODEL_UNAVAILABLE_MESSAGE)
    try:
        background_jobs.submit(
            service.run,
            job["id"],
            name=f"catalog-{source_id}",
            # notify_pending 刻意为 False:待确认中心聚合的是报告/治理/索引三类,
            # 命令目录不在其中。置 True 只会让每个 job 结束白刷一次 snapshot。
            # C1c 若把它纳入铃铛,改这一处即可。
            notify_pending=False,
        )
    except Exception:
        # 行已提交、线程没起来:必须当场落终态,否则单飞守卫会把这个来源
        # 永久挡在「已有任务运行中」上(离线进程无权自清,只能等重启)。
        service.fail_submission(job["id"])
        raise
    return CommandCatalogStartResponse(
        status="started", job=CommandCatalogJob.of(job)
    )


@router.get(
    "/notebooks/{notebook_id}/sources/{source_id}/command-catalog/job",
    response_model=CommandCatalogJobResponse,
    dependencies=[Depends(require_notebook_read)],
)
def command_catalog_job(
    notebook_id: str, source_id: str
) -> CommandCatalogJobResponse:
    _owned_source(notebook_id, source_id)
    service = command_catalog_service()
    job = service.latest_job(source_id)
    return CommandCatalogJobResponse(
        job=_job_with_pending(service, job) if job is not None else None
    )


@router.post(
    "/notebooks/{notebook_id}/sources/{source_id}/command-catalog/cancel",
    response_model=CommandCatalogCancelResponse,
    dependencies=[Depends(require_notebook_access)],
)
def cancel_command_catalog(
    notebook_id: str, source_id: str
) -> CommandCatalogCancelResponse:
    """取消:`cancelling`(worker 会在下一个分片边界停,飞行中的一次模型调用被
    取消时也会停,不必等它返回)/ `cancelled`(本进程没有在跑它,直接落终态)/
    `not_running`(没有活跃任务)。"""
    _owned_source(notebook_id, source_id)
    service = command_catalog_service()
    result = service.cancel(source_id)
    job = result.get("job")
    return CommandCatalogCancelResponse(
        status=str(result["status"]),
        job=_job_with_pending(service, job) if job else None,
    )


@router.get(
    "/notebooks/{notebook_id}/sources/{source_id}/command-catalog/candidates",
    response_model=CommandCatalogCandidatePage,
    dependencies=[Depends(require_notebook_read)],
)
def command_catalog_candidates(
    notebook_id: str,
    source_id: str,
    job_id: str = Query(""),
    state: str = Query("candidate"),
    cursor: int = Query(0, ge=0),
    limit: int = Query(_CANDIDATE_PAGE_DEFAULT, ge=1, le=MAX_APPLY_CANDIDATES),
) -> CommandCatalogCandidatePage:
    """一页候选(keyset)。`job_id` 省略时取该来源最近一次任务。"""
    _owned_source(notebook_id, source_id)
    service = command_catalog_service()
    job = service.scoped_job(notebook_id, source_id, job_id)
    if job is None:
        if job_id:
            raise HTTPException(status_code=404, detail="Catalog job not found")
        return CommandCatalogCandidatePage()
    try:
        page = service.candidates_page(
            job["id"], state=state, cursor=cursor, limit=limit
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="unsupported candidate state")
    return CommandCatalogCandidatePage(
        items=[CommandCatalogCandidate.of(row) for row in page["items"]],
        next_cursor=int(page["next_cursor"]),
        has_more=bool(page["has_more"]),
        counts=page["counts"],
    )


@router.post(
    "/notebooks/{notebook_id}/sources/{source_id}/command-catalog/apply",
    response_model=CommandCatalogApplyResult,
    dependencies=[Depends(require_notebook_access)],
)
def apply_command_catalog(
    notebook_id: str,
    source_id: str,
    payload: CommandCatalogApplyRequest = Body(...),
    job_id: str = Query(""),
    user: UserProfile = Depends(get_current_user),
) -> CommandCatalogApplyResult:
    """把选中的候选落进「命令目录：<来源标题>」表。

    v1 合并语义刻意保守:表不存在就建,存在只**新增**表里没有的命令;同名命令
    一律不改行、原样回报为 `conflicts`。落库全部走 knowhow 既有服务层,所以
    变更流水(record_change)照常产生;重投影与其他 knowhow 写端点同样由这里
    在服务层返回后调度。
    """
    _owned_source(notebook_id, source_id)
    if payload.all_pending and payload.candidate_ids:
        # R13: a payload that says BOTH "all of it" and "just these" used to
        # silently pick `all_pending` — a wider write than what the caller
        # explicitly enumerated, with no signal that `candidate_ids` was ever
        # read. Refuse instead of guessing which one the caller meant.
        raise user_error(422, _DUAL_SCOPE_MESSAGE)
    if not payload.all_pending and not payload.candidate_ids:
        raise user_error(400, _APPLY_EMPTY_MESSAGE)
    if not payload.all_pending and len(payload.candidate_ids) > MAX_APPLY_CANDIDATES:
        # A user-readable 422, not a silent truncation: confirming "the first
        # 100 of however many you sent" without saying so would read as
        # "done" on a selection the caller thought was smaller.
        raise user_error(422, _APPLY_TOO_MANY_MESSAGE)
    service = command_catalog_service()
    job = service.scoped_job(notebook_id, source_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Catalog job not found")
    _require_settled_job(job, _APPLY_WHILE_RUNNING_MESSAGE)
    repo = repository()
    # 稳定 creator id 与可读流水 label 分开传递(与 knowhow 既有写端点同一口径)。
    principal = session_audit_principal(user)
    try:
        result = service.apply(
            notebook_id,
            source_id,
            job["id"],
            candidate_ids=payload.candidate_ids,
            all_pending=payload.all_pending,
            actor=principal.audit_label,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Catalog job not found")
    except CatalogSourceChanged:
        # R8: the source was reparsed after this run read it. The service has
        # already expired this job's remaining candidates (with a recorded
        # reason), so 「请重新识别」 is an action the user can actually take the
        # moment they read it — the restart guard is released by the same call
        # that refuses this confirm.
        raise user_error(409, SOURCE_STALE_MESSAGE)
    except CatalogSourceBusy:
        # R10: a reparse is in flight and the service refused to write under
        # it. Deliberately a DIFFERENT 409 from the stale one above: nothing
        # was expired here, so telling the user 「请重新识别」 would send them
        # at a restart the pending-candidates guard is still blocking. The copy
        # says to wait for the parse, which is the step that actually unblocks.
        raise user_error(409, SOURCE_BUSY_MESSAGE)
    except CatalogApplyTargetInvalid:
        # 走策展常量而非 `str(exc)`,与同文件 CatalogModelUnavailable 的先例一致:
        # `user_error()` 的契约是「detail 可原样上屏」,把异常携带的文本喂进去会让
        # 将来任何一处改了该异常构造参数的抛出直接带标记上屏。这条异常的 message
        # 恰好本来就等于这个常量(见该异常的 docstring),但常量才是真源。
        # 下面那条裸 ValueError 仍是诊断用,不打标记。
        raise user_error(400, APPLY_TABLE_SHAPE_MESSAGE)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # R6 P2: gated on a RESOLVED target table, not on `rows_added`. A crash
    # between `append_knowhow_rows` (rows land) and `mark_candidates_applied`
    # means the apply call that actually wrote the rows never reaches this
    # line at all — it raised before returning. The retry that follows
    # resolves the SAME already-populated table and finds every one of its
    # candidates already present, so it reports `rows_added=0` with an
    # all-conflicts page; gating on `rows_added` would leave that already-
    # appended data permanently unscheduled for projection. Scheduling is
    # idempotent and bounded, so the cost of one extra call on the ordinary
    # "nothing to apply" branch (an empty `table_id`) is a non-issue.
    if result["table_id"]:
        knowhow_api.get_scheduler(repo).schedule(result["table_id"])
    return CommandCatalogApplyResult(
        table_id=result["table_id"],
        table_title=str(result["table_title"]),
        created=bool(result["created"]),
        applied=list(result["applied"]),
        rows_added=int(result["rows_added"]),
        conflicts=[
            CommandCatalogConflict(**conflict) for conflict in result["conflicts"]
        ],
        pending_remaining=int(result["pending_remaining"]),
    )


@router.post(
    "/notebooks/{notebook_id}/sources/{source_id}/command-catalog/dismiss",
    response_model=CommandCatalogDismissResult,
    dependencies=[Depends(require_notebook_access)],
)
def dismiss_command_catalog(
    notebook_id: str,
    source_id: str,
    payload: CommandCatalogDismissRequest = Body(...),
    job_id: str = Query(""),
) -> CommandCatalogDismissResult:
    """把选中的候选标记为「已跳过」,不写任何表。

    R7(codex PR #412 评审 P1):R5/R6 加的「有待审候选拦重跑」守卫要求审阅者
    先「确认或跳过」才能重新发起识别,但此前只有 `apply` 在冲突时才会自动把
    候选标记为 `dismissed`——审阅者刻意不要的一条候选没有任何路可走,会把
    整个来源永久锁在重新识别之外。这是那条缺失的显式路径,选择语义
    (`candidate_ids` 二选一 `all_pending`,上限同 `apply`)、锁边界(同一把
    per-target 锁,理由见 `CommandCatalogService.dismiss` 的 docstring)与权限
    (owner-only,同 `apply`)都镜像 `apply`,只是不落库,所以没有 knowhow
    调度、没有 actor/审计标签。
    """
    _owned_source(notebook_id, source_id)
    if payload.all_pending and payload.candidate_ids:
        # R13: mirrors apply's own dual-scope refusal — see that endpoint's
        # comment. Same constant, same reason: a caller writing both fields
        # gets told to pick one instead of `all_pending` silently winning.
        raise user_error(422, _DUAL_SCOPE_MESSAGE)
    if not payload.all_pending and not payload.candidate_ids:
        raise user_error(400, _DISMISS_EMPTY_MESSAGE)
    if not payload.all_pending and len(payload.candidate_ids) > MAX_APPLY_CANDIDATES:
        raise user_error(422, _DISMISS_TOO_MANY_MESSAGE)
    service = command_catalog_service()
    job = service.scoped_job(notebook_id, source_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Catalog job not found")
    # Same gate as `apply`, for the same row: a dismiss mid-run can equally
    # take a candidate out from under the write-back that is about to merge
    # into it, and the review panel does not offer either action until the run
    # settles. See `_APPLY_WHILE_RUNNING_MESSAGE`.
    _require_settled_job(job, _DISMISS_WHILE_RUNNING_MESSAGE)
    try:
        result = service.dismiss(
            notebook_id,
            source_id,
            job["id"],
            candidate_ids=payload.candidate_ids,
            all_pending=payload.all_pending,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Catalog job not found")
    except CatalogSourceChanged:
        # Same 409 as `apply`, and for the same reason the service takes the
        # guard on this path at all: the sweep it performs is what releases the
        # restart block, with the honest reason recorded on every row.
        raise user_error(409, SOURCE_STALE_MESSAGE)
    except CatalogSourceBusy:
        # Same 409 as `apply`: dismiss takes the same parse barrier, because
        # its own R8 sweep is a write that would otherwise record the wrong
        # reason on a set the in-flight reparse is about to kill.
        raise user_error(409, SOURCE_BUSY_MESSAGE)
    return CommandCatalogDismissResult(
        dismissed=list(result["dismissed"]),
        pending_remaining=int(result["pending_remaining"]),
    )
