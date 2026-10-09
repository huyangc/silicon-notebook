"""The one synchronous MCP ``ask`` and its resumable reader ``get_ask``.

``ask`` routes one question to a single notebook (the notebook Ask engine,
``askjob-`` jobs) or to 2-8 notebooks (global Ask, ``gask-`` jobs), runs it to
its terminal state inside the call, and returns the first page of one answer
shape for both. ``get_ask`` re-reads any page later.

Reasoning questions are first understood without reading any source, exactly
as the web UI does. A blocking ambiguity returns ``needs_clarification`` with
an ``intent_token``; the understood contract is stored server-side for an hour
(``ask_intent_handles``) and the Agent answers it by calling ``ask`` again with
the same question and ``intent={"intent_token", "answers", ...}``. Nothing
durable (no conversation, no job) exists before that answer.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field, ValidationError

from app.api.deps import ask_intent_handle_repository, notebook_access_repository
from app.domain.agent_tools import (
    AgentAccessDenied, capability_tier, scope_missing_message,
)
from app.domain.ask_engine import AskPluginEngineError
from app.domain.cancellation import AskExecutorGone, AskFollowFailed, AskWaitAbandoned
from app.models.ask import (
    ASK_OUTPUTS,
    ASK_QUESTION_MAX_CHARS,
    ASK_UNDERSTANDING_MS_MAX,
    EVIDENCE_OUTPUT_MODE_REFUSAL,
    AskEvidence,
    AskIntentConfirmation,
    AskRequest,
    AskResponse,
    QueryIntentAnswer,
    QueryIntentContract,
    stored_ask_question,
)
from app.models.global_ask import GlobalAskIntentPreviewRequest, GlobalAskRequest
from app.repositories.ports import AskRequestKeyConflict, AskRequestKeyReused
from app.services.ask_modes import UnknownAskMode
from app.services.query_intent import (
    conversation_intent_history,
    validate_confirmed_intent,
)
from app.services.source_scope import memory_access_context

from . import global_ask as _global
from ._shared import (
    RESULT_LIMIT,
    AgentToolError,
    _budget_response,
    _live_principal,
    _owner_request_context,
    _record_agent_call,
    _resolve_notebook_id,
    _run_with_progress,
    _selected_notebook,
)
from ._messages import chinese_value_error
from .ask_pages import answer_page
from .evidence_page import evidence_page
from .memory_context import _memory_read_allowed
from .refs import element_ref, memory_ref


logger = logging.getLogger(__name__)

# Mirrors AskIntentPreviewRequest.conversation_id.
CONVERSATION_ID_MAX_LENGTH = 200
GLOBAL_NOTEBOOKS_MAX = 8
INTENT_HANDLE_TTL_SECONDS = 3_600

# The gate and the advertised legal-mode list must never drift apart, so both
# read this one constant. The retired ``fast``/``global``/``graph`` aliases are
# deliberately absent: this Agent face offers only the two supported built-ins
# plus live plugin engines, unlike the browser registry.
_BUILTIN_ASK_MODES = ("chunk", "reasoning")
_ENGINE_UNAVAILABLE_TEXT = (
    "对应的扩展引擎暂不可用（部署未配置或未就绪），"
    "请改用 chunk/reasoning 或联系部署管理员"
)


def global_ask_service() -> Any:
    """Late-bound (discovery runs before the runtime exists)."""
    return _global.global_ask_service()


# ------------------------------------------------------------------ inputs


class _IntentReply(BaseModel):
    """The Agent's answer to a clarification: the handle plus the answers."""
    intent_token: str = Field(min_length=1, max_length=64)
    answers: list[QueryIntentAnswer] = Field(default_factory=list, max_length=8)
    # Optional: the wording the user settled on. Empty keeps the understood
    # wording, exactly as an unedited browser review does.
    resolved_question: str = Field(default="", max_length=ASK_QUESTION_MAX_CHARS)


def _validation_fields(exc: ValidationError) -> str:
    """Name the offending fields of a pydantic error in one readable line."""
    return "; ".join(
        f"{'.'.join(str(part) for part in err.get('loc', ())) or 'intent'}"
        for err in exc.errors()[:5]
    )


def _plugin_mode(mode: str) -> bool:
    """Whether ``mode`` names a registered, live plugin engine (else raises
    for anything that is neither a built-in nor such an engine).

    Deliberately finer than the HTTP entry point: an Agent needs "registered
    but unavailable" (``unavailable``) told apart from "unknown"
    (``invalid_argument``). The ``app.bootstrap`` import is lazy so this
    package never depends on the extension composition root at import time;
    a runtime that cannot be resolved reads as "no plugin registered".
    """
    if mode in _BUILTIN_ASK_MODES:
        return False
    try:
        from app.bootstrap import application_extension_runtime
        host = application_extension_runtime().ask_engines
    except Exception as exc:
        # Silent fallback would make "plugins installed but runtime broken"
        # byte-identical to "no plugins installed"; log the class name only.
        logger.warning(
            "ask mode validation could not resolve the extension runtime (%s)",
            type(exc).__name__,
        )
        host = None
    if host is not None and host.mode(mode) is not None:
        if host.is_available(mode):
            return True
        raise AgentToolError("unavailable", f"mode '{mode}' {_ENGINE_UNAVAILABLE_TEXT}")
    legal = [*_BUILTIN_ASK_MODES]
    if host is not None:
        legal.extend(sorted(
            item.descriptor.mode_id for item in host.registrations()
            if host.is_available(item.descriptor.mode_id)
        ))
    raise AgentToolError("invalid_argument", f"mode 只能是以下之一：{', '.join(legal)}")


