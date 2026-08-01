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
from app.services.query_intent import finalize_query_intent, plan_query_intent


router = APIRouter()


def _require_ask_available(notebook: NotebookSummary) -> None:
    """硬约束(PR#334):笔记本在任一模式下都取不到可检索证据(NotebookSummary
    .ask_available=False——无可见来源/knowhow chunk/可用 KG/带图参考库/confirmed memory)
    时,回答只会是凭空生成,拒绝提问。前端会据同一信号禁用对话框,但那只是 UX;这里是
    路由层**权威预检**——挡住前端快照陈旧(跨标签/并发)、直连 HTTP 等一切旁路。

    刻意留在路由层、不下沉到 ask 服务:后者是冻结的 facade 契约,且有约 16 个直调
    repo.ask(空库)的测试。代价是一个**已知且被接受的极窄 TOCTOU 残留**(codex 第11轮
    P2):预检通过后、检索真正取证据前的毫秒级窗口里删掉最后一份证据,本次 ask 仍会跑完、
    可能存下一条空答案。窗口极小、后果良性(仅一条无据答案,非崩溃/越权),故不为它把
    守卫下沉进服务层。"""
    if not notebook.ask_available:
        raise user_error(
            409,
            "该笔记本还没有可用于回答的内容，请先添加来源，"
            "或在「设置 → 编辑当前笔记本」里挂载一个参考库。",
        )


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
        _require_ask_available(notebook)
        history = _intent_history(
            repo, notebook_id, payload.conversation_id, user.id
        )
        try:
            return repo.preview_reasoning_intent(
                notebook_id, question, history, cancel_event=cancel_event
            )
        except ValueError as exc:
            raise user_error(
                422, "无法确认指定的来源，请核对标题或文件名后重试"
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
    _require_ask_available(notebook)
    try:
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
):
    # Task 23: 执行编排(begin→register→started→合成 start→copy_context worker→
    # trace 持久化 fail-open→finish→unregister→空会话清理→终态事件→哨兵)整体在
    # runtime-owned AskExecutionCoordinator;本函数保留冻结签名,只剩启动编排、
    # 交付队列消费与断连轮询。Task 24: 执行体 = runtime-owned AskService(三模式
    # 注册表派发在服务内),不再是 facade runner 回调。
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
    _require_ask_available(notebook)  # 硬约束:空库 ask 权威拒绝(复用上面已拉的快照,零额外查询)
    return StreamingResponse(
        _stream_ask_events(repo, notebook_id, payload, spec, request),
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
