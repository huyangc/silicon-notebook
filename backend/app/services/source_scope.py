"""Request-local retrieval scope: imported sources AND mounted reference libraries.

Two INDEPENDENT dimensions, identically shaped and deliberately never folded
together:

* the LOCAL dimension (``mode``/``source_ids``) selects individual visible
  sources inside the active notebook;
* the LIBRARY dimension (``base_mode``/``base_notebook_ids``) selects whole
  mounted reference libraries.

Each dimension answers two different questions, and picking the wrong one is
silent, so both names appear in every accessor:

1. CEILING -- "must I filter candidates against the frozen id list?"  True for
   EVERY submitted scope, *including* the browser's default "everything is
   checked", because the freeze is what stops a source uploaded (or a library
   mounted) after validation from joining a run a detached worker may still be
   executing hours later.  Ask ``source_scope_ceiling_active()`` /
   ``base_scope_ceiling_active()``.
2. NARROWING -- "did the user actually shrink this dimension, so a channel must
   be switched off?"  False for a full selection: declining to narrow must not
   cost the user their PPR, private Memory, community reports or corpus
   profile.  Ask ``source_scope_restricted()`` / ``base_scope_restricted()``.

⚠ ``source_scope_restricted()`` reads the LOCAL dimension only, on purpose.  It
gates the ACTIVE notebook's own non-source-partitionable channels (PPR, private
Memory, community reports, weak-support relations, exact-section lookup, the
report corpus profile), and unchecking one borrowed reference library is not a
reason to switch any of those off.  Library-dimension consumers ask
``notebook_in_scope`` / ``scoped_participants`` / ``base_scope_*`` instead.

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
    narrowed: bool | None = None
    # The boundary also decides WHOSE hidden sources these are, and the two
    # kinds are not alike: Knowhow projections are notebook-wide, so every
    # member's ceiling admits them, while a Memory projection belongs to its
    # ``memory_items.created_by`` and only that user's ceiling may admit it.
    # That filter lives in the SQL of ``scope_source_ids`` -- another member's
    # Memory source id never reaches this dataclass -- so nothing here needs
    # (or may add) a second owner test.
    hidden_source_ids: frozenset[str] = frozenset()
    # The identity that hidden half was read for, so the drift probe can re-read
    # the same partition in the same frame.  Empty for direct service-layer
    # construction, whose ``narrowed is None`` short-circuits that comparison.
    owner_id: str = ""
    # Library dimension: ``base_mode``/``base_notebook_ids`` mirror
    # ``mode``/``source_ids`` in shape, but select whole mounted reference
    # libraries rather than individual sources within them. The neutral
    # defaults ("exclude", empty) mean every mounted base notebook
    # participates -- the historical whole-scope behavior -- so a caller that
    # never supplies a base scope observes byte-identical behavior to before
    # these fields existed.
    base_mode: str = "exclude"
    base_notebook_ids: frozenset[str] = frozenset()
    base_narrowed: bool | None = None
    # Which dimensions the caller actually SUPPLIED, as opposed to which ones
    # ended up carrying their neutral default. The two are not the same thing:
    # a run that scoped only the library dimension still gets mode="exclude" /
    # source_ids=frozenset() here, which is indistinguishable by value from a
    # submitted "all local sources" selection. Only the payload accessors
    # consult these flags -- gating (``restricted``, ``allows``,
    # ``covers_notebook``) must stay value-driven, because a neutral default
    # and an explicit "all" have to filter identically.
    #
    # They exist because ``current_source_scope_payload()`` is re-persisted by
    # report_engine.prepare_intent into the report's understanding contract and
    # re-frozen by ``_validate_source_scope`` on confirm: fabricating a local
    # scope for a base-only run would freeze it into
    # ``include:[every visible source]``, locking out sources uploaded later
    # for a user who only unchecked a reference library.
    source_provided: bool = True
    base_provided: bool = True

    @property
    def ceiling_active(self) -> bool:
        """Whether active-notebook evidence is bounded by a frozen snapshot.

        This is deliberately distinct from ``restricted``.  An API-resolved
        include-list that happened to contain the whole visible universe is
        not a narrowed run, so graph channels may stay enabled, but the frozen
        list must still constrain source-partitioned candidate generation and
        final evidence if sources change while the run is in flight.
        """
        return self.mode == "include" or bool(self.source_ids)

    @property
    def base_ceiling_active(self) -> bool:
        """Library-dimension mirror of ``ceiling_active``."""
        return self.base_mode == "include" or bool(self.base_notebook_ids)

    @property
    def restricted(self) -> bool:
        """LOCAL narrowing only (R1) -- see this module's docstring.

        Never folded together with ``base_restricted``: this drives the ACTIVE
        notebook's own PPR/graph/private-Memory/community/exact-lookup
        channels, which have nothing to do with which borrowed libraries this
        run may read.
        """
        if self.narrowed is not None:
            return self.narrowed
        # exclude [] is the UI/default representation of "all local sources"
        # and must remain byte-for-byte compatible with historical direct
        # callers whose scope predates the server-computed narrowed bit.
        return self.ceiling_active

    @property
    def base_restricted(self) -> bool:
        """Library-dimension mirror of ``restricted``, for the same reason:
        the browser's "all libraries checked" default is frozen into an
        explicit include snapshot, so shape alone cannot tell it apart from a
        real narrowing."""
        if self.base_narrowed is not None:
            return self.base_narrowed
        return self.base_ceiling_active

    def covers_notebook(self, notebook_id: str) -> bool:
        """THE single collapsing point for the library dimension: may a
        candidate whose origin is ``notebook_id`` participate at all, judged
        purely on notebook identity (never on which local sources are checked)?

        A blank/falsy ``notebook_id`` and the active notebook itself are always
        covered -- callers use "" as a stand-in for "this run's active
        notebook" (see ``filter_retrieval_items``' ``origin`` default), and the
        active notebook's own sources are gated separately by ``allows()``.
        Any other notebook is a mounted base library: it participates unless
        the library scope explicitly excludes it (mode="exclude") or fails to
        include it (mode="include").
        """
        if not notebook_id or notebook_id == self.notebook_id:
            return True
        if not self.base_ceiling_active:
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
        if not self.ceiling_active:
            return True
        if not source_id:
            return False
        if self.mode == "include":
            return source_id in self.source_ids or source_id in self.hidden_source_ids
        return source_id not in self.source_ids


_CURRENT_SOURCE_SCOPE: ContextVar[ActiveSourceScope | None] = ContextVar(
    "current_source_scope", default=None
)


def _scope_dict(scope: Any) -> dict[str, Any] | None:
    if scope is None:
        return None
    if hasattr(scope, "model_dump"):
        raw = scope.model_dump()
        # Pydantic intentionally excludes server-only hidden ids from public
        # serialization.  The live request context still needs the validated
        # snapshot carried by the model object itself.
        hidden = getattr(scope, "hidden_source_ids", None)
        if hidden is not None:
            raw["hidden_source_ids"] = list(hidden)
        owner_id = getattr(scope, "scope_owner_id", None)
        if owner_id:
            raw["owner_id"] = str(owner_id)
        return raw
    return dict(scope)


def _narrowed_flag(raw: dict[str, Any] | None) -> bool | None:
    """Read the boundary-computed narrowing fact, tolerating its absence.

    Absent for scopes built before that field existed (a report's persisted
    ``understanding`` from an earlier release) and for direct service-layer
    construction -- both must keep the historical value-driven behavior, so the
    answer there is ``None``, not ``False``.
    """
    if raw is None:
        return None
    value = raw.get("narrowed")
    return None if value is None else bool(value)


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
        source_ids=frozenset(
            str(value) for value in (raw or {}).get("source_ids") or []
        ),
        narrowed=_narrowed_flag(raw),
        hidden_source_ids=frozenset(
            str(value) for value in (raw or {}).get("hidden_source_ids") or []
        ),
        # (raw or {}):库维度加入后 raw 可以为 None（只提交了 base_scope）。
        # master 那行写 raw.get(...) 在它自己的前提下成立——它没有第二个维度,
        # 到这里 raw 必然非空。合并把两边代码放到一起,前提却没跟着合并。
        owner_id=str((raw or {}).get("owner_id") or ""),
        base_mode=str((base_raw or {}).get("mode") or "exclude"),
        base_notebook_ids=frozenset(
            str(value) for value in (base_raw or {}).get("notebook_ids") or []
        ),
        base_narrowed=_narrowed_flag(base_raw),
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
    """The LOCAL dimension as a re-persistable payload, or None when this run
    never supplied one.

    ``None`` here is load-bearing, not merely tidy: report_engine writes the
    return value into the report's ``understanding`` contract, and a fabricated
    ``exclude:[]`` would be re-frozen on confirm into
    ``include:[every visible source]`` -- freezing a local ceiling onto a
    report whose author only unchecked a reference library.
    """
    scope = current_source_scope()
    if scope is None or not scope.source_provided:
        return None
    return {
        "mode": scope.mode,
        "source_ids": sorted(scope.source_ids),
        "narrowed": scope.narrowed,
    }


def current_base_scope_payload() -> dict[str, Any] | None:
    """Mirrors ``current_source_scope_payload`` for the library dimension.

    Deliberately a separate function/shape rather than extra keys folded into
    the source payload: that payload is re-fed into ``SourceScope`` elsewhere
    and this one into ``BaseNotebookScope``, so keeping them apart avoids
    coupling either model's field names to the other.

    Symmetrically returns None when this run never supplied a base scope:
    persisting a synthesised ``exclude:[]`` would be re-frozen on confirm into
    ``include:[libraries mounted at that moment]``, silently locking a
    later-mounted reference library out of a report the user never scoped.
    """
    scope = current_source_scope()
    if scope is None or not scope.base_provided:
        return None
    return {
        "mode": scope.base_mode,
        "notebook_ids": sorted(scope.base_notebook_ids),
        "narrowed": scope.base_narrowed,
    }


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
    through many handler return paths and, for streaming, through a worker
    thread that detaches from the connection. ``background_jobs.submit``
    snapshots the caller's context, so entering this manager around
    ``start_ask_stream`` reaches the detached worker unchanged.

    NOT part of the retrieval scope. ``ActiveSourceScope`` deliberately does
    not carry it: everything on that object is consulted by a gate, and the one
    guarantee this receipt must keep is that it is consulted by none.
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
    """CHANNEL question, LOCAL dimension only (R1 -- see module docstring)."""
    scope = current_source_scope()
    return bool(scope and scope.restricted)


def source_scope_ceiling_active() -> bool:
    """FILTERING question, local dimension."""
    scope = current_source_scope()
    return bool(scope and scope.ceiling_active)


def base_scope_restricted() -> bool:
    """CHANNEL question, LIBRARY dimension: did this run really shrink the
    mounted-reference-library selection?

    ⚠ Orthogonal to ``source_scope_restricted`` and never to be folded into it:
    unchecking one borrowed library is not a reason to disable the ACTIVE
    notebook's PPR, private Memory, community reports or corpus profile.
    """
    scope = current_source_scope()
    return bool(scope and scope.base_restricted)


def base_scope_ceiling_active() -> bool:
    """FILTERING question, library dimension: is a frozen reference-library
    allow-list binding on this run's candidates?"""
    scope = current_source_scope()
    return bool(scope and scope.base_ceiling_active)


