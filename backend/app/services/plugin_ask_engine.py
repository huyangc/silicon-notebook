"""Core-owned per-run ports and citation admission for ``ask.engine``.

SDK facades contain only opaque random tokens. Raw model clients,
repository/service callables, cancellation events and addressable evidence ids
remain in this core module's run-state registry and are released as soon as the
provider returns. This is capability hygiene for trusted in-process deployment
code, not a Python sandbox.
"""
from __future__ import annotations

from contextvars import Context, copy_context
from dataclasses import dataclass, field
from contextlib import contextmanager
import json
from threading import RLock
from typing import Any, Callable, Iterable, Sequence, TypeVar
from uuid import uuid4

from app.domain.ask_engine import AskEnginePortError, EngineEvidence
from app.domain.cancellation import AskCancelled
from app.domain.retrieval import RetrievedElement
from app.models.ask import TraceStep
from app.services.cancellation import raise_if_cancelled
from app.services.citation_markers import LOOSE_MARKER_RE, marker_keys
from app.services.retrieval_run import retrieval_fanout_slot
from app.services.source_scope import scoped_allowed_source_ids, scoped_participants


@dataclass(frozen=True, slots=True)
class PluginEvidenceRecord:
    evidence: EngineEvidence
    element_id: str
    source_id: str
    notebook_id: str
    source_file_name: str
    score: float


@dataclass(slots=True)
class _AuthorityLifecycle:
    revoked: bool = False
    in_flight: int = 0
    lock: Any = field(default_factory=RLock)


@dataclass(slots=True)
class _CancellationState:
    event: object | None
    cancelled: bool = False
    data_lock: Any = field(default_factory=RLock)
    lifecycle: _AuthorityLifecycle = field(default_factory=_AuthorityLifecycle)


@dataclass(slots=True)
class _RetrievalState:
    active_notebook_id: str
    cancellation: object | None
    max_k: int
    max_calls: int
    query_chars: int
    evidence_chars: int
    search_elements: Callable[..., list[RetrievedElement]]
    source_info: Callable[[Iterable[str]], dict[str, dict[str, str]]]
    source_keys: tuple[tuple[str, str], ...]
    source_origin: dict[str, str]
    request_context: Context
    search_calls: int = 0
    ledger: dict[str, PluginEvidenceRecord] = field(default_factory=dict)
    reverse: dict[tuple[str, str], str] = field(default_factory=dict)
    data_lock: Any = field(default_factory=RLock)
    lifecycle: _AuthorityLifecycle = field(default_factory=_AuthorityLifecycle)


@dataclass(slots=True)
class _ModelState:
    client: object
    cancellation: object | None
    max_calls: int
    max_chars: int
    calls: int = 0
    data_lock: Any = field(default_factory=RLock)
    lifecycle: _AuthorityLifecycle = field(default_factory=_AuthorityLifecycle)


@dataclass(slots=True)
class _TraceState:
    max_steps: int
    label_chars: int
    detail_chars: int
    steps: list[TraceStep] = field(default_factory=list)
    data_lock: Any = field(default_factory=RLock)
    lifecycle: _AuthorityLifecycle = field(default_factory=_AuthorityLifecycle)


_State = TypeVar("_State")
_STATE_LOCK = RLock()
_CANCELLATION_STATES: dict[str, _CancellationState] = {}
_RETRIEVAL_STATES: dict[str, _RetrievalState] = {}
_MODEL_STATES: dict[str, _ModelState] = {}
_TRACE_STATES: dict[str, _TraceState] = {}


def _install(store: dict[str, _State], state: _State) -> str:
    token = f"authority-{uuid4().hex}"
    with _STATE_LOCK:
        store[token] = state
    return token


def _lookup(store: dict[str, _State], token: str) -> _State:
    with _STATE_LOCK:
        state = store.get(token)
    if state is None:
        raise AskEnginePortError("plugin_engine_failed")
    return state


