import asyncio
import json
import queue
import threading
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import (
    get_current_user,
    notebook_access_repository,
    notebook_catalog_repository,
    repository,
    require_notebook_read,
    user_error,
)
from app.models.notebooks import NotebookSummary
from app.models.source_scope import (
    BaseNotebookScope,
    RetrievalScopeBaseReceipt,
    RetrievalScopeLocalReceipt,
    RetrievalScopeReceipt,
    SourceScope,
)
from app.models.ask import (
    AskIntentPreviewRequest,
    AskRequest,
    AskResponse,
    ConversationBulkDeleteResult,
    ConversationDetail,
    ConversationRenameRequest,
    ConversationSummary,
    FeedbackRequest,
    FeedbackResponse,
    NotebookSearchResponse,
    QueryIntentContract,
)
from app.models.identity import UserProfile
from app.repositories.ports import AskStreamPort, ConversationBusyError
from app.services.ask_modes import ASK_MODES, UnknownAskMode, resolve_mode
from app.services.cancellation import AskCancelled
from app.services.query_intent import (
    finalize_query_intent,
    has_explicit_source_restriction,
    plan_query_intent,
)
from app.services.source_scope import retrieval_scope_receipt_context


router = APIRouter()


def _validate_source_scope(repo, notebook: NotebookSummary,
                           scope: SourceScope | None) -> SourceScope | None:
    """Validate and freeze a checkbox scope as an explicit active-source ceiling.

    The browser uses exclusions while "all" is selected because that keeps
    toggles compact.  Workers must not carry that moving definition: normalize
    it once to an include-list so concurrent uploads cannot widen the run and
    every candidate producer can push the same allow-list below its LIMIT.

    Deliberately does NOT decide whether the resulting (possibly empty) local
    scope leaves the run with any evidence universe at all -- that D12 check
    also depends on the frozen base-library scope (see ``_validate_base_scope``)
    and is combined once in ``_require_non_empty_scope``, which Ask and the
    report entry points each call.

    codex #431 R8 (P2): membership, the frozen selection AND the universe it is
    measured against all come from ONE live read. The ``include`` branch used to
    take its universe from ``notebook.counts["sources"]`` -- a number cached on
    the summary the route loaded earlier -- while its selection came from the
    client's list. Two different sources for one comparison is a bug generator,
    and this one is reachable: an upload that completes between ``get_notebook``
    and here leaves the cached count equal to the submitted list's length, so
    ``narrowed`` computes False while the frozen include list demonstrably
    EXCLUDES the new source. A genuinely narrowed run would then keep private
    Memory, conversation history, PPR, community reports and the corpus profile
    switched on -- the exact "restricted" protections narrowing exists to apply.

    Round trips do not go up, because the one read also subsumes the membership
    check that used to be its own query:

    * ``exclude`` + [] (the browser's default "everything checked" payload, and
      the hot path): 1 before -- ``visible_source_ids`` short-circuits on the
      empty list -- and 1 now.
    * ``exclude`` + ids (the browser's partial-narrowing payload): 2 before,
      1 now.
    * ``include`` + ids: 1 before, 1 now (a whole-notebook id read instead of a
      batched subset read -- the same shape the ``exclude`` default already
      pays on every request).
    * ``include`` + [] ("clear all", and the default payload on a notebook with
      zero visible sources): 0 before, 1 now. The only increase, and it is
      unavoidable: "did selecting nothing narrow anything?" is exactly the
      question the stale cached count answers wrongly.

    codex #431 R9 (P1): the frozen ceiling covers the hidden Memory/Knowhow
    projection sources too, but ONLY when this request narrowed nothing.
    Separating "is a ceiling in force?" from "did the user narrow anything?"
    (R7) made the ceiling apply to every browser request, and the snapshot it
    freezes is the VISIBLE source set -- so hidden projection sources, which
    have no checkbox and therefore can never appear in a submitted selection,
    started failing ``allows()`` on the default "everything checked" request.
    A Knowhow-only notebook then passed ``local_evidence_available`` and
    retrieved nothing at all. The frozen hidden set restores their
    participation without giving up the freeze: a source that appears AFTER
    this snapshot -- visible or hidden -- is still outside the ceiling.

    ``narrowed`` gates it, so the existing "收窄来源维度时隐藏 Memory/Knowhow
    投影证据不参与" contract stays true by construction; the consumption side
    (``ActiveSourceScope.allows``) needs no second condition and cannot drift
    from this one.

    Round trips are unchanged: ``scope_source_ids`` returns both partitions
    from the single read that already answered the membership/universe
    question, so the hot path is still 1.
    """
    if scope is None:
        return None
    all_ids, hidden_ids = repo.scope_source_ids(notebook.id)
    universe = set(all_ids)
    valid = [
        source_id for source_id in scope.source_ids if source_id in universe
    ]
    if valid != scope.source_ids:
        raise user_error(422, "检索范围包含不属于当前笔记本的来源")
    if scope.mode == "include":
        # Provably a subset of ``all_ids``: the membership check above rejected
        # the request otherwise. So ``len(selected) <= len(all_ids)`` holds and
        # the comparison below is commensurable by construction.
        selected = list(scope.source_ids)
    else:
        excluded = set(scope.source_ids)
        selected = [
            source_id for source_id in all_ids if source_id not in excluded
        ]
    # Recomputed unconditionally: a client-supplied `narrowed` is never trusted.
    narrowed = len(selected) < len(all_ids)
    return SourceScope(
        mode="include", source_ids=selected,
        narrowed=narrowed,
        # Likewise recomputed, never taken from the request: a client-supplied
        # list here would name sources the checkbox universe deliberately hides
        # (another user's private Memory projection among them).
        hidden_source_ids=[] if narrowed else list(hidden_ids),
    )