def _validate_inputs(
    question: str, conversation_id: str, mode: str,
    intent: Mapping[str, Any] | None, notebooks: list[str] | None,
) -> tuple[_IntentReply | None, list[str] | None]:
    """Every input rail that must fail BEFORE any repository work.

    * ``question`` length -- the same rail the HTTP entry points enforce,
      checked here so the Agent reads what to do instead of a model dump
      (the shared conversation page serves a question verbatim, so every
      write side bounds it);
    * ``conversation_id`` length; ``reasoning`` needs a non-blank question;
    * ``notebooks``: unique non-empty ids, at most 8 (more is refused, never
      truncated); an empty list means "omitted";
    * ``intent`` -- only its SHAPE here; resolving the handle needs the
      principal. Only ``reasoning`` accepts it: chunk and plugin modes have
      no understanding step to answer.
    """
    if len(question) > ASK_QUESTION_MAX_CHARS:
        raise AgentToolError(
            "invalid_argument",
            f"question 过长：{len(question)} 个字符，上限是 "
            f"{ASK_QUESTION_MAX_CHARS}。请缩短问题，或把长材料作为来源加入笔记本后再提问。",
        )
    if len(conversation_id) > CONVERSATION_ID_MAX_LENGTH:
        raise AgentToolError("invalid_argument", "conversation_id 过长")
    if not question.strip():
        raise AgentToolError("invalid_argument", "question 不能为空")
    clean: list[str] | None = None
    if notebooks:
        clean = list(dict.fromkeys(str(item).strip() for item in notebooks))
        if any(not item for item in clean):
            raise AgentToolError("invalid_argument", "notebooks 里不能有空的笔记本 id")
        if len(clean) > GLOBAL_NOTEBOOKS_MAX:
            raise AgentToolError(
                "invalid_argument",
                f"一次最多问 {GLOBAL_NOTEBOOKS_MAX} 个笔记本，请先用 list_notebooks "
                "选出要问的笔记本",
            )
    if intent is None:
        return None, clean
    if mode != "reasoning":
        raise AgentToolError(
            "invalid_argument",
            'intent 只在 mode="reasoning" 下有效：chunk 与插件模式没有问题理解步骤，'
            "不要传 intent",
        )
    try:
        return _IntentReply.model_validate(intent), clean
    except ValidationError as exc:
        raise AgentToolError(
            "invalid_argument",
            "intent 格式不合法，需要 {intent_token, answers, resolved_question?}，"
            "出错的字段：" + _validation_fields(exc),
        ) from None


_EVIDENCE_SINGLE_NOTEBOOK = (
    'output="evidence" 只支持单个笔记本的提问：请只传一个笔记本（或不传 notebooks，'
    "用默认笔记本），也不要接着全局会话（gconv-）问"
)
_EVIDENCE_NOT_STORED = (
    '这是仅检索（output="evidence"）的提问：它不保存回答或证据，'
    "get_ask 没有内容可读；需要时请重新提问"
)
_EVIDENCE_NO_KEY = (
    'output="evidence" 不接受 client_request_id：仅检索的结果不保存，'
    "无法接回或重读，失败了直接再问一次即可"
)


def _validate_output(
    output: str, plugin: bool, notebooks: list[str] | None,
    conversation_id: str, client_request_id: str,
) -> None:
    """``output``'s rails, before any repository work. ``evidence`` (retrieval
    only, no synthesis) exists for the single-notebook path and the two
    built-in modes; it stores no answer, so there is nothing a key could
    re-attach to or ``get_ask`` could page."""
    if output not in ASK_OUTPUTS:
        raise AgentToolError("invalid_argument", "output 只能是 answer 或 evidence")
    if output != "evidence":
        return
    if plugin:
        raise AgentToolError("invalid_argument", EVIDENCE_OUTPUT_MODE_REFUSAL)
    if (notebooks and len(notebooks) > 1) or conversation_id.startswith("gconv-"):
        raise AgentToolError("invalid_argument", _EVIDENCE_SINGLE_NOTEBOOK)
    if client_request_id:
        raise AgentToolError("invalid_argument", _EVIDENCE_NO_KEY)


# --------------------------------------------------------------- routing


def _route(
    repo: Any, principal: Any, notebooks: list[str] | None, conversation_id: str,
) -> tuple[str, Any, Any]:
    """``("notebook", notebook_id, conversation | None)`` or
    ``("global", notebook_ids | None, None)``. A ``conv-`` conversation is read
    once here and handed on (its turns feed the understanding step).

    1. ``gconv-`` continues a global conversation (``notebooks`` optional; the
       conversation's scope is inherited when omitted).
    2. ``conv-`` continues a notebook conversation in ITS notebook; a
       conversation that is not the token owner's, sits outside the
       allowlist, or contradicts an explicit ``notebooks`` is ``not_found``
       (never a silently new conversation).
    3. Otherwise: omitted -> the token's default notebook; one -> that
       notebook; 2-8 -> global.
    """
    if conversation_id.startswith("gconv-"):
        return "global", notebooks, None
    if conversation_id.startswith("conv-"):
        conversation = _conversation(repo, principal, conversation_id)
        if notebooks is not None and notebooks != [conversation.notebook_id]:
            raise _conversation_not_found()
        return "notebook", conversation.notebook_id, conversation
    if conversation_id:
        raise _conversation_not_found()
    if notebooks is None:
        return "notebook", _resolve_notebook_id(principal, ""), None
    if len(notebooks) == 1:
        return "notebook", notebooks[0], None
    return "global", notebooks, None


def _conversation_not_found() -> AgentToolError:
    return AgentToolError(
        "not_found",
        "没有找到这个会话（不存在、不属于此凭证的主人，或它所在的笔记本不在白名单里）",
    )


def _conversation(repo: Any, principal: Any, conversation_id: str) -> Any:
    """A ``conv-`` conversation this token may continue (read once). Ownership
    is read from the access repository, the port ``ask_routes`` uses, before
    anything about the conversation is loaded."""
    if notebook_access_repository().conversation_owner(conversation_id) != principal.owner_id:
        raise _conversation_not_found()
    try:
        with _owner_request_context(principal):
            conversation = repo.get_conversation(conversation_id)
    except KeyError:
        raise _conversation_not_found() from None
    if conversation.notebook_id not in principal.notebook_ids:
        raise _conversation_not_found()
    return conversation


# --------------------------------------------------------- clarification


def _question_sha(question: str) -> str:
    return hashlib.sha256(question.strip().encode("utf-8")).hexdigest()


def _global_scope_key(notebooks: list[str] | None, conversation_id: str) -> str:
    ids = ",".join(sorted(notebooks)) if notebooks else "inherit"
    return f"global:{ids}|{conversation_id}"


def _notebook_scope_key(notebook_id: str, conversation_id: str) -> str:
    return f"notebook:{notebook_id}|{conversation_id}"


# Concurrent callers allowed to wait on one (owner, key/job) at a time: the
# original call plus one retry. A third concurrent wait is ``busy`` -- every
# waiter holds an anyio worker thread.
_WAITERS_PER_KEY = 2
_WAITERS: dict[tuple[str, str], int] = {}
_WAITERS_LOCK = threading.Lock()


