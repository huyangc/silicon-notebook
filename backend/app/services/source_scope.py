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
   ``base_scope_ceiling_active()``.  The ceiling is ALWAYS enforced by
   materializing the frozen list below every producer's ``LIMIT``, an
   all-selected freeze included -- see ``scoped_allowed_source_ids`` for why
   "the frozen list equals the live universe, so the predicate is vacuous"
   is not true of the universe a producer actually reads.
2. NARROWING -- "did the user actually shrink this dimension, so a channel must
   be switched off?"  False for a full selection: declining to narrow must not
   cost the user their PPR, private Memory, community reports or corpus
   profile.  Ask ``source_scope_restricted()`` / ``base_scope_restricted()``.

⚠ Dimension 1 answers what a producer must FILTER by; dimension 2 answers which
LANE it should take.  They are not interchangeable, and reading "a list was
handed down" as "this run is source-restricted" is the specific mistake that
put every browser default run on the lane a real narrowing needs (audit ASK-1):
an all-selected freeze carries a list that happens to span the whole visible
universe, so it must be pushed down for safety while the routing decisions
keyed on narrowing -- the lexical corpus-language gate above all -- must keep
answering "not narrowed".  ``retrieval_candidates
._lexical_gate_source_scoped`` is where that second question is asked.

A THIRD shape rides alongside those two without belonging to either:
``notebook_source_ceilings`` freezes a source id list PER NOTEBOOK, for any
participant including the nominal active one.  It is a CEILING in exactly the
sense of question 1 above and is NEVER narrowing: a federated run that selected
every library and every source still freezes each participant's visible source
list, precisely so a concurrent upload cannot widen an in-flight run.  It must
therefore be counted wherever a gate asks "is any ceiling in force?" and must
NOT be counted where a gate asks "did the user shrink this run?" -- the same
split the two dimensions above already live by.

Riding alongside all three is a bit that is not a ceiling at all:
``subjectless`` says this run answers for a SET and none of its libraries is
the subject.  It is stated by the single manager that installs a global run,
never inferred, because the third shape above is legal for a single-notebook
run too -- one that freezes its own visible source list still has a current
library.  Gates that mean "…for the current library" read
``subjectless_run_active()``; gates that filter rows read
``peer_scope_ceiling_active()``.

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

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence


# The STORED shape of ``ActiveSourceScope.notebook_source_ceilings``: pairs of
# ``(notebook_id, frozen source ids)``, sorted by notebook id.
NotebookSourceCeilings = tuple[tuple[str, frozenset[str]], ...]


class CeilingSet(frozenset):
    """A frozen source-id set that carries its own bound SQL forms.

    The enumeration page and its count bind a source ceiling as ONE statement
    parameter (``repositories/*/source_ceiling.ceiling_param``); building that
    parameter for ~49k ids costs ~10 ms, and a run issues it for every page
    and count.  The bound form therefore lives ON the set, in
    ``bound_forms`` (one entry per backend), for exactly as long as the set
    lives -- the run's scope -- instead of in a process-level cache that would
    keep megabyte-sized ceilings of finished runs alive.  The store reads it
    by duck typing (``getattr(ceiling, "bound_forms", None)``): a plain
    frozenset still binds, just without the memo.  A frozenset cannot change,
    so a form stored on it can never describe another set; two threads that
    build the same form race benignly (one dict assignment of an immutable
    value).  Equality and hashing are ``frozenset``'s.
    """

    __slots__ = ("bound_forms",)

    def __new__(cls, iterable: Any = ()) -> "CeilingSet":
        instance = super().__new__(cls, iterable)
        instance.bound_forms = {}
        return instance


def _ceiling_pairs(value: Any) -> NotebookSourceCeilings:
    """Normalize a per-notebook ceiling argument into the canonical stored shape.

    Accepts the ergonomic ``Mapping`` form callers write as well as an already
    normalized sequence of pairs, and always returns pairs sorted by notebook
    id, so two scopes built from equal mappings compare -- and hash -- equal.

    Sorting is keyed on the notebook id ALONE.  Ordering the pairs as whole
    tuples would fall through to comparing two ``frozenset``s on a key
    collision, and ``frozenset.__lt__`` is the SUBSET predicate, not a total
    order: ``sorted`` would then produce an arbitrary, input-order-dependent
    result instead of raising.
    """
    if not value:
        return ()
    items = value.items() if hasattr(value, "items") else value
    return tuple(sorted(
        (
            (str(notebook_id), _frozen_source_ids(source_ids))
            for notebook_id, source_ids in items
        ),
        key=lambda pair: pair[0],
    ))


def _frozen_source_ids(source_ids: Any) -> "CeilingSet":
    """One notebook's ceiling as a ``CeilingSet`` (a ``frozenset[str]`` that
    carries its bound SQL forms), built at most ONCE.

    ``source_scope_context`` normalizes the ceilings and then
    ``ActiveSourceScope.__post_init__`` normalizes the same value again, so an
    unconditional rebuild copied every ceiling twice per install -- a real cost
    once a library holds tens of thousands of visible sources.  An input that is
    already a frozenset of strings is therefore reused as-is; anything else is
    coerced exactly as before (no ordering is imposed either way: membership is
    the only question a ceiling answers).  Only a ``CeilingSet`` is reused: a
    plain frozenset is copied into one, so the enumeration's bound-form memo
    (``CeilingSet.bound_forms``) lives on every installed ceiling.

    Both the check and the coercion iterate in C (``map``): a default ceiling
    passes every mounted library's set through here twice per install, and a
    per-element Python generator cost ~8 ms per 49k-id library each time.
    ``type(sid) is str`` is stricter than ``isinstance`` -- a ``str`` subclass
    is simply coerced, as any non-canonical input is.  What this function (or
    ``_ordered_source_ids``) builds is a ``_CheckedSourceIds`` -- a
    ``CeilingSet``, so it carries the enumeration's bound-form memo -- which is
    returned without re-checking: the second normalisation of every ceiling
    on each install then costs nothing (the check alone is ~0.6 ms per 49k
    ids).
    """
    if type(source_ids) is _CheckedSourceIds:
        return source_ids
    if isinstance(source_ids, CeilingSet) and set(map(type, source_ids)) <= _STR_TYPE:
        return source_ids
    return _CheckedSourceIds(map(str, source_ids))


class _CheckedSourceIds(CeilingSet):
    """A ``CeilingSet`` whose elements were coerced to ``str`` when it was
    built (only ``_frozen_source_ids`` / ``_ordered_source_ids`` build one).
    Equal to, and hashing like, the plain ``frozenset`` of the same ids; it
    keeps ``CeilingSet.bound_forms`` so an installed ceiling binds once per
    run on the enumeration paths."""

    __slots__ = ()