def _validate_base_scope(notebook: NotebookSummary,
                         scope: BaseNotebookScope | None) -> BaseNotebookScope | None:
    """Validate and freeze a checkbox library scope as an explicit include
    ceiling over ``notebook``'s mounted reference libraries.

    Mirrors ``_validate_source_scope``'s exclude->include freeze for the same
    reason (a race-free, fixed set for every candidate producer to intersect
    against), but the valid universe is already loaded on ``notebook`` --
    mounted base libraries are not a per-request repo round trip like sources
    are.

    codex #431 R4 (P1, reverting R3): ``exclude`` + an EMPTY list -- the
    browser's compact "select all libraries" representation -- MUST still
    expand into an explicit ``include`` list naming every currently-mounted
    library, not short-circuit to ``None``. ``None`` reads as "no scope was
    submitted at all" to every downstream consumer, which is only harmless
    for Ask (validation and retrieval happen inside the same synchronous
    request, so "current mounts" and "mounts at submission time" are the same
    set). It is NOT harmless for a report: ``create_report`` calls this once
    and PERSISTS the result into ``understanding["base_scope"]``, and that
    persisted value is the authoritative ceiling re-applied, unchanged, at
    intent confirmation and again at generation start -- two later phases,
    potentially far apart in wall time. Freezing to ``None`` at create time
    throws away the fact "these are the libraries mounted right now"; a
    library mounted after creation but before confirm/generate would then
    silently join a report the user never scoped it into, which is exactly
    the class of bug freezing exists to prevent (see
    ``_require_non_empty_scope`` and ``_scope_receipt`` below, and
    ``test_report_base_scope_freezes_mount_set_at_create_time`` in
    ``test_report_api.py``).

    The actual production risk R3 was chasing -- a notebook with more mounted
    libraries than ``BaseNotebookScope.notebook_ids`` could hold, raising an
    uncaught pydantic ``ValidationError`` here -- is fixed at the cap instead:
    ``notebook_ids`` (and ``RetrievalScopeReceipt.bases``) are now bounded at
    10,000, a soft ceiling against pathological requests rather than a
    supported mount-scale limit (federated retrieval issues one query per
    participating library and stops being usable long before that count is
    reached).
    """
    if scope is None:
        return None
    valid_ids = {ref.id for ref in notebook.base_notebooks}
    submitted = set(scope.notebook_ids)
    if not submitted <= valid_ids:
        raise user_error(422, "检索范围包含未挂载到当前笔记本的参考库")
    if scope.mode == "include":
        selected = list(scope.notebook_ids)
    else:
        excluded = submitted
        selected = [ref.id for ref in notebook.base_notebooks if ref.id not in excluded]
    # Recomputed unconditionally (a client-supplied `narrowed` is never
    # trusted). The mount roster is already on the summary, so this is pure
    # arithmetic -- no round trip.
    return BaseNotebookScope(
        mode="include", notebook_ids=selected,
        narrowed=len(selected) < len(valid_ids),
    )


