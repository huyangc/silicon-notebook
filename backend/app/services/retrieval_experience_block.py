"""Render the deployment-global retrieval experience library as ONE prompt block.

Agentic Memory P2 (A / T6), design doc §6.1. This module is the injection-side
mirror of ``agent_profile_block.py`` and deliberately copies its shape: a fixed
header, one line of framing, ``- ...`` rows, one hard character cap, and a
``rendered_row_count`` so the caller reports what the model actually received
rather than what was selected. The reasons are the same ones stated there — the
block rides in every plan and every reflect prompt of a run, so its worst case
has to be a CONSTANT rather than a function of how much the distillation job
happened to write.

English scaffolding on purpose (same reason as the collection map and the P1
understanding block): this is prompt scaffolding sitting next to the other
English instructions, not user-facing copy.

⚠ Two hard boundaries, and they are different from P1's single one:

* **Never evidence.** Like the understanding block, this reaches the PLANNING
  and REFLECTION models only, never answer synthesis, and nothing in it is
  citable with ``[k]``. ``ReasoningResult`` carries no field for it.
* **Never SCOPE.** An experience says HOW to search — which channel is worth
  reaching for on this shape of question — and must never say WHAT a run may
  read. Retrieval scope is the user's own checkbox selection. That rule is
  structural rather than a request: the THEN-side vocabulary
  (``RETRIEVAL_ACTIONS``) contains no scope-shaped action, an entry carries no
  source/notebook/library field to render, and a reverse guard pins both.

⚠ Rows that do not fit the cap are DROPPED WHOLE, where P1 truncates its block
with a trailing "…". The difference is deliberate and it is about what the two
blocks contain: P1's values are descriptions of a library, where a clipped tail
still describes it; a row here is one line of ADVICE, and a truncated line of
advice reads as confident and complete having lost its qualifier — the same
argument that makes the distillation reject an over-length rationale instead of
clipping it (see ``parse_distillation_reply``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from app.repositories.ports import RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS
from app.services.retrieval_experience_projection import (
    EXPERIENCE_POLARITIES,
    RETRIEVAL_ACTIONS,
    situation_similarity,
    validate_situation,
)

#: Whole-block cap, INCLUDING header and framing line. Same order of magnitude
#: as the collection map, and well under P1's 1200: this block is an aside on a
#: prompt that already carries the understanding block, the collection map and
#: the candidate summary, and a hint that crowds out the candidates it is meant
#: to help rank has made the run worse, not better.
RETRIEVAL_EXPERIENCE_BLOCK_MAX_CHARS = 600

#: How many entries the selector may hand the renderer. The cap above decides
#: how many of those are actually DELIVERED — reporting the selection instead
#: would over-report, exactly as P1's ``rendered_row_count`` docstring explains.
RETRIEVAL_EXPERIENCE_INJECT_TOP_K = 3

#: How close a stored entry's situation must be to the CURRENT run's before it
#: is shown at all. Without a floor, a deployment holding a few hundred entries
#: would always find three "closest" ones and inject advice drawn from a
#: different shape of question — which is worse than injecting nothing, because
#: the model cannot tell the two apart. Same value and same reasoning as the
#: distillation's own floor for offering existing entries.
RETRIEVAL_EXPERIENCE_SIMILARITY_FLOOR = 0.5

_HEADER = "[Search tactics learned from earlier runs]"
_GUIDANCE = (
    "Hints on HOW to search, aggregated from earlier runs. NOT evidence: never "
    "cite it, never state it as a finding, and never let it change WHICH "
    "sources a run may read."
)

# --- Agentic Memory P4 (T5): consult_memory, the MODEL-PULLED sibling of the
# auto-injected block above. ------------------------------------------------
#
# Where ``select_experiences``/``render_experience_block`` push the same top-K
# entries into every plan/reflect round automatically, ``consult_memory`` is a
# zero-parameter reflect ACTION the model chooses to spend a turn on. The two
# read the SAME table and share the SAME similarity floor, closed vocabularies
# and "never scope, never evidence" rules — only the SELECTION differs: this
# half excludes whatever the auto-injected block already delivered (the model
# has already seen those rows every round) and prioritises entries about an
# action that has gone quiet THIS run, because that is precisely the moment a
# model would reach for this action.

#: How many NEW entries one consult_memory call may return. Deliberately the
#: same order of magnitude as ``RETRIEVAL_EXPERIENCE_INJECT_TOP_K`` — this is
#: still "a few tactical hints", not a library dump.
CONSULT_MEMORY_TOP_K = 3

#: Whole-block cap for the RENDERED consult_memory content, INCLUDING header
#: and framing line — same shape and same value as
#: ``RETRIEVAL_EXPERIENCE_BLOCK_MAX_CHARS``. This is the total budget for
#: everything the run has accumulated across every consult_memory call so far
#: (see the caller's docstring: the ACCUMULATED row set is re-rendered on every
#: call, not appended as a second capped block), so two calls in the same run
#: never cost more prompt budget than one auto-injected block would.
CONSULT_MEMORY_BLOCK_MAX_CHARS = 600

_CONSULT_HEADER = "[Recalled search tactics]"
_CONSULT_GUIDANCE = (
    "Tactics you asked to recall. Same rule as the auto-injected hints above: "
    "NOT evidence, never cite it, and it never says which sources a run may "
    "read."
)


def action_id_for(word: str) -> str:
    """The reflect ACTION ID for one stored-vocabulary word.

    Public wrapper around ``_ACTION_IDS`` so a caller outside this module (the
    step-level zero-hit nudge in ``reasoning_retrieval.py``, which needs to
    name an action in a sentence fed back to the model) does not have to reach
    into the private mapping table directly. Unknown words pass through
    unchanged rather than raising — the nudge is advisory text, not a schema
    boundary, so a stale/foreign word should degrade to "shown verbatim"
    rather than crash the reflect loop.
    """
    return _ACTION_IDS.get(word, word)

#: Vocabulary word -> the reflect ACTION ID the model can actually choose.
#:
#: The stored vocabulary is trace step TYPES (``ppr``, ``enumerate``) because
#: that is what a finished run persists and therefore what the observation side
#: can see. The planning/reflect model, however, chooses from action IDs
#: (``ppr_retrieve``, ``enumerate_elements``). Rendering the step type would
#: hand the model advice naming a word that appears nowhere in its own action
#: schema — advice it cannot act on. This table is the ONE place the two
#: spellings meet, and ``ADOPTION_ACTIONS`` below is its inverse, so "what we
#: recommended" and "what the model then chose" are read off the same mapping
#: rather than off two tables that can drift.
#:
#: ⚠ ``"enumerate": "enumerate_*"`` is the one entry that is NOT a literal
#: action id — there is no ``enumerate_*`` in the reflect schema, only the two
#: concrete siblings ``enumerate_elements``/``enumerate_kg_objects``. The
#: wildcard is deliberate: the stored vocabulary has no way to distinguish
#: which of the two an observed run reached for (``project_run_step`` only
#: keeps the trace step type, and both siblings share ``step_type="enumerate"``
#: — see ``RunObservation``'s privacy argument for why that narrowing is not
#: negotiable), so an entry about "enumerate" is genuinely advice about
#: EITHER. Naming one arbitrarily would be more precise-looking and less
#: honest. ``ADOPTION_ACTIONS`` below resolves the wildcard on the way back
#: in — both concrete ids fold to the one word — which is what keeps this
#: table's one non-literal entry from breaking round-tripping.
_ACTION_IDS: dict[str, str] = {
    "retrieve": "add_subquery",
    "ppr": "ppr_retrieve",
    "exact_lookup": "exact_lookup",
    "expand": "expand_graph",
    "expand_community": "expand_community",
    "follow_chain": "follow_chain",
    "enumerate": "enumerate_*",
    "outline": "update_outline",
}
assert set(_ACTION_IDS) == set(RETRIEVAL_ACTIONS), (
    "_ACTION_IDS must name exactly the actions in RETRIEVAL_ACTIONS"
)

#: reflect ``next_action`` -> vocabulary word, for the adoption counter.
#:
#: ⚠ Deliberately NOT derived from the run's trace step types, which would need
#: no table at all. ``adopted`` is the FIRST key of the eviction ordering and it
#: is supposed to mean "the model reached for this because we suggested it".
#: Several actions in the vocabulary also run DETERMINISTICALLY in every run —
#: the initial retrieval always happens, the PPR and exact-lookup seed passes
#: run before the model has decided anything — so counting step types would make
#: ``adopted`` a proxy for "was injected at all", and eviction would then keep
#: whatever has been injected most rather than whatever has been useful most.
#:
#: ``search_elements`` and ``answer`` map to nothing: the first lands on a
#: ``fallback`` step, which is not in the vocabulary, and the second is the run
#: ending.
ADOPTION_ACTIONS: dict[str, str] = {
    "add_subquery": "retrieve",
    "ppr_retrieve": "ppr",
    "exact_lookup": "exact_lookup",
    "expand_graph": "expand",
    "expand_community": "expand_community",
    "follow_chain": "follow_chain",
    "enumerate_elements": "enumerate",
    "enumerate_kg_objects": "enumerate",
    "update_outline": "outline",
}
assert set(ADOPTION_ACTIONS.values()) <= set(RETRIEVAL_ACTIONS), (
    "ADOPTION_ACTIONS must resolve into RETRIEVAL_ACTIONS"
)

_POLARITY_WORDS: dict[str, str] = {
    "good": "worth reaching for",
    "bad": "rarely pays off",
}
assert set(_POLARITY_WORDS) == set(EXPERIENCE_POLARITIES), (
    "_POLARITY_WORDS must name exactly the polarities in EXPERIENCE_POLARITIES"
)


def _clean(value: object) -> str:
    """Strip + collapse all internal whitespace (incl. newlines) to single
    spaces — the same forgery defence as ``agent_profile_block._clean``, and
    needed here for a reason that does not apply there: rows in this table can
    arrive from ANOTHER deployment through ``scripts/merge_dbs.py``'s global
    union, so "the write path already collapsed it" is not a property this
    renderer may assume. A rationale carrying literal newlines could otherwise
    forge a second ``- action — verdict: ...`` row, or a fake block header,
    inside the rendered block."""
    return " ".join(str(value or "").split())


def clip_rationale(value: object) -> str:
    """One line, capped at the same length the write path enforces.

    A no-op for every entry this deployment wrote (the distillation REJECTS an
    over-length rationale rather than clipping it). It exists for the merged-in
    case above: a foreign row long enough to blow the whole-block budget would
    otherwise cost every following row its place.
    """
    line = _clean(value)
    if len(line) > RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS:
        return line[: RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS - 1] + "…"
    return line


def usable_entry(entry: Mapping[str, Any]) -> bool:
    """Whether one stored row may be shown to a model at all.

    Re-validates the closed vocabularies on the READ side even though the write
    side already did. Not belt-and-braces: this table is unioned across
    deployments, and a row can also outlive a vocabulary change (an action word
    retired in a later version is still sitting in the table). Rendering a row
    whose action word no longer exists would hand the model advice about a
    channel it has no way to invoke.
    """
    if not isinstance(entry, Mapping):
        return False
    if str(entry.get("action") or "") not in RETRIEVAL_ACTIONS:
        return False
    if str(entry.get("polarity") or "") not in EXPERIENCE_POLARITIES:
        return False
    if not _clean(entry.get("rationale")):
        return False
    return validate_situation(entry.get("situation")) is not None


def select_experiences(
    entries: Sequence[Mapping[str, Any]],
    situation: Mapping[str, Any],
    *,
    top_k: int = RETRIEVAL_EXPERIENCE_INJECT_TOP_K,
    floor: float = RETRIEVAL_EXPERIENCE_SIMILARITY_FLOOR,
) -> list[Mapping[str, Any]]:
    """The rows worth showing THIS run, best match first.

    Pure, deterministic, and in memory: the whole library is a few hundred rows
    of closed enum values, and the ranking is a set overlap no index could
    answer anyway. Zero model calls and zero embeddings — the situation space
    has an exact answer, so a vector space would add a dependency, a cache and
    a source of run-to-run variation to a comparison that does not need one.

    Ordering is ``(-similarity, -support, id)``. ``support`` breaks ties toward
    the conclusion drawn from more runs; the content-addressed ``id`` breaks the
    remaining ones so two runs over an unchanged table select the same rows —
    the property that makes the caller's memo safe.
    """
    scored: list[tuple[float, int, str, Mapping[str, Any]]] = []
    for entry in entries or ():
        if not usable_entry(entry):
            continue
        score = situation_similarity(situation, entry.get("situation") or {})
        if score < floor:
            continue
        support = entry.get("support")
        support = support if isinstance(support, int) and not isinstance(
            support, bool) else 0
        scored.append((score, support, str(entry.get("id") or ""), entry))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    # codex #524 R14 P2:每个动作只留排名最高的一条——相似指纹可以各存一条
    # 同动作条目(好/坏极性都有),而渲染块不带指纹,规划模型收到「多用 ppr」
    # 和「别用 ppr」并排时没有任何依据分辨哪条适用。蒸馏侧 _offered_entries
    # 已按同一规则去重,这里镜像它;名额在去重后消耗(同 R11)。
    picked: list[Mapping[str, Any]] = []
    actions_taken: set[str] = set()
    limit = max(0, int(top_k))
    for _score, _support, _id, entry in scored:
        if len(picked) >= limit:
            break
        action = str(entry.get("action") or "")
        if action in actions_taken:
            continue
        actions_taken.add(action)
        picked.append(entry)
    return picked


def select_consultable(
    entries: Sequence[Mapping[str, Any]],
    situation: Mapping[str, Any],
    *,
    exclude_ids: Sequence[str] = (),
    zero_hit_actions: Sequence[str] = (),
    top_k: int = CONSULT_MEMORY_TOP_K,
    floor: float = RETRIEVAL_EXPERIENCE_SIMILARITY_FLOOR,
) -> list[Mapping[str, Any]]:
    """The rows worth returning to a ``consult_memory`` call THIS turn.

    Same floor, same closed-vocabulary filter and the same per-action
    uniqueness as ``select_experiences`` (two entries about the same action —
    one "good", one "bad" — would give the model no way to tell which one
    applies), but two differences that are the entire point of this being a
    separate function rather than a call to that one with a bigger ``top_k``:

    * ``exclude_ids`` drops whatever the auto-injected block (or an earlier
      consult_memory call THIS run) already delivered — the model has already
      seen those rows every round, so returning them again would not be new
      information, it would be the SAME advice at the cost of a turn.
    * ``zero_hit_actions`` — the storage-vocabulary words this run has already
      gone quiet on (see ``reasoning_retrieval``'s ``zero_hit_by_action``) —
      sort first. That is precisely the moment a model reaching for this
      action is trying to decide whether to keep pushing on a channel that
      has stopped paying off, so an entry about THAT channel is worth more
      than one about a channel nothing in this run has touched yet.

    Ordering is therefore ``(not-zero-hit, -similarity, -support, id)``: zero-
    hit-this-run first, then the same tie-break ``select_experiences`` uses.
    """
    exclude = {str(x) for x in (exclude_ids or ())}
    zero_hit = {str(x) for x in (zero_hit_actions or ())}
    scored: list[tuple[bool, float, int, str, Mapping[str, Any]]] = []
    for entry in entries or ():
        if not usable_entry(entry):
            continue
        entry_id = str(entry.get("id") or "")
        if entry_id and entry_id in exclude:
            continue
        score = situation_similarity(situation, entry.get("situation") or {})
        if score < floor:
            continue
        support = entry.get("support")
        support = support if isinstance(support, int) and not isinstance(
            support, bool) else 0
        action = str(entry.get("action") or "")
        scored.append((action not in zero_hit, score, support, entry_id, entry))
    scored.sort(key=lambda item: (item[0], -item[1], -item[2], item[3]))
    picked: list[Mapping[str, Any]] = []
    actions_taken: set[str] = set()
    limit = max(0, int(top_k))
    for _not_zero_hit, _score, _support, _id, entry in scored:
        if len(picked) >= limit:
            break
        action = str(entry.get("action") or "")
        if action in actions_taken:
            continue
        actions_taken.add(action)
        picked.append(entry)
    return picked


def worst_experience_for(
    entries: Sequence[Mapping[str, Any]],
    situation: Mapping[str, Any],
    action: str,
    *,
    floor: float = RETRIEVAL_EXPERIENCE_SIMILARITY_FLOOR,
) -> Optional[Mapping[str, Any]]:
    """The best-matching ``bad``-polarity entry about ONE specific action, or
    ``None``.

    Used by the step-level zero-hit nudge (Agentic Memory P4, T6): when a run
    has gone quiet on one action several times in a row, this answers "does
    the library have a documented reason to expect that", so the nudge can
    quote a real rationale instead of a bare "this has come back empty" the
    model already knows from its own trace.

    Same floor and closed-vocabulary filter as ``select_experiences`` — a
    stale or foreign-deployment row about a retired action word is still
    rejected by ``usable_entry``. Deliberately does NOT touch
    ``zero_hit_actions``/``exclude_ids`` bookkeeping: the caller decides once
    per action whether to show this at all (via ``nudged_actions``), so this
    function only has to answer "what would we say".
    """
    best: Optional[Mapping[str, Any]] = None
    best_key: Optional[tuple[float, int, str]] = None
    for entry in entries or ():
        if not usable_entry(entry):
            continue
        if str(entry.get("action") or "") != action:
            continue
        if str(entry.get("polarity") or "") != "bad":
            continue
        score = situation_similarity(situation, entry.get("situation") or {})
        if score < floor:
            continue
        support = entry.get("support")
        support = support if isinstance(support, int) and not isinstance(
            support, bool) else 0
        key = (score, support, str(entry.get("id") or ""))
        if best_key is None or key > best_key:
            best_key = key
            best = entry
    return best


@dataclass(frozen=True)
class RenderedConsultBlock:
    """What ``render_consult_block`` actually put in front of the model.

    Agentic Memory P4 (修复轮 spec④/Q-P1-3): the 600-character cap drops
    whole rows, so "the caller selected N rows this call" and "N rows are now
    visible in the rendered output" can differ, and only the second one is a
    fact the caller may act on — bump the delivered-ids bookkeeping so a
    dropped row can still be offered again later, write the trace step's
    ``entries`` count, and decide whether this call counted as "found
    something new" at all. A plain ``str`` return could not carry that
    distinction without the caller re-deriving it by re-scanning the output.

    ⚠ Field named ``rendered_text`` rather than the shorter ``text`` on
    purpose: this module is one of the three the retrieval-experience privacy
    guard (``test_retrieval_experience_privacy_guard.py``) statically scans
    for a fixed list of dangerous free-text identifiers, and ``text`` is one
    of them (a document's body, in every OTHER module that name would refer
    to) — the guard cannot tell "this text is the module's own bounded,
    already-privacy-checked render output" apart from "this text is raw
    document content" from the identifier alone, so it does not try; the
    identifier itself has to stay off the list.
    """

    rendered_text: str
    #: Ids of the ``rows`` entries that actually made it into
    #: ``rendered_text`` — a STRICT subset of what the caller passed in, in
    #: the same order.
    delivered_ids: tuple[str, ...]
    #: Whether ``extra_lines`` (the profile-overlay note, at most one line)
    #: made it into ``rendered_text``.
    overlay_rendered: bool


def render_consult_block(
    rows: Sequence[Mapping[str, Any]],
    extra_lines: Sequence[str] = (),
) -> RenderedConsultBlock:
    """Render a ``consult_memory`` result as one prompt block, hard-capped at
    ``CONSULT_MEMORY_BLOCK_MAX_CHARS`` — same shape as
    ``render_experience_block`` (rows dropped whole rather than clipped), with
    its own header so the model can tell "I asked for this" apart from "this
    showed up unasked every round".

    ``extra_lines`` carries the caller's own not-yet-delivered profile-overlay
    note (Agentic Memory P4 T5's "your own earlier notes" half) — pre-cleaned
    free text, rendered as one more ``- `` row inside the SAME cap rather than
    as a second block, because both halves answer the same question ("what do
    we already know that might help right now") and a model reading two
    separately-capped blocks back to back has no way to tell they are related.

    ⚠ Agentic Memory P4 (修复轮 Q-P1-3): ``extra_lines`` renders FIRST, ahead
    of ``rows`` — the overlay note is a single, bounded, personal signal (this
    member's own retrieval notes, no other channel surfaces them), where a
    library row is one of possibly many shared tactics that can simply be
    offered again on a later call if it gets crowded out this time. When
    budget is tight, the scarcer signal should win the seat.

    ``rows`` is expected to be the RUN's whole accumulated selection so far
    (the caller re-renders the full set on every ``consult_memory`` call
    rather than appending a freshly-capped block per call — see the call
    site), which is what keeps two calls in one run inside one 600-character
    budget instead of two.
    """
    lines: list[str] = []
    delivered_ids: list[str] = []
    overlay_rendered = False
    budget = (
        CONSULT_MEMORY_BLOCK_MAX_CHARS - len(_CONSULT_HEADER)
        - len(_CONSULT_GUIDANCE) - 2
    )
    for extra in extra_lines or ():
        cleaned = _clean(extra)
        if not cleaned:
            continue
        row = f"- {cleaned}"
        if len(row) + 1 > budget:
            break
        budget -= len(row) + 1
        lines.append(row)
        overlay_rendered = True
    for entry in rows or ():
        if not usable_entry(entry):
            continue
        action = _ACTION_IDS[str(entry.get("action"))]
        verdict = _POLARITY_WORDS[str(entry.get("polarity"))]
        row = f"- {action} — {verdict}: {clip_rationale(entry.get('rationale'))}"
        if len(row) + 1 > budget:
            break
        budget -= len(row) + 1
        lines.append(row)
        delivered_ids.append(str(entry.get("id") or ""))
    if not lines:
        return RenderedConsultBlock(
            rendered_text="", delivered_ids=(), overlay_rendered=False)
    joined = "\n".join([_CONSULT_HEADER, _CONSULT_GUIDANCE, *lines])
    return RenderedConsultBlock(
        rendered_text=joined, delivered_ids=tuple(delivered_ids),
        overlay_rendered=overlay_rendered,
    )


def render_experience_block(entries: Sequence[Mapping[str, Any]]) -> str:
    """Render the selected entries as one prompt block, hard-capped at
    ``RETRIEVAL_EXPERIENCE_BLOCK_MAX_CHARS``.

    Empty string when nothing survives — an empty header would spend prompt
    budget to tell the model nothing, and (unlike the collection map, where
    "zero formulas" is itself a fact) "no tactics recorded yet" has no
    informative zero to report. That is also the normal state of a deployment
    that has not distilled anything yet, and of every deployment while the
    injection switch is off.

    Rows that would push the block past the cap are dropped whole rather than
    clipped (see the module docstring). A block whose FIRST row does not fit is
    rendered as the empty string rather than as a header with no rows.
    """
    rows: list[str] = []
    budget = RETRIEVAL_EXPERIENCE_BLOCK_MAX_CHARS - len(_HEADER) - len(_GUIDANCE) - 2
    for entry in entries or ():
        if not usable_entry(entry):
            continue
        action = _ACTION_IDS[str(entry.get("action"))]
        verdict = _POLARITY_WORDS[str(entry.get("polarity"))]
        row = f"- {action} — {verdict}: {clip_rationale(entry.get('rationale'))}"
        if len(row) + 1 > budget:
            break
        budget -= len(row) + 1
        rows.append(row)
    if not rows:
        return ""
    return "\n".join([_HEADER, _GUIDANCE, *rows])


def rendered_row_count(rendered: str) -> int:
    """How many ``- ...`` rows are actually present in an already-rendered
    block. The DELIVERED count, for the same reason as P1's namesake: the
    whole-block cap can drop trailing rows, so counting the pre-render
    selection over-reports what the model saw.
    """
    return sum(1 for line in rendered.splitlines() if line.startswith("- "))


def adopted_entry_ids(
    entries: Sequence[Mapping[str, Any]], chosen_actions: Sequence[str]
) -> list[str]:
    """Which injected entries the run actually acted on.

    The intersection of "what we suggested" and "what the reflect loop then
    chose", resolved through ``ADOPTION_ACTIONS`` so both halves speak the
    stored vocabulary. Order follows ``entries`` and duplicates are dropped, so
    one entry is counted at most once per run however many times its action was
    chosen — ``adopted`` is meant to rank entries against each other, and a run
    that happens to walk the graph five times is not five times the evidence
    that the advice was good.

    ⚠ Only ``polarity == "good"`` entries can be adopted. A ``bad`` entry's
    advice is "avoid this channel" — if the model reaches for that action
    anyway (several of these actions also fire deterministically regardless of
    any hint, see ``ADOPTION_ACTIONS``'s own docstring), that is the model
    ignoring the advice, not following it. Counting it would credit exactly
    the entries whose advice was disregarded, which is the opposite of what
    the eviction ordering's ``adopted`` column is supposed to measure.

    ``entries`` must already be the DELIVERED set (the caller is expected to
    slice its selection down to ``rendered_row_count(block)`` before calling
    this) — an entry the block's character cap dropped was never shown to the
    model, so the model choosing that same action for unrelated reasons must
    not be credited as adoption of advice it never saw.
    """
    wanted = {
        ADOPTION_ACTIONS[action]
        for action in chosen_actions or ()
        if action in ADOPTION_ACTIONS
    }
    if not wanted:
        return []
    seen: set[str] = set()
    adopted: list[str] = []
    for entry in entries or ():
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("action") or "") not in wanted:
            continue
        if str(entry.get("polarity") or "") != "good":
            continue
        entry_id = str(entry.get("id") or "")
        if not entry_id or entry_id in seen:
            continue
        seen.add(entry_id)
        adopted.append(entry_id)
    return adopted


__all__ = [
    "ADOPTION_ACTIONS",
    "CONSULT_MEMORY_BLOCK_MAX_CHARS",
    "CONSULT_MEMORY_TOP_K",
    "RETRIEVAL_EXPERIENCE_BLOCK_MAX_CHARS",
    "RETRIEVAL_EXPERIENCE_INJECT_TOP_K",
    "RETRIEVAL_EXPERIENCE_SIMILARITY_FLOOR",
    "RenderedConsultBlock",
    "action_id_for",
    "adopted_entry_ids",
    "clip_rationale",
    "render_consult_block",
    "render_experience_block",
    "rendered_row_count",
    "select_consultable",
    "select_experiences",
    "usable_entry",
    "worst_experience_for",
]