def _release(store: dict[str, _State], token: str) -> None:
    """Atomically revoke and detach one authority without waiting on raw I/O.

    Calls that already entered may let an intrinsically synchronous backend
    operation finish safely, but their normal-exit gate observes ``revoked``
    and rejects the result. Calls that have not entered can no longer resolve
    the token. Not waiting for ``in_flight`` avoids a revoke/I/O deadlock.
    """

    with _STATE_LOCK:
        state = store.get(token)
        if state is None:
            return
        with state.lifecycle.lock:
            state.lifecycle.revoked = True
            store.pop(token, None)


def _ensure_live(state: object) -> None:
    lifecycle = state.lifecycle
    with lifecycle.lock:
        if lifecycle.revoked:
            raise AskEnginePortError("plugin_engine_failed")


@contextmanager
def _authority_use(state: _State):
    """Bracket the complete use of a raw authority with revoke admission."""

    lifecycle = state.lifecycle
    with lifecycle.lock:
        if lifecycle.revoked:
            raise AskEnginePortError("plugin_engine_failed")
        lifecycle.in_flight += 1
    try:
        yield state
    except BaseException:
        with lifecycle.lock:
            lifecycle.in_flight -= 1
        raise
    # Final liveness admission and in-flight retirement share one critical
    # section. Thus release observes either an in-flight call and wins revoke
    # (this call rejects), or an already-linearized completed call — never the
    # unsafe gap "live check passed while in_flight was still visible".
    with lifecycle.lock:
        revoked = lifecycle.revoked
        lifecycle.in_flight -= 1
    if revoked:
        raise AskEnginePortError("plugin_engine_failed")


class PluginCancellationToken:
    """SDK cancellation face with no raw event on the reachable instance."""

    __slots__ = ("__authority_token",)

    def __init__(self, event: object | None) -> None:
        self.__authority_token = _install(
            _CANCELLATION_STATES, _CancellationState(event)
        )

    def is_set(self) -> bool:
        state = _lookup(_CANCELLATION_STATES, self.__authority_token)
        with _authority_use(state):
            with state.data_lock:
                if state.cancelled:
                    return True
                if state.event is None:
                    return False
                value = state.event.is_set()
                if type(value) is not bool:
                    raise AskEnginePortError(
                        "plugin_engine_invalid_cancellation"
                    )
                state.cancelled = value
                return value

    def raise_if_cancelled(self) -> None:
        if self.is_set():
            raise AskCancelled()

    def __del__(self) -> None:
        try:
            _release(_CANCELLATION_STATES, self.__authority_token)
        except BaseException:
            return


