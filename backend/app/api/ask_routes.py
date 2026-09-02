import asyncio
import queue
import threading
from time import monotonic
from typing import Any, List

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.core.config import get_settings
from app.core.ask_retrieval_policy import DEFAULT_RETRIEVAL_EFFORT
from app.bootstrap import application_extension_runtime
from app.domain.ask_engine import AskPluginEngineError

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
    ResolvedSourceScope,
    SourceScope,
)
from app.models.ask import (
    AskIntentConfirmation,
    AskIntentPreviewRequest,
    AskRequest,
    AskResponse,
    ConversationBulkDeleteResult,
    ConversationDetail,
    ConversationRenameRequest,
    ConversationShareRequest,
    ConversationShareResponse,
    ConversationSummary,
    FeedbackRequest,
    FeedbackResponse,
    NotebookSearchResponse,
    PublicConversation,
    QueryIntentContract,
)
from app.models.identity import UserProfile
from app.repositories.ports import (
    AskStreamPort,
    ConversationBusyError,
    ConversationHasNoShareableAnswer,
    ConversationShareWatermarkStale,
)
from app.services.ask_modes import (
    ASK_MODES,
    AUTO_MODE,
    UnknownAskMode,
    resolve_mode,
    user_facing_modes,
)
from app.services.cancellation import AskCancelled
from app.services.query_intent import (
    auto_ask_mode_from_intent,
    finalize_query_intent,
    plan_query_intent,
)
from app.services.conversation_public_view import (
    public_conversation_payload,
    resolve_conversation_asset_alias,
)
from app.services.knowhow.assets import ALLOWED_MIME_EXTENSIONS, AssetService
from app.services.search_concurrency import run_under_search_gate
from app.services.source_scope import retrieval_scope_receipt_context
from app.api.task_stream import (
    ClosingStreamingResponse,
    NDJSON_STREAM_HEADERS,
    ndjson_line,
    task_stream_response,
)


# Protocol heartbeat: keep the NDJSON transport active across model calls whose
# own trace event may take longer than a reverse proxy's idle timeout.
ASK_STREAM_HEARTBEAT_SECONDS = 5.0

router = APIRouter()
# Anonymous surface (T3): the ONE conversation read that needs no session. It
# MUST NOT be mounted on the main API router — that router carries a router-level
# ``Depends(get_current_user)`` ("零逐路由遗漏"), which would 401 the very
# visitors this endpoint exists for. ``main.py`` includes this router WITHOUT
# that dependency (mirrors ``report_routes.public_router``).
public_router = APIRouter()


def _extension_ask_modes():
    host = application_extension_runtime().ask_engines
    return tuple(
        mode for mode in host.modes() if host.is_available(mode.id)
    )


def _valid_ask_mode_ids() -> list[str]:
    return [AUTO_MODE, *(
        mode.id for mode in user_facing_modes(_extension_ask_modes())
    )]


def _plugin_engine_http_error(exc: AskPluginEngineError) -> HTTPException:
    message = (
        "扩展引擎返回了无法核验的引用"
        if exc.code == "plugin_engine_unverified_citation"
        else "扩展引擎暂时无法完成回答，请重试"
    )
    # user_error() 打 X-User-Message 头——错误文案红线按**出处**放行:没有这个头,
    # 前端只显示按状态码泛化的通用文案,这两句中文永远上不了屏。detail 必须是纯
    # 文案字符串(前端展示闸拒绝带花括号的形态);exc.code 只进诊断通道,同步路
    # 的调用方不需要它(stream 路的 code 翻译在 frontend/app/errors.ts)。
    return user_error(502, message)