@contextmanager
def _waiter_slot(owner_id: str, key: str) -> Iterator[None]:
    slot = (owner_id, key)
    with _WAITERS_LOCK:
        if _WAITERS.get(slot, 0) >= _WAITERS_PER_KEY:
            raise AgentToolError(
                "busy", "这个问答已有调用在等待结果；请稍后用 get_ask 读取，不要并发重试"
            )
        _WAITERS[slot] = _WAITERS.get(slot, 0) + 1
    try:
        yield
    finally:
        with _WAITERS_LOCK:
            remaining = _WAITERS.get(slot, 1) - 1
            if remaining:
                _WAITERS[slot] = remaining
            else:
                _WAITERS.pop(slot, None)


_KEY_REUSED = "这个 client_request_id 已用于另一个问题，请换一个新的 client_request_id"
_JOB_NOT_FOUND = "没有找到这个问答任务"
_EXECUTOR_GONE = (
    "这次回答在别处仍显示为进行中，但已长时间没有执行者；请稍后用 get_ask 查看或重新提问"
)
_FOLLOW_FAILED = "暂时无法跟踪这次回答的进度，回答仍在进行；请稍后用 get_ask 查看结果"


def _memory_replay_refused() -> AgentToolError:
    """A job that ran with the private-Memory channel open, replayed by a
    token whose channel is closed now (no ``memory:read``): its stored answer
    and trace may hold Memory that stripping citations does not remove. The
    job is the caller's own, so the honest reason -- the missing tier -- is
    named rather than hidden behind ``not_found``."""
    return AgentToolError(
        "scope_missing",
        "这个回答运行时可以读取私人记忆，"
        + scope_missing_message(capability_tier("memory:read")),
    )


def _issue_handle(
    principal: Any, scope_key: str, question: str, contract: QueryIntentContract,
    understanding_ms: int,
) -> str:
    token = secrets.token_urlsafe(24)  # 192 random bits
    ask_intent_handle_repository().put_intent_handle(
        token=token, owner_id=principal.owner_id, scope_key=scope_key,
        question_sha256=_question_sha(question),
        contract=contract.model_dump(mode="json"),
        understanding_ms=understanding_ms, ttl_seconds=INTENT_HANDLE_TTL_SECONDS,
    )
    return token


def _confirm(
    reply: _IntentReply, principal: Any, scope_key: str, question: str,
) -> AskIntentConfirmation:
    """Resolve the handle, then freeze the contract with the rail HTTP runs.

    The result is the very ``AskIntentConfirmation`` the browser submits
    after its review, with the understanding wall clock carried over from the
    first call. The handle is not consumed: it stays valid for its hour, so an
    ask that fails downstream can be retried with the same answers.
    """
    row = ask_intent_handle_repository().get_intent_handle(
        reply.intent_token, owner_id=principal.owner_id
    )
    if row is None or row["scope_key"] != scope_key:
        raise AgentToolError(
            "invalid_argument",
            "intent_token 无效或已过期：澄清句柄一小时内有效，且只对签发它的同一个问题、"
            "同一组笔记本/会话有效；请用相同的 question、notebooks 和 conversation_id "
            "重试，或不带 intent 重新提问以获得新的澄清",
        )
    contract = QueryIntentContract.model_validate(row["contract"])
    resolved_question = reply.resolved_question.strip()
    try:
        validate_confirmed_intent(
            question,
            contract.model_dump(),
            resolved_question=resolved_question,
            answers=[item.model_dump() for item in reply.answers],
        )
    except ValueError as exc:
        # Its three messages are approved Chinese copy (unanswered required
        # rows, a contract that does not match the question, an empty
        # wording): the Agent's to fix, on both paths.
        raise AgentToolError(
            "invalid_argument", chinese_value_error(str(exc)) or "澄清回答不完整或与问题不匹配"
        ) from None
    if row["question_sha256"] != _question_sha(question):
        raise AgentToolError(
            "invalid_argument", "问题理解与当前问题不匹配，请用签发澄清时的同一个 question 重试"
        )
    return AskIntentConfirmation(
        contract=contract,
        resolved_question=resolved_question or contract.resolved_question,
        answers=reply.answers,
        understanding_ms=int(row["understanding_ms"]),
    )


def _understood(contract_of: Callable[[], QueryIntentContract]) -> tuple[QueryIntentContract, int]:
    started = time.monotonic()
    contract = contract_of()
    # Clamped, not rejected: this surface never abandons a call it is still
    # executing, and an understanding that took longer than the trace field
    # admits must not throw the run away.
    return contract, min(int((time.monotonic() - started) * 1000), ASK_UNDERSTANDING_MS_MAX)


def _auto_confirm(contract: QueryIntentContract, understanding_ms: int) -> AskIntentConfirmation:
    """A clear contract confirms itself, exactly as the browser does."""
    return AskIntentConfirmation(
        contract=contract, resolved_question=contract.resolved_question,
        answers=[], understanding_ms=understanding_ms,
    )


_CLARIFICATION_NEXT_STEP = (
    "尚未检索，也未创建会话或任务。把 intent.ambiguities 里 required 为 true 的每一项"
    "转述给用户（options 只是候选），然后用同一个 question、同样的 notebooks、"
    "conversation_id 与 output 再调 ask，传 intent="
    '{"intent_token": <本响应的 intent_token>, "answers": [{"id", "answer"}], '
    '"resolved_question": <可选>}。句柄一小时内有效。'
)

# Display caps for the review view. Everything in it is display-only (the
# contract stays server-side), so these sit far below the contract's own field
# ceilings: the view has to fit the output budget by construction, never by the
# convergence loop dropping a row the server will then demand an answer for.
_REVIEW_QUESTION_CHARS = 300
_REVIEW_FIELD_LIMITS = {
    "resolved_question": _REVIEW_QUESTION_CHARS,
    "question": _REVIEW_QUESTION_CHARS,
    "reason": 150, "options": 100, "entities": 100, "comparison_axes": 100,
    "constraints": 100, "excluded_topics": 100, "assumptions": 100,
}
_REVIEW_LIST_KEYS = (
    "entities", "comparison_axes", "constraints", "excluded_topics", "assumptions",
)
_REVIEW_LIST_ITEMS = 5
# Richest first. Each tier drops one class of extras whole: the descriptive
# lists, then per-row reasons, then per-row options. Ambiguity rows themselves
# (id, question, required) are never dropped by any tier.
_REVIEW_TIERS = (
    (True, True, True), (False, True, True), (False, False, True),
    (False, False, False),
)