class PluginRetrievalAccess:
    """Run-local, scope-before-LIMIT retrieval facade with opaque authority."""

    __slots__ = ("__authority_token",)

    def __init__(
        self,
        *,
        active_notebook_id: str,
        actor_id: str,
        cancellation: object | None,
        participant_notebook_ids: Callable[[str], Sequence[str]],
        all_visible_source_ids: Callable[[str], Sequence[str]],
        hidden_source_ids: Callable[[str, str], Sequence[str]],
        search_elements: Callable[..., list[RetrievedElement]],
        source_info: Callable[[Iterable[str]], dict[str, dict[str, str]]],
        max_k: int,
        max_calls: int,
        evidence_chars: int,
        query_chars: int,
    ) -> None:
        source_keys: list[tuple[str, str]] = []
        source_origin: dict[str, str] = {}
        raise_if_cancelled(cancellation)
        # 已登记性能边界(评审存疑项):端口构造在**每次**插件引擎提问时枚举全部
        # 参与库的可见+隐藏来源 id,并把完整 id 清单逐次下推进 search SQL——这是
        # 「把整库来源行搬回热路径」的形状,万级来源的库上每问多付一批 id 物化与
        # 大 IN 清单。正解是让端口在未收窄时走谓词而不是显式清单(镜像内建路径),
        # 需要 search 接缝支持「无清单 + 私有 Memory 谓词」模式,留作后续独立一件事。
        participants = scoped_participants(
            participant_notebook_ids(active_notebook_id)
        )
        for notebook_id in participants:
            raise_if_cancelled(cancellation)
            source_ids = tuple(dict.fromkeys((
                *all_visible_source_ids(notebook_id),
                *hidden_source_ids(notebook_id, actor_id),
            )))
            scoped_source_ids = scoped_allowed_source_ids(notebook_id, source_ids)
            if scoped_source_ids is None:
                scoped_source_ids = source_ids
            for source_id in scoped_source_ids:
                value = str(source_id or "")
                if not value:
                    continue
                source_keys.append((notebook_id, value))
                source_origin[value] = notebook_id
        self.__authority_token = _install(
            _RETRIEVAL_STATES,
            _RetrievalState(
                active_notebook_id=active_notebook_id,
                cancellation=cancellation,
                max_k=max_k,
                max_calls=max_calls,
                query_chars=query_chars,
                evidence_chars=evidence_chars,
                search_elements=search_elements,
                source_info=source_info,
                source_keys=tuple(source_keys),
                source_origin=source_origin,
                request_context=copy_context(),
            ),
        )

    def search(self, query: str, k: int) -> tuple[EngineEvidence, ...]:
        state = _lookup(_RETRIEVAL_STATES, self.__authority_token)
        with _authority_use(state):
            if type(query) is not str or not query.strip():
                raise AskEnginePortError("plugin_engine_invalid_query")
            if len(query) > state.query_chars:
                raise AskEnginePortError("plugin_engine_query_too_long")
            if type(k) is not int or k <= 0:
                raise AskEnginePortError(
                    "plugin_engine_invalid_retrieval_limit"
                )
            with state.data_lock:
                if state.search_calls >= state.max_calls:
                    raise AskEnginePortError(
                        "plugin_engine_search_call_limit"
                    )
                state.search_calls += 1
            limit = min(k, state.max_k)
            raise_if_cancelled(state.cancellation)

            def _retrieve_in_request_context():
                with retrieval_fanout_slot():
                    raise_if_cancelled(state.cancellation)
                    hits = state.search_elements(
                        state.active_notebook_id,
                        query,
                        allowed_source_keys=state.source_keys,
                        limit=limit,
                    )
                    # The leaf already inside backend I/O may finish safely, but a
                    # concurrent revoke must prevent the follow-up metadata read.
                    _ensure_live(state)
                    info = state.source_info(hit.source_id for hit in hits)
                return hits, info

            # Context objects cannot be entered concurrently. A fresh copy per
            # call preserves the frozen source/user/retrieval-run authorities for
            # providers that use worker threads without serializing those calls.
            hits, info = state.request_context.copy().run(
                _retrieve_in_request_context
            )
            _ensure_live(state)
            raise_if_cancelled(state.cancellation)
            issued: list[EngineEvidence] = []
            with state.data_lock:
                for hit in hits:
                    origin = state.source_origin.get(
                        hit.source_id, state.active_notebook_id
                    )
                    identity = (origin, hit.element_id)
                    key = state.reverse.get(identity)
                    if key is None:
                        key = f"pe-{uuid4().hex}"
                        metadata = info.get(hit.source_id) or {}
                        evidence = EngineEvidence(
                            evidence_key=key,
                            text=hit.text[: state.evidence_chars],
                            source_title=(
                                metadata.get("title") or hit.source_title
                            ),
                            location_label=hit.location_label,
                        )
                        state.ledger[key] = PluginEvidenceRecord(
                            evidence=evidence,
                            element_id=hit.element_id,
                            source_id=hit.source_id,
                            notebook_id=origin,
                            source_file_name=metadata.get("file_name", ""),
                            score=float(hit.score or 0.0),
                        )
                        state.reverse[identity] = key
                    issued.append(state.ledger[key].evidence)
            return tuple(issued)

    def fetch(self, evidence_key: str) -> EngineEvidence | None:
        if type(evidence_key) is not str:
            raise AskEnginePortError("plugin_engine_invalid_evidence_key")
        state = _lookup(_RETRIEVAL_STATES, self.__authority_token)
        with _authority_use(state):
            with state.data_lock:
                record = state.ledger.get(evidence_key)
                return record.evidence if record is not None else None

    def __del__(self) -> None:
        try:
            _release(_RETRIEVAL_STATES, self.__authority_token)
        except BaseException:
            return


