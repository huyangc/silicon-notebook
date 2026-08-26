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
import re
from threading import RLock
from typing import Any, Callable, Iterable, Sequence, TypeVar
from uuid import uuid4

from app.domain.ask_engine import AskEnginePortError, EngineEvidence
from app.domain.cancellation import AskCancelled
from app.domain.kg.edge_schema import NODE_TYPES, VALID_EDGE_TYPES
from app.domain.retrieval import RetrievedElement, RetrievedKnowledge
from app.models.ask import TraceStep
from app.services.cancellation import raise_if_cancelled
from app.services.citation_markers import LOOSE_MARKER_RE, marker_keys
from app.services.retrieval_run import retrieval_fanout_slot
from app.services.source_scope import (
    scoped_allowed_source_ids,
    scoped_participants,
    source_scope_restricted,
)


@dataclass(frozen=True, slots=True)
class PluginEvidenceRecord:
    evidence: EngineEvidence
    element_id: str
    source_id: str
    notebook_id: str
    source_file_name: str
    score: float
    # Verbatim grounding excerpt (the bound evidence element's own
    # `quoted_span`), separate from `evidence.text` which is a KG hit's
    # model-authored name+definition summary. Empty for element-path records
    # (the element path already puts verbatim text in `evidence.text`).
    quoted_span: str = ""


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
    search_knowledge: Callable[..., list[RetrievedKnowledge]] | None = None
    object_neighbors: Callable[..., Any] | None = None
    collection_overview: Callable[[str], str] | None = None
    # Bounded PK hydration of source_elements rows — the same seam the
    # collection-enumeration citations use to select "the first SURVIVING
    # evidence element". It is the element-liveness half of the citability
    # contract: source metadata alone cannot prove the element a citation
    # would open still exists (codex #603 R2 P1).
    evidence_elements: Callable[[Sequence[str]], dict[str, dict]] | None = None
    kg_max_calls: int = 0
    # Frozen at construction, inside the request's scope context: whether the
    # ACTIVE notebook's source selection is genuinely narrowed (the CHANNEL
    # question, `source_scope_restricted()` — not the ceiling/FILTERING
    # question, which is true for every frozen all-selected snapshot).
    active_scope_restricted: bool = False
    search_calls: int = 0
    kg_calls: int = 0
    ledger: dict[str, PluginEvidenceRecord] = field(default_factory=dict)
    reverse: dict[tuple[str, str], str] = field(default_factory=dict)
    # Knowledge objects keep their own two-way registry: element identity is
    # (notebook, element) and object identity is (notebook, object), so one
    # shared map would let an element id collide with an object id and let a
    # `kg_neighbors` anchor resolve to an element handle.
    kg_reverse: dict[tuple[str, str], str] = field(default_factory=dict)
    kg_objects: dict[str, tuple[str, str]] = field(default_factory=dict)
    overview_cache: str | None = None
    # Dedicated lock for `kg_overview`'s read-compute-write, deliberately
    # separate from `data_lock`: the computation it must wrap crosses raw I/O
    # (`collection_overview`), and holding `data_lock` across that would block
    # every `search`/`search_kg`/`kg_neighbors`/`fetch` call on this run for
    # the duration of that I/O. See `kg_overview`'s docstring for why this
    # cannot deadlock against revoke.
    overview_lock: Any = field(default_factory=RLock)
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


# 「引用样」残留探测:括号组里出现 k\d 即嫌疑,合法组由 LOOSE_MARKER_RE.fullmatch
# 放行(见 admit_plugin_engine_result 的注释;codex #602 R6 P1)。
_SUSPECT_MARKER_RE = re.compile(
    r"\[[^\[\]\n]*\bk\d+[^\[\]\n]*\]|【[^【】\n]*\bk\d+[^【】\n]*】"
)

# Protocol boundary: the collection overview crosses to plugin prompts as one
# opaque scaffolding string, so its ceiling is a contract value, not a budget.
KG_OVERVIEW_MAX_CHARS = 600

_KG_DIRECTIONS = frozenset({"both", "out", "in"})

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


def _claim_kg_call(state: _RetrievalState) -> None:
    """Spend one unit of the shared search_kg/kg_neighbors run budget."""

    with state.data_lock:
        if state.kg_calls >= state.kg_max_calls:
            raise AskEnginePortError("plugin_engine_kg_call_limit")
        state.kg_calls += 1