def _validate_source_scope(repo, notebook: NotebookSummary,
                           scope: SourceScope | None) -> SourceScope | None:
    """Validate and freeze a checkbox scope as an explicit active-source ceiling.

    The browser uses exclusions while "all" is selected because that keeps
    toggles compact.  Workers must not carry that moving definition: normalize
    it once to an include-list so concurrent uploads cannot widen the run and
    every candidate producer can push the same allow-list below its LIMIT —
    an all-selected freeze included, because this list is NOT the universe an
    unfiltered producer would read (it excludes other members' private Memory
    projections, and it is a snapshot rather than a live set).  ``narrowed``
    below therefore never decides whether the list is pushed down; it decides
    whether a producer's source predicate actually BOUNDS its scan, which is
    what the lexical corpus-language gate routes on (``retrieval_candidates
    ._lexical_gate_source_scoped``).

    The hidden half is read FOR THE REQUESTING USER.  Knowhow projection
    sources are notebook-wide (every member reads the table itself), but a
    Memory projection source belongs to one user — the same
    ``memory_items.created_by`` predicate the Memory retriever and every other
    Memory path already use.  Read without it, a shared notebook's default
    non-narrowed request froze every OTHER member's Memory projection source
    into this run's ceiling, and the elements and KG objects those sources own
    then became reachable through ordinary candidate retrieval and through the
    whole-graph/PPR channels a non-narrowed run re-enables.  The identity is
    taken here rather than passed in by each caller so there is exactly one
    definition of "whose Memory may enter a ceiling" and no call site can
    forget it; ``current_user()`` reads the request ContextVar, so it costs no
    round trip.  For a report re-frozen at confirm/generate that identity is
    whoever is driving THAT phase — which can differ from the report's author
    in a shared notebook, and is the fail-closed direction either way (a user
    only ever gets their own Memory).

    Deliberately NOT folded into the visible-half read: the compact ``include``
    path reads only the requested ids plus a count, and merging both partitions
    into one whole-universe query would put every source row back on the hot
    path to answer a question bounded by the notebook's Memory/Knowhow count.
    The hidden read stays separate and stays conditional on ``narrowed``.

    Deliberately does NOT decide whether the resulting (possibly empty) local
    scope leaves the run with any evidence universe at all -- that check also
    depends on the frozen base-library scope (see ``_validate_base_scope``) and
    is combined once in ``_require_non_empty_scope``, which Ask and the report
    entry points each call.
    """
    if scope is None:
        return None
    owner_id = repo.current_user().id
    if scope.mode == "include":
        selected = list(scope.source_ids)
        visible_selected, visible_count = repo.visible_source_scope_snapshot(
            notebook.id, selected
        )
        if len(visible_selected) != len(selected):
            raise user_error(422, "检索范围包含不属于当前笔记本的来源")
        narrowed = len(selected) != visible_count
    else:
        # Legacy/browser exclude scopes still need their complement frozen to
        # an explicit include-list. Compact few-source requests use ``include``
        # and never enter this whole-universe compatibility path.
        universe = repo.all_visible_source_ids(notebook.id)
        visible = set(universe)
        if any(source_id not in visible for source_id in scope.source_ids):
            raise user_error(422, "检索范围包含不属于当前笔记本的来源")
        excluded = set(scope.source_ids)
        selected = [
            source_id for source_id in universe
            if source_id not in excluded
        ]
        narrowed = bool(excluded)
    # ``selected`` is a trusted server snapshot, not the client's submitted
    # list.  Large notebooks can legitimately exceed SourceScope's request
    # ceiling when compact ``exclude`` is expanded into a frozen include-list.
    resolved = ResolvedSourceScope(
        mode="include",
        source_ids=selected,
        # Never trust a client-supplied value: narrowing is a relation between
        # the validated selection and the server's current visible universe.
        narrowed=narrowed,
    )
    resolved._hidden_source_ids = (
        [] if narrowed else repo.hidden_source_ids(notebook.id, owner_id)
    )
    # Carried even on a narrowed run: the drift probe reads the same partition
    # back and must do so as the same user, whether or not this freeze kept it.
    resolved._scope_owner_id = owner_id
    return resolved


