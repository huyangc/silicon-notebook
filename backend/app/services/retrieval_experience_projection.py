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
    closed vocabulary defined in this file or in
    ``app.domain.retrieval_experience``.

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

from app.domain.retrieval_experience import (
    SITUATION_ASK_MODES,
    SITUATION_RESULT_SCOPES,
    SITUATION_RETRIEVAL_EFFORTS,
    SITUATION_UNKNOWN,
    project_run_step,
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
#: codex #524 R16 P3:内容寻址主键的哈希截断长度(SHA-256 hex 前缀,128 bit)。
#: 这是**持久身份格式**,不是可调预算——改动它会让同一条 (situation, action)
#: 在新旧部署里哈希出两个不同主键,跨部署 merge_dbs 的全局并集从"同条目
#: 去重"退化成"同条目双行、证据各分一半"。协议边界,具名钉死,绝不调。
EXPERIENCE_ID_HASH_HEX_CHARS = 32

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
    run. Five scalars per action is the least that can still say "this action
    ran, came back empty this often, and — where the run's own results could be
    checked against what the answer actually cited — helped this often".

    ⚠ Agentic Memory P4 (T3): ``anchored_hits`` / ``attributable`` are the
    step→anchor attribution this module's own ``RunObservation`` docstring
    used to register as unrecoverable. Two fields, not one
    ``attributed_hits: int | None``: the privacy guard's judgement-one scanner
    (``test_retrieval_experience_privacy_guard.py``) walks field ANNOTATIONS
    and accepts exactly ``int`` / ``bool`` / a closed ``Literal`` — an
    ``Optional[int]`` annotation is a ``Union`` shape the scanner has no rule
    for, so it would either need a new exception carved into a judgement that
    exists specifically to have none, or slip through unnoticed. A plain
    ``bool`` needs no exception:

    * ``attributable`` — whether this run/action pair COULD be checked at all
      (the run's synthesis step produced a usable, non-truncated anchor set,
      AND at least one invocation of this action carried its own
      ``result_ids``). ``False`` means "no evidence either way", not "this
      action did not help" — the overwhelming common case is a run persisted
      before this phase, where the answer is simply that attribution never
      logs anything.
    * ``anchored_hits`` — how many of this action's OWN results (across every
      invocation this run) turned out to be ids the answer actually bound as
      an anchor. Meaningful only when ``attributable`` is ``True``; ``0`` when
      it is ``False`` is not "zero hits observed", it is "not observed at
      all" — the same shape of ambiguity ``zero_hits`` already lives with for
      an action that never ran, resolved the same way: a companion flag
      rather than a sentinel value.

    The intersection itself — one step's ``result_ids`` against the run's
    ``anchor_evidence_ids`` — happens once, locally, inside ``project_run``'s
    own loop, using local variables that never become a field on this type.
    See that function's docstring for why the raw ids cannot live here even
    transiently.
    """

    action: RetrievalAction
    invocations: int
    zero_hits: int
    anchored_hits: int
    attributable: bool


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

    OUTCOME GRANULARITY (updated Agentic Memory P4, T3 — this paragraph used
    to say attribution was unrecoverable; it no longer is, but the shape of
    what is observed is still worth spelling out precisely). Failures are
    observed PER ACTION (``zero_hits``) — always, for every persisted run,
    old or new. Successes are observed PER ACTION TOO now, but only where
    attribution is POSSIBLE (``ActionObservation.anchored_hits`` /
    ``attributable`` — see that type's docstring): the write path
    (``TraceStep.detail["result_ids"]`` on ``retrieve``/``ppr``/
    ``exact_lookup``/``expand``, ``TraceStep.detail["anchor_evidence_ids"]``
    on the run's ``synthesis``/``answer`` step) has to have run for THIS
    request, and the run's own anchor set has to be complete (not truncated
    by its own protocol cap). A run persisted before this phase, or a request
    whose action produced no ``result_ids`` (``expand_community``,
    ``follow_chain``, ``enumerate``, ``outline`` — the write side never
    touches these step types), has ``attributable=False`` for that
    action/run, and its success can only be read at RUN granularity —
    ``citations``/``answered`` below remain the fallback for exactly that
    case, not a redundant copy of the same signal.

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

    The store has already narrowed each step through
    ``app.domain.retrieval_experience.project_run_step`` (action type, one
    count, one duration, plus the intent step's closed situation). This
    function only aggregates and buckets; it reads no field that projection
    did not already restrict.

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

    # Pass 1 of 2 (Agentic Memory P4, T3): whether this run's own answer
    # anchors are a USABLE attribution target at all, and if so, the
    # (function-LOCAL, never persisted) id set pass 2 intersects each
    # action's ``result_ids`` against.
    #
    # ⚠ This has to be its own pass, not folded into pass 2 below: a run's
    # synthesis/answer step is not guaranteed to be the LAST entry in
    # ``steps`` (nothing about a persisted trace promises step order), and
    # computing a per-action ``anchored_hits`` needs the COMPLETE anchor set
    # before it can intersect a single action's ``result_ids`` against it. A
    # single forward pass would make attribution depend on step order,
    # which nothing here guarantees.
    run_anchor_ids: set[str] = set()
    run_attributable = False
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        if str(step.get("step_type") or "") not in ("synthesis", "answer"):
            continue
        # 修复轮 Q-P1-1: 按键存在判定,镜像 result_ids 的规则
        # (见 ``app.domain.retrieval_experience.project_run_step``)——不止
        # 一种 step_type 等于 "synthesis"/"answer" 的行(逐节撰写进度步、
        # 枚举回答分支、reasoning_retrieval.py
        # 的候选池汇总步都同名),只有真正携带 anchor_evidence_ids 键的那一条
        # 才是可用的锚点来源。``step_limit`` 会把一个 run 的 trace 行按 seq
        # 截尾,真正带锚点的"synthesis"步完全可能被切掉、只留下前面那条
        # 不带锚点的"answer"候选步——把它当成"锚点已知为空"会把每个动作的
        # anchored_hits 全部算成 0,而真相是这条 run 的锚点根本不可读。
        if "anchor_evidence_ids" not in step:
            continue
        if step.get("anchor_ids_truncated"):
            # A truncated anchor list is not usable as an attribution
            # target — some of the answer's real citations are missing from
            # it, so a "no hit" reading would be indistinguishable from "the
            # missing tail would have hit". One truncated step poisons the
            # whole run's attribution (not just itself): discard whatever
            # this run's other synthesis/answer step(s) contributed and stop
            # looking.
            run_anchor_ids = set()
            run_attributable = False
            break
        run_attributable = True
        raw_ids = step.get("anchor_evidence_ids")
        if isinstance(raw_ids, (list, tuple)):
            run_anchor_ids.update(
                item for item in raw_ids if isinstance(item, str) and item
            )

    situation: Mapping[str, Any] | None = None
    citations = 0
    answered = False
    invocations: dict[str, int] = {}
    zero_hits: dict[str, int] = {}
    # Pass 2's own per-action attribution accumulators — ``action_attributable``
    # records KEY PRESENCE (did at least one invocation of this action carry
    # its own ``result_ids`` at all — see ``project_run_step``'s docstring for
    # why that is the write side's own "old trace vs. this phase" signal),
    # ``action_anchored_hits`` the running intersection count. Both are
    # function-local and fold into ``ActionObservation.attributable`` /
    # ``anchored_hits`` below; neither ever holds a raw id itself.
    action_attributable: dict[str, bool] = {}
    action_anchored_hits: dict[str, int] = {}
    # 修复轮 spec②: 一个动作若在本 run 内任一次调用的 result_ids 被写侧截断
    # (``TraceStep.detail["result_ids_truncated"]``),这个动作在本 run 的
    # attribution 整体作废——被截掉的尾巴里可能恰好就是答案绑定的那个锚点,
    # "没交上截断的那部分"与"确实没命中"在读侧分不出来。镜像的是 pass 1
    # 里锚点列表本身被截断时对整个 run 的 poison 语义,只是这里的爆炸半径
    # 收窄到"这一个动作",不连坐同一 run 里其它没截断的动作。
    action_poisoned: dict[str, bool] = {}
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
        if "result_ids" in step:
            # This invocation ran under the write path that records result
            # ids at ALL — an old-shape trace row (persisted before this
            # phase) never carries this key. The intersection against
            # ``run_anchor_ids`` happens right here, in a local variable that
            # never becomes a field on anything this function returns.
            action_attributable[step_type] = True
            if step.get("result_ids_truncated"):
                action_poisoned[step_type] = True
            raw_result_ids = step.get("result_ids")
            if isinstance(raw_result_ids, (list, tuple)):
                action_anchored_hits[step_type] = action_anchored_hits.get(
                    step_type, 0
                ) + sum(
                    1 for rid in raw_result_ids
                    if isinstance(rid, str) and rid in run_anchor_ids
                )

    if situation is None:
        return None

    def _action_attributable(action: str) -> bool:
        return (
            run_attributable and action_attributable.get(action, False)
            and not action_poisoned.get(action, False)
        )

    actions = tuple(
        ActionObservation(
            action=action,  # type: ignore[arg-type]
            invocations=invocations[action],
            zero_hits=zero_hits.get(action, 0),
            attributable=_action_attributable(action),
            # codex #538 R1 P2:不可归因(含被截断 poison)的动作 anchored_hits
            # 一律归零——留着保留前缀算出的交集,会在与别的可归因 run 同组聚合
            # 时被当成有效成功计入 anchored= 分子,而分母只数别的 run。
            anchored_hits=(
                action_anchored_hits.get(action, 0)
                if _action_attributable(action) else 0
            ),
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


def current_situation(
    intent_detail: object, *, mode: str, retrieval_effort: str
) -> dict:
    """The IF side of a run that is ABOUT TO happen (Agentic Memory P2 / T6).

    ``project_run`` above answers "what shape of question WAS this" from a
    finished run's persisted steps; the injection side needs the same answer
    before any of that exists, from the intent contract the caller is holding.
    Both must produce the SAME eight keys with the same domains, or an entry
    filed under one shape would never be selected for the identical shape — so
    this function reaches the situation through ``project_run_step``, the one
    narrowing both halves already share, rather than reading the contract's
    fields itself.

    ⚠ ``intent_detail`` is the raw intent trace-step detail, and it DOES carry
    the user's own words. It is handed straight to ``project_run_step``, which
    reads exactly the closed-vocabulary and length fields listed in its
    docstring and nothing else — this module never touches a prose field of it,
    which is the property the privacy guard checks by scanning for those field
    names. Passing the mapping through the single narrowing point is what keeps
    "the situation cannot carry free text" true here as well as on the
    observation side.

    ``None``/malformed detail is not an error: every key falls back to
    ``unknown``/``none``/``False``. A caller with no intent contract (the deep
    report's per-section retrieval) still gets a scorable situation, just a
    less discriminating one.
    """
    projected = project_run_step({"step_type": "intent", "detail": intent_detail})
    raw = projected.get("situation") if isinstance(projected, Mapping) else None
    raw = raw if isinstance(raw, Mapping) else {}
    # The explicit effort argument wins over the contract's copy of it: the
    # caller's value is the tier the run is ACTUALLY executing under (its
    # limits row), while the contract's is what was recorded at confirmation
    # time. They agree on the Ask path; where they cannot (a caller with no
    # contract at all), falling back to the contract's value keeps the key from
    # collapsing to ``unknown`` for no reason.
    effort = _situation_value(
        {"retrieval_effort": retrieval_effort}, "retrieval_effort", None
    )
    if effort is None:
        effort = _situation_value(raw, "retrieval_effort", SITUATION_UNKNOWN)
    return {
        "mode": _situation_value({"mode": mode}, "mode", SITUATION_UNKNOWN),
        "result_scope": _situation_value(raw, "result_scope", SITUATION_UNKNOWN),
        "retrieval_effort": effort,
        "completeness_required": bool(raw.get("completeness_required")),
        "entity_count": count_band(_int_or_zero(raw.get("entity_count"))),
        "topic_count": count_band(_int_or_zero(raw.get("topic_count"))),
        "has_constraints": bool(raw.get("has_constraints")),
        "has_exclusions": bool(raw.get("has_exclusions")),
    }


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
    return f"rx_{digest[:EXPERIENCE_ID_HASH_HEX_CHARS]}"


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