def _hit_origin(hit: object, default_origin: str) -> str:
    """Resolve one KG hit's library id, falling back to a caller-supplied
    default rather than unconditionally the active notebook.

    ``search_kg`` hits come from ``federated_retrieve``, which always tags
    ``.notebook_id`` with the real participant library
    (``retrieval_candidates.py::_federated_retrieve_impl``), so its default
    (the active notebook) is only ever a defensive fallback there.
    ``kg_neighbors`` hits come from the one-hop expansion seam
    (``_retrieve_neighbors``), which leaves ``.notebook_id`` unset (production
    default ``""``) — the anchor's OWN library is the only correct default at
    that call site. Falling back to the active notebook there would silently
    misattribute every neighbor of a mounted-base anchor to a library that may
    not even contain its evidence source, and ``_bound_evidence``'s
    frozen-snapshot recheck would then downgrade all of them to context-only
    (``evidence_key == ""``).
    """
    return str(getattr(hit, "notebook_id", "") or default_origin)


def _evidence_items(hit: object) -> tuple[object, ...]:
    items = getattr(hit, "evidence", ()) or ()
    return tuple(items) if isinstance(items, (list, tuple)) else ()


def _kg_source_ids(hits: Sequence[object]) -> list[str]:
    return [
        str(getattr(item, "source_id", "") or "")
        for hit in hits
        for item in _evidence_items(hit)
    ]


def _kg_element_ids(hits: Sequence[object]) -> list[str]:
    return [
        str(getattr(item, "element_id", "") or "")
        for hit in hits
        for item in _evidence_items(hit)
    ]


def _bound_evidence(
    state: _RetrievalState,
    hit: object,
    origin: str,
    info: dict[str, dict[str, str]],
    live: dict[str, dict],
) -> tuple[object, str] | None:
    """The first SURVIVING evidence element this object may be cited by.

    Three predicates, all load-bearing. Element liveness comes first: `live`
    is the bounded PK hydration of the candidate element ids, so an element
    removed or replaced (reparse rotates element ids while the source row
    stays) is simply absent and can never become a citation that opens
    nothing (codex #603 R2 P1). The hydrated row's own `source_id` is then
    the AUTHORITATIVE source for the two source predicates — mirroring the
    enumeration citations, which also trust the hydrated row over the
    persisted binding's claim. Frozen-snapshot membership is the
    post-hydration source-scope re-check; metadata resolution proves the
    source itself still exists. Returns ``(binding, source_id)``.
    """

    for item in _evidence_items(hit):
        element_id = str(getattr(item, "element_id", "") or "")
        if not element_id:
            continue
        row = live.get(element_id)
        if row is None:
            continue
        source_id = (
            str(row.get("source_id") or "")
            or str(getattr(item, "source_id", "") or "")
        )
        if not source_id:
            continue
        if state.source_origin.get(source_id) != origin:
            continue
        if source_id in info:
            return item, source_id
    return None


def _payload_text(payload: dict, *names: str) -> str:
    """First non-empty string field, ignoring the non-scalar payload shapes a
    model-authored knowledge object may legitimately carry."""

    for name in names:
        value = payload.get(name)
        if type(value) is str and value.strip():
            return value.strip()
    return ""


def _kg_text(name: str, summary: str, snippet: str, max_chars: int) -> str:
    body = summary or str(snippet or "").strip()
    if name and body:
        return f"{name} — {body}"[:max_chars]
    return (name or body)[:max_chars]