def scoped_conversation_history(history: str) -> str:
    """Prevent prior answers from crossing into a newly narrowed run.

    Consults BOTH dimensions, unlike most gates in this module, which stay
    local-only so that narrowing the library dimension never disables the
    active notebook's own channels (R1).  History is different: a prior turn's
    answer can quote content from ANY participant library, including one the
    user has just unchecked, so it is inherently a CROSS-library value rather
    than an active-notebook channel.  Gating it on the local question alone
    would let a deselected library's content ride back into the next turn's
    query-rewrite/synthesis prompt through the history -- exactly the leak this
    feature exists to close.

    Trade-off, deliberately accepted: unchecking even a single reference
    library now clears conversation history for the next turn.  That is
    preferred over a deselected library's content silently surviving in the
    prompt, and it is symmetric with the existing local-narrowing behavior.
    """
    return "" if (source_scope_restricted() or base_scope_restricted()) else history


def source_allowed(notebook_id: str, source_id: str) -> bool:
    scope = current_source_scope()
    return True if scope is None else scope.allows(notebook_id, source_id)


def notebook_in_scope(notebook_id: str) -> bool:
    """Library-dimension gate for the per-participant retrieval loops.

    Federation already walks participants one library at a time, so an
    unchecked reference library can be skipped BEFORE its query runs rather
    than having its rows dropped at the result boundary -- the boundary filter
    stays as the fail-closed backstop, this is the cost half.

    In the federated candidate loops it is therefore a COST guard, not the
    correctness gate, and the difference is worth stating because the element
    arm looks like the exception: ``RetrievedElement`` carries no
    ``notebook_id``, so ``filter_retrieval_items(..., "element", ...)`` can only
    judge it against the active notebook.  Even there the skip is not
    load-bearing -- the inner ``_retrieve_elements`` intersects the same
    per-notebook allow-list and comes back empty -- it just makes an unchecked
    library free instead of merely harmless.  Two consumers ARE
    correctness-critical and are not this shape: ``scoped_participants``
    (collection reads, where the skip decides the denominator too) and
    ``EvidenceContextService.knowledge_context`` (where the hit becomes prompt
    text and a live anchor).

    Deliberately NOT ``source_scope_restricted()``-shaped: this asks the
    library question only, so it can never disable the active notebook's own
    PPR/graph/Memory channels.  With no scope -- or with the default whole-scope
    one -- it costs one ContextVar read and an early ``return True``.
    """
    scope = current_source_scope()
    return True if scope is None else scope.covers_notebook(notebook_id)