def _review_view(
    contract: QueryIntentContract, *, lists: bool, reasons: bool, options: bool
) -> tuple[dict[str, Any], int]:
    """One tier of the review view, and how many extras it left out."""
    omitted = 0
    rows: list[dict[str, Any]] = []
    for row in contract.ambiguities:
        item: dict[str, Any] = {
            "id": row.id, "question": row.question, "required": row.required,
        }
        if row.options:
            if options:
                item["options"] = list(row.options[:4])
            else:
                omitted += len(row.options)
        if row.reason:
            if reasons:
                item["reason"] = row.reason
            else:
                omitted += 1
        rows.append(item)
    view: dict[str, Any] = {
        "resolved_question": contract.resolved_question,
        "intent_type": contract.intent_type,
        "result_scope": contract.result_scope,
        "confidence": contract.confidence,
        "ambiguities": rows,
    }
    for key in _REVIEW_LIST_KEYS:
        values = list(getattr(contract, key))
        if not values:
            continue
        if lists:
            view[key] = values[:_REVIEW_LIST_ITEMS]
            omitted += max(0, len(values) - _REVIEW_LIST_ITEMS)
        else:
            omitted += len(values)
    return view, omitted


def _review_delivered_whole(
    payload: Mapping[str, Any], contract: QueryIntentContract, token: str
) -> bool:
    """Every ambiguity row, the handle and the instructions survived the budget."""
    intent = payload.get("intent")
    rows = intent.get("ambiguities") if isinstance(intent, dict) else None
    expected = contract.ambiguities
    if not isinstance(rows, list) or len(rows) != len(expected):
        return False
    for row, want in zip(rows, expected):
        if not isinstance(row, dict):
            return False
        if row.get("id") != want.id or row.get("required") != want.required:
            return False
        shown = row.get("question")
        if not isinstance(shown, str) or len(shown) < min(
            len(want.question), _REVIEW_QUESTION_CHARS
        ):
            return False
    return (
        payload.get("intent_token") == token
        and payload.get("next_step") == _CLARIFICATION_NEXT_STEP
    )