def _require_non_empty_scope(
    notebook: NotebookSummary,
    resolved_source_scope: SourceScope | None,
    resolved_base_scope: BaseNotebookScope | None,
) -> None:
    """D12: reject a retrieval scope whose evidence universe is empty across
    BOTH dimensions at once.

    Lives here rather than inside ``_validate_source_scope`` (where it used to
    be) because it needs the frozen base-library scope too, and lives in its
    own function rather than inside ``_require_ask_available`` because the
    report entry points must run this same check WITHOUT the
    ``ask_available`` gate: a report may legitimately be created on a notebook
    the Ask box would refuse, and widening that is a separate behaviour change.

    Each dimension answers with its FROZEN selection when the request actually
    NARROWED it, and with the notebook's real evidence universe when it did
    not. Neither fallback costs a round trip: both ride on the
    ``NotebookSummary`` the caller already loaded.

    A dimension the request did not narrow must NOT be read as a non-empty one.
    That shortcut is what let a base-only notebook (zero local evidence,
    ``ask_available`` true via its mounted libraries) submit ``base_scope``
    alone with every library unchecked and still be accepted: ``has_local``
    said True because nothing was submitted, and Ask then ran a whole round on
    an empty universe — for a report, that is a persisted row plus an intent
    model call, which is the exact spend this function exists to prevent.

    But a request that scoped NEITHER dimension is not making a selection at
    all, and is left alone: ``ask_available`` is the authority there.
    """
    if resolved_source_scope is None and resolved_base_scope is None:
        return
    # 本地维度的证据宇宙 ≠ 可见导入来源数。ask_available 的四个判据里有**三个**是本地的
    # (任意 chunk[含 Knowhow 格子]、本地可用 KG、该用户的已确认 Memory),而
    # counts["sources"] 只覆盖第一个的一部分:Knowhow 表与 Memory 根本没有可见来源。
    # 拿来源数当本地宇宙,会把「只有 Knowhow / 只有已确认 Memory + 显式取消勾选全部
    # 参考库」误拒成空范围 —— 而浏览器恒发 source_scope(sourceScopePayload 永不返回
    # undefined),零可见来源时它冻结成 include:[],所以那是**界面可达**的误拒
    # (codex #431 R7 P1)。
    # 故本地宇宙改问 NotebookSummary.local_evidence_available(catalog 侧由那三个本地
    # 判据算出,零新增查询),并与 counts["sources"] 取**或**:该信号只增不减,尚在解析
    # (有来源、还没有 chunk)的库仍按旧判据放行,列表投影未回填时(默认 False)也逐字
    # 回落到旧行为。
    # 反向护栏仍在:真正零本地证据的库两个加数都为假 → 与「全部取消勾选参考库」合起来
    # 照常 409,不烧那轮模型调用(见 test_base_only_submission_on_a_library_only_
    # notebook_is_409)。
    local_universe_non_empty = (
        bool(notebook.local_evidence_available)
        or int(notebook.counts.get("sources", 0)) > 0
    )
    if resolved_source_scope is None:
        has_local = local_universe_non_empty
    elif resolved_source_scope.source_ids:
        has_local = True
    else:
        # 冻结出**空集**有两种来源,判据是「这一维被收窄了吗」而不是「提交了吗」:
        #   * narrowed=True —— 用户真的把来源一个不剩地取消了(5 选 0)。以冻结选择
        #     为准,该拦;本地证据信号不得把用户主动点下的「清空」翻回来。
        #   * narrowed=False —— 0 选自 0,只是「这个库没有可见来源」。浏览器**默认**
        #     发的 exclude:[] 在这类库上就冻结成这个形状,它没有表达任何收窄意图,
        #     必须按真实本地证据宇宙作答,否则 Knowhow-only 的库一进来就被判空。
        has_local = (
            not resolved_source_scope.narrowed and local_universe_non_empty
        )
    # 库维度**不需要**同样的 narrowed 分支:`_validate_base_scope` 的
    # narrowed = len(selected) < len(mounted),空选择只有在零挂载时才 narrowed=False,
    # 而那时的回落值 bool(notebook.base_notebooks) 同样为假 —— 两条规则逐值相同。
    # 参考库也没有「没有可见行的隐藏证据」这回事:挂载集就是它完整的宇宙。
    has_base = (
        bool(resolved_base_scope.notebook_ids)
        if resolved_base_scope is not None
        else bool(notebook.base_notebooks)
    )
    if not has_local and not has_base:
        raise user_error(409, "当前检索范围为空，请至少选择一个来源或挂载参考库")


