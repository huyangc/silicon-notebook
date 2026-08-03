"""Request-local imported-source retrieval scope.

The context variable is set by Ask/report orchestration and copied into their
worker threads. Retrieval owners consult it at result boundaries, so the scope
applies consistently to chunk, KG, relation, element, and PPR evidence without
changing persisted scale-index artifacts.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Iterable, Iterator


@dataclass(frozen=True)
class ActiveSourceScope:
    notebook_id: str
    mode: str
    source_ids: frozenset[str]
    # Base-library dimension (library-level scope): independent of the local
    # source ceiling above. ``base_mode``/``base_notebook_ids`` mirror
    # ``mode``/``source_ids`` in shape, but select whole mounted reference
    # libraries rather than individual sources within them. Defaults
    # ("exclude", empty) mean every mounted base notebook participates --
    # the historical whole-scope behavior -- so a caller that never supplies
    # a base scope observes byte-identical behavior to before this field
    # existed.
    base_mode: str = "exclude"
    base_notebook_ids: frozenset[str] = frozenset()
    # Which dimensions the caller actually SUPPLIED, as opposed to which ones
    # ended up carrying their neutral default. The two are not the same thing:
    # a run that narrowed only the base dimension still gets mode="exclude" /
    # source_ids=frozenset() here, which is indistinguishable from a submitted
    # "all local sources" selection by value alone. Only the payload accessors
    # below consult these flags -- gating (`restricted`, `allows`,
    # `covers_notebook`) must stay value-driven, because a neutral default and
    # an explicit "all" must keep filtering identically.
    #
    # They exist because `current_source_scope_payload()` is re-persisted by
    # report_engine.prepare_intent into the report's understanding contract and
    # re-frozen by _validate_source_scope on confirm: fabricating a local scope
    # for a base-only run would freeze it into include:[every visible source],
    # whose `restricted` is True -- silently disabling private Memory, PPR,
    # community reports and the corpus profile for a user who only unchecked a
    # reference library. Deriving "was it provided?" at the call site instead
    # would mean guessing.
    source_provided: bool = True
    base_provided: bool = True

    @property
    def restricted(self) -> bool:
        # exclude [] is the UI/default representation of "all local sources"
        # and must remain byte-for-byte compatible with the historical path.
        # Base-library selection is a SEPARATE dimension (see
        # `base_restricted`) and must NEVER feed this property: it drives
        # source_scope_restricted(), which gates the *active* notebook's own
        # PPR/graph expansion and (ask_service._memory_hits) private Memory.
        # Those are orthogonal to which mounted libraries participate --
        # folding base-library exclusion in here would silently punish a user
        # who only unchecked a reference library with the same "restricted"
        # penalties meant for narrowing local sources.
        return self.mode == "include" or bool(self.source_ids)

    @property
    def base_restricted(self) -> bool:
        return self.base_mode == "include" or bool(self.base_notebook_ids)

    def covers_notebook(self, notebook_id: str) -> bool:
        """Single collapsing point: is a candidate whose origin is
        `notebook_id` allowed to participate at all, based purely on
        notebook identity (never on which local sources are checked).

        A blank/falsy `notebook_id` and the active notebook itself are
        always covered here -- callers use "" as a stand-in for "this run's
        active notebook" (see `filter_retrieval_items`'s `origin` default),
        and the active notebook's own sources are gated separately by
        `.allows()`. Any other notebook is a mounted base library: it
        participates unless the base-library scope explicitly excludes it
        (mode="exclude") or fails to include it (mode="include").
        """
        if not notebook_id or notebook_id == self.notebook_id:
            return True
        if not self.base_restricted:
            return True
        if self.base_mode == "include":
            return notebook_id in self.base_notebook_ids
        return notebook_id not in self.base_notebook_ids

    def allows(self, notebook_id: str, source_id: str) -> bool:
        if not self.covers_notebook(notebook_id):
            return False
        # Mounted base libraries are independent participants and are never
        # governed by the active notebook's source checkboxes.
        if notebook_id and notebook_id != self.notebook_id:
            return True
        if not self.restricted:
            return True
        if not source_id:
            return False
        if self.mode == "include":
            return source_id in self.source_ids
        return source_id not in self.source_ids


_CURRENT_SOURCE_SCOPE: ContextVar[ActiveSourceScope | None] = ContextVar(
    "current_source_scope", default=None
)


def _scope_dict(scope: Any) -> dict[str, Any] | None:
    if scope is None:
        return None
    if hasattr(scope, "model_dump"):
        return scope.model_dump()
    return dict(scope)


@contextmanager
def source_scope_context(
    notebook_id: str, scope: Any, base_scope: Any = None
) -> Iterator[None]:
    raw = _scope_dict(scope)
    base_raw = _scope_dict(base_scope)
    if raw is None and base_raw is None:
        yield
        return
    current = ActiveSourceScope(
        notebook_id=notebook_id,
        mode=str((raw or {}).get("mode") or "exclude"),
        source_ids=frozenset(str(value) for value in (raw or {}).get("source_ids") or []),
        base_mode=str((base_raw or {}).get("mode") or "exclude"),
        base_notebook_ids=frozenset(
            str(value) for value in (base_raw or {}).get("notebook_ids") or []
        ),
        source_provided=raw is not None,
        base_provided=base_raw is not None,
    )
    token = _CURRENT_SOURCE_SCOPE.set(current)
    try:
        yield
    finally:
        _CURRENT_SOURCE_SCOPE.reset(token)


def current_source_scope() -> ActiveSourceScope | None:
    return _CURRENT_SOURCE_SCOPE.get()


def current_source_scope_payload() -> dict[str, Any] | None:
    """The local dimension as a re-persistable payload, or None when this run
    never supplied one.

    ``None`` here is load-bearing, not merely tidy: report_engine writes the
    return value into the report's ``understanding`` contract, and a
    fabricated ``exclude:[]`` would be re-frozen on confirm into
    ``include:[every visible source]`` -- flipping ``restricted`` on for a run
    that only narrowed the base-library dimension. See the ``source_provided``
    comment on ``ActiveSourceScope``.
    """
    scope = current_source_scope()
    if scope is None or not scope.source_provided:
        return None
    return {"mode": scope.mode, "source_ids": sorted(scope.source_ids)}


def current_base_scope_payload() -> dict[str, Any] | None:
    """Mirrors ``current_source_scope_payload`` for the base-library
    dimension. Deliberately a separate function/shape (rather than extra
    keys folded into the source payload above): that payload's shape is
    re-fed into ``SourceScope`` elsewhere, and this one is re-fed into
    ``BaseNotebookScope`` -- keeping them apart avoids coupling either
    model's field names to the other.

    Symmetrically to ``current_source_scope_payload``, returns None when this
    run never supplied a base scope: persisting a synthesised ``exclude:[]``
    would be re-frozen on confirm into ``include:[libraries mounted at that
    moment]``, silently locking a later-mounted reference library out of a
    report the user never scoped."""
    scope = current_source_scope()
    if scope is None or not scope.base_provided:
        return None
    return {"mode": scope.base_mode, "notebook_ids": sorted(scope.base_notebook_ids)}


_CURRENT_SCOPE_RECEIPT: ContextVar[Any] = ContextVar(
    "current_retrieval_scope_receipt", default=None
)


@contextmanager
def retrieval_scope_receipt_context(receipt: Any) -> Iterator[None]:
    """Carry the DISPLAY-ONLY scope receipt from the API entry point to the
    single answer-persistence seam.

    Why a context variable rather than a parameter: the receipt is built where
    the ``NotebookSummary`` already exists (the route that authorized the run),
    but it has to survive until ``AskService._save_answer``, which is reached
    through ~15 handler return paths and, for streaming, through a worker
    thread that detaches from the connection. ``background_jobs.submit``
    snapshots the caller's context, so entering this manager around
    ``start_ask_stream`` reaches the detached worker unchanged.

    NOT part of the retrieval scope. ``ActiveSourceScope`` deliberately does
    not carry it: everything on that object is consulted by a gate, and the
    one guarantee this receipt must keep is that it is consulted by none.
    ``backend/tests/test_source_scope.py`` pins the reader set.
    """
    token = _CURRENT_SCOPE_RECEIPT.set(receipt)
    try:
        yield
    finally:
        _CURRENT_SCOPE_RECEIPT.reset(token)


def current_retrieval_scope_receipt() -> Any:
    """The display-only receipt for this run, or None when the request scoped
    nothing (or the caller never entered the context above)."""
    return _CURRENT_SCOPE_RECEIPT.get()


def source_scope_restricted() -> bool:
    scope = current_source_scope()
    return bool(scope and scope.restricted)


def scoped_conversation_history(history: str) -> str:
    """Prevent prior answers from crossing into a newly narrowed source run."""
    return "" if source_scope_restricted() else history


def source_allowed(notebook_id: str, source_id: str) -> bool:
    scope = current_source_scope()
    return True if scope is None else scope.allows(notebook_id, source_id)


def notebook_in_scope(notebook_id: str) -> bool:
    """Library-dimension gate for the per-participant retrieval loops.

    Federation already walks participants one library at a time, so an
    unchecked reference library can be skipped BEFORE its query runs rather
    than having its rows dropped at the result boundary — the boundary filter
    stays as the fail-closed backstop, this is the cost half.

    In the federated candidate loops it is therefore a COST guard, not the
    correctness gate, and the difference is worth stating because the element
    arm looks like the exception: ``RetrievedElement`` carries no
    ``notebook_id``, so ``filter_retrieval_items(..., "element", ...)`` can
    only judge it against the active notebook.  Even there the skip is not
    load-bearing — the inner ``_retrieve_elements`` intersects the same
    per-notebook allow-list and comes back empty — it just makes an unchecked
    library free instead of merely harmless.  Two consumers ARE
    correctness-critical and are not this shape: ``scoped_participants``
    (collection reads, where the skip decides the denominator too) and
    ``EvidenceContextService.knowledge_context`` (where the hit becomes prompt
    text and a live anchor).

    Deliberately NOT ``source_scope_restricted()``-shaped: this asks the
    library question only, so it can never disable the active notebook's own
    PPR/graph/Memory channels (see ``restricted``'s docstring).  With no scope
    — or with the default whole-scope one — it costs one ContextVar read and
    an early ``return True``.
    """
    scope = current_source_scope()
    return True if scope is None else scope.covers_notebook(notebook_id)


def scoped_participants(notebook_ids: Iterable[str]) -> tuple[str, ...]:
    """Narrow an already-resolved participant list to the checked libraries.

    THE collapsing point for every collection-wide read (the map, the typed
    collection enumerations, the source-title/evidence-scope resolvers).  Those
    paths do not walk candidates one row at a time the way federated retrieval
    does — they walk *participants*, and everything they report about a
    participant (its plan, its counts, its cursor identity, its closing
    fingerprint) is derived from this one list.  Filtering it here is therefore
    the only way to satisfy the enumeration contract's hardest rule: **the rows
    and the denominator must come from one predicate**.  Filtering rows
    somewhere downstream while the count still summed every mounted library
    would make ``returned_total != total`` for a walk that in fact finished,
    which the coverage rule turns into a permanent ``concurrent_change``.

    Deliberately NOT ``resolve_participants``/``mount_sql.py``: that predicate
    is shared with **permission** checks (cross-library source proxying,
    citation resolution, asset reads), and a per-request retrieval checkbox has
    no business narrowing an authorization set.  This is a consumption-boundary
    filter over its output, applied by the two services that read whole
    collections.

    It is also deliberately NOT ``source_scope_restricted()``-shaped: a run
    that only unchecked a reference library leaves that flag False on purpose
    (R1), so gating on it would leave enumeration reading every library.
    Conversely, folding the library dimension into ``restricted`` would
    switch the whole enumeration tool off, when the correct behavior is that
    the tool stays available and its scope shrinks.

    With no scope — or with the default whole-scope one — this is one
    ContextVar read and a tuple copy.
    """
    scope = current_source_scope()
    if scope is None or not scope.base_restricted:
        return tuple(str(value) for value in notebook_ids)
    return tuple(
        str(value) for value in notebook_ids if scope.covers_notebook(str(value))
    )


def scoped_allowed_source_ids(
    notebook_id: str, explicit: Iterable[str] | None = None
) -> tuple[str, ...] | None:
    """Intersect a producer's allow-list with the active checkbox ceiling.

    HTTP requests freeze checkbox exclusions to ``include`` before entering a
    worker, so the common path always returns an allow-list that SQL/FTS can
    apply before LIMIT.  The exclude branch remains for direct service callers:
    it can narrow an existing explicit list, while result-boundary filtering
    remains the fail-closed fallback when no universe was supplied.
    """
    allowed = (
        tuple(dict.fromkeys(str(value) for value in explicit if str(value)))
        if explicit is not None else None
    )
    scope = current_source_scope()
    if scope is None:
        return allowed
    if not scope.covers_notebook(notebook_id):
        # A whole-library exclusion is an explicit deny, never "no
        # restriction": an empty tuple must reach SQL/FTS producers before
        # LIMIT. Returning None here would be misread as unbounded by any
        # downstream `if allowed:` truthiness check -- callers must branch on
        # `is not None`, exactly as the include-ceiling branch below already
        # requires.
        return ()
    if not scope.restricted or notebook_id != scope.notebook_id:
        return allowed
    if scope.mode == "include":
        if allowed is None:
            return tuple(sorted(scope.source_ids))
        ceiling = scope.source_ids
        return tuple(value for value in allowed if value in ceiling)
    if allowed is not None:
        return tuple(value for value in allowed if value not in scope.source_ids)
    return None


def _evidence_source_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("source_id") or "")
    return str(getattr(value, "source_id", "") or "")


def filter_evidence(notebook_id: str, evidence: Iterable[Any]) -> list[Any]:
    return [
        item for item in evidence
        if source_allowed(notebook_id, _evidence_source_id(item))
    ]


def filter_retrieval_items(
    active_notebook_id: str, kind: str, items: Iterable[Any]
) -> list[Any]:
    scope = current_source_scope()
    values = list(items)
    # Short-circuit only when NEITHER dimension is restricted: a run that
    # narrowed only base-library selection (local `restricted` stays False,
    # see `restricted`'s docstring) must still walk the per-item loop below,
    # or its "element"/"chunk" branches -- which already delegate to
    # `scope.allows()` -- and its "knowledge"/"relation" branch -- which calls
    # `covers_notebook()` directly -- would never see the base-library ceiling
    # applied.
    if scope is None or not (scope.restricted or scope.base_restricted):
        return values
    out: list[Any] = []
    for item in values:
        origin = str(
            (item.get("notebook_id") if isinstance(item, dict)
             else getattr(item, "notebook_id", ""))
            or active_notebook_id
        )
        if kind == "element":
            # Deliberately `active_notebook_id`, not `origin`: RetrievedElement
            # has no notebook_id field, so `origin` is always the fallback here
            # anyway. The one cross-library element path
            # (federated_retrieve_elements) never reaches this function -- it is
            # gated upstream by explicit (notebook_id, source_id) pairs taken
            # from chunk hits that already passed covers_notebook().
            if scope.allows(active_notebook_id, str(getattr(item, "source_id", "") or "")):
                out.append(item)
            continue
        if kind == "chunk":
            if scope.allows(origin, str(getattr(item, "source_id", "") or "")):
                out.append(item)
            continue
        if kind in {"knowledge", "relation"}:
            if not scope.covers_notebook(origin):
                # A node from an unchecked reference library must be DROPPED,
                # not merely stripped of its evidence: evidence_context
                # .knowledge_context() never reads hit.evidence -- it re-queries
                # node_context(origin, object_id) for the definition/snippet and
                # assigns the hit its own `k{n}` anchor. Emptying evidence would
                # leave the excluded library's content in the answer prompt and
                # citable, just untraceable.
                continue
            raw_evidence = (
                item.get("evidence", ()) if isinstance(item, dict)
                else getattr(item, "evidence", ())
            ) or ()
            evidence = filter_evidence(origin, raw_evidence)
            # "No surviving evidence" only disqualifies an ACTIVE-notebook node
            # when the LOCAL dimension is what narrowed the run. When only the
            # base dimension is narrowed, `restricted` is False, nothing local
            # was filtered, and this loop is running solely because of
            # `base_restricted` -- dropping an already-evidence-less active
            # node here would be a filtering decision the user never asked for
            # (pre-feature the whole function short-circuited).
            if (
                origin != active_notebook_id
                or not scope.restricted
                or evidence
            ):
                if isinstance(item, dict):
                    out.append({**item, "evidence": evidence})
                    continue
                try:
                    item = replace(item, evidence=evidence)
                except TypeError:
                    item.evidence = evidence
                out.append(item)
            continue
        out.append(item)
    return out


def scoped_subgraph_nodes(subgraph: Iterable[Any]) -> list[Any]:
    """Drop graph-walk triples whose node came from an unchecked library.

    Why the library gate is applied to the traversal RESULT rather than to
    ``_federated_rx_graph``'s own per-participant build loop (which is where
    every other federated loop got its skip): that graph is memoised in the
    process-wide ``_vector_cache`` under ``{active}:fed_rxgraph`` plus a
    version key derived from mutation sequence numbers only.  A scope-aware
    build would publish a library-less graph under a scope-blind key and serve
    it to every later request in the process; putting the scope INTO the key
    would instead force a full multi-million-node rebuild per checkbox
    combination.  Filtering here is cache-safe and bounded by the walk's own
    fan-out.

    An excluded library's node can therefore still act as a transit hop and
    influence WHICH allowed nodes surface, but none of its own content reaches
    the rendered context or becomes citable.

    Only the base dimension is consulted: a locally narrowed run never gets
    here at all (both callers replace the whole-graph walk with isolated
    source-bounded seeds when ``restricted``).

    ONE STATED PREMISE, because the filter fails OPEN on it: a node carrying no
    ``notebook_id`` is kept.  That is safe only because every node the walk can
    surface as content is labelled at build time — ``_federated_rx_graph``'s
    loader stamps ``notebook_id`` on each real node as it loads that
    participant, and the only unlabelled vertices are the synthetic cluster
    hubs, which ``build_rx_graph``/``multihop_subgraph`` already exclude from
    the result, the render and the verifier by ``kind``.  Fail-closed instead
    (dropping unlabelled nodes) would silently delete real evidence the day a
    producer legitimately omits the field, which is the worse failure for a
    filter whose whole job is *which* library a row came from — but it does
    mean a FUTURE producer that emits content nodes without stamping the field
    would leak through here.  A new node producer must stamp ``notebook_id``.
    """
    scope = current_source_scope()
    if scope is None or not scope.base_restricted:
        return list(subgraph)
    out: list[Any] = []
    for triple in subgraph:
        node = triple[0] if isinstance(triple, (tuple, list)) else triple
        origin = str((node or {}).get("notebook_id") or "")
        if scope.covers_notebook(origin):
            out.append(triple)
    return out


def evidence_json_allowed(notebook_id: str, raw: Any) -> bool:
    scope = current_source_scope()
    if scope is None:
        return True
    if not scope.covers_notebook(notebook_id):
        return False
    if not scope.restricted or notebook_id != scope.notebook_id:
        return True
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception:
            raw = []
    return bool(filter_evidence(notebook_id, raw or []))