def _clarification_payload(
    token: str, contract: QueryIntentContract, understanding_ms: int,
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    """The structured pause, as the Agent sees it.

    Invariant: every ambiguity row the server will later demand an answer for
    reaches the Agent, with its id, its required flag and (at least the
    display cap of) its question. The view is built richest-first and each
    poorer tier drops one class of extras whole; a tier is accepted only
    after the budget pass demonstrably left the rows, the handle and the
    instructions intact. Should even the poorest tier stop fitting, the call
    fails loudly rather than shipping a row set the Agent cannot complete.
    """
    for lists, reasons, options in _REVIEW_TIERS:
        view, omitted = _review_view(
            contract, lists=lists, reasons=reasons, options=options
        )
        payload = _budget_response({
            "status": "needs_clarification",
            "mode": "reasoning",
            "scope": dict(scope),
            "intent_token": token,
            "intent": view,
            "understanding_ms": understanding_ms,
            "next_step": _CLARIFICATION_NEXT_STEP,
        }, initial_omitted_items=omitted, field_limits=_REVIEW_FIELD_LIMITS)
        if _review_delivered_whole(payload, contract, token):
            return payload
    raise AgentToolError(
        "internal",
        "澄清问题超出 MCP 响应预算，无法完整投递给调用方"
        "（reason: clarification_over_budget）",
    )


# ------------------------------------------------------- notebook answers


def _strip_memory_items(answer: Any, allow_memory: bool) -> tuple[list, list]:
    """``(anchors, citations)`` of ``answer`` that this token may receive.

    Without ``memory:read`` every Memory-backed item goes: a citation with a
    ``memory_id`` and an anchor whose ``object_type`` is ``'memory'``.

    NOT an enforcement point. Enforcement is the closed Memory channel
    (``memory_access_context``) and the retrieval ceiling; this keeps the two
    structured lists consistent with the token and never touches the answer
    body. Filtered BEFORE any paging, so the hidden Memory count cannot leak
    out through the totals and cursors.
    """
    anchors = list(getattr(answer, "anchors", None) or [])
    citations = list(getattr(answer, "citations", None) or [])
    if allow_memory:
        return anchors, citations
    return (
        [item for item in anchors if item.object_type != "memory"],
        [item for item in citations if not item.memory_id],
    )


def _anchor_row(anchor: Any) -> dict[str, Any]:
    row = {
        "key": anchor.key,
        "object_id": anchor.object_id,
        "object_type": anchor.object_type,
        "label": anchor.label,
        "source_title": anchor.source_title,
        "location_label": anchor.location_label,
        "source_id": anchor.source_id,
        "element_id": anchor.element_id,
        "tier": anchor.tier,
        "provenance": anchor.provenance,
    }
    # No ``ref`` here on purpose: anchors share a 3,500-character sub-budget
    # with their provenance, and every anchor that points at an element is
    # also a citation, which carries the ref. External evidence (a reflect
    # plugin action's out-of-library material) is the only anchor carrying a
    # URL, and that URL is its ONLY handle. Omitted when empty.
    if anchor.url:
        row["url"] = anchor.url
    if anchor.knowhow is not None:
        row["knowhow"] = {
            "table_id": anchor.knowhow.table_id,
            "row_id": anchor.knowhow.row_id,
        }
    return row


def _citation_row(citation: Any, notebook_id: str) -> dict[str, Any]:
    row = {
        "label": citation.label,
        "source_id": citation.source_id,
        "element_id": citation.element_id,
        "location_label": citation.location_label,
        "quoted_span": citation.quoted_span,
        "source_file_name": citation.source_file_name,
        "tier": citation.tier,
        "content_is_untrusted_evidence": True,
    }
    # The ref reads the citation back in the context of the notebook that was
    # asked: a mounted library's element is readable there exactly as it was
    # citable there (``read_reference``'s participant check).
    ref = (
        memory_ref(notebook_id, citation.memory_id) if citation.memory_id
        else element_ref(notebook_id, citation.source_id, citation.element_id)
    )
    if ref:
        row["ref"] = ref
    # Omitted when empty: a citation from the asked notebook itself carries
    # notebook_id="", and a non-Memory citation carries memory_id="".
    if citation.notebook_id:
        row["notebook_id"] = citation.notebook_id
    if citation.memory_id:
        row["memory_id"] = citation.memory_id
    if citation.url:  # 外部引用唯一可解析的句柄
        row["url"] = citation.url
    if citation.knowhow is not None:
        row["knowhow"] = {
            "table_id": citation.knowhow.table_id,
            "row_id": citation.knowhow.row_id,
        }
    return row


_NOTEBOOK_STATUS = {"done": "answered", "running": "running", "failed": "failed",
                    "cancelled": "cancelled", "interrupted": "failed"}


def notebook_view(
    job_id: str, notebook_id: str, response: AskResponse | None,
    detail: Mapping[str, Any] | None, allow_memory: bool,
) -> dict[str, Any]:
    """One notebook Ask job in the shape ``answer_page`` pages.

    ``response`` is the answer (from the run itself, or the stored answer
    payload); ``detail`` the job row (status, trace, conversation). A failed
    job's stored error is an internal exception text and is never echoed.
    """
    anchors, citations = _strip_memory_items(response, allow_memory) if response else ([], [])
    detail = detail or {}
    status = "answered" if response is not None else _NOTEBOOK_STATUS.get(
        str(detail.get("status") or "failed"), "failed"
    )
    if status == "answered" and response is None:
        # The job row says done but its answer row is missing: never an
        # empty "answered" page.
        status = "failed"
    trace = detail.get("trace")
    if trace is None:
        trace = getattr(response, "reasoning_trace", None) or []
    skipped = [
        {"notebook_id": item.notebook_id, "name": item.name}
        for item in (getattr(response, "skipped_libraries", None) or ())
    ]
    answer_text = (
        getattr(response, "answer", "") or getattr(response, "conclusion", "") or ""
    ) if response is not None else ""
    return {
        "job_id": job_id,
        "conversation_id": str(
            getattr(response, "conversation_id", "") or detail.get("conversation_id") or ""
        ),
        "status": status,
        "mode": str(getattr(response, "mode", "") or detail.get("mode") or ""),
        "answer_id": str(getattr(response, "answer_id", "") or ""),
        "answer": answer_text,
        "citations": [_citation_row(item, notebook_id) for item in citations],
        "grounded": bool(getattr(response, "grounded", False)),
        "coverage_counts": {
            "resolved": 1, "searched": 1 if status == "answered" else 0,
        },
        "skipped": skipped,
        "degraded": [],
        "citation_check": {},
        "trace": list(trace),
        "answer_object": response,
        "anchors": [_anchor_row(item) for item in anchors[:RESULT_LIMIT]],
        # Counted from the FILTERED list: the unfiltered count is how a hidden
        # Memory anchor would leak out by arithmetic.
        "anchors_omitted": max(0, len(anchors) - RESULT_LIMIT),
        "error": "" if status in ("answered", "running") else (
            _global.FAILED_COPY if status == "failed" else "这次回答已被取消"
        ),
        "completeness_notice": getattr(response, "completeness_notice", "") or "",
        "scope": {"kind": "notebook", "notebook_ids": [notebook_id]},
    }


# -------------------------------------------------------------- the runs


def _notebook_history(conversation: Any) -> str:
    """The history block ``/ask/intent`` builds for the understanding step,
    from the conversation ``_route`` already read and authorized."""
    return conversation_intent_history(conversation.turns) if conversation else ""


def _plugin_failure(exc: AskPluginEngineError) -> AgentToolError:
    if exc.code == "plugin_engine_unavailable":
        return AgentToolError("unavailable", f"{_ENGINE_UNAVAILABLE_TEXT}（reason: {exc.code}）")
    message = (
        "扩展引擎返回了无法核验的引用"
        if exc.code == "plugin_engine_unverified_citation"
        else "扩展引擎暂时无法完成回答，请重试"
    )
    return AgentToolError("unavailable", f"{message}（reason: {exc.code}）")


def _run_notebook(
    repo: Any, principal: Any, notebook_id: str, question: str, mode: str,
    conversation: Any, reply: _IntentReply | None, client_request_id: str,
    stop: threading.Event, output: str = "answer",
) -> dict[str, Any]:
    """The single-notebook path's blocking body (``ask_current`` semantics:
    the memory channel, mounted libraries, plugin engines, the
    ``ask_available`` gate, ``submitted_via="mcp"``), returning its page.

    ``memory:read`` (the ``read`` tier) opens or closes the private-Memory
    channel for everything run here, entered IN this worker thread.

    ``output="evidence"`` (retrieval only, built-in modes, never keyed) runs
    the same gates and understanding step, then ``ask_current``'s
    retrieval-only twin, and returns ``evidence_page`` -- the context the
    synthesis would have read, unpaged -- after the same delivery recheck.
    """
    allow_memory = _memory_read_allowed(repo, principal, notebook_id)
    conversation_id = conversation.id if conversation is not None else ""
    scope_key = _notebook_scope_key(notebook_id, conversation_id)
    with _owner_request_context(principal), memory_access_context(allow_memory):
        request = AskRequest(
            question=question, mode=mode,
            conversation_id=conversation_id or None,
            client_request_id=client_request_id or None,
        )
        try:
            # A retried key attaches BEFORE any preflight: the original
            # submission already passed it (the HTTP stream route's order).
            attached = repo.ask_with_job(
                notebook_id, request, submitted_via="mcp", attach_only=True,
                stop=stop,
            ) if client_request_id else None
            if attached is None:
                confirmation = (
                    _confirm(reply, principal, scope_key, question)
                    if reply is not None else None
                )
                # 硬约束(PR#334):空库一律拒绝,与 /ask、/ask/stream 同一权威闸门;
                # 排在理解步骤之前:空库不该先花一次理解模型调用再被拒。
                if not repo.get_notebook(notebook_id).ask_available:
                    raise AgentToolError(
                        "unavailable",
                        "该笔记本还没有可用于回答的内容，请先添加来源，"
                        "或在「设置 → 编辑当前笔记本」里挂载一个参考库。",
                    )
                if mode == "reasoning" and confirmation is None:
                    contract, understanding_ms = _understood(
                        lambda: repo.preview_reasoning_intent(
                            notebook_id, question.strip(),
                            _notebook_history(conversation),
                        )
                    )
                    if contract.needs_clarification:
                        token = _issue_handle(
                            principal, scope_key, question, contract, understanding_ms
                        )
                        return _clarification_payload(
                            token, contract, understanding_ms,
                            {"kind": "notebook", "notebook_ids": [notebook_id]},
                        )
                    confirmation = _auto_confirm(contract, understanding_ms)
                request.intent = confirmation
                attached = repo.ask_with_job(
                    notebook_id, request, submitted_via="mcp", stop=stop,
                    output=output,
                )
        except UnknownAskMode:
            # Availability flipped between the mode check and dispatch.
            raise AgentToolError("unavailable", f"mode '{mode}' {_ENGINE_UNAVAILABLE_TEXT}") from None
        except AskPluginEngineError as exc:
            raise _plugin_failure(exc) from None
        except AskRequestKeyConflict:
            raise AgentToolError(
                "invalid_argument", "这个 client_request_id 已在另一个笔记本用过，请换一个"
            ) from None
        except AskRequestKeyReused as exc:
            if exc.reason == "surface":
                raise AgentToolError("not_found", _JOB_NOT_FOUND) from None
            if exc.reason == "memory":
                raise _memory_replay_refused() from None
            raise AgentToolError("invalid_argument", _KEY_REUSED) from None
        except AskExecutorGone:
            raise AgentToolError("unavailable", _EXECUTOR_GONE) from None
        response, job_id = attached
    # The run (or the wait) may have taken minutes: the answer is delivered
    # under the token as it is NOW, by get_ask's own rules.
    live, allow_now = _recheck_notebook_delivery(
        repo, principal, notebook_id, job_id, ran_with_memory=allow_memory,
    )
    if isinstance(response, AskEvidence):
        _record_agent_call(repo, live, notebook_id, "ask:execute")
        return evidence_page(notebook_id, response, allow_now, _anchor_row)
    with _owner_request_context(live):
        # The job row is the one source of the trace (the same one get_ask
        # reads), so the first page's trace cursor matches every later one.
        try:
            detail = repo.ask_job_detail(job_id)
        except KeyError:
            detail = None
    _record_agent_call(repo, live, notebook_id, "ask:execute")
    return answer_page(notebook_view(job_id, notebook_id, response, detail, allow_now))


def _refreshed_principal(repo: Any, principal: Any) -> Any:
    """The calling token as it is now: ``token_inactive`` once it was revoked
    or expired (or rebound to another owner) since the call began."""
    try:
        live = repo.refresh_agent_principal(principal.token_id)
    except PermissionError:
        live = None
    if live is None or live.owner_id != principal.owner_id:
        raise AgentToolError("token_inactive", _global._INACTIVE)
    return live


def _notebook_delivery_gate(
    repo: Any, principal: Any, notebook_id: str, ran_with_memory: bool,
) -> bool:
    """get_ask's checks for handing out one notebook job's stored content:
    the ``ask`` tier and the notebook still allowlisted and readable (a
    missing tier or a dead token says so; anything else is ``not_found``),
    and a run that had the Memory channel open only to a token that may read
    Memory now. Returns whether Memory items may reach the page."""
    try:
        repo.require_agent_access(principal, "ask:execute", notebook_id)
    except AgentAccessDenied as exc:
        if exc.reason in ("inactive", "scope_missing"):
            raise
        raise AgentToolError("not_found", _JOB_NOT_FOUND) from None
    allow_memory = _memory_read_allowed(repo, principal, notebook_id)
    if ran_with_memory and not allow_memory:
        raise _memory_replay_refused()
    return allow_memory


def _recheck_notebook_delivery(
    repo: Any, principal: Any, notebook_id: str, job_id: str, *,
    ran_with_memory: bool,
) -> tuple[Any, bool]:
    """Re-authorize a synchronous ``ask`` at the moment it hands back its
    answer -- executing or attached alike: the token refreshed, then
    ``_notebook_delivery_gate``. Whether the run had Memory open comes from
    the job row (``memory_access``) when there is one, else from the run's
    own channel. The answer stays saved either way; a refused caller just
    does not receive it here (get_ask applies the same rule later)."""
    live = _refreshed_principal(repo, principal)
    origin = repo.ask_job_origin(job_id)
    if origin is not None:
        ran_with_memory = bool(origin.get("memory_access", True))
    return live, _notebook_delivery_gate(repo, live, notebook_id, ran_with_memory)


def _run_global(
    repo: Any, principal: Any, notebooks: list[str] | None, question: str,
    mode: str, conversation_id: str, reply: _IntentReply | None,
    client_request_id: str, stop: threading.Event,
) -> dict[str, Any]:
    """The 2-8 notebook path: ``GlobalAskService.start`` then ``wait``, the
    first page of the terminal job. A clear reasoning question is understood
    once and auto-confirmed; an ambiguous one pauses with a stored handle. A
    retry under the same ``client_request_id`` is recognised before the
    understanding pass (it is a model call whose result would be thrown away).

    The question is spelled as the single-notebook store spells it
    (``stored_ask_question``) before it becomes the request, so a retry that
    only differs in surrounding whitespace is the same submission on both MCP
    paths. Only here: the browser's global request keeps its own semantics.
    """
    question = stored_ask_question(question)
    service = global_ask_service()

    def live_authority() -> list[str]:
        current = _global._authorize(repo, token_id=principal.token_id)
        if current.owner_id != principal.owner_id:
            raise AgentToolError("token_inactive", "Agent 凭证已失效，请重新连接")
        return list(current.notebook_ids)

    scope = {"mode": "include", "notebook_ids": notebooks} if notebooks else None
    try:
        payload = GlobalAskRequest(
            question=question, notebook_scope=scope,
            conversation_id=conversation_id or None,
            client_request_id=client_request_id or None, mode=mode,
        )
    except ValidationError:
        raise AgentToolError(
            "invalid_argument", "问题或检索范围格式不正确，请检查问题长度和所选笔记本后重试"
        ) from None
    scope_key = _global_scope_key(notebooks, conversation_id)
    common = {
        "user_id": principal.owner_id,
        "allowed_notebook_ids": principal.notebook_ids,
    }
    with _owner_request_context(principal):
        try:
            replayed = service.replay(
                payload, authority_check=live_authority, **common
            ) if client_request_id else None
            if replayed is not None and replayed.submitted_via != "mcp":
                # A key the browser spent: never attached to from MCP.
                raise AgentToolError("not_found", _JOB_NOT_FOUND)
            if replayed is None and mode == "reasoning":
                if reply is not None:
                    # Outside the broad handler below in effect: _confirm
                    # raises the Agent's own error (re-raised as is).
                    payload.intent = _confirm(reply, principal, scope_key, question)
                else:
                    contract, understanding_ms = _understood(
                        lambda: service.preview_intent(
                            GlobalAskIntentPreviewRequest(
                                question=payload.question,
                                conversation_id=payload.conversation_id,
                                notebook_scope=payload.notebook_scope,
                            ),
                            authority_check=live_authority, **common,
                        )
                    )
                    if contract.needs_clarification:
                        token = _issue_handle(
                            principal, scope_key, question, contract, understanding_ms
                        )
                        return _clarification_payload(
                            token, contract, understanding_ms,
                            {"kind": "global", "notebook_ids": notebooks or []},
                        )
                    payload.intent = _auto_confirm(contract, understanding_ms)
            job = replayed if replayed is not None else global_ask_service().start(
                payload, submitted_via="mcp", authority_check=live_authority, **common
            )
            if job.submitted_via != "mcp":
                # ``start`` itself replays (or recovers an insert race to) the
                # job already under this key -- which a browser request may
                # have created after the probe above. Same rule as get_ask: a
                # browser job is never reached from MCP, before any ledger
                # entry, wait or content. (Same-request identity is ``start``'s
                # own check: a different request under the key is its 409.)
                raise AgentToolError("not_found", _JOB_NOT_FOUND)
            if job.status == "running":
                with _waiter_slot(principal.owner_id, job.job_id):
                    job = service.wait(job.job_id, stop=stop, **common)
            # The run (or the wait) may have taken minutes: deliver under the
            # token as it is NOW, by get_ask's rules -- the ``ask`` tier, and
            # the job re-read against the live allowlist and read access.
            live = _global._authorize(repo, token_id=principal.token_id)
            if live.owner_id != principal.owner_id:
                raise AgentToolError("token_inactive", _global._INACTIVE)
            job = service.get_job(
                job.job_id, user_id=live.owner_id,
                allowed_notebook_ids=live.notebook_ids,
            )
            if job.submitted_via != "mcp":
                raise AgentToolError("not_found", _JOB_NOT_FOUND)
            # Booked only for a delivered answer, after every gate.
            for notebook_id in job.resolved_notebook_ids:
                _record_agent_call(repo, live, notebook_id, "ask:execute")
        except (AgentToolError, AskWaitAbandoned):
            # The Agent's own error, or a wait the cancelled call let go of:
            # neither is a failure of the run (no warning, no internal).
            raise
        except AskExecutorGone:
            # Another process's job made no progress for ATTACH_STALL_SECONDS
            # (the notebook wait's rule); the job is not cancelled.
            raise AgentToolError("unavailable", _EXECUTOR_GONE) from None
        except AskFollowFailed:
            raise AgentToolError("unavailable", _FOLLOW_FAILED) from None
        except Exception as exc:  # noqa: BLE001 - only GlobalAskError copy is shown
            mapped = _global.global_ask_tool_error(exc)
            if mapped is None:
                logger.warning("global ask via MCP failed (%s)", type(exc).__name__)
                mapped = AgentToolError("internal", _global._GLOBAL_FALLBACK)
            raise mapped from None
    return answer_page(_global.global_view(job))


def _read_notebook_job(
    repo: Any, principal: Any, job_id: str,
) -> tuple[str, AskResponse | None, dict[str, Any], bool]:
    """A notebook Ask job this token may read: the checks of
    ``ask_routes``' job detail -- the job's creator is the token owner, and its
    notebook is allowlisted and readable (plus the ``ask`` tier). Anything else
    is ``not_found`` without disclosing which check failed."""
    origin = repo.ask_job_origin(job_id)
    if (
        origin is None
        or origin.get("created_by") != principal.owner_id
        # A job the browser (or any other surface) started is not readable
        # through MCP, even by its own owner.
        or origin.get("submitted_via") != "mcp"
    ):
        raise AgentToolError("not_found", _JOB_NOT_FOUND)
    notebook_id = str(origin.get("notebook_id") or "")
    # A run with the Memory channel open (a row without the fact counts as
    # open) is replayed only to a token that may read Memory now.
    allow_memory = _notebook_delivery_gate(
        repo, principal, notebook_id, bool(origin.get("memory_access", True))
    )
    try:
        detail = repo.ask_job_detail(job_id)
    except KeyError:
        raise AgentToolError("not_found", _JOB_NOT_FOUND) from None
    if detail.get("output") == "evidence":
        # A retrieval-only job stored no answer: nothing to page.
        raise AgentToolError("invalid_argument", _EVIDENCE_NOT_STORED)
    response = None
    if detail.get("status") == "done":
        answer = repo.ask_answer_detail(str(detail.get("answer_id") or ""))
        if answer is not None:
            response = AskResponse.model_validate(answer["payload"])
    return notebook_id, response, detail, allow_memory


def _offsets(*values: int) -> None:
    if any(int(value) < 0 for value in values):
        raise AgentToolError("invalid_argument", "分页位置不能小于零，请从第一页重新读取")


# ------------------------------------------------------------ the tools


def register_ask_tools(server: FastMCP, repository_provider: Callable[[], Any]) -> None:
    @server.tool(
        tier="ask",
        description=(
            "Ask a question and get the answer in this call (synchronous; the "
            "server never gives up on a call it is still executing, so set a "
            "generous client read timeout -- reasoning and plugin engines can run "
            "for minutes). Routing: notebooks omitted = the token's default "
            "notebook; one id = that notebook; 2-8 ids = a cross-notebook "
            "(global) answer; more than 8 is refused. conversation_id continues a "
            "conversation: a conv- id continues that notebook conversation (it must "
            "be yours and in the allowlist, else not_found), a gconv- id a global "
            "one (its notebook scope is inherited when notebooks is omitted). mode: "
            "\"reasoning\" (default) or \"chunk\", or the mode id of a "
            "deployment-installed engine plugin that is live (single notebook "
            "only). In reasoning mode the question is first understood without "
            "reading any source: a clear question continues to the answer; a "
            "blocking ambiguity returns status=\"needs_clarification\" with an "
            "intent_token and every ambiguity (id/question/options/required) and "
            "creates nothing. Relay the questions to the user, then call ask again "
            "with the same question, notebooks and conversation_id and "
            "intent={\"intent_token\", \"answers\": [{\"id\", \"answer\"}], "
            "\"resolved_question\"?} (the token lives one hour). Otherwise the "
            "result is status answered|failed|cancelled with job_id, "
            "conversation_id, answer, citations (each with a `ref` for "
            "read_reference), coverage, trace and, for a single notebook, anchors; "
            "long results continue with get_ask via next_answer_offset / "
            "next_citation_offset / next_coverage_offset / trace.next_offset. "
            "client_request_id makes a retry safe: the same key returns the job "
            "it already started (waiting for it if it is still running) instead "
            "of asking again. output=\"evidence\" (one notebook, reasoning or "
            "chunk, no client_request_id) runs the same retrieval -- reasoning "
            "still understands first and may return needs_clarification -- but "
            "skips the final synthesis and returns, in this one call and unpaged, "
            "the evidence the synthesis would have been given (status="
            "\"retrieved\", items each with a `ref` for read_reference). Its size "
            "follows the synthesis budget, not the 12,000-byte one, so raise the "
            "client's tool-output limit; conversation_id is read for history, "
            "never appended to, and nothing is saved but a retrieval-only job "
            "record (get_ask has nothing to read for it). Requires the ask "
            "permission; private Memory takes part only with the read permission "
            "too."
        ),
    )
    async def ask(
        question: str, ctx: Context, notebooks: list[str] | None = None,
        conversation_id: str = "", mode: str = "reasoning",
        intent: dict[str, Any] | None = None, client_request_id: str = "",
        output: str = "answer",
    ) -> dict[str, Any]:
        reply, clean = _validate_inputs(question, conversation_id, mode, intent, notebooks)
        plugin = _plugin_mode(mode)
        _validate_output(output, plugin, clean, conversation_id, client_request_id)
        if client_request_id and len(client_request_id) > 128:
            raise AgentToolError("invalid_argument", "client_request_id 最多 128 个字符")
        repo = repository_provider()
        # Set when this call is cancelled: a pure WAIT (an attached key, a
        # global job) lets go of its worker thread; an executing ask runs on.
        stop = threading.Event()

        def run() -> dict[str, Any]:
            principal = _live_principal(repo)
            kind, target, conversation = _route(repo, principal, clean, conversation_id)
            if kind == "global" and output == "evidence":
                raise AgentToolError("invalid_argument", _EVIDENCE_SINGLE_NOTEBOOK)
            if kind == "global":
                if plugin:
                    raise AgentToolError(
                        "invalid_argument", "扩展引擎只能用于单个笔记本的问答，请只传一个笔记本"
                    )
                _global._authorize(repo, token_id=principal.token_id)
                return _run_global(
                    repo, principal, target, question, mode, conversation_id,
                    reply, client_request_id, stop,
                )
            try:
                # Booked only when the answer is delivered (after the
                # delivery recheck, ``_run_notebook``), never before a gate.
                principal, notebook_id = _selected_notebook(
                    repo, target, "ask:execute", record=False
                )
            except AgentAccessDenied as exc:
                if conversation_id and exc.reason not in ("inactive", "scope_missing"):
                    raise _conversation_not_found() from None
                raise
            if not client_request_id:
                return _run_notebook(
                    repo, principal, notebook_id, question, mode, conversation,
                    reply, client_request_id, stop, output,
                )
            with _waiter_slot(principal.owner_id, "key:" + client_request_id):
                return _run_notebook(
                    repo, principal, notebook_id, question, mode, conversation,
                    reply, client_request_id, stop,
                )

        try:
            return await _run_with_progress(ctx, run, label="ask", on_cancel=stop.set)
        except AskWaitAbandoned:  # pragma: no cover - only after a cancel
            raise AgentToolError("unavailable", "等待已取消；稍后可用 get_ask 读取结果") from None

    @server.tool(
        tier="ask",
        description=(
            "Re-read a result of ask by its job_id (askjob-... for a single "
            "notebook, gask-... for several): the current status, and any page of "
            "the answer text, citations (each with a `ref`), coverage receipts and "
            "reasoning trace -- follow next_answer_offset, next_citation_offset, "
            "next_coverage_offset and trace.next_offset independently until null. "
            "Use it to continue a long answer or to read a result after a dropped "
            "connection. Each call rechecks the token, the allowlist and read "
            "access. Requires the ask permission."
        ),
    )
    async def get_ask(
        job_id: str, ctx: Context, answer_offset: int = 0, citation_offset: int = 0,
        coverage_offset: int = 0, trace_offset: int = 0,
    ) -> dict[str, Any]:
        _offsets(answer_offset, citation_offset, coverage_offset, trace_offset)
        offsets = (answer_offset, citation_offset, coverage_offset, trace_offset)
        repo = repository_provider()

        def load() -> dict[str, Any]:
            if job_id.startswith("gask-"):
                principal = _global._authorize(repo)
                with _owner_request_context(principal):
                    try:
                        job = _global.global_ask_service().get_job(
                            job_id, user_id=principal.owner_id,
                            allowed_notebook_ids=principal.notebook_ids,
                        )
                    except Exception as exc:  # noqa: BLE001
                        raise _global.global_ask_tool_error(exc) or AgentToolError(
                            "internal", _global._GLOBAL_FALLBACK
                        ) from None
                if job.submitted_via != "mcp":
                    # A browser-started global job is not readable via MCP.
                    raise AgentToolError("not_found", _JOB_NOT_FOUND)
                return answer_page(_global.global_view(job), *offsets)
            if not job_id.startswith("askjob-"):
                raise AgentToolError("not_found", _JOB_NOT_FOUND)
            principal = _live_principal(repo)
            with _owner_request_context(principal):
                notebook_id, response, detail, allow = _read_notebook_job(
                    repo, principal, job_id
                )
            return answer_page(
                notebook_view(job_id, notebook_id, response, detail, allow), *offsets
            )

        return await _run_with_progress(ctx, load, label="get_ask")