class PluginEngineModelAccess:
    __slots__ = ("__authority_token",)

    def __init__(
        self,
        client: object,
        *,
        cancellation: object | None,
        max_calls: int,
        max_chars: int,
    ) -> None:
        self.__authority_token = _install(
            _MODEL_STATES,
            _ModelState(client, cancellation, max_calls, max_chars),
        )

    def complete(self, prompt: str) -> str:
        state = _lookup(_MODEL_STATES, self.__authority_token)
        with _authority_use(state):
            if type(prompt) is not str or not prompt:
                raise AskEnginePortError("plugin_engine_invalid_prompt")
            if len(prompt) > state.max_chars:
                raise AskEnginePortError("plugin_engine_prompt_too_long")
            with state.data_lock:
                if state.calls >= state.max_calls:
                    raise AskEnginePortError(
                        "plugin_engine_model_call_limit"
                    )
                state.calls += 1
            raise_if_cancelled(state.cancellation)
            if getattr(state.client, "configured", True) is not True:
                raise AskEnginePortError("plugin_engine_model_unconfigured")
            try:
                # 刻意**不**传 cap_kwargs:合同(AGENTS.md ask.engine 条)写明输出
                # cap「继承绑定客户端的普通输出上限」——显式塞 answer_max_tokens 会
                # 在部署给 plugin_engine 绑了小 cap 客户端时越过它(codex #602 R2 P2)。
                raw = state.client.chat_json(
                    [{"role": "user", "content": prompt}],
                    '{"text":"string"}',
                    cancel_event=state.cancellation,
                )
                parsed = json.loads(raw)
                text = parsed.get("text") if isinstance(parsed, dict) else None
                if type(text) is not str:
                    raise ValueError("invalid structured completion")
                return text
            except AskCancelled:
                raise
            except AskEnginePortError:
                raise
            except BaseException:
                raise AskEnginePortError(
                    "plugin_engine_model_failed"
                ) from None

    def __del__(self) -> None:
        try:
            _release(_MODEL_STATES, self.__authority_token)
        except BaseException:
            return


class PluginEngineTrace:
    __slots__ = ("__authority_token",)

    def __init__(self, *, max_steps: int, label_chars: int, detail_chars: int) -> None:
        self.__authority_token = _install(
            _TRACE_STATES, _TraceState(max_steps, label_chars, detail_chars)
        )

    def step(self, label: str, detail: str = "") -> None:
        state = _lookup(_TRACE_STATES, self.__authority_token)
        with _authority_use(state):
            with state.data_lock:
                malformed = type(label) is not str or type(detail) is not str
                if malformed:
                    _mark_trace_truncated(state)
                    return
                clipped_label = label[: state.label_chars]
                clipped_detail = detail[: state.detail_chars]
                clipped = clipped_label != label or clipped_detail != detail
                if len(state.steps) >= state.max_steps:
                    _mark_trace_truncated(state)
                    return
                state.steps.append(TraceStep(
                    step_type="plugin",
                    summary=clipped_label,
                    detail={
                        "detail": clipped_detail,
                        **({"truncated": True} if clipped else {}),
                    },
                ))

    def __del__(self) -> None:
        try:
            _release(_TRACE_STATES, self.__authority_token)
        except BaseException:
            return