_STR_TYPE = frozenset({str})


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
    # submitted "all local sources" selection. No GATE consults these flags --
    # gating (``restricted``, ``allows``, ``covers_notebook``) must stay
    # value-driven, because a neutral default and an explicit "all" have to
    # filter identically.  Their readers are the two payload accessors below
    # and ``refreshed_ceiling_context`` (keeps a synthesised local dimension
    # synthesised when it inherits it).  A default ceiling's local dimension
    # binds while ``source_provided`` is False; nothing reads "not provided"
    # as "synthesise this dimension myself" any more (the extension-engine
    # path runs under ``AskService._retrieval_ceiling`` like every Ask).
    #
    # They exist because ``current_source_scope_payload()`` is re-persisted by
    # report_engine.prepare_intent into the report's understanding contract and
    # re-frozen by ``_validate_source_scope`` on confirm: fabricating a local
    # scope for a base-only run would freeze it into
    # ``include:[every visible source]``, locking out sources uploaded later
    # for a user who only unchecked a reference library.
    source_provided: bool = True
    base_provided: bool = True
    # PER-NOTEBOOK CEILING: a hard include list for ANY notebook id, the
    # nominal ``self.notebook_id`` included.  Absent means every gate below
    # behaves exactly as it did before this field existed.
    #
    # Two writers.  The federated/global run, where "the active notebook" is
    # a naming anchor with no retrieval privilege and each participant
    # carries its OWN frozen visible-source list (``mode``/``source_ids``
    # cannot express that: they are singular and bind ``self.notebook_id``
    # only).  And every single-notebook run: ``default_ceiling_context``
    # freezes each mounted library to its visible sources here, so a
    # single-notebook run is byte-identical to before only when it has no
    # mounted library (and no other member's Memory to leave out).
    #
    # STORED AS SORTED PAIRS, NOT A MAPPING, although ``__post_init__`` accepts
    # a Mapping so callers may write the obvious thing.  This dataclass is
    # ``frozen=True`` with the default ``eq=True``, so Python generates
    # ``__hash__`` from EVERY field; a ``dict`` field would make ``hash(scope)``
    # raise ``TypeError`` for scoped runs only.  No caller hashes a scope
    # today, which is exactly why the failure would surface later, in whichever
    # future cache key first tries -- and only for a federated run.  Read it
    # through ``source_ceiling_for``.
    notebook_source_ceilings: NotebookSourceCeilings = ()
    # THE EXPLICIT "this run has no subject library" BIT, and deliberately a
    # field of its own rather than something derived from the ceilings above.
    #
    # "Carries per-notebook ceilings" and "has no subject library" are NOT the
    # same fact.  A single-notebook run is allowed to freeze its own visible
    # source list through this very field set -- one entry, keyed by its own
    # ``notebook_id`` -- and that run still has a subject: the notebook the user
    # is looking at, whose private Memory, selected-source graph, index badge
    # and citation normalisation all remain correct.  Inferring "subjectless"
    # from ``peer_ceiling_active`` would silently sweep such a caller into peer
    # mode the day it appears, which is exactly the class of drift this field
    # exists to make impossible.
    #
    # Set ONLY by the one manager that installs a global run (it is the single
    # writer, pinned by a guard), and read through ``subjectless_run_active``.
    # Absent (the default) means every gate that asks the MODE question keeps
    # its historical single-library answer.
    subjectless: bool = False
    # THE PER-NOTEBOOK CEILINGS ARE TOTAL: every library other than
    # ``self.notebook_id`` that has NO entry in ``notebook_source_ceilings``
    # does not participate at all.  Without it a missing entry falls through
    # to the library dimension -- which a run that submitted none leaves open
    # -- and then to "mounted libraries are independent participants", i.e.
    # fail-OPEN.  Two real shapes reach that fall-through: a library mounted
    # after the ceilings were frozen (several graph/community readers resolve
    # the mount set live instead of through the run's memo), and, in a
    # subjectless run, a public library the user never selected (ledger E-1).
    # Honoured by ``covers_notebook`` -- and therefore by every gate that
    # collapses onto it -- plus the short-circuits that would otherwise skip
    # it.  Set by ``default_ceiling_context`` / ``refreshed_ceiling_context``;
    # ``source_scope_context(ceilings_total=True)`` accepts it from any other
    # installer.
    ceilings_total: bool = False
    # The asker's OWN Memory projection sources that ``default_ceiling_context``
    # deliberately left OUT of ``hidden_source_ids`` because the Memory channel
    # was closed (``memory_access_context(False)``).  Read in two places: the
    # drift probe (``source_scope_visible_universe_matches``) and
    # ``refreshed_ceiling_context`` (via ``_refreshed_local``), which carries
    # the set over when it inherits the local dimension.  No gate admits a
    # withheld id.
    #
    # The drift probe counts the set back in (the live hidden read is raw), so
    # a closed channel alone is NOT drift: the whole-graph walk, PPR, relation
    # and exact-lookup channels stay on and keep the asker's own Memory out by
    # the ceiling itself (PR-E2) -- the walk judges every node of the scope's
    # own notebook too (``retrieval_candidates._ceiling_scoped_subgraph``),
    # PPR applies the ceiling before its cut, exact lookup pushes it into its
    # probe, and the weak-support hint states it as a list, never by owner
    # (``RetrievalService._weak_support_viewer_form``).  The verdict binds
    # (``_ceiling_binds_uncached``): an unbounded read would include them.
    withheld_hidden_source_ids: frozenset[str] = frozenset()
    # Mounted libraries the default-ceiling constructors froze to
    # ``frozenset()`` because their visible-source list could not be read in
    # time, as ``(library id, reason code)`` pairs -- ids and codes only, no
    # content.  Not a gate (the empty ceiling already denies the library); it
    # records a failure that changes the answer, so the entry point can tell the
    # user (the single-notebook counterpart of a global run's ``dropped``
    # receipts).  Written only by ``default_ceiling_context`` /
    # ``refreshed_ceiling_context``; read through ``skipped_mounted_libraries``.
    # ``compare=False``: it describes how the freeze went, not what it admits.
    _skipped_libraries: tuple[tuple[str, str], ...] = field(
        default=(), compare=False,
    )
    # The two store probes ``run_ceiling_binds`` needs for the scope's own
    # notebook (``CeilingVerdictProbes``), handed over by the constructor
    # that built this scope from store readers.  ``None`` (every other
    # installer, every direct construction) keeps the conservative answer:
    # the ceiling binds wherever one exists.  Not a gate and not compared.
    _verdict_probes: Any = field(default=None, compare=False)
    # ``(visible ids, hidden ids)`` in the order the synthesising read
    # returned them (the store's ``ORDER BY id``), for the freeze's digest;
    # ``None`` for any other shape.  Not compared.
    _universe_read_order: Any = field(default=None, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "notebook_source_ceilings",
            _ceiling_pairs(self.notebook_source_ceilings),
        )
        if self.subjectless:
            # A subjectless scope is only coherent in ONE shape, so refuse every
            # other one at construction time rather than let a half-built scope
            # reach the gates.  Each clause below is a fact some reader depends
            # on, not tidiness:
            #
            # * ceilings must exist and must cover ``self.notebook_id`` -- the
            #   nominal active is a naming anchor with no retrieval privilege,
            #   and without its own entry ``filter_retrieval_items`` falls to
            #   "no ceiling binds here" and stops defending it entirely, while
            #   ``ask_service._peer_ceiling_participants`` (fail-closed on a
            #   missing entry) and ``allows()`` (fail-open on the same fact)
            #   would answer differently about the same library;
            # * neither the local nor the library dimension may be submitted --
            #   both express "the subject library's own selection", which a run
            #   with no subject cannot have, and a local ceiling covering the
            #   anchor is already rejected below as ambiguous.
            if not self.notebook_source_ceilings:
                raise ValueError(
                    "a subjectless scope must carry per-notebook source "
                    "ceilings"
                )
            if self.source_ceiling_for(self.notebook_id) is None:
                raise ValueError(
                    "a subjectless scope must cover its own nominal active "
                    "notebook with a per-notebook source ceiling"
                )
            if self.source_provided or self.base_provided:
                raise ValueError(
                    "a subjectless scope must not submit the local or the "
                    "library dimension"
                )
        # Reject the ambiguous combination at CONSTRUCTION time: with a local
        # mode/source_ids ceiling AND a per-notebook ceiling both covering the
        # nominal active notebook, two rules answer the same question about the
        # same library, and every reader below would have to pick one.  There
        # is no correct pick, so there must be no such object.
        if (
            self.ceiling_active
            and self.source_ceiling_for(self.notebook_id) is not None
        ):
            raise ValueError(
                "notebook_source_ceilings must not cover the active notebook "
                "while the local mode/source_ids ceiling also binds it"
            )

    def source_ceiling_for(self, notebook_id: str) -> frozenset[str] | None:
        """This notebook's frozen per-notebook ceiling, or None when it has none.

        ``None`` and ``frozenset()`` are DIFFERENT answers and every caller
        must branch on ``is not None``: the first means "no per-notebook
        ceiling binds here", the second means "this notebook is frozen to zero
        sources" -- an explicit deny.

        A linear scan rather than a dict: the list is one entry per participant
        library (bounded by the federation participant cap), and the absent
        case -- every single-notebook run -- returns on the emptiness check
        without iterating at all.
        """
        if not self.notebook_source_ceilings:
            return None
        for candidate_id, ceiling in self.notebook_source_ceilings:
            if candidate_id == notebook_id:
                return ceiling
        return None

    def source_ceiling_binds(self, notebook_id: str) -> bool:
        """Does a SOURCE ceiling bind ``notebook_id``'s evidence on this run?

        THE one answer to that question -- ``filter_retrieval_items``' knowledge
        branch, ``evidence_json_allowed`` and ``scoped_node_context_row`` all
        ask it here.  Same branch order as ``allows``: the notebook's own
        per-notebook ceiling first (``is not None`` -- ``frozenset()`` is an
        explicit deny, not "no ceiling"), then the local mode/source_ids
        ceiling, which binds only the scope's own notebook.  A peer or mounted
        library is never bound by the active notebook's checkboxes.

        A blank ``notebook_id`` is callers' stand-in for the scope's own
        notebook -- exactly as ``covers_notebook`` and ``allows`` read it -- so
        the local ceiling binds it (fail-closed).  The library dimension is not
        this question: an excluded library is answered by ``covers_notebook``
        -- and asked FIRST here: a library the scope does not cover (an
        excluded one, or one ``ceilings_total`` does not name) has no admitted
        source at all, so a ceiling binds it (fail-closed).
        """
        if not self.covers_notebook(notebook_id):
            return True
        if self.source_ceiling_for(notebook_id) is not None:
            return True
        return self.ceiling_active and (
            not notebook_id or notebook_id == self.notebook_id
        )

    @cached_property
    def _library_ceiling_memo(self) -> dict[tuple[str, Any], Any]:
        """Per-run memo behind ``library_source_ceiling`` / ``scoped_allowed_
        source_ids``: ``(notebook_id, ordered?) -> ceiling``.  ``False`` keys
        hold the frozenset, ``True`` keys the ordered tuple SQL producers bind:
        the store's own ``ORDER BY id`` order (its collation, not Python's) for
        a per-library ceiling a default-ceiling constructor read -- pre-filled
        by ``source_scope_context`` from that read -- and ``sorted`` for every
        other ceiling, computed on first use.  ``"bindable"`` keys hold the
        ``CeilingSet`` wrapper of a plain local ceiling
        (``bindable_library_ceiling``).

        A scope is frozen for the run, so each library's normalised ceiling is
        a fixed value; a whole-library include list is ~49k ids and re-deriving
        it (union + sort) cost ~8 ms per KG hit.  ``cached_property`` writes the
        instance ``__dict__`` directly, so the frozen dataclass stays frozen and
        its generated ``__eq__`` / ``__hash__`` / ``__repr__`` (fields only) are
        untouched; ``dataclasses.replace`` builds a fresh, empty memo.  The
        scope is shared by worker threads through ``copy_context()``: two
        threads may compute the same entry concurrently, and each store is a
        single dict assignment of a complete immutable value -- a benign race
        that can repeat work but never exposes a torn value.
        """
        return {}

    @cached_property
    def _frozen_universe_digests(self) -> tuple[str, str]:
        """``(visible, hidden)`` digests of this freeze, in the shape the drift
        probe's one-row store read returns (``universe_digest``): the
        submitted/synthesised include list, and the hidden half plus the ids
        withheld from it.  A pure function of frozen fields, computed at most
        once per scope -- a value, not a cached verdict: every probe still
        re-reads the live side."""
        visible_order, hidden_order = self._universe_read_order or (None, None)
        return (
            _ordered_digest(visible_order, self.source_ids),
            _ordered_digest(
                hidden_order,
                self.hidden_source_ids | self.withheld_hidden_source_ids,
            ),
        )

    @cached_property
    def _ceiling_bound_libraries(self) -> set[str]:
        """Libraries ``ceiling_binds`` answered or was flipped to "binds" on
        this run -- only ever added to (``set.add`` is atomic), so the verdict
        is monotone: a stale "does not bind" can never overwrite it."""
        return set()

    @cached_property
    def _ceiling_binds_memo(self) -> dict[str, bool]:
        """Per-run memo behind ``ceiling_binds``: ``notebook_id -> verdict``.
        Same mechanics and thread-safety argument as ``_library_ceiling_memo``."""
        return {}

    @cached_property
    def _collection_drift_memo(self) -> set[str]:
        """Libraries whose collection reads found a source outside the frozen
        ceiling on this run (``record_collection_ceiling_drift``).  Only ever
        grows; ``set.add`` of a str is atomic, so the ``_library_ceiling_memo``
        argument holds (a racing reader sees the drift now or on its next
        read, never a torn value)."""
        return set()

    def skipped_mounted_libraries(self) -> dict[str, str]:
        """``{library id: reason code}`` for each mounted library this run
        should have searched but froze to nothing because its visible-source
        list could not be read in time (``timeout`` / ``saturated`` /
        ``queue_deadline`` / ``unavailable``).

        Content-free.  A library the scope's library dimension does not admit
        is left out: the user unchecked it, so its absence is not a failure
        that changes the answer.  Empty on a healthy run and on every scope
        these constructors did not build.
        """
        return {
            library: reason for library, reason in self._skipped_libraries
            if self.covers_notebook(library)
        }

    @property
    def peer_ceiling_active(self) -> bool:
        """Whether ANY per-notebook ceiling binds on this run.

        The FILTERING question (dimension 1) for the per-notebook shape, and
        deliberately without a ``restricted`` counterpart: freezing each
        participant's current visible sources is not a narrowing, so it must
        never switch a channel off.  ``filter_retrieval_items``' short-circuit
        is where leaving it out would fail open.
        """
        return bool(self.notebook_source_ceilings)

    @property
    def ceiling_active(self) -> bool:
        """Whether active-notebook evidence is bounded by a frozen snapshot.

        This is deliberately distinct from ``restricted``.  An API-resolved
        include-list that happened to contain the whole visible universe is
        not a narrowed run, so graph channels may stay enabled, but the frozen
        list must still constrain source-partitioned candidate generation and
        final evidence if sources change while the run is in flight.  Both
        cases carry that constraint the same way -- a materialized list pushed
        below every producer's ``LIMIT`` -- because the all-selected freeze is
        not the universe an unfiltered producer would read (see
        ``scoped_allowed_source_ids``).  What narrowing changes is the LANE,
        not the ceiling.
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

        ⛔ ``peer_ceiling_active`` is NOT consulted, here or in
        ``base_restricted``.  A per-notebook ceiling freezes what each
        participant may contribute; it never expresses that the user shrank
        anything, and a federated run that selected everything must not lose
        its history, PPR, Memory or community reports to a bookkeeping freeze.
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
        if self.ceilings_total and self.source_ceiling_for(notebook_id) is None:
            # A library the frozen ceilings do not name is not a participant
            # (see ``ceilings_total``).  Placed before the library dimension so
            # an unsubmitted library scope cannot readmit it.
            return False
        if not self.base_ceiling_active:
            return True
        if self.base_mode == "include":
            return notebook_id in self.base_notebook_ids
        return notebook_id not in self.base_notebook_ids

    def allows(self, notebook_id: str, source_id: str) -> bool:
        if not self.covers_notebook(notebook_id):
            return False
        # Same branch order as ``scoped_allowed_source_ids``, for the same
        # reasons and under the same prohibition on reordering: library
        # exclusion first, the per-notebook ceiling second, the two historical
        # local-dimension branches last.  A blank ``notebook_id`` (callers' "the
        # active notebook" stand-in) never matches a per-notebook key, so it
        # keeps its historical answer.
        ceiling = self.source_ceiling_for(notebook_id)
        if ceiling is not None:
            return source_id in ceiling
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


class _PendingCeiling:
    """A default ceiling installed LAZILY (``default_ceiling_context(...,
    lazy=True)``): nothing is read until the first reader of the scope --
    ``current_source_scope()`` -- asks for it, then it is built once, by the
    same constructor an eager install runs, and every later reader (worker
    threads included: they copy this holder through ``copy_context()``) gets
    that same object.  A failure is remembered and raised to every reader:
    a retrieval that consumes the scope fails rather than running without it.
    """

    def __init__(self, build: Callable[[], "ActiveSourceScope | None"]) -> None:
        self._build = build
        self._lock = threading.Lock()
        self._done = False
        self._scope: ActiveSourceScope | None = None
        self._error: BaseException | None = None

    def resolve(self) -> "ActiveSourceScope | None":
        with self._lock:
            if not self._done:
                try:
                    self._scope = self._build()
                except BaseException as exc:
                    self._error = exc
                    raise
                finally:
                    self._done = True
        if self._error is not None:
            raise self._error
        return self._scope


_PENDING_CEILING: ContextVar[_PendingCeiling | None] = ContextVar(
    "pending_default_ceiling", default=None
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
    notebook_id: str,
    scope: Any,
    base_scope: Any = None,
    notebook_source_ceilings: Any = None,
    *,
    subjectless: bool = False,
    ceilings_total: bool = False,
    local_synthesized: bool = False,
    _withheld_hidden_source_ids: Iterable[str] = (),
    _skipped_libraries: Mapping[str, str] | None = None,
    _ceiling_read_order: Mapping[str, tuple[frozenset[str], tuple[str, ...]]]
    | None = None,
    _verdict_probes: Any = None,
    _universe_read_order: Any = None,
) -> Iterator[None]:
    """Install this run's retrieval scope, if it has one at all.

    ``ceilings_total`` sets ``ActiveSourceScope.ceilings_total``.
    ``local_synthesized`` says the LOCAL ``scope`` was built by
    ``default_ceiling_context`` rather than submitted by the caller: it binds
    exactly like a submitted include, but ``source_provided`` stays False so
    neither persistable payload reports a scope the user never chose.
    ``_withheld_hidden_source_ids`` is private to this module's constructors
    (see ``ActiveSourceScope.withheld_hidden_source_ids``): it widens the set
    the drift probe expects, so it is never read from a payload -- a key of
    that name inside ``scope`` is ignored.  ``_skipped_libraries`` (library id
    -> reason code) and ``_ceiling_read_order`` (library id -> ``(the frozenset
    passed in notebook_source_ceilings, the reader's ordered ids)``) are private
    to the same constructors: the first becomes
    ``ActiveSourceScope._skipped_libraries``, the second pre-fills
    ``_library_ceiling_memo`` (``(lib, False)`` / ``(lib, True)``) -- but only
    for a library whose effective ceiling (``_library_ceiling_uncached``) IS
    that frozenset object, so an order can never be handed out for a
    different set, nor for a library the library dimension excludes.
    ``_verdict_probes`` becomes ``ActiveSourceScope._verdict_probes``.

    ``notebook_source_ceilings`` is the third, independently optional input: a
    ``{notebook_id: source ids}`` mapping (see ``ActiveSourceScope``).  Supplying
    ONLY it builds a scope whose LOCAL and LIBRARY dimensions are both
    UNSUBMITTED -- ``mode="exclude"``/``source_ids=frozenset()`` with
    ``narrowed=None`` and ``source_provided=False``, and the mirror for the
    library half -- which is the exact representation a base-only run already
    produces today.  That matters beyond tidiness: ``narrowed=None`` keeps the
    drift probe returning True, ``ceiling_active``/``restricted`` stay False, and
    both persistable payloads keep returning ``None``.

    With all three absent no scope is installed at all, exactly as before.

    ``subjectless`` is keyword-only and is the MODE bit described on
    ``ActiveSourceScope.subjectless``: this run answers for a SET of libraries
    and none of them is its subject.  It is not inferable from the three inputs
    above (a single-notebook run may legitimately freeze its own visible source
    list), so it has to be stated, and stating it commits the caller to the one
    coherent shape -- ceilings covering every participant including the nominal
    active, and neither of the other two dimensions submitted.
    """
    raw = _scope_dict(scope)
    base_raw = _scope_dict(base_scope)
    ceilings = _ceiling_pairs(notebook_source_ceilings)
    if raw is None and base_raw is None and not ceilings and not ceilings_total:
        # Checked BEFORE the short-circuit: reaching here with ``subjectless``
        # set would otherwise install nothing at all and return silently, which
        # is the one failure mode a caller cannot notice -- every gate would
        # answer "single library" for a run that has no subject.
        if subjectless:
            raise ValueError(
                "a subjectless scope must carry per-notebook source ceilings"
            )
        yield
        return
    current = ActiveSourceScope(
        notebook_id=notebook_id,
        mode=str((raw or {}).get("mode") or "exclude"),
        source_ids=_frozen_source_ids(
            (raw or {}).get("source_ids") or ()
        ),
        narrowed=_narrowed_flag(raw),
        hidden_source_ids=_frozen_source_ids(
            (raw or {}).get("hidden_source_ids") or ()
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
        source_provided=raw is not None and not local_synthesized,
        base_provided=base_raw is not None,
        notebook_source_ceilings=ceilings,
        subjectless=subjectless,
        ceilings_total=ceilings_total,
        withheld_hidden_source_ids=frozenset(
            str(value) for value in _withheld_hidden_source_ids
        ),
        _skipped_libraries=tuple((_skipped_libraries or {}).items()),
        _verdict_probes=_verdict_probes,
        _universe_read_order=_universe_read_order,
    )
    for library, (frozen, order) in (_ceiling_read_order or {}).items():
        # Only where the library's effective ceiling IS that set: an excluded
        # library (``frozenset()`` there) or a different set never gets it.
        if frozen is not None and _library_ceiling_uncached(current, library) is frozen:
            current._library_ceiling_memo[(library, False)] = frozen
            current._library_ceiling_memo[(library, True)] = order
    token = _CURRENT_SOURCE_SCOPE.set(current)
    try:
        yield
    finally:
        _CURRENT_SOURCE_SCOPE.reset(token)


def current_source_scope() -> ActiveSourceScope | None:
    scope = _CURRENT_SOURCE_SCOPE.get()
    if scope is None:
        pending = _PENDING_CEILING.get()
        if pending is not None:
            # A lazily installed default ceiling: built on this first read.
            return pending.resolve()
    return scope


def current_skipped_mounted_libraries() -> dict[str, str]:
    """``ActiveSourceScope.skipped_mounted_libraries`` for the current run:
    ``{library id: reason code}`` of mounted libraries left out of this answer
    because they could not be read in time; ``{}`` without a scope."""
    scope = current_source_scope()
    return {} if scope is None else scope.skipped_mounted_libraries()


def current_source_scope_payload() -> dict[str, Any] | None:
    """The LOCAL dimension as a re-persistable payload, or None when this run
    never supplied one.

    ``None`` here is load-bearing, not merely tidy: report_engine writes the
    return value into the report's ``understanding`` contract, and a fabricated
    ``exclude:[]`` would be re-frozen on confirm into
    ``include:[every visible source]`` -- freezing a local ceiling onto a
    report whose author only unchecked a reference library.

    ⛔ ``notebook_source_ceilings`` is NEVER serialized here, and neither is any
    derivative of it.  The same re-persist/re-freeze path is the reason: a
    per-notebook ceiling describes OTHER libraries, so writing it into a report's
    ``understanding`` contract would freeze one library's source list onto a
    report scoped to a different one -- and a peer-only run reaches this function
    with ``source_provided=False``, so it must return exactly what an unscoped
    run returns: ``None``.
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

    ⛔ Carries no ``notebook_source_ceilings`` either, for the reason spelled out
    in ``current_source_scope_payload``.
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


def peer_scope_ceiling_active() -> bool:
    """FILTERING question, PER-NOTEBOOK dimension: does any participant carry a
    frozen source ceiling of its own on this run?

    The third of three questions this module answers, and the three are not
    interchangeable:

    * ``source_scope_ceiling_active()`` -- "is the ACTIVE notebook's own
      checkbox snapshot binding?"  One notebook, the local mode/source_ids
      pair.
    * ``base_scope_ceiling_active()`` -- "is a frozen reference-LIBRARY
      allow-list binding?"  The library dimension: which participants are in at
      all, never which sources inside one.
    * this one -- "is ANY notebook, active or peer, bounded to a frozen source
      list of its own?"  The per-notebook dimension a federated run uses to
      freeze each participant's visible sources.

    A federated run submits neither of the first two (no local checkboxes, no
    library checkboxes), so a gate that asks only those two skips its whole
    filter and every out-of-ceiling row survives.  That is why this exists as a
    peer of them rather than as something folded into either.

    ⛔ There is deliberately no ``peer_scope_restricted()``: freezing each
    participant's currently visible sources is not a narrowing and must never
    switch a channel off (see ``ActiveSourceScope.peer_ceiling_active``).

    ⚠ This is the FILTERING question and nothing else.  "Does this run have a
    subject library at all" is a different question with a different answer for
    a single-notebook run that froze its own source list, and it is asked
    through ``subjectless_run_active()``.
    """
    scope = current_source_scope()
    return bool(scope and scope.peer_ceiling_active)


def subjectless_run_active() -> bool:
    """MODE question: does this run answer for a SET, with no subject library?

    The fourth question this module answers, and the only one that is about the
    SHAPE of the run rather than about which rows survive a filter.  Every step
    whose meaning is "…for the current library" -- the private Memory channel,
    the ``index_required`` call to action, the selected-source-graph lane, the
    prompt's peer-authority rules, the workbook lane's participant list, and
    citation origin normalisation -- asks this one.  Filtering points
    (``filter_retrieval_items``, ``follow_chain``'s ceiling test) keep asking
    ``peer_scope_ceiling_active()``: a frozen source list must bind whether or
    not the run has a subject.

    Separating them is what keeps a single-notebook run that freezes its own
    visible sources out of peer mode.  Such a scope is constructible today and
    only test-reachable, but "has per-notebook ceilings" would have become
    "has no current library" for it the moment any production caller froze its
    own source list -- silently, and in the direction that folds one user's
    private Memory into a cross-library answer.

    Reads a stated bit rather than inferring one, and is deliberately NOT a
    reader of the participant override: the citation/prompt side must not
    become another way to learn which libraries a run may search, and the one
    manager that installs a global run installs both facts together under a
    loud all-or-nothing assertion, so the two cannot disagree.
    """
    scope = current_source_scope()
    return bool(scope and scope.subjectless)


def citation_active_id(notebook_id: str) -> str:
    """The id every citation producer must normalise its origin AGAINST.

    ``domain.citation_origin.foreign_notebook_id(origin, active)`` blanks an
    origin that equals ``active``, because the frontend resolves a non-empty id
    through a library-name map that includes the active notebook -- echoing it
    back badges the user's own notes 「来自「当前笔记本自己的名字」」.  That rule
    needs a notion of "the current library", and in PEER mode there is none: the
    nominal active is ``ParticipantOverride.notebook_ids[0]``, a naming anchor
    the user never singled out, and the interface has to say which of the
    selected libraries each citation came from -- including that one.

    So this returns ``""`` there, and ``foreign_notebook_id(x, "")`` passes any
    non-empty ``x`` through unchanged (its own first branch still blanks a
    missing origin), which is exactly what the pre-unification global-only
    synthesis stage achieved by hard-coding ``""`` at its single anchor site.
    Making it a shared rule is what lets the SEVEN producers that build
    cross-library citations share one answer instead of each deciding.

    Reads ``subjectless_run_active()`` rather than the participant override:
    the citation/prompt side is not on the override's reader whitelist and must
    not become an eighth way to learn which libraries a run may search.  "Does
    this run have a subject library" is a strictly weaker question that cannot
    replace any set, and the one manager that installs a global run installs
    both facts together, so the two can never disagree.

    ⛔ NOT ``peer_scope_ceiling_active()``.  Blanking the origin is only right
    when there is no current library to badge against; a single-notebook run
    that froze its own visible source list still has one, and answering "" for
    it would badge the user's own notes with their own library's name on every
    citation.
    """
    return "" if subjectless_run_active() else notebook_id


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

    ⛔ ``peer_ceiling_active`` is not consulted: this decides WHICH LIBRARIES
    participate, and a per-notebook source ceiling never removes a library --
    it bounds what the library may contribute once it is in.  Folding it in
    would make ``scoped_subgraph_nodes``/``covers_notebook``, which share this
    library-only question, start answering a source-shaped one.

    ``ceilings_total`` IS consulted, and is not the same thing: it is a
    library-shaped fact ("a library the ceilings do not name is not in"), which
    ``covers_notebook`` already answers -- skipping the loop on it would let a
    library mounted mid-run into the denominator.
    """
    scope = current_source_scope()
    if scope is None or not (scope.base_ceiling_active or scope.ceilings_total):
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

    ⛔ An all-selected freeze (``narrowed is False``) is NOT exempt, and the
    tempting argument for exempting it is wrong in two independent ways.  That
    argument runs: the frozen list equals the live universe, so filtering by it
    cannot change a candidate, so hand producers ``None`` and let every fast
    path an unscoped run takes stay on its fast path (audit ASK-1; the
    ``None``-returning form was written, reviewed, and reverted -- codex PR
    #640 R1, two P1s).  Both premises fail:

    * The frozen list is NOT the universe a producer reads.  It is
      ``visible ∪ hidden(owner)`` -- the hidden half is read *for the
      requesting user* (``SourceStore.hidden_source_ids``' ``memory_items
      .created_by`` predicate).  The universe an unfiltered producer reads is
      ``visible ∪ hidden(EVERY member)``.  In a shared notebook holding another
      member's confirmed Memory the two differ by exactly that member's
      projection, and only two producers in the whole retrieval path re-derive
      the owner predicate for themselves (``question_index_rows`` and
      ``retrieval_contribution_rows``).  The element arm and both KG arms do
      not: dropping the list lets another member's private Memory elements and
      Memory-derived KG objects compete for Top-K, which
      ``docs/product-and-api.md`` rules out in those words ("post-filtering
      alone is not authority because excluded candidates can consume Top-K").
      The drift probe cannot rescue this either -- it compares the freeze
      against ``visible`` and ``hidden(owner)``, so the foreign half it never
      saw can never make it report drift.
    * Even where the sets do coincide, they coincide *at the instant the
      question is asked*.  A source that finishes extracting between that
      instant and the producer's ``SELECT`` is in the read and not in the
      freeze, which is precisely the "concurrent uploads cannot widen an
      in-flight run" guarantee the freeze exists to provide.

    So a list is materialized here whenever the ceiling can exclude something.
    What changed (E1-2, P2-C2) is that "can it?" is now ANSWERED instead of
    assumed: ``run_ceiling_binds`` probes the two premises above -- another
    member's Memory in the notebook (``foreign_hidden``) and a universe that
    no longer digests to the freeze (``drifted``) -- once per run, plus the
    narrowed and withheld bits; only when all say "nothing to exclude" does
    the no-``explicit`` call return ``None``.  The concurrent-upload window
    after that verdict is closed by verify-on-read: a producer that read
    without the list checks its rows (``verify_unbound_read``), flips the
    verdict on the first outsider and re-runs bound.  A scope without probes
    (every installer but the store-wired default ceiling) keeps the list.

    The performance question it was trying to answer is otherwise answered
    where it belongs: producers that must
    choose a LANE (the lexical corpus-language gate above all) ask
    ``retrieval_candidates._lexical_gate_source_scoped`` -- "did this run
    genuinely narrow, so its predicate actually bounds the scan?" -- instead of
    inferring narrowing from the presence of a list.

    ``explicit`` is intersected, never passed through, because callers
    enumerate LIVE (``plugin_ask_engine`` builds its ``source_keys`` from
    ``all_visible_source_ids`` at port-construction time).  Live ∩ frozen is
    the frozen universe; live passed through is the drifted one.  This is the
    ONLY enforcement point on the plugin element path: ``RetrievedElement``
    carries no ``notebook_id``, ``filter_retrieval_items`` is not applied to
    it, and the port's own post-check judges an element by the very
    ``source_origin`` map built from that list -- so a drifted source that got
    into the list would be retrieved AND issued as evidence.
    """
    scope = current_source_scope()
    if explicit is None:
        # The common producer call: memoised per run and library, see
        # ``library_source_ceiling``.  PUSH-DOWN (E1-2): where the run's
        # verdict says the ceiling binds nothing (``run_ceiling_binds`` --
        # the scope's own notebook, not narrowed, nothing withheld, no drift,
        # no other member's Memory), producers get NO list and read what an
        # unscoped run reads; they verify it (``verify_unbound_read``).
        if scope is None or not run_ceiling_binds(scope, notebook_id):
            return None
        return _sorted_library_ceiling(scope, notebook_id)
    allowed = tuple(dict.fromkeys(str(value) for value in explicit if str(value)))
    if scope is None:
        return allowed
    if not scope.covers_notebook(notebook_id):
        # A whole-library exclusion is an explicit deny, never "no
        # restriction": an empty tuple must reach SQL/FTS producers before
        # LIMIT.  Returning None here would be misread as unbounded by any
        # downstream ``if allowed:`` truthiness check -- callers must branch on
        # ``is not None``, exactly as the include-ceiling branch below already
        # requires.  Deliberately FIRST: a library-excluded notebook denies
        # everything regardless of the local dimension's shape, so no branch
        # added below may ever be hoisted above it.
        return ()
    # ② PER-NOTEBOOK CEILING.  Its position is fixed on BOTH sides and neither
    # may be relaxed:
    #   * strictly BELOW ① -- hoisting it above the library exclusion would turn
    #     "this library does not participate at all" (``()``, an explicit deny)
    #     into "this library is frozen to its own source list", readmitting an
    #     unchecked reference library through the very branch meant to freeze
    #     the checked ones;
    #   * strictly ABOVE ③ -- ③ hands ``allowed`` straight back for every
    #     notebook that is not the scope's own, which is exactly the set of
    #     notebooks this ceiling exists to bind.  Below ③ it would be dead code
    #     for every peer library and the ceiling would silently fail open.
    ceiling = scope.source_ceiling_for(notebook_id)
    if ceiling is not None:
        # Materialized here for the same two reasons the docstring gives for the
        # local ceiling, and ``explicit`` is intersected, never passed through:
        # the federated chunk/KG legs enumerate their peer's visible sources
        # LIVE, so live ∩ frozen is the frozen universe while live passed
        # through is the drifted one.  Intersection preserves ``allowed``'s
        # order because that order is the producer's, not ours.
        return tuple(value for value in allowed if value in ceiling)
    if not scope.ceiling_active or notebook_id != scope.notebook_id:
        return allowed
    # ⛔ No ``narrowed``-shaped branch may be added here.  Narrowing decides
    # which LANE a producer takes, never whether the ceiling binds it; see the
    # docstring above and ``_lexical_gate_source_scoped``.
    if scope.mode == "include":
        ceiling = library_source_ceiling(scope, notebook_id)
        return tuple(value for value in allowed if value in ceiling)
    return tuple(value for value in allowed if value not in scope.source_ids)


def _library_ceiling_uncached(scope: ActiveSourceScope, notebook_id: str) -> frozenset[str] | None:
    """``scoped_allowed_source_ids(notebook_id)``'s answer (no ``explicit``) as a
    frozenset, computed from scratch.  Branch order is that function's ①②③ and
    is not negotiable for the same reasons given there: library exclusion
    first (``frozenset()``, an explicit deny), the per-notebook ceiling second
    (returned as stored -- it already is a frozenset, no copy), the local
    ceiling last and only for the scope's own notebook.  ``None`` = nothing to
    materialise: no ceiling binds, or the local ``exclude`` shape, which has no
    allow-list without a producer's universe."""
    if not scope.covers_notebook(notebook_id):
        return frozenset()
    ceiling = scope.source_ceiling_for(notebook_id)
    if ceiling is not None:
        return ceiling
    # A blank id is callers' stand-in for the scope's own notebook -- the same
    # reading ``source_ceiling_binds`` gives it -- so the local ceiling
    # materialises for it too (fail closed: the two never disagree on "").
    if not scope.ceiling_active or (notebook_id and notebook_id != scope.notebook_id):
        return None
    if scope.mode == "include":
        return scope.source_ids | scope.hidden_source_ids
    return None


def library_source_ceiling(
    scope: ActiveSourceScope, notebook_id: str,
) -> frozenset[str] | None:
    """The frozen source ceiling binding ``notebook_id`` on this run, normalised
    at most once per run and library (``ActiveSourceScope._library_ceiling_memo``).

    Same value as ``scoped_allowed_source_ids(notebook_id)`` as a set: callers
    that only test membership (``node_context``'s store takes a frozenset) get
    it without ordering it; SQL producers keep the ordered tuple
    (``_sorted_library_ceiling``), also memoised."""
    memo = scope._library_ceiling_memo
    key = (notebook_id, False)
    if key not in memo:
        memo[key] = _library_ceiling_uncached(scope, notebook_id)
    return memo[key]


def _sorted_library_ceiling(scope: ActiveSourceScope, notebook_id: str) -> tuple[str, ...] | None:
    """``library_source_ceiling`` as a deterministic tuple, memoised per scope.

    Despite the name, not always a Python sort: for a per-library ceiling a
    default-ceiling constructor read, ``source_scope_context`` pre-fills this
    entry with that read's own order -- the store's ``ORDER BY id`` under the
    database's collation, already materialised -- so nothing is sorted; any
    other ceiling is ``sorted`` once on first use.  Either way it never
    depends on hash order (``PYTHONHASHSEED``)."""
    memo = scope._library_ceiling_memo
    key = (notebook_id, True)
    if key not in memo:
        ceiling = library_source_ceiling(scope, notebook_id)
        memo[key] = None if ceiling is None else tuple(sorted(ceiling))
    return memo[key]


def bindable_library_ceiling(
    scope: ActiveSourceScope, notebook_id: str,
) -> frozenset[str] | None:
    """``library_source_ceiling`` as a ``CeilingSet``, so a store that binds
    it builds its bound SQL form once per run (``CeilingSet.bound_forms``,
    read by ``source_ceiling.ceiling_param`` and the exact probe).

    A per-library ceiling already is one; the local include ceiling is the
    union ``source_ids | hidden_source_ids`` -- a plain frozenset -- and is
    wrapped once per scope, in the scope's own memo (it dies with the run)."""
    ceiling = library_source_ceiling(scope, notebook_id)
    if ceiling is None or isinstance(ceiling, CeilingSet):
        return ceiling
    memo = scope._library_ceiling_memo
    key = (notebook_id, "bindable")
    if key not in memo:
        memo[key] = CeilingSet(ceiling)
    return memo[key]


def scoped_source_ceiling(notebook_id: str) -> frozenset[str] | None:
    """``library_source_ceiling`` for the current run's scope (``None`` without
    one).  ``None`` = no ceiling to push; ``frozenset()`` = deny everything."""
    scope = current_source_scope()
    return None if scope is None else library_source_ceiling(scope, notebook_id)


def source_ceiling_exists(notebook_id: str) -> bool:
    """Whether ANY source ceiling binds ``notebook_id`` on the current run
    (``ActiveSourceScope.source_ceiling_binds``) -- the conservative stand-in
    for ``ceiling_binds`` where no store probes are wired (direct service
    constructions in tests)."""
    scope = current_source_scope()
    return scope is not None and scope.source_ceiling_binds(notebook_id)


def ceiling_binds(
    scope: ActiveSourceScope,
    notebook_id: str,
    *,
    drifted: Callable[[], bool],
    foreign_hidden: Callable[[], bool],
) -> bool:
    """THE verdict "the source ceiling binds KG content of library
    ``notebook_id`` on this run" for ``node_context`` re-reads
    (``EvidenceContextService.knowledge_context``, ``RetrievalService
    .node_context``).  False → the store gets NO ceiling and the row is used as
    read: the O(1) path whose bytes (hub fused descriptions included) are the
    ones a run without a scope gets.

    True when a ceiling exists for the library (``source_ceiling_binds``) AND
    it can exclude something the store would otherwise read:

    * it denies everything to ANOTHER library (an excluded or skipped one; the
      scope's own empty freeze is judged by the arms below like any other);
    * the run is subjectless (global: each library carries its own frozen
      ceiling), or the library carries a per-notebook freeze outside peer mode
      -- nothing proves that freeze equals the library's current sources;
    * the user narrowed the scope (``restricted``, which also answers True for
      legacy scopes without the server-computed bit);
    * the sources changed after the freeze (``drifted``: the visible universe
      or the asker's hidden half no longer equals the frozen lists);
    * the library holds a hidden source the asker may not read
      (``foreign_hidden``: another member's Memory -- a frozen all-selected
      list excludes it, an unbounded read would not).

    The two probes are callables so this module stays store-free; each is
    called at most once per run and library, and the whole verdict is
    memoised on the scope (``_ceiling_binds_memo``).  Memoising is sound ONLY
    together with verify-on-read (``node_context_row_within_ceiling``).  The
    verdict is taken when the first KG hit of the run is re-read, but the
    library can change after it: a concurrent upload or Memory write can merge
    evidence of a source outside the frozen ceiling into an object recall
    already admitted, and candidate filtering cannot see a change made after
    recall.  So a False verdict never admits a row on its own: each row read
    without a ceiling is accepted only when every occurrence's ``source_id``
    and a ``defines_evidence`` definition's ``definition_source_id`` are inside
    the frozen ceiling.  The first row that is not flips the memo to True
    (``record_ceiling_drift``) -- every later hit of the run takes the bound
    path without probing again -- and that same hit is re-read and judged as
    if the verdict had been True from the start.  (Unlike
    ``_unsafe_source_scope_restricted``, which gates candidate GENERATION and
    must re-probe per call, a drift here costs nothing irreversible: the check
    runs before the row's text is used.)

    What the check cannot see: a ``cluster_description`` or ``defines_name``
    definition and ``steps`` carry no source id in the row.  The bound path
    judges the first and the last in the store (the Q1 member predicate, the
    step elements' sources) and clears the second; on a row that passed the
    check they are used as read.

    The collection-enumeration twin is ``reasoning_retrieval
    .ceiling_binds_for_run`` (passed as ``ceiling_binds`` to every enumeration
    and catalog entry point and applied by ``collection_catalog
    .CollectionCatalogService.source_ceiling``).  They agree on every arm but
    one, and deliberately: the enumeration verdict has NO ``foreign_hidden``
    arm, because listings, counts and evidence references exclude private
    Memory unconditionally by another rule (the owner-column exclusion and the
    Memory-ref drop), so another member's Memory never needs a ceiling there.
    The other arms line up: deny-all and a per-library freeze bind in
    ``source_ceiling`` whatever the verdict says; a subjectless run always
    binds; narrowed or drifted is the verdict itself.  Both are memoised only
    together with verify-on-read, each on what its reads hold: this one on a
    row's occurrences and definition source (``node_context_row_within_
    ceiling`` → ``record_ceiling_drift``); the enumeration one on the signal
    rows every plan, roster, map and fingerprint reads, on every KG row a
    fast-path page returns (the bound path's own criterion), and on the
    Knowhow complete enumeration's catalogued projection sources
    (``record_collection_ceiling_drift``).  Its drift record is separate
    because it must not inherit ``foreign_hidden``.  Two functions, not one,
    because the arms are not identical.
    """
    if not scope.source_ceiling_binds(notebook_id):
        return False
    key = notebook_id or scope.notebook_id
    # Only-increasing, like ``_collection_drift_memo``: a library once found
    # (or flipped) to bind stays bound for the run, whatever a racing thread
    # that computed "does not bind" earlier writes afterwards.
    bound = scope._ceiling_bound_libraries
    if key in bound:
        return True
    memo = scope._ceiling_binds_memo
    if key not in memo:
        # Single flight: a report's sections ask the same verdict at once;
        # one computes it (two probe reads), the others wait and read it.
        with _verdict_lock(scope):
            if key not in memo and key not in bound:
                if _ceiling_binds_uncached(
                    scope, key, drifted=drifted, foreign_hidden=foreign_hidden,
                ):
                    bound.add(key)
                    return True
                memo[key] = False
    return key in bound


_VERDICT_LOCK_GUARD = threading.Lock()


def _verdict_lock(scope: ActiveSourceScope) -> "threading.Lock":
    """The scope's own lock for computing a ``ceiling_binds`` verdict, made
    once (under a module guard, so two first callers cannot each make one)
    and kept in the instance ``__dict__`` like the scope's other per-run
    memos -- the frozen dataclass's fields, equality and hash are untouched.
    Held only while one library's verdict is computed."""
    lock = scope.__dict__.get("_verdict_lock")
    if lock is None:
        with _VERDICT_LOCK_GUARD:
            lock = scope.__dict__.setdefault("_verdict_lock", threading.Lock())
    return lock


def _ceiling_binds_uncached(
    scope: ActiveSourceScope,
    notebook_id: str,
    *,
    drifted: Callable[[], bool],
    foreign_hidden: Callable[[], bool],
) -> bool:
    ceiling = library_source_ceiling(scope, notebook_id)
    if ceiling is not None and not ceiling and notebook_id != scope.notebook_id:
        # An excluded library, or a mounted/peer one frozen to nothing.  The
        # scope's OWN empty freeze is judged like any other below: an empty
        # notebook that is not narrowed, not drifted and holds no other
        # member's Memory has nothing a read could add.
        return True
    if scope.subjectless or scope.source_ceiling_for(notebook_id) is not None:
        return True
    if notebook_id != scope.notebook_id or scope.restricted:
        return True
    if scope.withheld_hidden_source_ids:
        # The Memory channel is closed and the asker's own Memory was left out
        # of the freeze: an unbounded read would include it (E1-1).
        return True
    return bool(drifted()) or bool(foreign_hidden())


def node_context_row_within_ceiling(
    scope: ActiveSourceScope | None, notebook_id: str, row: Any,
) -> bool:
    """Verify-on-read for a row re-read WITHOUT a ceiling because
    ``ceiling_binds`` said False: is every source the row attributes its text
    to inside the frozen ceiling of ``notebook_id``?

    Checked, O(row) and without normalising anything: each occurrence's
    ``source_id``, and ``definition_source_id`` when the definition is
    ``defines_evidence`` -- exactly the ids ``scoped_node_context_row`` judges
    per item.  Membership is tested in ``library_source_ceiling``'s memoised
    frozenset (``ActiveSourceScope.allows`` for the local ``exclude`` shape,
    which materialises none); a blank id is outside, as under ``allows``.
    True → the bound path would keep all of it, so the row is used as read
    (bytes of a run without a scope).  False → the ceiling drifted after the
    verdict (a concurrent upload or Memory write merged new-source evidence into
    an already-recalled object): the caller records it (``record_ceiling_drift``)
    and re-processes the hit through the bound path.

    Vacuously True with no scope, when no source ceiling binds the library
    (``ceiling_binds`` never consulted a memo there), and for a non-dict row.
    Not visible here, because the row names no source for them: a
    ``cluster_description`` definition, a ``defines_name`` definition, and
    ``steps`` -- see ``ceiling_binds``' docstring for what that leaves.
    """
    if scope is None or not isinstance(row, dict):
        return True
    if not scope.source_ceiling_binds(notebook_id):
        return True
    ceiling = library_source_ceiling(scope, notebook_id)

    def inside(source_id: str) -> bool:
        if ceiling is not None:
            return source_id in ceiling
        return scope.allows(notebook_id, source_id)

    for occurrence in row.get("occurrences") or ():
        if not inside(_evidence_source_id(occurrence)):
            return False
    if row.get("definition") and row.get("definition_basis") == "defines_evidence":
        return inside(str(row.get("definition_source_id") or ""))
    return True


def record_ceiling_drift(scope: ActiveSourceScope, notebook_id: str) -> None:
    """Turn ``ceiling_binds``' memoised verdict for ``notebook_id`` to True for
    the rest of the run: a re-read failed ``node_context_row_within_ceiling``.
    Same key as ``ceiling_binds``, written to the only-increasing set
    (``_ceiling_bound_libraries``): once bound, no racing thread's earlier
    "does not bind" can undo it."""
    scope._ceiling_bound_libraries.add(notebook_id or scope.notebook_id)


def record_collection_ceiling_drift(scope: ActiveSourceScope, notebook_id: str) -> None:
    """The collection-read twin of ``record_ceiling_drift``: a collection read
    on the un-bound fast path (``reasoning_retrieval.ceiling_binds_for_run``
    False) saw a source outside ``notebook_id``'s frozen ceiling.  From here on
    every collection read of that library on this run binds the ceiling
    (``collection_ceiling_drifted``), and the run's verdict reads True, so the
    listing is disclosed as source-scoped.  Separate from ``_ceiling_binds_memo``
    on purpose: that verdict has a ``foreign_hidden`` arm the enumeration
    verdict must not inherit."""
    scope._collection_drift_memo.add(notebook_id or scope.notebook_id)


def collection_ceiling_drifted(scope: ActiveSourceScope, notebook_id: str) -> bool:
    """Whether ``record_collection_ceiling_drift`` fired for ``notebook_id``."""
    return (notebook_id or scope.notebook_id) in scope._collection_drift_memo


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
    #
    # ``peer_ceiling_active`` is in this disjunction for the identical reason,
    # one step further out: a federated run supplies neither a local nor a
    # library scope, so without it a run whose ONLY ceiling is per-notebook
    # would skip the whole loop and every out-of-ceiling item would survive --
    # fail-OPEN, on the one path that is the fail-closed backstop for the
    # producer-side ceilings.
    if scope is None or not (
        scope.ceiling_active
        or scope.base_ceiling_active
        or scope.peer_ceiling_active
        or scope.ceilings_total
    ):
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
            elif unbound_ceiling(active_notebook_id) is not None:
                # Read without its list and outside the freeze: the verdict
                # flips (verify-on-read, see ``verify_unbound_read``).
                record_ceiling_drift(scope, active_notebook_id)
            continue
        if kind == "chunk":
            if scope.allows(origin, str(getattr(item, "source_id", "") or "")):
                out.append(item)
            elif unbound_ceiling(origin) is not None:
                record_ceiling_drift(scope, origin)
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
            if (
                len(evidence) == len(list(raw_evidence))
                and unbound_ceiling(origin) is not None
            ):
                # The run's verdict says this library's ceiling binds nothing
                # (``run_ceiling_binds``) and the item names no source outside
                # it: kept exactly as an unscoped run keeps it -- an
                # evidence-less object included, as it always was without a
                # scope.  An item whose evidence DID lose an entry falls
                # through below: that is a source outside the freeze (or no
                # attributable source at all), so the verdict flips first.
                out.append(item)
                continue
            if len(evidence) < len(list(raw_evidence)) and unbound_ceiling(origin) is not None:
                record_ceiling_drift(scope, origin)
            # "No surviving evidence" disqualifies a node whenever A SOURCE
            # CEILING IS WHAT EMPTIED IT.  The question is asked about the
            # node's OWN library, never about the nominal active one: the
            # scope's own notebook answers it with the local mode/source_ids
            # ceiling or a per-notebook one naming itself, a peer answers it
            # with its own per-notebook ceiling, and both filter through
            # ``allows()`` -- ``ActiveSourceScope.source_ceiling_binds`` is that
            # one predicate, shared with ``scoped_node_context_row``.
            #
            # ⛔ Do NOT collapse this back to ``origin != active_notebook_id``.
            # That spelling fails OPEN for every peer: ``_federated_retrieve_
            # relations_impl`` runs an ANN/FTS relation probe that is NOT
            # source-partitioned, so a peer relation supported only by that
            # library's hidden Memory projection is recalled, has its evidence
            # emptied here by the peer's ceiling, and would then be kept with
            # empty evidence -- and ``evidence_context.knowledge_context()``
            # never reads ``hit.evidence``: it re-queries ``node_context(origin,
            # object_id)`` for the definition/snippet and mints a live ``k{n}``
            # anchor.  The same datum would get opposite verdicts depending on
            # which library it came from.
            #
            # When no source ceiling binds the origin (a base-only run supplies
            # no source scope at all), nothing source-shaped was filtered and
            # this loop is running solely because of the library dimension --
            # dropping an already-evidence-less node there would be a filtering
            # decision the user never asked for (before these fields existed
            # the whole function short-circuited).
            if evidence or not scope.source_ceiling_binds(origin):
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


_DEFINITION_FIELDS = (
    "definition", "definition_basis", "definition_source_id",
    "definition_element_id",
)


def scoped_node_context_row(
    notebook_id: str, row: dict[str, Any], *, ceiling_pushed: bool
) -> dict[str, Any] | None:
    """The retrieval layer's verdict on one ``node_context`` row.

    Shared by both consumers of a re-read row --
    ``EvidenceContextService.knowledge_context`` and
    ``RetrievalService.node_context`` (reasoning's reads) -- so the two cannot
    drift.  It lives here, below both, because it is a scope rule.

    ``row`` is what a knowledge store returned for ``notebook_id`` (the object's
    OWN library -- a peer/mounted library's object is judged by that library's
    ceiling).  ``ceiling_pushed`` says whether the caller handed the store
    ``allowed_source_ids=scoped_source_ceiling(notebook_id)``.

    Returns ``row`` itself -- same object, not a copy -- whenever no source
    ceiling binds ``notebook_id`` (``ActiveSourceScope.source_ceiling_binds``),
    so a run without a ceiling is value- and identity-identical to before.
    Under a binding ceiling:

    * ``occurrences`` are filtered through ``filter_evidence`` (idempotent
      after a store that honoured the ceiling; the only gate after one that did
      not, or when the ceiling could not be pushed -- the local ``exclude``
      shape has no materialised allow-list).
    * ``None`` = drop the WHOLE object when no occurrence survives.  That
      covers both "a non-empty list was emptied" and "the object never had
      evidence": a store that honoured the ceiling returns the first case
      already emptied, so the two are indistinguishable here, and neither can be
      attributed to an in-ceiling source.  Same rule as
      ``filter_retrieval_items``' knowledge branch -- the object's NAME is what
      renders into the prompt behind a live ``k{n}`` anchor, so emptying the
      text alone is not enough.
    * the definition is kept only when it is attributable: ``defines_evidence``
      whose ``definition_source_id`` the ceiling allows (the service-layer
      backstop against a store that ignored the kwarg), or
      ``cluster_description`` when the ceiling WAS pushed (the store applies
      the strict Q1 member predicate; it cannot be judged here).
      ``defines_name``, an unknown basis, or a cluster description the store
      never judged are cleared -- all four definition fields together, so no
      attribution survives the text.  Callers then fall back to the first
      in-ceiling occurrence.
    * ``steps`` are dropped when the ceiling was not pushed: a step carries no
      source id, so only the store can judge it.
    """
    scope = current_source_scope()
    if scope is None or not scope.source_ceiling_binds(notebook_id):
        return row
    occurrences = filter_evidence(notebook_id, row.get("occurrences") or [])
    if not occurrences:
        return None
    scoped = {**row, "occurrences": occurrences}
    basis = str(row.get("definition_basis") or "")
    if basis == "defines_evidence":
        attributable = source_allowed(
            notebook_id, str(row.get("definition_source_id") or "")
        )
    else:
        attributable = basis == "cluster_description" and ceiling_pushed
    if row.get("definition") and not attributable:
        scoped.update(dict.fromkeys(_DEFINITION_FIELDS))
    if not ceiling_pushed:
        scoped["steps"] = None
    return scoped


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

    ⚠ ONLY THE LIBRARY DIMENSION IS DECIDED HERE, and the old wording for that
    ("a locally narrowed run never gets here at all") is no longer the whole
    truth, so do not lean on it.  It described one shape -- a run that narrowed
    LOCAL sources, where both callers replace the whole-graph walk with isolated
    source-bounded seeds because ``source_scope_restricted()`` is True.  A run
    whose only ceiling is the PER-NOTEBOOK one reaches this function with
    ``restricted is False`` by deliberate design (see
    ``ActiveSourceScope.restricted``: freezing each participant's visible
    sources is not a narrowing and must not switch channels off), so the
    whole-graph walk runs and its nodes arrive here.

    This function cannot judge them.  A graph node carries
    ``{type, name, tier, notebook_id}`` and no ``source_id``, so "is this node
    supported by a source inside its library's ceiling" is a question with no
    answer in the data it receives.  It therefore answers the library question
    only and leaves the source question OPEN for the walk result.

    Who does own it: the seeds are safe --
    ``federated_retrieve``/``federated_retrieve_relations`` intersect
    ``scoped_allowed_source_ids`` per participant and are then re-checked by
    ``filter_retrieval_items``, whose knowledge/relation branch drops a node
    whose own library's ceiling emptied its evidence.  The 1-hop EXPANSION was
    not: ``render_subgraph_context`` writes each expanded node's NAME and the
    incoming edge's first evidence QUOTE into the prompt behind a live ``k{n}``
    anchor, and neither passed any source-level gate, so a peer ceiling in force
    meant that library's excluded sources reaching the prompt.  CLOSED by
    ``retrieval_candidates.CandidateRetrievalService._ceiling_scoped_subgraph``,
    which runs on this function's output inside ``_chunk_kg_overlay``'s
    non-restricted branch: it drops every node no in-ceiling source of its own
    library supports (one batched evidence read, bounded by the walk) and
    narrows each surviving edge's evidence to that ceiling.  Nothing here
    changed -- this function still answers the library question only.  Both
    production installers write per-notebook ceilings (a global run for every
    participant; ``default_ceiling_context`` for every mounted library of a
    single-notebook run), so that walk check runs on every run with a mount.

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
    # ``ceilings_total`` joins the library question for the reason given in
    # ``scoped_participants``: it removes libraries, it bounds no sources.
    if scope is None or not (scope.base_ceiling_active or scope.ceilings_total):
        return list(subgraph)
    out: list[Any] = []
    for triple in subgraph:
        node = triple[0] if isinstance(triple, (tuple, list)) else triple
        origin = str((node or {}).get("notebook_id") or "")
        if scope.covers_notebook(origin):
            out.append(triple)
    return out


def evidence_json_allowed(notebook_id: str, raw: Any) -> bool:
    """Same branch order as ``scoped_allowed_source_ids``/``allows``: library
    exclusion first, then ``ActiveSourceScope.source_ceiling_binds`` -- the one
    "does a source ceiling bind this library" predicate, per-notebook ceiling
    before the local one (a blank ``notebook_id`` is the scope's own notebook
    and is bound by the local ceiling, as in ``allows``)."""
    scope = current_source_scope()
    if scope is None:
        return True
    if not scope.covers_notebook(notebook_id):
        return False
    if not scope.source_ceiling_binds(notebook_id):
        return True
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception:
            raw = []
    return bool(filter_evidence(notebook_id, raw or []))


def _ordered_digest(order: Sequence[str] | None, ids: frozenset[str]) -> str:
    """``universe_digest(ids)`` without the sort when ``order`` is the store's
    own ``ORDER BY id`` read of exactly that set (the synthesising read; the
    length check rejects a set that lost or gained ids since -- a stripped
    hidden half, say).  ~3.7 ms instead of ~18.7 ms at 49k ids."""
    if order is None or len(order) != len(ids):
        return universe_digest(ids)
    return hashlib.md5("\x1e".join(order).encode("utf-8")).hexdigest() if order else ""


def universe_digest(source_ids: Iterable[str]) -> str:
    """THE drift-probe fingerprint of a source-id set: md5 hex of the ids in
    code-point order joined by U+001E, ``""`` for the empty set.

    The stores compute the same value over the LIVE universe in one statement
    (``SourceStore.all_visible_source_ids(..., digest_for_owner=)``:
    PostgreSQL ``md5(string_agg(id, E'\\x1e' ORDER BY id))`` over COLLATE
    "C" ids, SQLite an ordered ``group_concat`` hashed in Python), so equal
    digests mean equal sets -- ids are server-minted, not adversarial -- and a
    probe ships one row instead of every id.  Deliberately NOT ``(count,
    max(created_at))`` or ``(count, max(rowid))``: deleting one source and
    adding another with an older timestamp, or re-using a freed rowid, leaves
    both unchanged while the set changed.
    """
    ids = sorted(set(map(str, source_ids)))
    if not ids:
        return ""
    return hashlib.md5("\x1e".join(ids).encode("utf-8")).hexdigest()


def source_scope_visible_universe_matches(
    notebook_id: str,
    current_visible_source_ids: Iterable[str] | None = None,
    current_hidden_source_ids: Iterable[str] | None = None,
    *,
    current_digests: Sequence[str] | None = None,
) -> bool:
    """Check whether an all-selected graph run still sees frozen participants.

    Narrowed runs are already unsafe for whole-graph channels.  Legacy scopes
    without the server-computed bit keep their historical behavior.  For a
    server-resolved all-selected include snapshot, any visible-source drift or
    hidden-projection drift disables non-partitioned graph/PPR/relation/exact
    channels before I/O. ``None`` keeps compatibility for bounded test doubles
    that predate the hidden-participant probe; production supplies both sets.

    Production passes ``current_digests`` instead of the sets: the store's
    one-row fingerprint ``(visible digest, hidden digest)`` of the live
    universe (``universe_digest``), compared against the freeze's own digests
    (``ActiveSourceScope._frozen_universe_digests``).  Still a live read on
    every call -- only its size changed (one statement, one row, where the
    two full reads shipped ~49k ids each and cost 30-50 ms at 49k sources).

    Both halves must be read for the SAME identity the freeze used — the
    hidden half is owner-scoped (Memory is private to its creator), so a live
    read taken as a different user, or with no owner filter at all, would
    differ from the frozen snapshot on every request in a shared notebook and
    pin these channels off permanently.  The caller owns that: it passes
    ``ActiveSourceScope.owner_id``.

    ⛔ ``notebook_source_ceilings`` deliberately plays no part here, and adding
    it would be a bug in both directions.  For a PEER notebook the first
    predicate below already answers True (this probe describes the scope's own
    notebook and nothing else).  For the scope's own notebook a federated run
    carries ``narrowed is None`` -- it submitted no local scope -- so the probe
    answers True there too, and it must: a peer-only run has not narrowed
    anything, and reporting drift would pin ``_unsafe_source_scope_restricted``
    True for the whole run and reroute the lexical arm off its normal lane.
    """
    scope = current_source_scope()
    if scope is None or notebook_id != scope.notebook_id:
        return True
    return _universe_matches(
        scope, current_visible_source_ids, current_hidden_source_ids,
        current_digests,
    )


def _universe_matches(
    scope: ActiveSourceScope,
    current_visible_source_ids: Iterable[str] | None,
    current_hidden_source_ids: Iterable[str] | None,
    current_digests: Sequence[str] | None,
) -> bool:
    """``source_scope_visible_universe_matches`` for a given scope's own
    notebook (the probe body, shared with ``run_ceiling_binds``)."""
    if scope.narrowed is None or scope.narrowed or scope.mode != "include":
        return True
    if current_digests is not None:
        visible_digest, hidden_digest = (str(value) for value in current_digests)
        frozen_visible, frozen_hidden = scope._frozen_universe_digests
        return visible_digest == frozen_visible and hidden_digest == frozen_hidden
    visible_matches = set(
        str(value) for value in (current_visible_source_ids or ())
    ) == set(scope.source_ids)
    if not visible_matches or current_hidden_source_ids is None:
        return visible_matches
    # ``withheld_hidden_source_ids`` is empty unless the Memory channel was
    # closed when the default ceiling was frozen; the live read is raw.
    return set(str(value) for value in current_hidden_source_ids) == set(
        scope.hidden_source_ids | scope.withheld_hidden_source_ids
    )


def live_universe_digests(
    visible_reader: Callable[..., Any], notebook_id: str, owner_id: str,
) -> Sequence[str] | None:
    """The store's one-row drift fingerprint through ``visible_reader``
    (``SourceStore.all_visible_source_ids``), or ``None`` for a reader whose
    signature does not take ``digest_for_owner`` (bounded test doubles that
    predate it), whose callers then fall back to comparing the two full sets.
    Decided from the signature, never by catching ``TypeError``: an error
    raised inside a store that does take the keyword must surface, not
    silently turn every probe back into two full reads."""
    import inspect

    try:
        parameters = inspect.signature(visible_reader).parameters
    except (TypeError, ValueError):
        return None
    if "digest_for_owner" not in parameters and not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return None
    digests = visible_reader(notebook_id, digest_for_owner=str(owner_id or ""))
    return tuple(str(value) for value in digests)


@dataclass(frozen=True)
class CeilingVerdictProbes:
    """The two store reads behind ``run_ceiling_binds`` for a scope's own
    notebook, bound by the wiring that binds ``CeilingReaders``:

    * ``universe_digests(notebook_id, owner_id)`` -- the drift fingerprint
      (``SourceStore.all_visible_source_ids(..., digest_for_owner=)``);
    * ``foreign_hidden(notebook_id, owner_id)`` -- does the notebook hold a
      hidden source ``owner_id`` may not read (another member's Memory)?
    """

    universe_digests: Callable[[str, str], Sequence[str]]
    foreign_hidden: Callable[[str, str], bool]


def _probe_or_bind(probe: Callable[[], Any]) -> bool:
    """A verdict probe's answer, or ``True`` ("the ceiling binds") when the
    probe itself fails -- a saturated pool must not fail a result filter that
    otherwise does no I/O, and binding is the conservative answer.  A Stop and
    the participant override's control error still propagate."""
    from app.domain.retrieval_control import RetrievalControlError
    from app.services.cancellation import AskCancelled

    try:
        return bool(probe())
    except (AskCancelled, RetrievalControlError):
        raise
    except Exception:  # noqa: BLE001 - fail closed: bind
        return True


def run_ceiling_binds(scope: ActiveSourceScope, notebook_id: str) -> bool:
    """``ceiling_binds`` for this run with the probes the scope carries.

    A scope built by ``default_ceiling_context`` from production readers
    carries ``CeilingVerdictProbes``; for its own notebook the verdict is
    False -- nothing a producer could read is outside the ceiling -- when the
    run is not narrowed, no Memory was withheld, the live universe still
    digests to the freeze (``_universe_matches``) and no other member's
    Memory sits in the notebook.  Memoised per run and library, monotone
    (``_ceiling_bound_libraries``; shared with ``NodeContextCeilingVerdict``)
    and therefore sound only with verify-on-read: every producer that reads
    without the list checks what it read against the ceiling it captured
    BEFORE the read (``verify_unbound_read``, ``filter_retrieval_items``) and
    flips the verdict on the first row from outside the freeze.

    Always "binds" -- today's conservative answer, ``source_ceiling_binds`` --
    when the scope carries no probes (every installer but the store-wired
    default ceiling).  A Deep Report phase is judged like any other run: the
    pushed-down ANN lane still plans its sidecar coverage from the frozen
    ceiling in Python (``_retrieve_chunks_ann``), so a source the scale index
    has not folded yet is still recalled through ``report_delta_fallback``.
    A probe that fails answers "binds" (``_probe_or_bind``).
    """
    probes = scope._verdict_probes
    if probes is None:
        return scope.source_ceiling_binds(notebook_id)
    library = notebook_id or scope.notebook_id
    return ceiling_binds(
        scope,
        notebook_id,
        drifted=lambda: _probe_or_bind(lambda: not _universe_matches(
            scope, None, None, probes.universe_digests(library, scope.owner_id),
        )),
        foreign_hidden=lambda: _probe_or_bind(
            lambda: probes.foreign_hidden(library, scope.owner_id)
        ),
    )


def unbound_ceiling(notebook_id: str) -> frozenset[str] | None:
    """The frozen ceiling this run does NOT hand ``notebook_id``'s producers.

    ``scoped_allowed_source_ids(notebook_id)`` returns ``None`` -- no source
    list in the SQL -- when ``run_ceiling_binds`` says the ceiling binds
    nothing there; this returns that ceiling.  A producer takes it BEFORE its
    read and hands it to ``verify_unbound_read`` afterwards.  ``None`` when
    the list is handed out (or no ceiling exists): nothing to verify.  The
    verdict is monotone, so a producer that got ``None`` here cannot read
    unbound afterwards."""
    scope = current_source_scope()
    if scope is None:
        return None
    ceiling = library_source_ceiling(scope, notebook_id)
    if ceiling is None or run_ceiling_binds(scope, notebook_id):
        return None
    return ceiling


def verify_unbound_read(
    notebook_id: str,
    ceiling: frozenset[str] | None,
    source_ids: Iterable[Any],
) -> bool:
    """Verify-on-read for a producer that may have read ``notebook_id``
    without its source list.  ``ceiling`` is what ``unbound_ceiling`` gave
    the producer BEFORE the read (``None``: the read was bound, nothing to
    check); judged by membership in it, never by the verdict as it stands now
    -- a concurrent flip between the read and the check must not wave an
    outsider through.  True when every returned source is inside; the first
    one outside flips the verdict (``record_ceiling_drift``), every later
    call binds, and the caller re-runs this call bound (or drops the
    outsiders) so none takes a top-K slot."""
    if ceiling is None:
        return True
    if all(str(value or "") in ceiling for value in source_ids):
        return True
    scope = current_source_scope()
    if scope is not None:
        record_ceiling_drift(scope, notebook_id)
    return False


# ---------------------------------------------------------------------------
# The Memory channel switch (``memory:read``) and the default ceiling.
# ---------------------------------------------------------------------------

_MEMORY_CHANNEL_ALLOWED: ContextVar[bool] = ContextVar(
    "memory_channel_allowed", default=True
)


@contextmanager
def memory_access_context(allowed: bool) -> Iterator[None]:
    """Open or close the private-Memory channel for everything run inside.

    A context variable of its own rather than a field of ``ActiveSourceScope``:
    an MCP token without ``memory:read`` must close the channel for
    ``search_notebook_context`` too, and that tool installs no retrieval scope.

    TIGHTEN-ONLY.  ``memory_access_context(True)`` inside a frame that closed
    the channel leaves it closed: the frame that closed it is the one that
    knows the caller lacks the permission, and no nested helper may reopen it.
    """
    token = _MEMORY_CHANNEL_ALLOWED.set(
        bool(allowed) and _MEMORY_CHANNEL_ALLOWED.get()
    )
    try:
        yield
    finally:
        _MEMORY_CHANNEL_ALLOWED.reset(token)


def memory_channel_allowed() -> bool:
    """Whether this run may read the asker's private Memory (default True)."""
    return _MEMORY_CHANNEL_ALLOWED.get()


@dataclass(frozen=True)
class CeilingReaders:
    """The store reads ``default_ceiling_context`` needs, injected by the caller.

    This module imports no repository; each entry point hands over bound
    methods it already owns.  Production wiring (both backends implement all
    four on their ``SourceStore`` / ``NotebookStore``):

    * ``participants(notebook_id)`` -- ``participant_notebook_ids``: the active
      notebook first, then every mount that is valid right now;
    * ``visible(notebook_id)`` -- ``all_visible_source_ids``: ONE library's
      visible sources.  Deliberately one statement per library rather than the
      batched ``visible_source_ids_by_notebook``: a mounted library must fail
      on its own (see ``_mounted_library_ceilings``), and one statement per
      library was also measured cheaper for 6 libraries x 49k sources, both
      forms with their ``ORDER BY`` (SQLite 123 ms vs 168 ms, PostgreSQL
      156 ms vs 306 ms; same machine as the COST figures, warm, machine load
      24-45, 2026-09-29 fix round);
    * ``hidden(notebook_id, owner_id)`` -- ``hidden_source_ids``: the RAW
      owner-scoped hidden half (the owner's Memory projections plus the
      notebook-wide Knowhow ones), exactly what the drift probe re-reads;
    * ``memory_sources(notebook_id)`` -- ``memory_source_ids`` on a connection
      of the caller's: the notebook's Memory source ids (primary keys only),
      read only when the Memory channel is closed.

    ``emit`` receives the content-free ``default_ceiling_library_skipped``
    event (``notebook_id`` and ``reason`` only); optional, and fail-open.

    ``verdict_probes`` (optional) are the two store reads the installed scope
    keeps for ``run_ceiling_binds``: with them, an un-narrowed, undrifted run
    over a notebook holding no other member's Memory hands its producers no
    source list at all (``scoped_allowed_source_ids``); without them the
    ceiling binds wherever it exists.

    ``read_workers`` is how many mounted libraries these readers can read at
    once to advantage -- a property of the STORE behind them, so the wiring
    that binds the store states it (``_mounted_library_ceilings``).  Measured
    at six mounted libraries x 49k visible sources, paired against today's
    route freeze (2026-09-30):

    * PostgreSQL: 4 workers turn the constructor from +17.5 / -5.8 ms into
      -111 / -62 ms against today (faster in 30 and 29 of 31 pairs); pass
      ``POSTGRES_MOUNTED_READ_WORKERS``.
    * SQLite: keep 1.  ``sqlite3`` releases and re-acquires the GIL around
      every row, so concurrent reads in one process convoy on it: 6 raw
      49k-row reads took 170 ms serial, 320 ms on 4 threads and 469 ms on 6,
      and the constructor went from +16 ms to +245..+629 ms.
    """

    participants: Callable[[str], Iterable[str]]
    visible: Callable[[str], Iterable[str]]
    hidden: Callable[[str, str], Iterable[str]]
    memory_sources: Callable[[str], Iterable[str]]
    emit: Callable[[dict], Any] | None = None
    read_workers: int = 1
    verdict_probes: "CeilingVerdictProbes | None" = None


# Per-library budget for a mounted library's visible read.  The same default as
# ``GLOBAL_ASK_NOTEBOOK_TIMEOUT_SECONDS``, the per-library allowance federation
# already applies to a library read inside a global run.
DEFAULT_MOUNTED_READ_SECONDS = 5.0
# Budget for ALL mounted-library reads of one freeze together, derived once
# before the first of them.  Each library reads under ``min(this stage
# deadline, now + DEFAULT_MOUNTED_READ_SECONDS)`` -- the pairing
# ``chunk_federation._federated_tasks`` gives ``_prepared_peer`` -- so M slow
# libraries cost at most this long, not M x 5 s, before the run starts.  Two
# per-library allowances: 6 x 49k visible sources were read in 0.3-0.4 s under
# load (see the COST section of ``default_ceiling_context``), so the stage
# bound only ever bites a database that is already failing.
DEFAULT_MOUNTED_TOTAL_SECONDS = 2 * DEFAULT_MOUNTED_READ_SECONDS
# ``CeilingReaders.read_workers`` for a PostgreSQL store (see its docstring for
# the measurements).  Each concurrent read holds one pooled connection for its
# duration, so this stays well under the pool (``POSTGRES_POOL_MAX_SIZE``,
# default 10) and under federation's own fan-out bound
# (``DEFAULT_CHUNK_FANOUT_MAX_WORKERS`` = 8), which runs after this freeze,
# never at the same time.
POSTGRES_MOUNTED_READ_WORKERS = 4

# Deadline of each ceiling read's budget: PostgreSQL's default
# ``statement_timeout`` (30 s), i.e. no shorter than what a read had before.
CEILING_READ_SECONDS = 30.0


def cancellable_ceiling_readers(
    readers: "CeilingReaders | None",
    cancel_event: Any,
    *,
    seconds: float = CEILING_READ_SECONDS,
) -> "CeilingReaders | None":
    """``readers`` whose every read runs under a budget carrying ``cancel_event``.

    ``default_ceiling_context`` reads the ACTIVE notebook (participants, its
    visible set, its hidden half, its Memory sources) with no ``read_budget``
    of its own, so without this a Stop pressed during those reads waited for
    them to finish.  Each call here enters ``read_budget(now + seconds,
    cancel_event)``: on SQLite the progress handler interrupts the statement
    as soon as the event is set; on PostgreSQL the budget is checked before
    each statement and a budgeted connection caps every statement at
    ``postgres_chunk_fts_timeout_seconds`` (3 s by default), so a Stop waits
    at most that long instead of up to the 30 s ``statement_timeout``.  (The
    same cap already applies to each mounted library's read, which runs under
    a budget inside the constructor; nested budgets only get shorter.)  The
    active notebook's reads measure ~11 ms of server time at 49k sources, so
    the 3 s cap is kept rather than widened in the PostgreSQL adapter.

    Used by every entry point that has a cancel event: the report worker and
    its refresh (``report_execution``, ``report_engine``) and the Ask service
    (``AskService._retrieval_ceiling``).

    This only makes the read stoppable; it does not decide what an
    interrupted read means.  The constructor still gets ``cancel_event`` and
    turns an interrupted MOUNTED read into ``AskCancelled`` (rather than a
    skipped library); the entry point turns an interrupted ACTIVE-notebook
    read into ``AskCancelled`` (rather than a failed report or ask).  ``None``
    readers or no cancel event -> ``readers`` unchanged.
    """
    if readers is None or cancel_event is None:
        return readers
    from app.repositories.read_budget import read_budget

    def bounded(read: Callable) -> Callable:
        def call(*args):
            with read_budget(time.monotonic() + float(seconds), cancel_event):
                return read(*args)
        return call

    probes = readers.verdict_probes
    return replace(
        readers,
        participants=bounded(readers.participants),
        visible=bounded(readers.visible),
        hidden=bounded(readers.hidden),
        memory_sources=bounded(readers.memory_sources),
        # The run verdict's two reads happen later, during retrieval; a Stop
        # must reach them too.
        verdict_probes=None if probes is None else CeilingVerdictProbes(
            universe_digests=bounded(probes.universe_digests),
            foreign_hidden=bounded(probes.foreign_hidden),
        ),
    )


def partition_memory_sources(
    source_ids: Iterable[str],
    memory_source_ids: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split source ids into ``(non-Memory, Memory)``, order preserved.

    THE ceiling side's spelling of "which of these sources are Memory": the
    membership test against the notebook's Memory source ids
    (``SourceStore.memory_source_ids``, the store's single definition of that
    set).  Pure -- the caller performs the one read.
    """
    memory_ids = frozenset(str(value) for value in memory_source_ids)
    kept: list[str] = []
    memory: list[str] = []
    for source_id in dict.fromkeys(str(value) for value in source_ids if value):
        (memory if source_id in memory_ids else kept).append(source_id)
    return tuple(kept), tuple(memory)


def _without_memory(
    local: Any,
    readers: CeilingReaders,
    notebook_id: str,
    withheld: Iterable[str] = (),
) -> tuple[Any, tuple[str, ...]]:
    """The local dimension as the Memory channel allows it, plus withheld ids.

    Channel open -> ``local`` unchanged.  Channel closed -> every Memory source
    is removed from BOTH halves of the include list, whether the list was
    synthesised or submitted: the constructor must not rely on "no entry point
    submits a scope while closing the channel".  The Memory ids removed from
    the hidden half are returned as withheld, so the drift probe -- which
    re-reads the raw owner-scoped hidden set -- keeps matching; none removed
    from the visible half need recording (a Memory source is never in the
    live visible set the probe compares against).  One ``memory_sources`` read,
    and only when there is something to classify.

    An EXCLUDE-form local scope cannot be bounded this way -- it admits every
    source it does not name, other members' Memory included -- so with the
    channel closed it is refused rather than run.  Every entry point freezes an
    include (routes via ``_validate_source_scope``, the constructor itself), so
    this only fails a caller that would otherwise run unbounded.
    """
    if local is None or memory_channel_allowed():
        return local, tuple(withheld)
    raw = _scope_dict(local) or {}
    if str(raw.get("mode") or "exclude") != "include":
        raise ValueError(
            "an exclude-form local scope cannot be bounded while the Memory "
            "channel is closed"
        )
    source_ids = raw.get("source_ids") or ()
    hidden = raw.get("hidden_source_ids") or ()
    if not source_ids and not hidden:
        return raw, tuple(withheld)
    memory = frozenset(str(value) for value in readers.memory_sources(notebook_id))
    if memory.isdisjoint(source_ids) and memory.isdisjoint(hidden):
        # Nothing to strip: keep the caller's (possibly canonical) sets as-is.
        return raw, tuple(withheld)
    kept_visible, _ = partition_memory_sources(source_ids, memory)
    kept_hidden, removed = partition_memory_sources(hidden, memory)
    return (
        {**raw, "source_ids": kept_visible, "hidden_source_ids": kept_hidden},
        tuple(dict.fromkeys((*withheld, *removed))),
    )


def _emit_ceiling_event(readers: CeilingReaders, event: dict) -> None:
    if readers.emit is None:
        return
    try:
        readers.emit(event)
    except Exception:  # noqa: BLE001 - observability is fail-open
        pass


def _effective_cancel_event(cancel_event: Any) -> Any:
    """The caller's cancel token, else the one on the read budget it holds.

    A caller that passes no ``cancel_event`` but runs inside a cancellable
    ``read_budget`` has still asked to be stoppable: ``read_budget`` already
    inherits that token, so a Stop interrupts the mounted read in flight, and
    without this fallback the resulting driver error would be recorded as a
    skipped library (``timeout``) instead of propagating as the user's stop.
    """
    if cancel_event is not None:
        return cancel_event
    from app.repositories.read_budget import current_read_budget

    budget = current_read_budget()
    return None if budget is None else budget.cancel_event


@dataclass
class _MountedCeilings:
    """What ``_mounted_library_ceilings`` froze, in the shapes
    ``source_scope_context`` takes: the per-library ceilings, each ceiling's
    ``(frozenset, reader order)`` for the hand-out, and the skipped libraries
    with their reason codes."""

    ceilings: dict[str, frozenset[str]] = field(default_factory=dict)
    read_order: dict[str, tuple[frozenset[str], tuple[str, ...]]] = field(
        default_factory=dict,
    )
    skipped: dict[str, str] = field(default_factory=dict)


def _ordered_source_ids(values: Iterable[str]) -> tuple[tuple[str, ...], frozenset[str]]:
    """A reader's list as ``(ids in the reader's order, the same ids as a
    frozenset)``, each built once and in C.  Duplicates -- which the production
    read of primary keys cannot return -- keep their first position."""
    order = tuple(map(str, values))
    frozen = _CheckedSourceIds(order)
    if len(frozen) != len(order):
        order = tuple(dict.fromkeys(order))
    return order, frozen


def _mounted_library_ceilings(
    libraries: Iterable[str],
    readers: CeilingReaders,
    cancel_event: Any,
    seconds: float,
    total_seconds: float,
    workers: int = 1,
) -> _MountedCeilings:
    """Each mounted library's VISIBLE sources, at most ``workers`` at a time.

    Every read runs under its OWN ``read_budget`` ending at ``min(stage
    deadline, now + seconds)``, the stage deadline being ``total_seconds``
    after the reads start (and each nested inside any budget the caller
    already holds -- the caller's context is copied into each worker -- so it
    can only get shorter).  A library whose read fails or exceeds its budget
    -- or whose turn comes after the stage deadline, when it is not read at
    all (``queue_deadline``) -- is frozen to ``frozenset()``, an explicit
    deny, with a content-free event, instead of failing the request.  That is
    the fault isolation federation already gives a library it cannot
    enumerate (``chunk_federation_skipped``), and it is fail-CLOSED: the
    library is not searched at all, never searched without a ceiling.  The
    skip is also recorded with its reason (``_MountedCeilings.skipped``), so
    the installed scope can report it (``skipped_mounted_libraries``).  A
    healthy library keeps the reader's order for the hand-out.

    Bounded parallelism (``_run_bounded``): up to ``workers`` libraries are
    read at once, each on its own connection, so M healthy libraries cost
    about the slowest wave rather than the sum; a library's "turn" is the
    moment a worker picks it up.  Results and events are assembled in the
    order ``libraries`` was given, whatever order the reads finished in.

    Reason codes: ``queue_deadline`` (no turn before the stage deadline);
    else whatever ``classify_read_failure`` names (``timeout`` for the budget
    or a driver's interrupt, ``saturated`` for a pool lease); an unclassified
    error counts as ``timeout`` once this library's deadline has passed (a
    driver may surface the interrupt as its own generic error) and as
    ``unavailable`` before it.

    A stop is not a library failure: cancellation (the caller's token, else the
    one on its read budget) is checked before each read and again after a
    failed one, and ``AskCancelled`` (like the participant override's control
    error, which an injected reader may raise) propagates -- before any event
    is emitted.
    """
    from app.domain.retrieval_control import RetrievalControlError
    from app.repositories.read_budget import classify_read_failure, read_budget
    from app.services.cancellation import AskCancelled, raise_if_cancelled

    cancel_event = _effective_cancel_event(cancel_event)
    libraries = tuple(libraries)
    stage_deadline = time.monotonic() + float(total_seconds)

    def read_one(library: str) -> tuple[Any, str | None]:
        raise_if_cancelled(cancel_event)
        started = time.monotonic()
        deadline = min(stage_deadline, started + float(seconds))
        try:
            if started >= stage_deadline:
                raise _StageDeadline()
            with read_budget(deadline, cancel_event):
                return _ordered_source_ids(readers.visible(library)), None
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:  # noqa: BLE001 - one library must not fail the run
            raise_if_cancelled(cancel_event)
            return None, (
                "queue_deadline" if isinstance(exc, _StageDeadline)
                else classify_read_failure(exc) or (
                    "timeout" if time.monotonic() >= deadline
                    else "unavailable"
                )
            )

    result = _MountedCeilings()
    for library, (read, reason) in zip(
        libraries, _run_bounded(read_one, libraries, workers),
    ):
        if reason is None:
            order, frozen = read
            result.ceilings[library] = frozen
            result.read_order[library] = (frozen, order)
            continue
        result.ceilings[library] = frozenset()
        result.skipped[library] = reason
        _emit_ceiling_event(readers, {
            "kind": "default_ceiling_library_skipped",
            "notebook_id": library,
            "reason": reason,
        })
    return result


def _read_workers(readers: CeilingReaders, override: int | None) -> int:
    """The concurrency for this freeze's mounted reads: the caller's explicit
    ``mounted_read_workers`` if given, else what the readers' store states."""
    return max(1, int(readers.read_workers if override is None else override))


def _run_bounded(
    fn: Callable[[str], Any], items: Sequence[str], workers: int,
) -> list[Any]:
    """``[fn(item) for item in items]``, up to ``workers`` at a time.

    Inline (no thread) for a single item or ``workers <= 1``.  Otherwise an
    executor owned by this call -- the pattern of
    ``chunk_federation._run_tasks`` without a plan -- each submission running
    in a COPY of the caller's context (the read budget, the request user),
    since two threads cannot share one context.  The first exception
    propagates in item order; items not yet started are cancelled and the
    ones in flight are awaited (each is bounded by its own read budget), so
    no read outlives this call.
    """
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    if len(items) <= 1 or workers <= 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(
        max_workers=min(int(workers), len(items)),
        thread_name_prefix="default-ceiling-read",
    ) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, fn, item)
            for item in items
        ]
        try:
            return [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            raise


class _StageDeadline(Exception):
    """This library's turn came after the stage deadline: nothing was read."""


@contextmanager
def default_ceiling_context(
    notebook_id: str,
    owner_id: str,
    readers: CeilingReaders,
    *,
    local_scope: Any = None,
    base_scope: Any = None,
    cancel_event: Any = None,
    mounted_read_seconds: float = DEFAULT_MOUNTED_READ_SECONDS,
    mounted_total_seconds: float = DEFAULT_MOUNTED_TOTAL_SECONDS,
    mounted_read_workers: int | None = None,
    lazy: bool = False,
) -> Iterator[None]:
    """Install the retrieval ceiling EVERY entry point runs under.

    ``lazy=True`` installs it without reading anything: the first
    ``current_source_scope()`` inside builds it (``_PendingCeiling``), exactly
    as an eager install would have, and a block that never consumes the scope
    -- the intent precheck, one model call with no retrieval dependency --
    pays no read at all.

    There is no "no scope = no ceiling": an unscoped run used to install
    nothing, and every producer without an owner predicate of its own (the
    element arm, both KG arms, relations, community neighbours, derivation
    chains) then read ``visible ∪ hidden(EVERY member)`` -- another member's
    private Memory included -- and every mounted library's hidden projections.

    1. An OUTER scope is already installed (a global run, a nested call such as
       the report engine inside its worker) -> pass through untouched and read
       nothing.  The subjectless bit therefore keeps its single writer.
    2. LOCAL dimension.  ``local_scope`` submitted (the route already froze an
       include) -> used as-is, own hidden half and all (a narrowed selection
       deliberately carries no hidden sources).  Omitted -> synthesised as
       ``include: visible(notebook)``
       with ``hidden_source_ids = hidden(notebook, owner)``, ``narrowed=False``
       and ``owner_id``, but ``source_provided=False``: it binds exactly like the
       browser's all-selected freeze (``ceiling_active`` True, ``restricted``
       False) while ``current_source_scope_payload()`` keeps returning None, so
       the report ``understanding`` contract persists nothing the user never
       chose.  With the Memory channel closed, Memory sources are removed from
       the include list in BOTH cases -- synthesised or submitted -- and those
       taken from the hidden half are recorded in
       ``withheld_hidden_source_ids`` for the drift probe (``_without_memory``;
       an exclude-form submission is refused there).
    3. LIBRARY dimension.  ``base_scope`` submitted -> as-is; omitted ->
       unsubmitted (``base_provided=False``), no library is filtered by it.
    4. PER-LIBRARY CEILINGS.  Every mounted participant other than
       ``notebook_id`` that the library dimension admits is frozen to its
       VISIBLE sources only -- a mounted library's Memory/Knowhow projections
       belong to its own members -- and ``ceilings_total`` is set, so a library
       mounted after this freeze, or one the library dimension excludes (it
       is never read), participates in nothing.  Up to
       ``mounted_read_workers`` (default: ``readers.read_workers``) mounted
       libraries are read at once, each under
       its own budget -- ``min(stage deadline, now + mounted_read_seconds)``,
       the stage deadline being ``mounted_total_seconds`` after the mounted
       reads start -- and
       ``cancel_event`` (else the cancel token of the read budget the caller
       holds); one whose read fails, overruns, or never gets a turn before the
       stage deadline is frozen to ``frozenset()`` (deny) with a content-free
       ``default_ceiling_library_skipped`` event, and recorded on the scope
       (``current_skipped_mounted_libraries()``: library id -> reason code) so
       the entry point can put it in the answer's result notice -- a missing
       mounted library changes the answer.  A Stop propagates as
       ``AskCancelled``, never as a skipped library.  The ACTIVE notebook's own
       reads are not isolated: if they fail the request fails -- a run never
       proceeds unscoped.  ``chunk_federation._peer_visible_sources`` hands the
       frozen ceiling back to every federated leg, in this read's order, so
       each mounted library's visible set is read once per run, here.

    COST.  Reader calls: a synthesised local dimension makes 3 + M (the
    participants, the notebook's visible set, its hidden half, and one visible
    read per mounted library the library dimension admits, M), plus 1
    ``memory_sources`` read when the Memory channel is closed; a submitted one
    makes 1 + M.  None of that depends on the number of SOURCES, and M is
    bounded by the mount set.  Bytes and time do not share that property: both
    grow linearly with the total number of visible sources across the notebook
    and its mounted libraries, because each library's ceiling is materialised
    once as a ``frozenset`` plus the reader-order tuple
    ``_peer_visible_sources`` hands out (no sort; together ~1.4-2 ms per 49k
    ids).  The M mounted reads run ``readers.read_workers`` at a time (4 on
    PostgreSQL, 1 on SQLite -- see ``CeilingReaders``).  Worst case before the
    run starts: the active notebook's reads plus ``mounted_total_seconds``.

    Measured against TODAY's route-level freeze in the same process
    (``ask_routes._validate_source_scope`` on the all-selected scope plus its
    install, plus the one live visible read per mounted library a
    single-library run pays through federation's run memo), paired: route and
    constructor alternate 31 times after a warm-up and the median of the
    per-pair difference is reported.  Conditions: Apple M5 Max, 18 cores,
    64 GB; local PostgreSQL 16.15; Python 3.14.6; 49k visible sources per
    library (ids of 43 ASCII characters) plus 300 Knowhow and 50 Memory
    sources in the notebook; machine load 15-45 from other work (2026-09-30).
    ``all_visible_source_ids`` and ``hidden_source_ids`` carry ``ORDER BY
    id``; ``memory_source_ids`` and the participant read do not.  Constructor
    minus route, SQLite / PostgreSQL:

    * no mount: -9.5 ms (38 vs 28) / -42 ms (106 vs 52); 2 vs 3 reads;
    * one mount: -8.4 ms (68 vs 60) / -25 ms (102 vs 82); 3 vs 4 reads;
    * six mounts, SQLite serial: +16.2 / +16.8 ms at load ~20 (route 171,
      constructor 184-186), +33.7 ms at load ~44; PostgreSQL with 4 workers:
      -111 / -62 / -109 ms (faster in 30, 29, 28 of 31 pairs), where serially
      it measured +17.5 / -5.8 ms.  8 vs 9 reads.  Before the third fix round
      (sorted hand-out, SQLite progress handler every 1 000 VM steps) the same
      pairing measured +210 to +290 ms / +91 ms.
    * peak allocation (second fix round, not re-measured): 10.6 / 16.7 /
      38.2 MB vs 9.7 / 15.8 / 45.9 MB (SQLite), 13.7 / 20.6 / 42.2 MB vs
      13.7 / 19.8 / 49.9 MB (PostgreSQL).

    So with six mounted libraries PostgreSQL is now cheaper than today's route
    freeze, and SQLite still costs ~16 ms more (more under load): the ceiling
    itself -- six 49k-id frozensets and hand-out tuples, ~10-12 ms measured
    alone -- plus the participant read (~1 ms); the route freeze never
    materialises a mounted library's set, and SQLite cannot win it back by
    reading in parallel (see ``CeilingReaders.read_workers``).  With fewer
    mounts both are cheaper.  On SQLite the per-library ``read_budget`` adds
    little GC work: with the progress handler every 100 000 VM steps
    (``_READ_BUDGET_VM_STEPS``) six budgeted 49k-row reads run ~16 young
    collections (unbudgeted: ~5; every 1 000 steps: ~208) and take about as
    long as unbudgeted ones.

    Once installed, runs that had no scope before (MCP ``ask_notebook``,
    unscoped API asks, the report worker) start checking for drift
    (``source_scope_visible_universe_matches``, asked before the whole-graph,
    PPR, relation and exact-lookup channels): each check is one single-row
    fingerprint read (``live_universe_digests``) compared with the freeze's
    own digest, never cached (codex #634).  Measured at 49k sources, load
    7-8: 9-23 ms per read on SQLite and 5-13 ms on PostgreSQL in an MCP
    question, 11-16 / 6-10 ms inside a report phase (sections check
    concurrently and wait for the GIL; up to ~130 / ~50 ms under load
    20-36).  An MCP chunk question makes 3 checks, with a rerank 6, a
    reasoning question 7-10; a 6-section report 3 while planning and 31
    while generating.  A run whose list is pushed down skips the lexical
    lane's routing check (``_lexical_gate_drift``): with no list it cannot
    change anything.
    On top comes the run verdict (``run_ceiling_binds``: one fingerprint read
    and one foreign-Memory read per run and library), which lets a run that
    cannot exclude anything read without the list.  What the checks buy:
    while a notebook is ingesting, a source that finishes after the freeze
    reads as drift, and those channels -- which are not partitioned by
    source -- are switched off for that question instead of admitting a
    source outside the freeze, which is what the browser path already does.
    A closed Memory channel is not drift: those channels stay on and keep the
    withheld sources out by the ceiling (see ``withheld_hidden_source_ids``).
    """
    if current_source_scope() is not None:
        yield
        return
    fresh = dict(
        local_scope=local_scope, base_scope=base_scope,
        cancel_event=cancel_event, mounted_read_seconds=mounted_read_seconds,
        mounted_total_seconds=mounted_total_seconds,
        mounted_read_workers=mounted_read_workers,
    )
    if not lazy:
        with _fresh_default_ceiling(notebook_id, owner_id, readers, **fresh):
            yield
        return

    def build() -> ActiveSourceScope | None:
        # Built where it is first read, by the eager constructor; the
        # holder is cleared while building so nothing it calls can re-enter.
        from app.services.cancellation import AskCancelled, raise_if_cancelled

        token = _PENDING_CEILING.set(None)
        try:
            with _fresh_default_ceiling(notebook_id, owner_id, readers, **fresh):
                return _CURRENT_SOURCE_SCOPE.get()
        except AskCancelled:
            raise
        except Exception:
            # A read the Stop interrupted is the Stop, as for an eager install.
            raise_if_cancelled(cancel_event)
            raise
        finally:
            _PENDING_CEILING.reset(token)

    token = _PENDING_CEILING.set(_PendingCeiling(build))
    try:
        yield
    finally:
        _PENDING_CEILING.reset(token)


@contextmanager
def _fresh_default_ceiling(
    notebook_id: str,
    owner_id: str,
    readers: CeilingReaders,
    *,
    local_scope: Any,
    base_scope: Any,
    cancel_event: Any,
    mounted_read_seconds: float,
    mounted_total_seconds: float,
    mounted_read_workers: int | None = None,
) -> Iterator[None]:
    """Steps 2-4 of ``default_ceiling_context``, whatever is installed outside.

    Only participants the library dimension admits are read (the same
    ``_library_admits`` predicate the refresh uses): an excluded library would
    otherwise spend a read -- and, when slow, the stage budget a selected
    library needed -- on a ceiling no gate ever consults.
    """
    from app.services.cancellation import raise_if_cancelled

    cancel_event = _effective_cancel_event(cancel_event)
    raise_if_cancelled(cancel_event)
    admits = _library_admits(notebook_id, base_scope)
    peers = tuple(dict.fromkeys(
        str(value) for value in readers.participants(notebook_id)
        if value and str(value) != notebook_id and admits(str(value))
    ))
    synthesize_local = local_scope is None
    local = (
        _synthesised_local(notebook_id, owner_id, readers)
        if synthesize_local else local_scope
    )
    local, withheld = _without_memory(local, readers, notebook_id)
    mounted = _mounted_library_ceilings(
        peers, readers, cancel_event, mounted_read_seconds,
        mounted_total_seconds, _read_workers(readers, mounted_read_workers),
    )
    with source_scope_context(
        notebook_id,
        local,
        base_scope,
        mounted.ceilings,
        ceilings_total=True,
        local_synthesized=synthesize_local,
        _withheld_hidden_source_ids=withheld,
        _skipped_libraries=mounted.skipped,
        _ceiling_read_order=mounted.read_order,
        _verdict_probes=readers.verdict_probes,
        _universe_read_order=(local or {}).get("_read_order") if synthesize_local else None,
    ):
        yield


def _library_admits(notebook_id: str, base_scope: Any) -> Callable[[str], bool]:
    """The library question alone -- "does this library dimension admit that
    library?" -- answered by the one predicate that owns it
    (``ActiveSourceScope.covers_notebook`` of a scope carrying only this
    library dimension).  Used by both constructors to decide which mounted
    libraries are read at all."""
    base_raw = _scope_dict(base_scope) or {}
    named = base_raw.get("notebook_ids") or ()
    return ActiveSourceScope(
        notebook_id=notebook_id,
        mode="exclude",
        source_ids=frozenset(),
        source_provided=False,
        base_mode=str(base_raw.get("mode") or "exclude"),
        base_notebook_ids=_frozen_source_ids(named) if named else frozenset(),
        base_provided=_scope_dict(base_scope) is not None,
    ).covers_notebook


def _synthesised_local(
    notebook_id: str, owner_id: str, readers: CeilingReaders,
) -> dict[str, Any]:
    """The all-selected local freeze: ``visible ∪ hidden(owner)``, not narrowed.

    Both reader results are materialised HERE, where they enter: a reader that
    returns a generator would otherwise be consumed by ``_without_memory``'s
    disjointness check and leave the ceiling empty.  The visible set becomes
    the frozenset the scope keeps (``_frozen_source_ids`` reuses it downstream,
    so it is still built exactly once).
    """
    visible = tuple(map(str, readers.visible(notebook_id)))
    hidden = tuple(str(value) for value in readers.hidden(notebook_id, owner_id))
    return {
        "mode": "include",
        "source_ids": _frozen_source_ids(visible),
        "hidden_source_ids": hidden,
        "narrowed": False,
        "owner_id": owner_id,
        # The readers' own ``ORDER BY id`` order -- exactly the order the
        # store's drift fingerprint concatenates in -- so the freeze's digest
        # needs no sort (``ActiveSourceScope._frozen_universe_digests``).
        "_read_order": (visible, hidden),
    }


@contextmanager
def refreshed_ceiling_context(
    notebook_id: str,
    owner_id: str,
    readers: CeilingReaders,
    *,
    local_scope: Any = None,
    base_scope: Any = None,
    cancel_event: Any = None,
    mounted_read_seconds: float = DEFAULT_MOUNTED_READ_SECONDS,
    mounted_total_seconds: float = DEFAULT_MOUNTED_TOTAL_SECONDS,
    mounted_read_workers: int | None = None,
) -> Iterator[None]:
    """Re-install a REFRESHED freeze inside a run that already has a ceiling.

    The one place a scope legitimately replaces the one outside it: the report
    worker's auto-confirm re-validates the persisted scope and must plan and
    generate under the refreshed freeze, not the one the worker started with
    (``report_engine.run``).  ``default_ceiling_context`` cannot express that
    -- it passes through whenever an outer scope exists -- and a bare
    ``source_scope_context`` would drop the per-library ceilings and
    ``ceilings_total``, reopening mounted libraries' hidden projections and any
    library mounted mid-run.

    * An outer scope for ANOTHER notebook -> ``ValueError``, before anything
      else is decided (a subjectless outer included).
    * Outer scope SUBJECTLESS (a global run) -> pass through; a global run has
      no local or library dimension to refresh.
    * No outer scope -> a fresh default ceiling over the refreshed dimensions.
    * Any other outer scope -- a default ceiling (``ceilings_total``) or an
      older-style scope installed by ``source_scope_context`` -> the refreshed
      ``local_scope`` / ``base_scope`` replace those dimensions, and a
      dimension passed as ``None`` keeps the OUTER one (the report did not
      scope it, so the run's selection stands).

    THE RULE.  The SELECTION never widens: on a dimension the caller did not
    refresh, no source and no library the outer selection refused is
    admitted.  The local dimension is inherited as it binds -- an include list
    as-is; a per-notebook entry for this notebook as-is (checked first, so an
    outer that also carries ``ceilings_total`` cannot lose it); an exclusion
    list, or a local dimension that binds nothing, re-expressed as the
    constructor's synthesised ``visible ∪ hidden(owner)`` minus what it
    excluded (narrower than the outer: another member's Memory, which an
    exclusion list cannot keep out, drops out).  With the Memory channel
    closed every one of those shapes loses its Memory sources.  The library
    dimension, however, is a selection of LIBRARIES, not a freeze of the
    mount set: one that names no library by inclusion (unsubmitted, or an
    exclusion list) resolves against the CURRENT mount set, so a library
    mounted between the outer freeze and this refresh is admitted -- as the
    report path's unscoped library dimension did before any freeze existed --
    even though the outer's ``ceilings_total`` refused it.  Such a library is
    read once, visible sources only, under the constructor's per-library
    budget, stage bound and isolation; whether it is a valid mount at all is
    the participants read's answer.  The outer ``notebook_source_ceilings``
    are INHERITED unchanged (no library they name is read again, and their
    hand-out order and skip records come along), ``ceilings_total`` is set,
    and a participant the refreshed library dimension does not admit gets no
    entry and is refused.

    Reads: participants plus one visible read per newly admitted library (plus
    the notebook's visible and hidden sets when the local dimension is
    re-expressed as above, and one ``memory_sources`` read while the Memory
    channel is closed).
    """
    outer = current_source_scope()
    if outer is not None and outer.notebook_id != notebook_id:
        raise ValueError(
            "a refreshed ceiling must be installed for the run's own notebook"
        )
    if outer is not None and outer.subjectless:
        yield
        return
    if outer is None:
        with _fresh_default_ceiling(
            notebook_id, owner_id, readers,
            local_scope=local_scope, base_scope=base_scope,
            cancel_event=cancel_event, mounted_read_seconds=mounted_read_seconds,
            mounted_total_seconds=mounted_total_seconds,
            mounted_read_workers=mounted_read_workers,
        ):
            yield
        return
    from app.services.cancellation import raise_if_cancelled

    cancel_event = _effective_cancel_event(cancel_event)
    raise_if_cancelled(cancel_event)
    local, local_synthesized, withheld, own_entry = _refreshed_local(
        outer, local_scope, readers, owner_id,
    )
    base = base_scope
    if base is None and outer.base_provided:
        base = {
            "mode": outer.base_mode,
            "notebook_ids": outer.base_notebook_ids,
            "narrowed": outer.base_narrowed,
        }
    admits = _library_admits(notebook_id, base)
    # An outer may bind its OWN notebook through a per-notebook entry instead
    # of the local dimension; that entry is the local dimension, and
    # ``_refreshed_local`` hands it back (Memory-stripped) only while the local
    # dimension is inherited as it.
    inherited = {
        library: ceiling for library, ceiling in outer.notebook_source_ceilings
        if library != notebook_id
    }
    if own_entry is not None:
        inherited[notebook_id] = own_entry
    # Only orders the outer memo already holds (non-None ``(lib, True)``
    # entries), each paired with the set it was taken from -- the outer's
    # ``(lib, False)`` entry.  ``source_scope_context`` keeps an order only
    # where the new scope's effective ceiling IS that object: a library the
    # outer excluded paired ``frozenset()`` and never matches, and a
    # Memory-stripped own entry is a different set.  ``copy()``: worker
    # threads may be filling the outer memo right now.
    outer_memo = outer._library_ceiling_memo.copy()
    read_order = {
        library: (outer_memo.get((library, False)), order)
        for (library, ordered), order in outer_memo.items()
        if ordered and order is not None and library in inherited
    }
    skipped = {
        library: reason for library, reason in outer._skipped_libraries
        if library in inherited
    }
    added = tuple(dict.fromkeys(
        str(value) for value in readers.participants(notebook_id)
        if value and str(value) != notebook_id
        and str(value) not in inherited and admits(str(value))
    ))
    mounted = _mounted_library_ceilings(
        added, readers, cancel_event, mounted_read_seconds,
        mounted_total_seconds, _read_workers(readers, mounted_read_workers),
    )
    inherited.update(mounted.ceilings)
    read_order.update(mounted.read_order)
    skipped.update(mounted.skipped)
    with source_scope_context(
        notebook_id,
        local,
        base,
        inherited,
        ceilings_total=True,
        local_synthesized=local_synthesized,
        _withheld_hidden_source_ids=withheld,
        _skipped_libraries=skipped,
        _ceiling_read_order=read_order,
        # The refresh's readers first; else the outer's (same store).
        _verdict_probes=readers.verdict_probes or outer._verdict_probes,
    ):
        yield


def _refreshed_local(
    outer: ActiveSourceScope,
    local_scope: Any,
    readers: CeilingReaders,
    owner_id: str,
) -> tuple[Any, bool, tuple[str, ...], frozenset[str] | None]:
    """``(local, local_synthesized, withheld, own_entry)`` for a refresh
    inside ``outer`` -- the local half of ``refreshed_ceiling_context``'s rule.

    Exactly one of ``local`` / ``own_entry`` is not None.  ``own_entry`` is the
    outer's per-notebook entry for its own notebook, kept (Memory-stripped
    while the channel is closed) when the caller did not refresh the local
    dimension.  It is checked BEFORE the inherited include list: an outer that
    carries ``ceilings_total`` alongside such an entry has an unbound
    ``exclude []`` local dimension, and inheriting that instead would leave the
    notebook unbounded (another member's Memory included).
    """
    notebook_id = outer.notebook_id
    if local_scope is not None:
        local, withheld = _without_memory(local_scope, readers, notebook_id)
        return local, False, withheld, None
    own_entry = outer.source_ceiling_for(notebook_id)
    if own_entry is not None:
        stripped, _ = _without_memory(
            {"mode": "include", "source_ids": own_entry}, readers, notebook_id,
        )
        return None, False, (), _frozen_source_ids(stripped["source_ids"])
    if outer.mode == "include":
        local, withheld = _without_memory(
            {
                "mode": outer.mode,
                "source_ids": outer.source_ids,
                "hidden_source_ids": outer.hidden_source_ids,
                "narrowed": outer.narrowed,
                "owner_id": outer.owner_id,
            },
            readers, notebook_id, outer.withheld_hidden_source_ids,
        )
        return local, not outer.source_provided, withheld, None
    # An exclusion list, or a local dimension that binds nothing: the
    # synthesised freeze minus whatever the outer excluded -- never wider.
    synthesised = _synthesised_local(notebook_id, owner_id, readers)
    excluded = outer.source_ids
    if excluded:
        synthesised.update(
            source_ids=synthesised["source_ids"] - excluded,
            hidden_source_ids=tuple(
                value for value in synthesised["hidden_source_ids"]
                if value not in excluded
            ),
            narrowed=outer.restricted,
        )
    local, withheld = _without_memory(synthesised, readers, notebook_id)
    return local, not (excluded and outer.source_provided), withheld, None
