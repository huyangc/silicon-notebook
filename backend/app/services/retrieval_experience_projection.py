"""Agentic Memory P2 (A / T5): what ONE finished retrieval run is allowed to
become before the deployment-GLOBAL experience library ever sees it.

This module is the feature's privacy boundary, and it is a STRUCTURAL one.
Everywhere else in this repository, isolation is a predicate written into the
reading SQL (``memory_items.created_by``, ``agent_notebook_profile``'s
``owner_id IN ('', ?)``). The experience library has no tenancy column and no
predicate to write: its entries are statements about retrieval TACTICS, drawn
from every user's runs and read back by every user. So the guarantee has to be
made one layer earlier, on the SHAPE of what can be observed at all:

    ``RunObservation`` and every type reachable from it have NO free-text
    field. Every field is an ``int``, a ``bool``, or a ``Literal`` over a
    closed vocabulary defined in this file or in ``ports.py``.

That is a property a test can check by reading the annotations, which is the
entire reason the design was collapsed to closed vocabularies in the first
place. The alternative the design doc sketched — free-text experiences that a
prompt asks the model to "parameterise" (``set_db`` → ``{identifier}``) — makes
the isolation a request rather than a fact, and a leak in that shape has no
error, no failing test and no way to notice.

Consequence worth stating plainly, because it is a real cost and not a
technicality: an entry cannot say "look up the table of contents first, then
drill in by title". It can only say "``enumerate`` is worth reaching for in
this shape of question, ``exact_lookup`` is not". Expressiveness was traded for
a guarantee that holds without anyone re-checking it.

⚠ Two things this module deliberately does NOT read, both of which the design
plan originally listed as situation features:

* **anything derived from the question text** — whether it contains a quoted
  phrase, whether it contains a look-uppable identifier. Both are booleans, and
  both would require this module to touch ``question``. The guard that keeps
  free text out has to scan this module and the job module TOGETHER (otherwise
  moving a read from one to the other defeats it), so "compute a boolean from
  the question here" is not available. Registered, not forgotten.
* **notebook shape** — document count band, corpus language, whether a
  knowledge graph exists. Two reasons. It costs one bounded query per distinct
  notebook per batch on a path whose whole premise is that it is free; and, more
  importantly, notebook-shape dimensions make an entry more IDENTIFYING — with
  ``support = 1``, an entry carrying a distinctive corpus fingerprint describes
  one person's one run in one library, inside a table every user reads.

Both are additive later: ``situation_json`` is an open-ended map over a closed
KEY registry, and because ids are content-addressed, adding a key simply means
new entries while old ones age out through the ordinary eviction.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from app.repositories.ports import (
    SITUATION_ASK_MODES,
    SITUATION_RESULT_SCOPES,
    SITUATION_RETRIEVAL_EFFORTS,
    SITUATION_UNKNOWN,
)

#: The closed THEN-side vocabulary: the retrieval actions an experience entry
#: may be about. These are TRACE STEP TYPE spellings (``ppr``, not
#: ``ppr_retrieve``; ``enumerate``, not ``enumerate_elements``), because that
#: is what a finished run actually persists — and because the observation side
#: and the injection side must agree, one spelling is safer than a mapping
#: table that only one of them consults.
#:
#: ⚠ It contains no action that could change retrieval SCOPE, and it must not
#: grow one. An experience influences HOW a run searches — which channels to
#: reach for — never WHICH sources or reference libraries it may read; that is
#: the user's checkbox selection and nothing in this feature is allowed to
#: touch it. The reverse guard on this tuple is the structural form of that
#: rule.
#:
#: ⚠ No ``memory`` entry, on purpose (removed in the T5 fix round, not
#: overlooked): a ``memory`` TRACE step is only ever emitted on a HIT — a
#: miss is recorded as a ``skip`` step instead (see ``project_run``'s
#: docstring for why ``skip`` steps are discarded whole). ``zero_hits`` for
#: ``memory`` would therefore be structurally always 0, which is not evidence
#: of anything. And it fails the other half of the test that earns a slot
#: here: memory recall is not something the reflect loop CHOOSES to invoke —
#: there is no ``memory`` action id for the model to reach for — so a THEN
#: side entry about it would recommend a channel nobody can act on.
RETRIEVAL_ACTIONS: tuple[str, ...] = (
    "retrieve",
    "ppr",
    "exact_lookup",
    "expand",
    "expand_community",
    "follow_chain",
    "enumerate",
    "outline",
)

RetrievalAction = Literal[
    "retrieve",
    "ppr",
    "exact_lookup",
    "expand",
    "expand_community",
    "follow_chain",
    "enumerate",
    "outline",
]

#: An entry's verdict. Two values, and the second one carries most of the
#: value: R²-Mem's finding is that failed attempts teach more than successful
#: ones, and a failure is also the half this feature can observe at STEP
#: granularity (see ``OUTCOME`` below).
EXPERIENCE_POLARITIES: tuple[str, ...] = ("good", "bad")
ExperiencePolarity = Literal["good", "bad"]

#: Count bands. Deliberately coarse: the point is "did this question name a
#: couple of things or a dozen", and a finer band would make the situation
#: fingerprint more identifying without making the advice better.
CountBand = Literal["none", "few", "many"]
_FEW_MAX = 2

#: The closed KEY registry of a situation fingerprint. A key outside it, or a
#: value outside its key's domain, means the whole entry is DISCARDED — never
#: repaired, never guessed. ``situation_domain`` below is the single source of
#: truth for both halves, and ``validate_situation`` is both the runtime check
#: and what the tests assert against.
SITUATION_KEYS: tuple[str, ...] = (
    "mode",
    "result_scope",
    "retrieval_effort",
    "completeness_required",
    "entity_count",
    "topic_count",
    "has_constraints",
    "has_exclusions",
)

_BOOLEAN_KEYS = frozenset(
    {"completeness_required", "has_constraints", "has_exclusions"}
)
_COUNT_BAND_KEYS = frozenset({"entity_count", "topic_count"})


def situation_domain(key: str) -> tuple[Any, ...]:
    """Every value ``key`` may take. Empty tuple = not a registered key."""
    if key == "mode":
        return (*SITUATION_ASK_MODES, SITUATION_UNKNOWN)
    if key == "result_scope":
        return (*SITUATION_RESULT_SCOPES, SITUATION_UNKNOWN)
    if key == "retrieval_effort":
        return (*SITUATION_RETRIEVAL_EFFORTS, SITUATION_UNKNOWN)
    if key in _BOOLEAN_KEYS:
        return (True, False)
    if key in _COUNT_BAND_KEYS:
        return ("none", "few", "many")
    return ()


def count_band(value: int) -> CountBand:
    if value <= 0:
        return "none"
    return "few" if value <= _FEW_MAX else "many"


@dataclass(frozen=True)
class ActionObservation:
    """What ONE kind of retrieval action did during ONE run.

    Aggregated per action rather than kept as a step SEQUENCE: an ordered list
    would be a strictly richer signal, and it would also make the observation
    describe the run closely enough that a distinctive sequence identifies the
    run. Three integers per action is the least that can still say "this action
    ran and came back empty".
    """

    action: RetrievalAction
    invocations: int
    zero_hits: int


@dataclass(frozen=True)
class RunObservation:
    """ONE finished ask, reduced to the only thing the distillation may see.

    ⚠ EVERY field here — and in ``ActionObservation``, the only type reachable
    from it — is an ``int``, a ``bool`` or a ``Literal``. That is the property
    the privacy guard checks, and the reason it can be checked at all. Adding a
    ``str``/``Any``/``dict``/``list[str]`` field is not a small widening: it is
    the difference between a guarantee and a promise.

    ⚠ ``run_id`` is NOT here, on purpose. It is bookkeeping (provenance
    de-duplication), not content, and keeping it outside this type means the
    guard's rule needs no exception carved into it — see ``ObservedRun``.

    OUTCOME GRANULARITY, registered as a v1 limitation because it is easy to
    misread this type as saying more than it does: failures are observed PER
    ACTION (``zero_hits``), successes only PER RUN (``citations``,
    ``answered``). Step→anchor attribution is not recoverable from what runs
    persist today — ``ask_trace_steps.detail`` records counts and never result
    ids for ``retrieve``/``ppr``/``exact_lookup``/``expand``/``enumerate`` — so
    "this action's results are what the answer ended up citing" cannot be
    computed. Making it computable means changing the trace WRITE path on a hot
    request path, with its own disclosure argument; that is deliberately left
    to a later phase.

    ⚠ There is no field here for ``skip`` steps either, and that is a
    SEPARATE registered decision from the outcome-granularity one above, not
    the same gap: a skip step's reason is a sentence written for a human
    reading the trace, and folding it in would mean either inventing a closed
    vocabulary for every skip reason this codebase can produce, or keeping
    the free text — the exact leak this type exists to rule out. See
    ``project_run``'s docstring for the full argument. If a later phase wants
    skip signal here, it needs a NEW closed vocabulary; it must not add a
    ``str`` reason field to get there.
    """

    mode: Literal["chunk", "reasoning", "graph", "unknown"]
    result_scope: Literal["ranked", "complete", "aggregate", "hybrid", "unknown"]
    retrieval_effort: Literal[
        "overview", "standard", "deep", "thorough", "exhaustive", "unknown"
    ]
    completeness_required: bool
    entity_count: CountBand
    topic_count: CountBand
    has_constraints: bool
    has_exclusions: bool
    citations: int
    answered: bool
    actions: tuple[ActionObservation, ...]

    def situation(self) -> dict:
        """The IF side of this run, as the map that gets fingerprinted."""
        return {
            "mode": self.mode,
            "result_scope": self.result_scope,
            "retrieval_effort": self.retrieval_effort,
            "completeness_required": self.completeness_required,
            "entity_count": self.entity_count,
            "topic_count": self.topic_count,
            "has_constraints": self.has_constraints,
            "has_exclusions": self.has_exclusions,
        }


@dataclass(frozen=True)
class ObservedRun:
    """The bookkeeping envelope: one opaque run id beside its observation.

    ⚠ This type is NOT prompt input and the privacy guard's "no free text"
    rule does not apply to it — it applies to ``RunObservation`` and what is
    reachable FROM it. The separation is the point: ``run_id`` has to exist
    (an entry's provenance list is what makes re-distilling an overlapping
    batch idempotent, and what lets this feature work without a cursor table),
    and burying it inside ``RunObservation`` would have forced the guard to
    carve out an exception — at which point the next field to want an
    exception gets one too.
    """

    run_id: str
    observation: RunObservation


def _situation_value(raw: Mapping[str, Any] | None, key: str, default: Any) -> Any:
    if not isinstance(raw, Mapping):
        return default
    value = raw.get(key, default)
    return value if value in situation_domain(key) else default


def project_run(run: Mapping[str, Any]) -> ObservedRun | None:
    """Turn one row from ``recent_completed_ask_runs`` into an observation.

    Returns ``None`` — the run is skipped, not repaired — when it carries no
    run id or no ``intent`` step. A run with no intent step has no situation,
    and an experience whose IF side is "all unknown" would match every future
    run equally, which is worse than having no entry.

    The store has already narrowed each step through ``ports.project_run_step``
    (action type, one count, one duration, plus the intent step's closed
    situation). This function only aggregates and buckets; it reads no field
    that projection did not already restrict.

    ⚠ Step types outside ``RETRIEVAL_ACTIONS`` — most notably every ``skip``
    step (an exact-lookup teaching message, a memory miss, an enumeration
    skip reason) — are discarded WHOLE by the loop below, never folded into
    an "attempted but skipped" tally. Registered as a decision, not an
    oversight: a skip step's reason is a sentence written for a HUMAN reading
    the trace, and there is no closed vocabulary that could represent it
    without either enumerating every skip reason this codebase can produce
    (a vocabulary that grows every time a retrieval channel grows one), or
    keeping the free text — which is exactly the leak this module exists to
    rule out. If a later phase wants skip signal in the experience library,
    it has to design a NEW closed vocabulary for skip reasons; it must not
    resurrect the discarded text.
    """
    run_id = str(run.get("run_id") or "")
    if not run_id:
        return None
    steps = run.get("steps")
    if not isinstance(steps, Sequence):
        return None

    situation: Mapping[str, Any] | None = None
    citations = 0
    answered = False
    invocations: dict[str, int] = {}
    zero_hits: dict[str, int] = {}
    entity_count = 0
    topic_count = 0
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        step_type = str(step.get("step_type") or "")
        if step_type == "intent":
            raw = step.get("situation")
            if isinstance(raw, Mapping):
                situation = raw
                entity_count = _int_or_zero(raw.get("entity_count"))
                topic_count = _int_or_zero(raw.get("topic_count"))
            continue
        if step_type in ("synthesis", "answer"):
            # The run-level success signal. ``project_trace_step``'s allowlist
            # already resolved these steps' one count to their ``citations``
            # key, so this is the number of anchors the answer actually bound —
            # the closest thing to "did this run produce grounded output" that
            # survives into persistence. ``max`` rather than a sum because a
            # run can record both step types for the same answer.
            count = step.get("count")
            if isinstance(count, int) and not isinstance(count, bool):
                citations = max(citations, count)
            answered = True
            continue
        if step_type not in RETRIEVAL_ACTIONS:
            continue
        invocations[step_type] = invocations.get(step_type, 0) + 1
        count = step.get("count")
        if isinstance(count, int) and not isinstance(count, bool) and count <= 0:
            zero_hits[step_type] = zero_hits.get(step_type, 0) + 1

    if situation is None:
        return None

    actions = tuple(
        ActionObservation(
            action=action,  # type: ignore[arg-type]
            invocations=invocations[action],
            zero_hits=zero_hits.get(action, 0),
        )
        # Iterated over the VOCABULARY rather than over the observed dict, so
        # the tuple order is fixed by this file rather than by the order steps
        # happened to run in. Two runs with the same actions must produce
        # identical observations, or the batch aggregation below double-counts.
        for action in RETRIEVAL_ACTIONS
        if action in invocations
    )
    return ObservedRun(
        run_id=run_id,
        observation=RunObservation(
            mode=_situation_value(  # type: ignore[arg-type]
                {"mode": run.get("mode")}, "mode", SITUATION_UNKNOWN
            ),
            result_scope=_situation_value(  # type: ignore[arg-type]
                situation, "result_scope", SITUATION_UNKNOWN
            ),
            retrieval_effort=_situation_value(  # type: ignore[arg-type]
                situation, "retrieval_effort", SITUATION_UNKNOWN
            ),
            completeness_required=bool(situation.get("completeness_required")),
            entity_count=count_band(entity_count),
            topic_count=count_band(topic_count),
            has_constraints=bool(situation.get("has_constraints")),
            has_exclusions=bool(situation.get("has_exclusions")),
            citations=citations,
            answered=answered,
            actions=actions,
        ),
    )


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def validate_situation(situation: object) -> dict | None:
    """Accept a situation map only if every key AND value is registered.

    Returns the normalised map, or ``None`` — the caller discards the whole
    entry. There is no repair path on purpose: a situation with one unknown
    value is a situation nobody can interpret, and "drop the bad key" would
    silently turn an entry about one shape of question into an entry about a
    broader one.
    """
    if not isinstance(situation, Mapping):
        return None
    if set(situation) != set(SITUATION_KEYS):
        return None
    normalised: dict = {}
    for key in SITUATION_KEYS:
        value = situation[key]
        domain = situation_domain(key)
        if key in _BOOLEAN_KEYS:
            if not isinstance(value, bool):
                return None
        elif not isinstance(value, str) or value not in domain:
            return None
        normalised[key] = value
    return normalised


def experience_id(situation: Mapping[str, Any], action: str) -> str:
    """The entry's CONTENT-ADDRESSED primary key.

    Deterministic across processes and across deployments, which is what makes
    ``scripts/merge_dbs.py``'s ``INSERT OR IGNORE`` union correct: the same
    (situation, action) computes the same id everywhere, so merging two
    databases keeps one row per conclusion instead of duplicating it. An
    incrementing or random id would break that in opposite directions —
    incrementing ids collide across deployments and silently drop rows, random
    ids split one conclusion into two rows with the evidence divided between
    them.

    ``sort_keys=True`` makes the hash independent of dict ordering, so an entry
    read back out of either backend re-hashes to its own id — the property that
    lets a merged database be audited rather than trusted.
    """
    payload = json.dumps(
        {"situation": dict(situation), "action": str(action)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"rx_{digest[:32]}"


def situation_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    """How close two situations are, in [0, 1] — a plain agreement ratio over
    the closed key registry.

    ⚠ Deterministic set overlap, NOT an embedding. Both sides are maps over the
    same eight keys with small closed domains; there is nothing for a vector
    space to discover here, and an embedding call would add a model dependency,
    a cache, and a source of run-to-run variation to a comparison that has an
    exact answer. This is the same reasoning that keeps the injection side free
    of model calls entirely.
    """
    keys = SITUATION_KEYS
    agree = sum(1 for key in keys if left.get(key) == right.get(key))
    return agree / len(keys)