def _scope_receipt(
    notebook: NotebookSummary,
    resolved_source_scope: SourceScope | None,
    resolved_base_scope: BaseNotebookScope | None,
) -> RetrievalScopeReceipt | None:
    """Build the display-only "what this run was allowed to search" receipt.

    ABSENT when the request narrowed NEITHER dimension. That is the same rule
    ``current_source_scope_payload``/``current_base_scope_payload`` already
    enforce and for the same reason: a run that submitted no scope made no
    selection, and a receipt reading "1 of 84 sources" that the user never
    asked for is worse than no receipt at all. When ONE dimension was scoped
    the other is still reported, truthfully, as whole -- that is the pairing
    the real incident turned on (a single local source checked while 84
    reference-library papers answered the question), so suppressing the
    untouched half would hide exactly the line worth reading.

    Costs no round trip: every input rides on the ``NotebookSummary`` the
    caller already loaded for ``_require_ask_available``. ``counts["sources"]``
    is ``visible_source_count`` -- ``scope_source_ids``' visible half counted
    under an identical predicate -- so ``selected`` and ``total`` are commensurable
    (see ``_require_non_empty_scope``).

    Deliberately keeps reading that cached count even though
    ``_validate_source_scope`` now takes its own universe from a live read
    (codex #431 R8 P2): a concurrent upload can make this ``total`` lag the
    frozen scope by one, and for a DISPLAY line that is a tolerable staleness,
    whereas ``narrowed`` is a GATE -- getting it wrong leaves a narrowed run
    with private Memory and PPR switched on. Different consequence, different
    price worth paying. Re-reading here would add a second whole-notebook id
    read per request to restate a sentence the user reads once.

    Both dimensions are read through their ``mode``. Every route caller hands
    over the frozen include ceiling, so the exclude branches are unreachable
    from HTTP -- but reading an exclude scope as if it were an include one
    would INVERT the receipt (report the unchecked libraries as the searched
    ones), and a scope report that can lie about the scope is worth less than
    no report.
    """
    if resolved_source_scope is None and resolved_base_scope is None:
        return None
    total = max(int(notebook.counts.get("sources", 0) or 0), 0)
    included_ids: set[str] | None = None
    excluded_ids: set[str] = set()
    if resolved_base_scope is not None:
        if resolved_base_scope.mode == "include":
            included_ids = set(resolved_base_scope.notebook_ids)
        else:
            excluded_ids = set(resolved_base_scope.notebook_ids)
    if resolved_source_scope is None:
        # An unscoped local dimension searched every visible source, which is
        # what ``total`` counts -- reporting it as ``selected`` states "all of
        # them", never a narrowing the user did not request.
        selected = total
    elif resolved_source_scope.mode == "include":
        selected = len(resolved_source_scope.source_ids)
    else:
        selected = max(total - len(resolved_source_scope.source_ids), 0)
    return RetrievalScopeReceipt(
        local=RetrievalScopeLocalReceipt(selected=selected, total=total),
        bases=[
            RetrievalScopeBaseReceipt(
                notebook_id=ref.id,
                name=str(ref.name or "")[:500],
                included=(
                    ref.id not in excluded_ids if included_ids is None
                    else ref.id in included_ids
                ),
            )
            # Bounded so a pathological mount count can never turn the receipt
            # into a ValidationError that costs the user their answer. The cap
            # must track RetrievalScopeReceipt.bases: a *lower* one would
            # silently drop libraries the model would have accepted, and the
            # receipt is a disclosure — a truncated list presented as exhaustive
            # makes the frontend compute a wrong denominator and can hide a
            # narrowing applied to an omitted library (codex #431 P3).
            for ref in list(notebook.base_notebooks)[:10_000]
        ],
    )