def scoped_participants(notebook_ids: Iterable[str]) -> tuple[str, ...]:
    """Narrow an already-resolved participant list to the checked libraries.

    THE collapsing point for every collection-wide read (the collection map,
    the typed collection enumerations).  Those paths do not walk candidates one
    row at a time the way federated retrieval does -- they walk *participants*,
    and everything they report about a participant (its plan, its counts, its
    cursor identity, its closing fingerprint) is derived from this one list.
    Filtering it here is therefore the only way to satisfy the enumeration
    contract's hardest rule: **the rows and the denominator must come from one
    predicate**.  Filtering rows downstream while the count still summed every
    mounted library would make ``returned_total != total`` for a walk that in
    fact finished, which the coverage rule turns into a permanent
    ``concurrent_change``.

    Deliberately NOT ``resolve_participants``/``mount_sql.py``: that predicate
    is shared with **permission** checks (cross-library source proxying,
    citation resolution, asset reads), and a per-request retrieval checkbox has
    no business narrowing an authorization set.  This is a consumption-boundary
    filter over its output.

    It is also deliberately NOT ``source_scope_restricted()``-shaped: a run
    that only unchecked a reference library leaves that answer False on purpose
    (R1), so gating on it would leave enumeration reading every library.
    Conversely, folding the library dimension into the local question would
    switch the whole enumeration tool off, when the correct behavior is that
    the tool stays available and its scope shrinks.

    With no scope -- or with the default whole-scope one -- this is one
    ContextVar read and a tuple copy.
    """
    scope = current_source_scope()
    if scope is None or not scope.base_ceiling_active:
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

    R1 behavior restoration (audit ASK-1, P0): an all-selected freeze
    (``narrowed is False``) must return exactly what an unscoped run would
    have returned -- ``allowed`` untouched, never the materialized
    visible+hidden ceiling -- because every hot-path candidate producer
    branches on ``allowed_source_ids is not None`` to pick its fast path
    (the lexical corpus-language gate, the GiST KNN native path, un-degraded
    elements/chunks retrieval). Handing those producers an explicit tuple
    that happened to equal the whole universe silently pushed every browser
    default-selection run onto the same slow path a real narrowing requires,
    for zero narrowing benefit. ``narrowed`` is a three-state fact and this
    function must treat each state differently, on purpose:

    * ``True``  -- a real narrowing: fall through unchanged and return the
      frozen include/exclude intersection below, exactly as before this fix.
    * ``False`` -- the browser's default "everything checked" freeze: short-
      circuit here to ``allowed`` (``None`` on the common no-explicit-list
      call), so downstream producers see "no ceiling" and take the same fast
      path an unscoped run would. This only restores candidates that a real
      "no scope" run would already have produced -- it is not new leniency:
      ``source_allowed`` / ``filter_evidence`` / ``filter_retrieval_items``
      still cut final evidence down to the visible+hidden universe at the
      result boundary, and none of them call this function, so that fence is
      untouched.
    * ``None`` -- legacy/direct-construction scopes that predate the
      server-computed bit (or bypass ``_validate_source_scope`` entirely):
      ``is False`` deliberately does not match ``None``, so this state keeps
      falling through to the historical byte-identical materialized-tuple
      behavior. ``plugin_ask_engine.py`` already treats a ``None`` return
      from this function as "use the full source list" rather than
      "unbounded" -- the same fallback shape this state preserves.
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
        # LIMIT.  Returning None here would be misread as unbounded by any
        # downstream ``if allowed:`` truthiness check -- callers must branch on
        # ``is not None``, exactly as the include-ceiling branch below already
        # requires.
        return ()
    if (
        not scope.ceiling_active
        or notebook_id != scope.notebook_id
        # ``is False``, never ``not scope.narrowed``: narrowed is a three-state
        # fact and ``None`` (legacy/direct-construction scopes) must keep
        # falling through to the materialized-tuple branch below, not be
        # misread here as "not narrowed" alongside the real ``False`` case.
        or scope.narrowed is False
    ):
        return allowed
    if scope.mode == "include":
        ceiling = scope.source_ids | scope.hidden_source_ids
        if allowed is None:
            return tuple(sorted(ceiling))
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
    # Short-circuit only when NEITHER dimension has a ceiling in force: a run
    # that supplied only a base-library scope leaves the local ceiling off and
    # must still walk the per-item loop below, or its "element"/"chunk"
    # branches -- which delegate to ``scope.allows()`` -- and its
    # "knowledge"/"relation" branch -- which calls ``covers_notebook()``
    # directly -- would never see the library ceiling applied at all.
    if scope is None or not (scope.ceiling_active or scope.base_ceiling_active):
        return values
    out: list[Any] = []
    for item in values:
        origin = str(
            (item.get("notebook_id") if isinstance(item, dict)
             else getattr(item, "notebook_id", ""))
            or active_notebook_id
        )
        if kind == "element":
            # Deliberately ``active_notebook_id``, not ``origin``:
            # RetrievedElement has no notebook_id field, so ``origin`` is
            # always the fallback here anyway.  The one cross-library element
            # path (_federated_retrieve_elements_impl) skips unchecked
            # libraries in its own participant loop, where the origin is still
            # known.
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
                # assigns the hit its own ``k{n}`` anchor.  Emptying evidence
                # would leave the excluded library's content in the answer
                # prompt and citable, just untraceable.
                continue
            raw_evidence = (
                item.get("evidence", ()) if isinstance(item, dict)
                else getattr(item, "evidence", ())
            ) or ()
            evidence = filter_evidence(origin, raw_evidence)
            # "No surviving evidence" disqualifies an ACTIVE-notebook node
            # whenever the LOCAL CEILING is what emptied it.  When no local
            # ceiling is in force (a base-only run supplies no source scope, so
            # ``ceiling_active`` is False), nothing local was filtered and this
            # loop is running solely because of the library dimension --
            # dropping an already-evidence-less active node there would be a
            # filtering decision the user never asked for (before this field
            # existed the whole function short-circuited).
            if (
                origin != active_notebook_id
                or not scope.ceiling_active
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
    ``graph_retrieval._federated_rx_graph``'s own per-participant build loop
    (which is where every other federated loop got its skip): that graph is
    memoised in a process-wide cache under an ``{active}:fed_rxgraph`` key plus
    a version key derived from mutation sequence numbers only.  A scope-aware
    build would publish a library-less graph under a scope-blind key and serve
    it to every later request in the process; putting the scope INTO the key
    would instead force a full multi-million-node rebuild per checkbox
    combination.  Filtering here is cache-safe and bounded by the walk's own
    fan-out.

    An excluded library's node can therefore still act as a transit hop and
    influence WHICH allowed nodes surface, but none of its own content reaches
    the rendered context or becomes citable.  Deliberately accepted.

    Only the library dimension is consulted: a locally narrowed run never gets
    here at all (both callers replace the whole-graph walk with isolated
    source-bounded seeds when ``source_scope_restricted()``).

    ONE STATED PREMISE, because the filter fails OPEN on it: a node carrying no
    ``notebook_id`` is kept.  That is safe only because every node the walk can
    surface as content is labelled at build time -- the loader stamps
    ``notebook_id`` on each real node as it loads that participant, and the only
    unlabelled vertices are the synthetic cluster hubs, which
    ``build_rx_graph``/``multihop_subgraph`` already exclude from the result,
    the render and the verifier by ``kind``.  Failing closed instead would
    silently delete real evidence the day a producer legitimately omits the
    field -- but it does mean a FUTURE node producer must stamp
    ``notebook_id``.
    """
    scope = current_source_scope()
    if scope is None or not scope.base_ceiling_active:
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
    if not scope.ceiling_active or notebook_id != scope.notebook_id:
        return True
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception:
            raw = []
    return bool(filter_evidence(notebook_id, raw or []))


def source_scope_visible_universe_matches(
    notebook_id: str,
    current_visible_source_ids: Iterable[str],
    current_hidden_source_ids: Iterable[str] | None = None,
) -> bool:
    """Check whether an all-selected graph run still sees frozen participants.

    Narrowed runs are already unsafe for whole-graph channels.  Legacy scopes
    without the server-computed bit keep their historical behavior.  For a
    server-resolved all-selected include snapshot, any visible-source drift or
    hidden-projection drift disables non-partitioned graph/PPR/relation/exact
    channels before I/O. ``None`` keeps compatibility for bounded test doubles
    that predate the hidden-participant probe; production supplies both sets.

    Both sets must be read for the SAME identity the freeze used — the hidden
    half is owner-scoped (Memory is private to its creator), so a live read
    taken as a different user, or with no owner filter at all, would differ
    from the frozen snapshot on every request in a shared notebook and pin
    these channels off permanently.  The caller owns that: it passes
    ``ActiveSourceScope.owner_id``.
    """
    scope = current_source_scope()
    if (
        scope is None
        or notebook_id != scope.notebook_id
        or scope.narrowed is None
        or scope.narrowed
        or scope.mode != "include"
    ):
        return True
    visible_matches = set(
        str(value) for value in current_visible_source_ids
    ) == set(scope.source_ids)
    if not visible_matches or current_hidden_source_ids is None:
        return visible_matches
    return set(str(value) for value in current_hidden_source_ids) == set(
        scope.hidden_source_ids
    )