def _issue_kg_evidence(
    state: _RetrievalState,
    hits: Sequence[object],
    info: dict[str, dict[str, str]],
    live: dict[str, dict],
    *,
    default_origin: str,
) -> tuple[EngineEvidence, ...]:
    """Issue (or replay) one run-local handle per knowledge-object hit.

    ``default_origin`` is call-site-specific — see `_hit_origin`'s docstring;
    it is never the same constant for `search_kg` and `kg_neighbors`.
    """

    issued: list[EngineEvidence] = []
    for hit in hits:
        origin = _hit_origin(hit, default_origin)
        identity = (origin, str(getattr(hit, "object_id", "") or ""))
        key = state.kg_reverse.get(identity)
        if key is not None:
            issued.append(state.ledger[key].evidence)
            continue
        payload = getattr(hit, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        name = _payload_text(payload, "name")
        summary = _payload_text(payload, "definition", "evidence")
        object_type = str(getattr(hit, "object_type", "") or "")
        bound_pair = _bound_evidence(state, hit, origin, info, live)
        if bound_pair is None:
            # Context-only hit: no ledger record and no anchor registration, so
            # it can be neither cited nor used as a `kg_neighbors` anchor.
            # Reached when every binding's element is gone, its source left the
            # frozen snapshot, or its source no longer resolves.
            issued.append(EngineEvidence(
                evidence_key="",
                text=_kg_text(name, summary, "", state.evidence_chars),
                source_title="",
                location_label="",
                object_type=object_type,
            ))
            continue
        bound, bound_source_id = bound_pair
        quoted_span = str(getattr(bound, "quoted_span", "") or "")
        text = _kg_text(name, summary, quoted_span, state.evidence_chars)
        if not text:
            # name/definition/quoted_span all empty: there is nothing a user
            # could verify, so this hit must not be signed as citable even
            # though its evidence element is live. Same non-registration shape
            # as the `bound is None` branch above (no ledger, no anchor).
            issued.append(EngineEvidence(
                evidence_key="",
                text="",
                source_title="",
                location_label="",
                object_type=object_type,
            ))
            continue
        source_id = bound_source_id
        metadata = info.get(source_id) or {}
        key = f"pe-{uuid4().hex}"
        evidence = EngineEvidence(
            evidence_key=key,
            text=text,
            source_title=(
                metadata.get("title")
                or str(getattr(bound, "source_title", "") or "")
            ),
            location_label=str(getattr(bound, "location_label", "") or ""),
            object_type=object_type,
        )
        state.ledger[key] = PluginEvidenceRecord(
            evidence=evidence,
            element_id=str(getattr(bound, "element_id", "") or ""),
            source_id=source_id,
            notebook_id=origin,
            source_file_name=metadata.get("file_name", ""),
            score=float(getattr(hit, "score", 0.0) or 0.0),
            quoted_span=quoted_span,
        )
        state.kg_reverse[identity] = key
        state.kg_objects[key] = identity
        issued.append(evidence)
    return tuple(issued)


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
        search_knowledge: Callable[..., list[RetrievedKnowledge]] | None = None,
        object_neighbors: Callable[..., Any] | None = None,
        collection_overview: Callable[[str], str] | None = None,
        evidence_elements: Callable[[Sequence[str]], dict] | None = None,
        kg_max_calls: int = 0,
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
                search_knowledge=search_knowledge,
                object_neighbors=object_neighbors,
                collection_overview=collection_overview,
                evidence_elements=evidence_elements,
                kg_max_calls=kg_max_calls,
                active_scope_restricted=source_scope_restricted(),
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

    def search_kg(
        self, query: str, k: int, object_types: tuple[str, ...] = ()
    ) -> tuple[EngineEvidence, ...]:
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
            # Container/element shape is validated BEFORE the budget claim
            # below (contract §3.1): a malformed request must never spend the
            # shared KG call budget. Only tuple/list are accepted — a bare
            # str like "concept" is itself iterable-of-str and would
            # otherwise silently explode into single characters
            # (`tuple("concept")` == `('c','o','n','c','e','p','t')`), turning
            # a plugin's missing-comma bug into a request nobody can debug.
            if type(object_types) not in (tuple, list):
                raise AskEnginePortError("plugin_engine_invalid_kg_request")
            if any(type(value) is not str for value in object_types):
                raise AskEnginePortError("plugin_engine_invalid_kg_request")
            requested = tuple(object_types)
            types = tuple(
                value for value in requested if value in NODE_TYPES
            )
            if requested and not types:
                # An impossible filter cannot produce a hit, so it must not
                # spend this run's shared KG budget on proving that.
                return ()
            search_knowledge = state.search_knowledge
            evidence_elements = state.evidence_elements
            if search_knowledge is None or evidence_elements is None:
                raise AskEnginePortError("plugin_engine_failed")
            _claim_kg_call(state)
            limit = min(k, state.max_k)
            raise_if_cancelled(state.cancellation)

            def _retrieve_in_request_context():
                with retrieval_fanout_slot():
                    raise_if_cancelled(state.cancellation)
                    # The frozen source keys are the isolation predicate: they
                    # reach the candidate seam before its LIMIT, so another
                    # member's private-Memory-derived objects are structurally
                    # absent rather than filtered out afterwards.
                    hits = list(search_knowledge(
                        state.active_notebook_id,
                        query,
                        types=types or None,
                        allowed_source_keys=state.source_keys,
                    ) or ())[:limit]
                    _ensure_live(state)
                    info = state.source_info(_kg_source_ids(hits))
                    live = evidence_elements(_kg_element_ids(hits)) or {}
                return hits, info, live

            hits, info, live = state.request_context.copy().run(
                _retrieve_in_request_context
            )
            _ensure_live(state)
            raise_if_cancelled(state.cancellation)
            with state.data_lock:
                # `federated_retrieve` hits always carry a real `.notebook_id`
                # (see `_hit_origin`'s docstring); the active notebook here is
                # only ever a defensive fallback, never load-bearing.
                return _issue_kg_evidence(
                    state, hits, info, live,
                    default_origin=state.active_notebook_id,
                )

    def kg_neighbors(
        self,
        evidence_key: str,
        k: int,
        edge_type: str = "",
        direction: str = "both",
    ) -> tuple[EngineEvidence, ...]:
        state = _lookup(_RETRIEVAL_STATES, self.__authority_token)
        with _authority_use(state):
            if type(evidence_key) is not str:
                raise AskEnginePortError("plugin_engine_invalid_evidence_key")
            if type(k) is not int or k <= 0:
                raise AskEnginePortError(
                    "plugin_engine_invalid_retrieval_limit"
                )
            if type(edge_type) is not str or type(direction) is not str:
                raise AskEnginePortError("plugin_engine_invalid_kg_request")
            if direction not in _KG_DIRECTIONS:
                raise AskEnginePortError("plugin_engine_invalid_kg_request")
            if edge_type and edge_type not in VALID_EDGE_TYPES:
                return ()
            object_neighbors = state.object_neighbors
            with state.data_lock:
                anchor = state.kg_objects.get(evidence_key)
            # An element handle, a handle from another run and a context-only
            # knowledge hit are all simply "not an anchor" here.
            if anchor is None:
                return ()
            notebook_id, object_id = anchor
            # Channel gate for genuinely narrowed runs (codex #603 R1 P2):
            # one-hop expansion has no source predicate below its bounded
            # read, so in a narrowed ACTIVE-notebook run out-of-scope
            # neighbours could consume the whole window and filter-after can
            # never recover rows that were not returned. Mirroring the
            # built-in discipline (restricted runs close graph channels
            # instead of filtering after the LIMIT), the channel closes and
            # returns empty. Anchors in a mounted base stay open — the
            # library dimension is whole-notebook checkboxes, there is no
            # per-source narrowing inside a base for the window to collide
            # with, and folding the two dimensions together is exactly what
            # the scope-orthogonality contract forbids.
            if (
                notebook_id == state.active_notebook_id
                and state.active_scope_restricted
            ):
                return ()
            evidence_elements = state.evidence_elements
            if object_neighbors is None or evidence_elements is None:
                raise AskEnginePortError("plugin_engine_failed")
            _claim_kg_call(state)
            limit = min(k, state.max_k)
            raise_if_cancelled(state.cancellation)

            def _expand_in_request_context():
                with retrieval_fanout_slot():
                    raise_if_cancelled(state.cancellation)
                    # One-hop expansion has no source-key parameter. Narrowed
                    # active-notebook runs never reach this point (channel
                    # gate above); for the runs that do, the frozen ceiling
                    # still reaches the wrapper's result filter through the
                    # request context, and `_bound_evidence` re-checks the
                    # snapshot before issuing any handle.
                    expansion = object_neighbors(
                        notebook_id, object_id, edge_type or None, direction
                    )
                    # `expansion.truncated` is deliberately discarded — the
                    # port contract (§3.2) already tells providers that a full
                    # page must be assumed to have more, so there is no signal
                    # left to add here.
                    hits = list(getattr(expansion, "hits", ()) or ())[:limit]
                    _ensure_live(state)
                    info = state.source_info(_kg_source_ids(hits))
                    live = evidence_elements(_kg_element_ids(hits)) or {}
                return hits, info, live

            hits, info, live = state.request_context.copy().run(
                _expand_in_request_context
            )
            _ensure_live(state)
            raise_if_cancelled(state.cancellation)
            with state.data_lock:
                # Deliberate divergence from the built-in reasoning engine's
                # expand/neighbors, which pins `notebook_id` to the ACTIVE
                # notebook only (reasoning_retrieval.py:4086-4088: a base-tier
                # hit's neighbors live in the base notebook, so deep cross-tier
                # graph walks are graph mode's job, not reasoning mode).  This
                # port's contract instead promises a one-hop take within the
                # anchor's OWN participant library: `notebook_id` here is that
                # library (unpacked from the anchor identity above), not
                # `state.active_notebook_id`. Authorization is not weakened by
                # this: `notebook_id` can only be a library this run's
                # participant-set already resolved (it came from a handle this
                # same run issued), and `_bound_evidence`'s frozen
                # source-key recheck is still the backstop that decides
                # citability — this is not the disallowed unbounded cross-tier
                # graph walk that decision forbids.
                return _issue_kg_evidence(
                    state, hits, info, live, default_origin=notebook_id
                )

    def kg_overview(self) -> str:
        state = _lookup(_RETRIEVAL_STATES, self.__authority_token)
        with _authority_use(state):
            # A genuinely narrowed run gets no overview: the collection map
            # counts the WHOLE active notebook (its counting seam applies only
            # the library dimension), so handing it out under a narrowed scope
            # would leak out-of-scope aggregate counts and mislead the model
            # about what this run may actually read (codex #603 R2 P2). The
            # format is contractually unparseable, so an empty string is a
            # valid "nothing to show" shape for providers.
            if state.active_scope_restricted:
                return ""
            # `overview_lock` brackets the WHOLE read-compute-write critical
            # section (not just the final write) so concurrent first callers
            # cannot each observe an empty memo and each pay for
            # `collection_overview`'s underlying full-notebook count — that
            # was the bug (8 concurrent callers measured as 8 real calls).
            # This cannot deadlock against a concurrent revoke: `_release`
            # never waits on `in_flight` (this call is already counted via
            # `_authority_use`'s bracket above), and `_ensure_live` only ever
            # takes `state.lifecycle.lock` for one instantaneous read — it is
            # never held while acquiring `overview_lock`, so the two locks are
            # never nested in the reverse order.
            with state.overview_lock:
                cached = state.overview_cache
                if cached is not None:
                    return cached
                collection_overview = state.collection_overview
                if collection_overview is None:
                    raise AskEnginePortError("plugin_engine_failed")
                raise_if_cancelled(state.cancellation)

                def _read_in_request_context():
                    # Deliberately no fan-out slot: the collection map is a
                    # counting read behind its own memo, not a retrieval leaf,
                    # and the built-in reasoning path builds the same map
                    # without a slot.
                    return collection_overview(state.active_notebook_id)

                text = state.request_context.copy().run(
                    _read_in_request_context
                )
                _ensure_live(state)
                raise_if_cancelled(state.cancellation)
                value = str(text or "")[:KG_OVERVIEW_MAX_CHARS]
                state.overview_cache = value
                return value

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
        # Every retrieval budget can issue up to ``max_k`` handles per call, so
        # the ceiling must follow both pools or a legitimate KG-heavy answer is
        # rejected for citing handles this run really did issue.
        issuable = (state.max_calls + state.kg_max_calls) * state.max_k
        if len(citations) > issuable:
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
        # 残留的「引用样」括号组整份拒绝(codex #602 R6 P1):`[k1, nope]` 这类畸形
        # 组不被 LOOSE_MARKER_RE 匹配、会原样留在正文里,渲染成一个从未被核验的
        # 引用外观——与核心「复合组遇到未知键整体失败关闭」同一条纪律。归一化输出
        # 自己写回的合法组恰好 fullmatch LOOSE_MARKER_RE,不会误伤。
        for suspect in _SUSPECT_MARKER_RE.finditer(normalized):
            if LOOSE_MARKER_RE.fullmatch(suspect.group(0)) is None:
                raise AskEnginePortError("plugin_engine_unverified_citation")
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