def _require_ask_available(
    notebook: NotebookSummary, repo=None,
    source_scope: SourceScope | None = None,
    base_scope: BaseNotebookScope | None = None,
) -> tuple[SourceScope | None, BaseNotebookScope | None]:
    """硬约束(PR#334):笔记本在任一模式下都取不到可检索证据(NotebookSummary
    .ask_available=False——无可见来源/knowhow chunk/可用 KG/带图参考库/confirmed memory)
    时,回答只会是凭空生成,拒绝提问。前端会据同一信号禁用对话框,但那只是 UX;这里是
    路由层**权威预检**——挡住前端快照陈旧(跨标签/并发)、直连 HTTP 等一切旁路。

    刻意留在路由层、不下沉到 ask 服务:后者是冻结的 facade 契约,且有约 16 个直调
    repo.ask(空库)的测试。代价是一个**已知且被接受的极窄 TOCTOU 残留**(codex 第11轮
    P2):预检通过后、检索真正取证据前的毫秒级窗口里删掉最后一份证据,本次 ask 仍会跑完、
    可能存下一条空答案。窗口极小、后果良性(仅一条无据答案,非崩溃/越权),故不为它把
    守卫下沉进服务层。

    Also freezes and returns the checkbox library scope alongside the source
    scope, and defers the combined "is anything left to search?" decision to
    the shared ``_require_non_empty_scope`` (which the report entry points call
    directly -- see its docstring).
    """
    if not notebook.ask_available:
        raise user_error(
            409,
            "该笔记本还没有可用于回答的内容，请先添加来源，"
            "或在「设置 → 编辑当前笔记本」里挂载一个参考库。",
        )
    resolved_source_scope = source_scope
    if repo is not None:
        resolved_source_scope = _validate_source_scope(repo, notebook, source_scope)
    resolved_base_scope = _validate_base_scope(notebook, base_scope)
    _require_non_empty_scope(notebook, resolved_source_scope, resolved_base_scope)
    return resolved_source_scope, resolved_base_scope


@router.get("/notebooks/{notebook_id}/search", response_model=NotebookSearchResponse, dependencies=[Depends(require_notebook_read)])
def search_notebook(
    notebook_id: str,
    q: str = Query(""),
) -> NotebookSearchResponse:
    try:
        return notebook_catalog_repository().search_notebook(notebook_id, q)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


