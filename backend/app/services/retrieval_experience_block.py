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

from typing import Any, Mapping, Sequence

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
    return [entry for _score, _support, _id, entry in scored[: max(0, int(top_k))]]


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
        entry_id = str(entry.get("id") or "")
        if not entry_id or entry_id in seen:
            continue
        seen.add(entry_id)
        adopted.append(entry_id)
    return adopted


__all__ = [
    "ADOPTION_ACTIONS",
    "RETRIEVAL_EXPERIENCE_BLOCK_MAX_CHARS",
    "RETRIEVAL_EXPERIENCE_INJECT_TOP_K",
    "RETRIEVAL_EXPERIENCE_SIMILARITY_FLOOR",
    "adopted_entry_ids",
    "clip_rationale",
    "render_experience_block",
    "rendered_row_count",
    "select_experiences",
    "usable_entry",
]