def _validate_base_scope(
    notebook: NotebookSummary, scope: BaseNotebookScope | None
) -> BaseNotebookScope | None:
    """Validate and freeze a checkbox library scope as an explicit include
    ceiling over ``notebook``'s mounted reference libraries.

    Mirrors ``_validate_source_scope``'s exclude→include freeze for the same
    reason (a race-free, fixed set for every candidate producer to intersect
    against), but the valid universe already rides on ``notebook`` -- mounted
    base libraries are not a per-request repo round trip the way sources are,
    so this costs zero extra queries even on the default all-selected path.

    ``exclude`` + an EMPTY list -- the browser's compact "select all libraries"
    representation -- MUST still expand into an explicit ``include`` list
    naming every currently-mounted library, never short-circuit to ``None``.
    ``None`` reads as "no scope was submitted at all" to every downstream
    consumer, which is only harmless for Ask (validation and retrieval happen
    inside one synchronous request).  It is NOT harmless for a report:
    ``create_report`` calls this once and PERSISTS the result into
    ``understanding["base_scope"]``, and that persisted value is the
    authoritative ceiling re-applied at intent confirmation and again at
    generation start -- two later phases, potentially hours apart.  Freezing to
    ``None`` at create time would throw away "these are the libraries mounted
    right now", so a library mounted afterwards would silently join a report
    the user never scoped it into.
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
        selected = [
            ref.id for ref in notebook.base_notebooks if ref.id not in submitted
        ]
    # Recomputed unconditionally (a client-supplied ``narrowed`` is never
    # trusted). The mount roster is already on the summary, so this is pure
    # arithmetic -- no round trip. Both sides come from that one roster, so the
    # comparison is commensurable by construction (membership was checked
    # above, hence ``selected ⊆ valid_ids``).
    return BaseNotebookScope(
        mode="include",
        notebook_ids=selected,
        narrowed=len(selected) < len(valid_ids),
    )


def _require_non_empty_scope(
    notebook: NotebookSummary,
    resolved_source_scope: SourceScope | None,
    resolved_base_scope: BaseNotebookScope | None,
) -> None:
    """Reject a retrieval scope whose evidence universe is empty across BOTH
    dimensions at once.

    Lives here rather than inside ``_validate_source_scope`` (where the local
    half used to be) because it needs the frozen base-library scope too, and in
    its own function rather than inside ``_require_ask_available`` because the
    report entry points must run this same check WITHOUT the ``ask_available``
    gate: a report may legitimately be created on a notebook the Ask box would
    refuse, and widening that is a separate behaviour change.

    判据是「这次勾了哪些库」而不是「挂了哪些库」。Each dimension answers with
    its FROZEN selection when the request actually NARROWED it, and with the
    notebook's real evidence universe when it did not. Neither fallback costs a
    round trip: both ride on the ``NotebookSummary`` the caller already loaded.

    A dimension the request did not narrow must NOT be read as a non-empty one:
    that shortcut is what would let a library-only notebook (zero local
    evidence, ``ask_available`` true through its mounted libraries) submit
    ``base_scope`` alone with every library unchecked and still be accepted.
    But a request that scoped NEITHER dimension is not making a selection at
    all and is left alone -- ``ask_available`` is the authority there.
    """
    if resolved_source_scope is None and resolved_base_scope is None:
        return
    # 本地维度的证据宇宙 ≠ 可见导入来源数。ask_available 的四个判据里有**三个**是本地的
    # (任意 chunk[含 Knowhow 格子]、本地可用 KG、该用户的已确认 Memory),而
    # counts["sources"] 只覆盖第一个的一部分:Knowhow 表与 Memory 根本没有可见来源行。
    # 拿来源数当本地宇宙,会把「只有 Knowhow / 只有已确认 Memory + 显式取消勾选全部
    # 参考库」误拒成空范围 —— 而浏览器恒发 source_scope,零可见来源时它冻结成
    # include:[],所以那是**界面可达**的误拒。
    # 故本地宇宙改问 NotebookSummary.local_evidence_available(catalog 侧由那三个本地
    # 判据算出,零新增查询),并与 counts["sources"] 取**或**:该信号只增不减,尚在解析
    # (有来源、还没有 chunk)的库仍按旧判据放行,列表投影未回填时(默认 False)也逐字
    # 回落到旧行为。
    local_universe_non_empty = (
        bool(notebook.local_evidence_available)
        or int(notebook.counts.get("sources", 0) or 0) > 0
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

    ABSENT when the request narrowed NEITHER dimension -- the same rule
    ``current_source_scope_payload``/``current_base_scope_payload`` enforce and
    for the same reason: the browser always submits both scopes, so a receipt
    reading "5 of 5 来源" would appear on every ordinary question. When ONE
    dimension was narrowed the other is still reported, truthfully, as whole --
    that pairing is what the real incident turned on (a single local source
    checked while 84 reference-library papers answered the question), so
    suppressing the untouched half would hide exactly the line worth reading.

    Costs no round trip: every input rides on the ``NotebookSummary`` the
    caller already loaded. ``counts["sources"]`` is ``visible_source_count`` --
    ``all_visible_source_ids`` counted under an identical predicate -- so
    ``selected`` and ``total`` are commensurable.

    Deliberately keeps reading that cached count even though
    ``_validate_source_scope`` takes its universe from a live read: a
    concurrent upload can make this ``total`` lag the frozen scope by one, and
    for a DISPLAY line that is tolerable staleness, whereas ``narrowed`` is a
    GATE. Different consequence, different price worth paying.

    Both dimensions are read through their ``mode``. Every route caller hands
    over the frozen include ceiling, so the exclude branches are unreachable
    from HTTP -- but reading an exclude scope as if it were an include one
    would INVERT the receipt (report the unchecked libraries as the searched
    ones), and a scope report that can lie about the scope is worth less than
    no report.
    """
    source_narrowed = bool(
        resolved_source_scope is not None and resolved_source_scope.narrowed
    )
    base_narrowed = bool(
        resolved_base_scope is not None and resolved_base_scope.narrowed
    )
    if not source_narrowed and not base_narrowed:
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
            # silently drop libraries, and the receipt is a disclosure -- a
            # truncated list presented as exhaustive makes the reader compute a
            # wrong denominator.
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
    directly -- see its docstring)."""
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
async def search_notebook(
    notebook_id: str,
    q: str = Query(""),
) -> NotebookSearchResponse:
    # 说明写成注释而不是 docstring:FastAPI 会把路由函数的 docstring 原样搬进
    # OpenAPI 的 operation.description,而 OpenAPI 形状是冻结契约
    # (tests/test_repository_api_contract.py)。给内部读者的解释不该改公开契约。
    #
    # Z8 (P0 止血): 服务端并发闸,与 MCP search_notebook_context 共用同一个信号量
    # (见 search_concurrency 模块 docstring)。
    #
    # 刻意是 async def 而不是同步路由。同步路由跑在 Starlette 的 anyio 线程池里
    # (40 个 token,**全站同步端点共享**),在那里阻塞式 acquire 会让每个等待者占住
    # 一个 token:一次 10 用户 x 前端 4 路扇出的搜索突发就能把 40 个 token 全部占满,
    # /notebooks、来源列表、checkup、上传统统拿不到线程——闸本身变成全站宕机的成因
    # (批 0 评审 P1)。改成 async 之后等待发生在事件循环上,等待者是一个挂起的协程:
    # 不占线程、不占连接、不占 token;只有拿到票的那 <= 4 个才用 asyncio.to_thread
    # 各占一条线程和一条连接跑真正的扫描。
    #
    # 刻意不设超时/拒绝——拒绝会静默收窄某次搜索的结果覆盖面。响应语义与同步版本
    # 逐字相同,包括 KeyError -> 404 这条映射。
    #
    # 走 run_under_search_gate 而不是裸 async with:客户端断连/超时/关停会取消这个
    # 请求任务,但已经在扫描里的工作线程停不下来。裸 async with 会在取消展开时立刻
    # 放票,后面四个请求随即进场,而被放弃的那次扫描还占着线程和连接——真实并发
    # 超过上限,池耗尽重演(codex #627 R3 P1)。票绑在**工作**上,线程真正跑完才归还。

    def run_search() -> NotebookSearchResponse:
        return notebook_catalog_repository().search_notebook(notebook_id, q)

    try:
        return await run_under_search_gate(lambda: asyncio.to_thread(run_search))
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


def _resolve_auto_ask_request(
    repo,
    notebook_id: str,
    payload: AskRequest,
    history: str = "",
    cancel_event: threading.Event | None = None,
):
    """Resolve request-only ``mode=auto`` before durable state is created."""
    question = payload.question.strip()
    if not question:
        spec = resolve_mode("chunk", _extension_ask_modes())
        return payload.model_copy(update={"mode": spec.id}), spec
    contract = repo.preview_reasoning_intent(
        notebook_id,
        question,
        history,
        cancel_event=cancel_event,
    )
    selected = auto_ask_mode_from_intent(contract)
    # The classifier has a closed return contract, but the route still resolves
    # through the canonical registry rather than trusting a model-adjacent value.
    spec = resolve_mode(selected, _extension_ask_modes())
    updates = {
        "mode": spec.id,
        # The simplified interface intentionally has no quality/cost control.
        # Ignore values supplied by stale or direct clients and freeze the
        # documented automatic-mode default before durable state is created.
        "retrieval_effort": DEFAULT_RETRIEVAL_EFFORT,
    }
    if spec.id == "reasoning":
        # Clear automatic-mode questions use the same model-produced contract
        # as the advanced UI's clear-intent auto-confirm path.  This avoids a
        # second understanding call and preserves its retrieval directions.
        updates["intent"] = AskIntentConfirmation(
            contract=contract,
            resolved_question=contract.resolved_question,
        )
    return payload.model_copy(update=updates), spec


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

    def prepare_preview():
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
        return repo, resolved_source_scope, resolved_base_scope, history

    repo, resolved_source_scope, resolved_base_scope, history = (
        await asyncio.to_thread(prepare_preview)
    )

    def run_preview() -> QueryIntentContract:
        from app.services.source_scope import source_scope_context

        # No ValueError handler here on purpose.  The one that used to live at
        # this seam existed to surface the model-inferred source-scope errors
        # ("找不到指定来源：…"), and those are gone with that feature.  What
        # remains reachable is pydantic's ValidationError — a ValueError
        # subclass carrying English internals — so re-adding a
        # ``user_error(422, str(exc))`` here would trust an error by its shape
        # rather than its provenance and leak that text as a user message.
        # 预检必须用与执行**同一个**库上限:预检期若还看得见被取消勾选的库,
        # 它侦察出的证据面与提交时按同一份 base_scope 重新解析的结果会不一致,
        # 用户会拿到一份走不通的确认。
        with source_scope_context(
            notebook_id, resolved_source_scope, resolved_base_scope
        ):
            return repo.preview_reasoning_intent(
                notebook_id, question, history, cancel_event=cancel_event
            )

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


@router.post(
    "/notebooks/{notebook_id}/ask/intent/stream",
    dependencies=[Depends(require_notebook_read)],
)
async def preview_ask_intent_stream(
    notebook_id: str,
    payload: AskIntentPreviewRequest,
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> StreamingResponse:
    """Heartbeat-capable browser transport for the request-local intent pass.

    The original JSON endpoint remains available for compatibility clients.
    Scope/history validation finishes before the first frame so those failures
    retain their authoritative HTTP status; only the potentially slow model
    call runs inside the stream.
    """
    question = payload.question.strip()
    if not question:
        raise user_error(422, "问题不能为空")

    def prepare_preview():
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
        return repo, resolved_source_scope, resolved_base_scope, history

    repo, resolved_source_scope, resolved_base_scope, history = (
        await asyncio.to_thread(prepare_preview)
    )
    cancel_event = threading.Event()

    def run_preview() -> QueryIntentContract:
        from app.services.source_scope import source_scope_context

        with source_scope_context(
            notebook_id, resolved_source_scope, resolved_base_scope
        ):
            return repo.preview_reasoning_intent(
                notebook_id, question, history, cancel_event=cancel_event
            )

    return task_stream_response(
        request,
        run_preview,
        stage="ask_intent",
        error_code="ask_intent_failed",
        cancel_event=cancel_event,
    )


def _validate_confirmed_reasoning_intent(payload: AskRequest, spec) -> None:
    """Reject malformed reviewed intent before a durable Ask job exists."""
    if spec.id != "reasoning":
        return
    if payload.intent is None:
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


def _apply_resolved_scopes(
    payload: AskRequest,
    resolved_source_scope: SourceScope | None,
    resolved_base_scope: BaseNotebookScope | None,
) -> AskRequest:
    """Swap the frozen ceilings onto the payload, copying only when one of them
    actually changed identity.

    ``model_copy`` is skipped entirely when neither dimension was submitted, so
    a compatibility client's payload reaches ``repo.ask``/``start_ask_stream``
    as the very same object it was before either scope existed.
    """
    updates: dict = {}
    if resolved_source_scope is not payload.source_scope:
        updates["source_scope"] = resolved_source_scope
    if resolved_base_scope is not payload.base_scope:
        updates["base_scope"] = resolved_base_scope
    return payload.model_copy(update=updates) if updates else payload


@router.post("/notebooks/{notebook_id}/ask", response_model=AskResponse, dependencies=[Depends(require_notebook_read)])
def ask(notebook_id: str, payload: AskRequest) -> AskResponse:
    repo = repository()
    try:
        notebook = repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    # 先校验具名模式(422 malformed 请求),再查可用性(409 前置条件不满足),口径与
    # stream 一致。auto 是请求级选择器,不是 registry engine；空库先 409，避免为
    # 一次注定不能执行的请求花路由模型调用。
    spec = None
    if payload.mode != AUTO_MODE:
        try:
            spec = resolve_mode(payload.mode, _extension_ask_modes())
        except UnknownAskMode as exc:
            raise HTTPException(status_code=422, detail={
                "error": "unknown ask mode", "mode": exc.mode,
                "valid": _valid_ask_mode_ids()})
        _validate_confirmed_reasoning_intent(payload, spec)
    resolved_source_scope, resolved_base_scope = _require_ask_available(
        notebook, repo, payload.source_scope, payload.base_scope
    )
    payload = _apply_resolved_scopes(
        payload, resolved_source_scope, resolved_base_scope
    )
    if spec is None:
        history = _intent_history(
            repo,
            notebook_id,
            payload.conversation_id,
            repo.current_user().id,
        )
        payload, spec = _resolve_auto_ask_request(
            repo, notebook_id, payload, history
        )
        _validate_confirmed_reasoning_intent(payload, spec)
    try:
        # The whole synchronous ask runs inside this thread's context, so the
        # receipt reaches AskService's single answer-persistence seam.
        with retrieval_scope_receipt_context(
            _scope_receipt(notebook, resolved_source_scope, resolved_base_scope)
        ):
            return repo.ask(notebook_id, payload)
    except UnknownAskMode as exc:
        raise HTTPException(status_code=422, detail={
            "error": "unknown ask mode", "mode": exc.mode,
            "valid": _valid_ask_mode_ids()})
    except AskCancelled:
        raise HTTPException(status_code=409, detail="Ask cancelled")
    except AskPluginEngineError as exc:
        raise _plugin_engine_http_error(exc)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/ask-modes")
def ask_modes() -> list[dict[str, Any]]:
    """Sanitized built-in truth plus live deployment-engine projection."""
    builtins = [
        {
            "id": mode.id,
            "group": mode.group,
            "requires_kg": mode.requires_kg,
            "streaming": mode.streaming,
            "streams_trace": mode.streaming,
        }
        for mode in ASK_MODES.values() if mode.user_facing
    ]
    host = application_extension_runtime().ask_engines
    extensions: list[dict[str, Any]] = []
    for item in host.registrations():
        if not host.is_available(item.descriptor.mode_id):
            continue
        mode = host.mode(item.descriptor.mode_id)
        if mode is None:  # Defensive only; registrations and modes share one host.
            continue
        extensions.append({
            "id": item.descriptor.mode_id,
            "group": "extension",
            "label": item.descriptor.label,
            "desc": item.descriptor.description,
            "requires_kg": item.descriptor.requires_kg,
            "streaming": mode.streaming,
            "streams_trace": mode.streaming,
        })
    return [*builtins, *extensions]


async def _stream_ask_events(
    repo: AskStreamPort,
    notebook_id: str,
    payload: AskRequest,
    spec,
    request: Request,
    *,
    scope_receipt: RetrievalScopeReceipt | None = None,
    resolve=None,
):
    # ``resolve`` (auto mode): engine selection runs inside the detached worker
    # AFTER the durable job exists and ``started`` is queued, so leaving the page
    # during selection can no longer lose the question (see the coordinator).
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
        # Z4: repo.start_ask_stream() 本身是同步 DB/编排调用(register+begin+
        # 合成 start 事件),不下沉到线程会阻塞事件循环。asyncio.to_thread 会
        # copy_context() 后在该线程内跑 func,所以上面刚 set 的
        # retrieval_scope_receipt_context 依旧对这次调用(以及它内部
        # background_jobs.submit 的第二层 copy_context)可见 —— 与本函数
        # docstring 要求的"同一个 __anext__、不跨 yield"一致,这里跨的是
        # await,不是该 async generator 自己的 yield。用一个零参闭包(而不是
        # 直接把 repo.start_ask_stream 当可调用对象传给 to_thread)包一层,
        # 是为了让 test_repository_protocol_coverage.py 的 AskStreamPort
        # 调用面扫描(按字面 ``repo.start_ask_stream(...)`` 调用形态识别)仍能
        # 认出这个调用点——纯按引用传参会让它从「被调用」变成「被引用」。
        def _start_ask_stream():
            # ``resolve`` only travels on the auto-mode path, so resolved-mode
            # callers (and their narrow test doubles) keep the frozen call shape.
            return repo.start_ask_stream(
                notebook_id, payload, spec,
                user_id=repo.current_user().id,
                **({"resolve": resolve} if resolve is not None else {}),
            )

        events = await asyncio.to_thread(_start_ask_stream)
    last_delivery = monotonic()
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
                now = monotonic()
                if now - last_delivery >= ASK_STREAM_HEARTBEAT_SECONDS:
                    # An empty NDJSON line is transport-only: it carries no
                    # notebook content and existing clients already ignore it.
                    yield "\n"
                    last_delivery = now
                continue
        if event is None:
            break
        yield ndjson_line(event)
        last_delivery = monotonic()


async def _stream_auto_ask_events(
    repo,
    notebook_id: str,
    payload: AskRequest,
    history: str,
    request: Request,
    *,
    scope_receipt: RetrievalScopeReceipt,
):
    """Automatic mode: the durable job comes FIRST, engine selection second.

    Engine selection is a model call that can take seconds. It used to run
    here, before any durable state existed, so a refresh, a closed tab or a
    navigation-triggered disconnect during that window lost the question
    entirely. Now the coordinator begins the job under the request-only
    ``auto`` id and queues ``started`` immediately; ``resolve`` runs inside the
    detached worker (same copied request context), then the resolved engine is
    recorded on the job row and executed. A transport disconnect therefore
    behaves exactly like every other detached Ask — the job runs to completion
    — and only the explicit cancel endpoint (via the cancel event ``resolve``
    is handed) aborts selection.
    """
    def resolve(cancel_event: threading.Event):
        routed_payload, spec = _resolve_auto_ask_request(
            repo, notebook_id, payload, history, cancel_event,
        )
        _validate_confirmed_reasoning_intent(routed_payload, spec)
        return routed_payload, spec

    async for line in _stream_ask_events(
        repo,
        notebook_id,
        payload,
        None,
        request,
        scope_receipt=scope_receipt,
        resolve=resolve,
    ):
        yield line


@router.post("/notebooks/{notebook_id}/ask/stream", dependencies=[Depends(require_notebook_read)])
async def ask_stream(notebook_id: str, request: Request, payload: AskRequest) -> StreamingResponse:
    repo = repository()

    # Z4: 这整段(get_notebook + _require_ask_available[含 48k 级 all_visible_
    # source_ids/hidden_source_ids 回表] + _intent_history)原来在事件循环线程
    # 上直接跑,大库上是秒级同步阻塞。逐字对齐 preview_ask_intent/
    # preview_ask_intent_stream 的 prepare_preview() 包装形态:同一批同步调用
    # 打包成一个函数,整体丢进 asyncio.to_thread,异常(KeyError→404、
    # UnknownAskMode→422、_require_ask_available 的 409/422)原样从线程里抛出、
    # 经 await 传播回来,语义不变。
    def prepare_ask_stream():
        try:
            notebook = repo.get_notebook(notebook_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Notebook not found")
        spec = None
        if payload.mode != AUTO_MODE:
            try:
                spec = resolve_mode(payload.mode, _extension_ask_modes())
            except UnknownAskMode as exc:
                raise HTTPException(status_code=422, detail={
                    "error": "unknown ask mode", "mode": exc.mode,
                    "valid": _valid_ask_mode_ids()})
            _validate_confirmed_reasoning_intent(payload, spec)
        resolved_source_scope, resolved_base_scope = _require_ask_available(
            notebook, repo, payload.source_scope, payload.base_scope
        )  # 空库/空检索范围权威拒绝
        updated_payload = _apply_resolved_scopes(
            payload, resolved_source_scope, resolved_base_scope
        )
        history = None
        if spec is None:
            history = _intent_history(
                repo,
                notebook_id,
                updated_payload.conversation_id,
                repo.current_user().id,
            )
        return (
            notebook, spec, updated_payload,
            resolved_source_scope, resolved_base_scope, history,
        )

    (
        notebook, spec, payload,
        resolved_source_scope, resolved_base_scope, history,
    ) = await asyncio.to_thread(prepare_ask_stream)

    if spec is None:
        return ClosingStreamingResponse(
            _stream_auto_ask_events(
                repo,
                notebook_id,
                payload,
                history,
                request,
                scope_receipt=_scope_receipt(
                    notebook, resolved_source_scope, resolved_base_scope
                ),
            ),
            media_type="application/x-ndjson",
            headers=NDJSON_STREAM_HEADERS,
        )
    return StreamingResponse(
        _stream_ask_events(
            repo, notebook_id, payload, spec, request,
            scope_receipt=_scope_receipt(
                notebook, resolved_source_scope, resolved_base_scope
            ),
        ),
        media_type="application/x-ndjson",
        headers=NDJSON_STREAM_HEADERS,
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


# Over-length titles are refused here with 422 and never stored clipped:
# `ConversationRenameRequest.title` carries `max_length=CONVERSATION_TITLE_MAX_CHARS`,
# so FastAPI rejects the body before this function runs. That refusal is what
# lets the public share page serve the title verbatim; see the constant.
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


# --- 会话公开分享(T2:发放/回读/撤销 + 水位推进)------------------------
#
# 三个已认证端点,镜像 report_routes 的 share/get/unshare 形态:notebook 层
# `require_notebook_read`(owner ∪ 只读成员 ∪ 有效授权边)+ 行级 created_by 门。
# 匿名页(`GET /public/conversations/{token}`)与图片通道是 T3/T4,不在此。


def _own_conversation_or_404(
    repo, notebook_id: str, conversation_id: str
) -> str:
    """Row-level gate for the authenticated conversation-share endpoints.

    One notebook-scoped read answers membership and ownership at once (mirrors
    ``report_routes._own_report_or_404``): ``conversation_creator`` filters on
    BOTH ids, so a conversation id belonging to another notebook can never
    resolve here. Three outcomes, IN THIS ORDER:

    * not in this notebook -> 404. "Exists but not yours" and "does not exist"
      are the same 404 — a distinguishable response would turn the endpoint into
      an oracle for whose conversations live in a readable notebook.
    * empty ``created_by`` -> 409, fail closed. ``conversations.created_by`` is
      ``DEFAULT ''``, so legacy rows can be creatorless. Such a conversation can
      never be live-re-authorized (design doc §五 / §3.1), and a public link
      that can never be re-checked is the one state this feature refuses to
      create — so every share operation on it surfaces the same actionable 409.
    * creator != caller -> 404 (same indistinguishable 404 as not-found).

    Checking empty BEFORE the equality gate is what makes the 409 reachable:
    ``''`` never equals a real user id, so an equality-first gate would 404
    creatorless rows and the fail-closed branch would be dead code.

    Returns the (equal) creator id; callers currently discard it, exactly like
    the report ``cancel``/``delete``/``unshare`` handlers discard their row.
    """
    owner = repo.conversation_creator(notebook_id, conversation_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not owner:
        raise user_error(409, "这条会话缺少创建者，无法公开分享。")
    if owner != repo.current_user().id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return owner


@router.post(
    "/notebooks/{notebook_id}/conversations/{conversation_id}/share",
    response_model=ConversationShareResponse,
    dependencies=[Depends(require_notebook_read)],
)
def share_conversation_route(
    notebook_id: str,
    conversation_id: str,
    payload: ConversationShareRequest | None = Body(default=None),
) -> ConversationShareResponse:
    """Publish one conversation behind an unguessable link AND advance the read
    watermark — "share" and "update to latest" are the same call (design doc
    §四), so the button label alone decides which one the user thinks they
    pressed.

    ``expected_through_id`` (the newest answer the client saw in the turns it
    disclosed) pins the watermark to EXACTLY that answer (codex #522 R2 P1): the
    published snapshot then equals the disclosed one, closing the TOCTOU where an
    in-flight answer completes (or another tab adds a turn) between the client's
    disclosure read and this POST and the watermark would otherwise jump to an
    unreviewed, unconsented answer. When the disclosed boundary answer has since
    been deleted the store raises ``ConversationShareWatermarkStale`` → 409, so
    the user reloads and re-reviews rather than publishing "latest" silently.
    An empty/absent body falls back to "current latest" (legacy behaviour).

    Row-level gated (creator-only). Only conversations with at least one written
    answer can be shared (design doc §七 item 5): an in-flight or never-answered
    conversation has nothing completed to show. ``share_conversation`` enforces
    that ATOMICALLY — it raises ``ConversationHasNoShareableAnswer`` inside the
    same write transaction and never mints a token (codex #522 R5), so there is
    no NULL-watermark row to compensate away afterwards. This replaced the old
    share-then-check path (mint a NULL-watermark token, then a second
    ``discard_unwatermarked_share`` rolls it back), which could leave a permanent
    token-without-watermark row if the process died between the two steps.
    """
    repo = repository()
    _own_conversation_or_404(repo, notebook_id, conversation_id)
    expected = payload.expected_through_id if payload else ""
    try:
        state = repo.share_conversation(notebook_id, conversation_id, expected)
    except KeyError:
        # Deleted between the gate read and the share write.
        raise HTTPException(status_code=404, detail="Conversation not found")
    except ConversationShareWatermarkStale:
        # The disclosed boundary answer was deleted — no token was issued; make
        # the user reload and re-review rather than publish an unconsented span.
        raise user_error(409, "这条会话已有变化，请刷新后重新分享。")
    except ConversationHasNoShareableAnswer:
        # No committed answer to bound the snapshot — the store refused to mint a
        # token atomically, so nothing to roll back (codex #522 R5).
        raise user_error(409, "这条会话还没有已完成的回答，暂时无法分享。")
    return ConversationShareResponse(
        share_token=state["share_token"],
        shared_through_at=state["shared_through_at"],
        shared_through_id=state["shared_through_id"],
    )


@router.get(
    "/notebooks/{notebook_id}/conversations/{conversation_id}/share",
    response_model=ConversationShareResponse,
    dependencies=[Depends(require_notebook_read)],
)
def get_conversation_share_route(
    notebook_id: str, conversation_id: str
) -> ConversationShareResponse:
    """Read back the existing link + watermark. Write-guarded: the token *is*
    the grant, so it is served only to the creator, never to a read-only member
    (mirrors ``report_routes.get_report_share_route``). Not shared -> 404, the
    same as an unknown token.
    """
    repo = repository()
    _own_conversation_or_404(repo, notebook_id, conversation_id)
    try:
        state = repo.conversation_share_state(notebook_id, conversation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not state.get("share_token"):
        raise HTTPException(status_code=404, detail="conversation is not shared")
    return ConversationShareResponse(
        share_token=state["share_token"],
        shared_through_at=state["shared_through_at"],
        shared_through_id=state["shared_through_id"],
    )


@router.delete(
    "/notebooks/{notebook_id}/conversations/{conversation_id}/share",
    status_code=204,
    dependencies=[Depends(require_notebook_read)],
)
def unshare_conversation_route(
    notebook_id: str, conversation_id: str
) -> None:
    """Revoke the link. The next public request 404s like any unknown token.
    Row-level gated (creator-only) like the other two: the silent zero-row
    UPDATE underneath would report success either way.
    """
    repo = repository()
    _own_conversation_or_404(repo, notebook_id, conversation_id)
    repo.unshare_conversation(notebook_id, conversation_id)


def _public_conversation_or_404(repo, token: str) -> dict:
    """Resolve a shared conversation by token and run the live creator re-check.

    Shared by the page endpoint and the image endpoint so the two gates can
    NEVER diverge — the design (§七 item 3) requires the image channel to run
    "the SAME live re-check" as the page. Both stay legal on the anonymous
    router because nothing here binds a request user: ``public_conversation_by_token``
    and ``user_can_read_notebook`` take their ids explicitly and never consult
    the ContextVar (which falls back to the seeded admin when unset).

    Three failures, all the SAME 404 as an unknown token (a distinguishable
    response would report on someone's group membership to an anonymous caller):

    * unknown/revoked token — nothing to serve;
    * empty ``created_by`` — fail-closed defense in depth (codex T3 review).
      ``user_can_read_notebook`` does NOT reject an empty user id on its own: a
      notebook carrying an ``everyone`` grant has a read arm that ignores the
      user id and returns True for ``created_by=""``. T2's share gate already
      409s an empty creator so no such shared row exists today, but the anonymous
      surface must not lean on an upstream gate as its only defense;
    * creator no longer has read access — live re-authorization (design §五 /
      C-2). A group member may publish a conversation built from a shared
      notebook's corpus, but that link lasts exactly as long as their read
      access: revoke the grant / remove them from the group / delete the group
      and it dies immediately; restore access and the SAME token revives. This
      lives here rather than as a cascade at every revocation point, for the
      same reason mount validity is a live predicate — there are several ways to
      lose read access and a cascade would have to be re-derived at each.

    Returns the row with its GATE fields (``notebook_id``/``created_by``) still
    present; the page route pops them before projection, the image route only
    reads notebook scope from them and never projects the row.
    """
    row = repo.public_conversation_by_token(token)
    if row is None:
        raise HTTPException(status_code=404, detail="shared conversation not found")
    creator = str(row.get("created_by") or "")
    if not creator:
        raise HTTPException(status_code=404, detail="shared conversation not found")
    if not repo.user_can_read_notebook(str(row.get("notebook_id") or ""), creator):
        raise HTTPException(status_code=404, detail="shared conversation not found")
    return row


@public_router.get(
    "/public/conversations/{token}", response_model=PublicConversation
)
def public_conversation_route(token: str) -> PublicConversation:
    """The one conversation read that needs no session — the token is the whole
    grant (T3). Mirrors ``report_routes.public_report_route``.

    Deliberately has NO ``Depends(get_current_user)``: this is the anonymous
    surface. That also means no request user is bound, so nothing here may call
    an owner-scoped repository method — ``current_user`` falls back to the seeded
    admin when the ContextVar is unset, which would silently run as an
    administrator. ``public_conversation_by_token`` takes the token alone for
    exactly that reason, and the payload is an explicit allowlist rather than the
    stored row.
    """
    repo = repository()
    row = _public_conversation_or_404(repo, token)
    # Pop the GATE fields before projection: ``notebook_id``/``created_by`` are
    # for the live re-check ONLY and must never cross to an anonymous reader
    # (store docstring). Defense-in-depth — the allowlist ignores them anyway.
    row.pop("notebook_id", None)
    row.pop("created_by", None)
    # The raw share token derives each image's opaque alias (T4); the deployment
    # image switch is passed as a bool so the projection stays a pure function.
    return PublicConversation(
        **public_conversation_payload(
            row,
            share_token=token,
            images_enabled=get_settings().mineru_return_images,
        )
    )


@public_router.get("/public/conversations/{token}/assets/{alias}")
def public_conversation_asset_route(token: str, alias: str) -> FileResponse:
    """Anonymous image bytes for a shared conversation (T4, design §六).

    The reader gets an opaque, token-derived ``alias`` from the page projection,
    never a raw ``asset_id``; this reverses it against the snapshot's referenced
    assets only. Every failure is the SAME 404 as an unknown token — "not found"
    and "not referenced in this share" are indistinguishable on purpose.

    Same anonymous-router discipline and the SAME live creator re-check as the
    page endpoint (shared ``_public_conversation_or_404``): the link's lifetime
    is exactly the creator's read access, so an image must die the instant the
    page does.

    ⚠ Cross-notebook boundary (design §六): a referenced image may live in a
    MOUNTED reference library, so ``asset["notebook_id"]`` can differ from the
    conversation's notebook. The authorization here is deliberately "referenced
    inside the frozen snapshot + creator's live read access to the conversation
    notebook", NOT a fresh participant-scope re-check of the asset's own
    notebook. The frozen snapshot IS the grant — when the answer was generated,
    that library was in the creator's participant set and the evidence (hence the
    image) was legitimately assembled. We do not widen this to re-authorize the
    asset's notebook (there is no request user to authorize) nor narrow it to
    same-notebook assets (that would silently drop legitimate federated images).

    ``Cache-Control: no-store``: revocation must not be architected away by an
    anonymous browser cache — harder here than the cross-library proxy endpoint
    (that one caches only within a mount's lifetime; this is anonymous), so we
    take the least-ambiguous header.
    """
    repo = repository()
    # Image storage off deployment-wide: the projection emits no aliases, so a
    # reader has none to present, but short-circuit anyway (defense in depth) —
    # turning MINERU_RETURN_IMAGES off must stop serving bytes for aliases handed
    # out while it was on.
    if not get_settings().mineru_return_images:
        raise HTTPException(status_code=404, detail="shared image not found")
    row = _public_conversation_or_404(repo, token)
    asset_id = resolve_conversation_asset_alias(row, token, alias)
    if not asset_id:
        raise HTTPException(status_code=404, detail="shared image not found")
    asset = repo.get_notebook_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="shared image not found")
    # Only serve the existing image mime whitelist (reusing the asset service's
    # allowlist / extension map, never a fresh path join). Assets are only ever
    # persisted with one of these mimes, so this is belt-and-suspenders.
    if asset["mime"] not in ALLOWED_MIME_EXTENSIONS:
        raise HTTPException(status_code=404, detail="shared image not found")
    path = AssetService(repo).path_for(asset)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="shared image not found")
    return FileResponse(
        path,
        media_type=asset["mime"],
        headers={"Cache-Control": "no-store"},
    )


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