def _intent_history(repo, notebook_id: str, conversation_id: str | None,
                    user_id: str) -> str:
    if not conversation_id:
        return ""
    if notebook_access_repository().conversation_owner(conversation_id) != user_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        detail = repo.get_conversation(conversation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if detail.notebook_id != notebook_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    lines = []
    for turn in detail.turns[-5:]:
        # Only the user's own wording may resolve references. Assistant answers
        # are corpus-derived and would let retrieved material bias this
        # otherwise corpus-blind understanding step indirectly.
        lines.append(f"User: {turn.question}")
    return "\n".join(lines)


@router.post(
    "/notebooks/{notebook_id}/ask/intent",
    response_model=QueryIntentContract,
    dependencies=[Depends(require_notebook_read)],
)
async def preview_ask_intent(
    notebook_id: str,
    payload: AskIntentPreviewRequest,
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> QueryIntentContract:
    """Understand a reasoning question before creating a conversation/job."""
    question = payload.question.strip()
    if not question:
        raise user_error(422, "问题不能为空")
    cancel_event = threading.Event()

    def run_preview() -> QueryIntentContract:
        repo = repository()
        try:
            notebook = repo.get_notebook(notebook_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Notebook not found")
        resolved_source_scope, resolved_base_scope = _require_ask_available(
            notebook, repo, payload.source_scope, payload.base_scope
        )
        history = _intent_history(
            repo, notebook_id, payload.conversation_id, user.id
        )
        try:
            from app.services.source_scope import source_scope_context

            # 预检必须用与执行**同一个**库上限:预检期 resolve_evidence_scope()
            # 若还看得见被取消勾选的库,会为它里面的来源签出一份确认预览,而提交时
            # 按 payload.base_scope 重新解析必然失败——用户拿到一个走不通的确认。
            with source_scope_context(
                notebook_id, resolved_source_scope, resolved_base_scope
            ):
                return repo.preview_reasoning_intent(
                    notebook_id, question, history, cancel_event=cancel_event
                )
        except ValueError as exc:
            raise user_error(
                422, str(exc)
            ) from exc

    task = asyncio.create_task(asyncio.to_thread(run_preview))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.05)
            if not task.done() and await request.is_disconnected():
                cancel_event.set()
                # The provider observes cancel_event. Consume its terminal
                # exception without delaying a client that has already left.
                task.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )
                raise HTTPException(status_code=499, detail="Client Closed Request")
        return task.result()
    except asyncio.CancelledError:
        cancel_event.set()
        task.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        raise
    except AskCancelled:
        raise HTTPException(status_code=499, detail="Client Closed Request")


def _validate_confirmed_reasoning_intent(payload: AskRequest, spec) -> None:
    """Reject malformed reviewed intent before a durable Ask job exists."""
    if spec.id != "reasoning":
        return
    if payload.intent is None:
        if has_explicit_source_restriction(payload.question):
            raise user_error(422, "问题限定了来源范围，请先确认问题理解")
        # Direct compatibility clients may bypass /ask/intent. Keep clear
        # requests compatible, but fail closed on the same deterministic vague
        # wording before begin_durable_job can publish a transient session.
        seed = plan_query_intent(
            None, payload.question.strip(), "", max_topics=1
        )
        if seed.get("needs_clarification"):
            raise user_error(422, "问题仍有关键歧义，请先确认问题理解")
        return
    seed = payload.intent.contract.model_dump()
    if str(seed.get("objective") or "").strip() != payload.question.strip():
        raise user_error(422, "问题理解与当前问题不匹配，请重新确认")
    try:
        finalize_query_intent(
            seed,
            resolved_question=payload.intent.resolved_question,
            answers=[row.model_dump() for row in payload.intent.answers],
        )
    except ValueError as exc:
        if "必填澄清" in str(exc):
            raise user_error(422, "请先回答所有必填澄清问题")
        raise user_error(422, "确认后的问题不能为空")


def _validate_reasoning_scope_preflight(repo, notebook_id: str, spec, payload: AskRequest) -> None:
    """Keep invalid/expired selected scopes out of durable jobs and streams."""
    if spec.id != "reasoning":
        return
    from app.services.source_scope import source_scope_context

    try:
        # This runs before a durable job or stream header exists, so it must
        # see the same frozen checkbox ceiling as the eventual worker.  In
        # particular, a signed intent preview cannot be replayed after its
        # named source was unchecked.
        with source_scope_context(notebook_id, payload.source_scope, payload.base_scope):
            repo.validate_reasoning_submission(notebook_id, payload)
    except ValueError as exc:
        raise user_error(409, str(exc)) from exc


@router.post("/notebooks/{notebook_id}/ask", response_model=AskResponse, dependencies=[Depends(require_notebook_read)])
def ask(notebook_id: str, payload: AskRequest) -> AskResponse:
    repo = repository()
    try:
        notebook = repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    # 先校验模式(422 malformed 请求),再查可用性(409 前置条件不满足),口径与 stream 一致。
    try:
        spec = resolve_mode(payload.mode)
    except UnknownAskMode as exc:
        raise HTTPException(status_code=422, detail={
            "error": "unknown ask mode", "mode": exc.mode, "valid": list(ASK_MODES)})
    _validate_confirmed_reasoning_intent(payload, spec)
    resolved_source_scope, resolved_base_scope = _require_ask_available(
        notebook, repo, payload.source_scope, payload.base_scope
    )
    scope_updates: dict = {}
    if resolved_source_scope is not payload.source_scope:
        scope_updates["source_scope"] = resolved_source_scope
    if resolved_base_scope is not payload.base_scope:
        scope_updates["base_scope"] = resolved_base_scope
    if scope_updates:
        payload = payload.model_copy(update=scope_updates)
    _validate_reasoning_scope_preflight(repo, notebook_id, spec, payload)
    try:
        # The whole synchronous ask runs inside this thread's context, so the
        # receipt reaches AskService's single answer-persistence seam.
        with retrieval_scope_receipt_context(
            _scope_receipt(notebook, resolved_source_scope, resolved_base_scope)
        ):
            return repo.ask(notebook_id, payload)
    except UnknownAskMode as exc:
        raise HTTPException(status_code=422, detail={
            "error": "unknown ask mode", "mode": exc.mode, "valid": list(ASK_MODES)})
    except AskCancelled:
        raise HTTPException(status_code=409, detail="Ask cancelled")
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/ask-modes")
def ask_modes() -> list[dict[str, Any]]:
    """User-facing ask modes (single source: app/services/ask_modes.py).
    Copy/labels live in the frontend; this exposes ids + behavioural flags."""
    return [
        {"id": m.id, "group": m.group,
         "requires_kg": m.requires_kg, "streaming": m.streaming}
        for m in ASK_MODES.values() if m.user_facing
    ]


def _ndjson_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


async def _stream_ask_events(
    repo: AskStreamPort,
    notebook_id: str,
    payload: AskRequest,
    spec,
    request: Request,
    *,
    scope_receipt: RetrievalScopeReceipt | None = None,
):
    # Task 23: 执行编排(begin→register→started→合成 start→copy_context worker→
    # trace 持久化 fail-open→finish→unregister→空会话清理→终态事件→哨兵)整体在
    # runtime-owned AskExecutionCoordinator;本函数保留冻结签名,只剩启动编排、
    # 交付队列消费与断连轮询。Task 24: 执行体 = runtime-owned AskService(三模式
    # 注册表派发在服务内),不再是 facade runner 回调。
    # ``scope_receipt`` is keyword-only with a default so every existing
    # positional caller is unchanged. It cannot be set by ``ask_stream`` around
    # its own body: the generator is iterated after that coroutine returns, so
    # the context must be entered HERE, around the submit that snapshots it for
    # the detached worker. The block contains no ``yield`` -- set and reset
    # happen inside one ``__anext__``, never across a suspension point.
    with retrieval_scope_receipt_context(scope_receipt):
        events = repo.start_ask_stream(
            notebook_id, payload, spec,
            user_id=repo.current_user().id,
        )
    # 客户端断连只停止本次流(break),**不** set cancel_event —— worker 脱离连接
    # 跑到完、答案照存。唯一取消入口是 POST …/ask/jobs/{job_id}/cancel。
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.to_thread(events.get, True, 0.1)
            except queue.Empty:
                continue
        if event is None:
            break
        yield _ndjson_line(event)


@router.post("/notebooks/{notebook_id}/ask/stream", dependencies=[Depends(require_notebook_read)])
async def ask_stream(notebook_id: str, request: Request, payload: AskRequest) -> StreamingResponse:
    repo = repository()
    try:
        notebook = repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    try:
        spec = resolve_mode(payload.mode)
    except UnknownAskMode as exc:
        raise HTTPException(status_code=422, detail={
            "error": "unknown ask mode", "mode": exc.mode, "valid": list(ASK_MODES)})
    _validate_confirmed_reasoning_intent(payload, spec)
    resolved_source_scope, resolved_base_scope = _require_ask_available(
        notebook, repo, payload.source_scope, payload.base_scope
    )  # 空库/空来源范围权威拒绝
    scope_updates: dict = {}
    if resolved_source_scope is not payload.source_scope:
        scope_updates["source_scope"] = resolved_source_scope
    if resolved_base_scope is not payload.base_scope:
        scope_updates["base_scope"] = resolved_base_scope
    if scope_updates:
        payload = payload.model_copy(update=scope_updates)
    _validate_reasoning_scope_preflight(repo, notebook_id, spec, payload)
    return StreamingResponse(
        _stream_ask_events(
            repo, notebook_id, payload, spec, request,
            scope_receipt=_scope_receipt(
                notebook, resolved_source_scope, resolved_base_scope
            ),
        ),
        media_type="application/x-ndjson",
    )


@router.post("/notebooks/{notebook_id}/ask/jobs/{job_id}/cancel",
             dependencies=[Depends(require_notebook_read)])
def cancel_ask_job(notebook_id: str, job_id: str) -> dict:
    repo = repository()
    try:
        detail = repo.ask_job_detail(job_id)
        if (
            detail["notebook_id"] != notebook_id
            or detail["created_by"] != repo.current_user().id
        ):
            raise KeyError(job_id)
        return repo.cancel_ask_job(job_id, repo.current_user().id)
    except KeyError:
        raise HTTPException(status_code=404, detail="ask job not found")


@router.get("/notebooks/{notebook_id}/ask/jobs/{job_id}",
            dependencies=[Depends(require_notebook_read)])
def get_ask_job(notebook_id: str, job_id: str) -> dict:
    repo = repository()
    try:
        detail = repo.ask_job_detail(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="ask job not found")
    if (
        detail["notebook_id"] != notebook_id
        or detail["created_by"] != repo.current_user().id
    ):
        raise HTTPException(status_code=404, detail="ask job not found")
    return detail


@router.get("/notebooks/{notebook_id}/conversations", response_model=List[ConversationSummary], dependencies=[Depends(require_notebook_read)])
def list_conversations(notebook_id: str) -> List[ConversationSummary]:
    try:
        return repository().list_conversations(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, user: UserProfile = Depends(get_current_user)) -> ConversationDetail:
    if notebook_access_repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        return repository().get_conversation(conversation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, payload: ConversationRenameRequest, user: UserProfile = Depends(get_current_user)):
    if notebook_access_repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        repository().rename_conversation(conversation_id, payload.title)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: UserProfile = Depends(get_current_user)):
    if notebook_access_repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        repository().delete_conversation(conversation_id)
        return {"ok": True}
    except ConversationBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.delete(
    "/notebooks/{notebook_id}/conversations",
    response_model=ConversationBulkDeleteResult,
    dependencies=[Depends(require_notebook_read)],
)  # 仓库层按 created_by scope,成员删自己的旧会话
def bulk_delete_conversations(
    notebook_id: str, older_than_days: int = Query(..., ge=1)
) -> ConversationBulkDeleteResult:
    try:
        return repository().bulk_delete_conversations(notebook_id, older_than_days)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/answers/{answer_id}/feedback", response_model=FeedbackResponse)
def submit_feedback(answer_id: str, payload: FeedbackRequest, user: UserProfile = Depends(get_current_user)) -> FeedbackResponse:
    if not notebook_access_repository().user_can_read_answer(answer_id, user.id):  # owner ∪ 成员(spec §3.3)
        raise HTTPException(status_code=404, detail="Answer not found")
    try:
        return repository().submit_feedback(answer_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Answer not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