def _mark_trace_truncated(state: _TraceState) -> None:
    if not state.steps:
        return
    last = state.steps[-1]
    state.steps[-1] = TraceStep(
        step_type="plugin",
        summary=last.summary,
        detail={**last.detail, "truncated": True},
    )


def admit_plugin_engine_result(
    access: PluginRetrievalAccess,
    answer_markdown: str,
    citations: tuple[str, ...],
) -> tuple[str, tuple[PluginEvidenceRecord, ...]]:
    """Core-only evidence-handle admission; it is not on the SDK facade."""

    if type(access) is not PluginRetrievalAccess:
        raise AskEnginePortError("invalid_plugin_engine_result")
    token = object.__getattribute__(
        access, "_PluginRetrievalAccess__authority_token"
    )
    state = _lookup(_RETRIEVAL_STATES, token)
    with _authority_use(state):
        if type(answer_markdown) is not str or type(citations) is not tuple:
            raise AskEnginePortError("invalid_plugin_engine_result")
        if len(citations) > state.max_calls * state.max_k:
            raise AskEnginePortError("plugin_engine_citation_limit")
        with state.data_lock:
            records: list[PluginEvidenceRecord] = []
            for evidence_key in citations:
                if (
                    type(evidence_key) is not str
                    or evidence_key not in state.ledger
                ):
                    raise AskEnginePortError(
                        "plugin_engine_unverified_citation"
                    )
                records.append(state.ledger[evidence_key])

        cited_marker_indexes: set[int] = set()

        def normalize(match) -> str:
            keys = marker_keys(match.group(0))
            for key in keys:
                try:
                    index = int(key[1:])
                except (TypeError, ValueError):
                    raise AskEnginePortError(
                        "plugin_engine_unverified_citation"
                    ) from None
                if index < 1 or index > len(records):
                    raise AskEnginePortError(
                        "plugin_engine_unverified_citation"
                    )
                cited_marker_indexes.add(index)
            return "[" + ", ".join(keys) + "]"

        normalized = LOOSE_MARKER_RE.sub(normalize, answer_markdown)
        if cited_marker_indexes != set(range(1, len(records) + 1)):
            raise AskEnginePortError("plugin_engine_unverified_citation")
        return normalized, tuple(records)


def plugin_engine_trace_steps(trace: PluginEngineTrace) -> tuple[TraceStep, ...]:
    if type(trace) is not PluginEngineTrace:
        raise AskEnginePortError("plugin_engine_failed")
    token = object.__getattribute__(trace, "_PluginEngineTrace__authority_token")
    state = _lookup(_TRACE_STATES, token)
    with _authority_use(state):
        with state.data_lock:
            return tuple(state.steps)


def release_plugin_engine_ports(*ports: object) -> None:
    """Release every core authority before any plugin can retain a live port."""

    for port in ports:
        try:
            if type(port) is PluginRetrievalAccess:
                token = object.__getattribute__(
                    port, "_PluginRetrievalAccess__authority_token"
                )
                _release(_RETRIEVAL_STATES, token)
            elif type(port) is PluginEngineModelAccess:
                token = object.__getattribute__(
                    port, "_PluginEngineModelAccess__authority_token"
                )
                _release(_MODEL_STATES, token)
            elif type(port) is PluginEngineTrace:
                token = object.__getattribute__(
                    port, "_PluginEngineTrace__authority_token"
                )
                _release(_TRACE_STATES, token)
            elif type(port) is PluginCancellationToken:
                token = object.__getattribute__(
                    port, "_PluginCancellationToken__authority_token"
                )
                _release(_CANCELLATION_STATES, token)
        except BaseException:
            continue


__all__ = [
    "PluginCancellationToken",
    "PluginEngineModelAccess",
    "PluginEngineTrace",
    "PluginEvidenceRecord",
    "PluginRetrievalAccess",
    "admit_plugin_engine_result",
    "plugin_engine_trace_steps",
    "release_plugin_engine_ports",
]
