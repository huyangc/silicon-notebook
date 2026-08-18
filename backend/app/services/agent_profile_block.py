"""Render the agent's per-notebook "understanding" blocks as ONE prompt block.

Agentic Memory P1 (T3), design doc §5.2. The block rides in every plan and
every reflect prompt of a run, so — exactly like
``collection_catalog.render_collection_map``, which this module deliberately
mirrors — its worst case has to be a CONSTANT, not a function of how much the
consolidation job happened to write. Hence two hard caps below and no
dependency on the retrieval effort tier: the value of the block is "aim the
retrieval better", and a tier-scaled version of that would make the cheapest
tier the one that most needs the hint and least gets it.

English scaffolding on purpose (same reason as the collection map): this is
prompt scaffolding sitting next to the other English instructions, not
user-facing copy, so the interface-vocabulary guard does not apply to it.

⚠ Hard boundary (design §5.2): the rendered block reaches the PLANNING and
REFLECTION models only. It must never enter the answer-synthesis context —
it is the agent's own accumulated impression, not evidence, and nothing in it
is citable with ``[k]``. That is why ``ReasoningResult`` carries no field for
it: ``ask_service`` cannot forward what it never receives.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

# The five app-layer labels, in render order. This tuple is also the whitelist:
# a row whose label is not here renders nothing at all. Dropping beats
# rendering-at-the-end because the block's whole point is a fixed, predictable
# shape the planning model sees identically on every round of every run; an
# unknown label can only come from a future writer that has not been taught
# what this block means yet, and a stray line is worse than a missing one.
PROFILE_LABEL_ORDER: tuple[str, ...] = (
    "corpus_shape",
    "key_entities",
    "corpus_gaps",
    "retrieval_notes",
    "usage_gaps",
)

# Human-readable name per label. Kept next to the order tuple so adding a label
# in one place without the other is a KeyError at import-adjacent test time
# rather than a silently unnamed line in a production prompt.
_LABEL_NAMES: dict[str, str] = {
    "corpus_shape": "corpus shape",
    "key_entities": "key entities",
    "corpus_gaps": "corpus gaps",
    "retrieval_notes": "retrieval notes",
    "usage_gaps": "usage gaps",
}

# Per-block cap. The API (T6) rejects longer user edits outright with a 422 and
# the consolidation job (T4) clamps its own output, so reaching this here means
# a writer misbehaved — truncate rather than let one block eat the whole block
# budget and silently push the others out.
AGENT_PROFILE_VALUE_MAX_CHARS = 400
# Whole-block cap, INCLUDING the header and the guidance line. Same shape as
# ``COLLECTION_MAP_MAX_CHARS``: overflow keeps a prefix and appends "…" so the
# model can see that it is reading a truncated block.
AGENT_PROFILE_BLOCK_MAX_CHARS = 1200

_HEADER = "[What the agent knows about this library]"
# One line of framing, inside the cap. Without it the block reads like a set of
# established facts, and a planning model has no way to tell "this is a hunch
# accumulated from earlier work here" from "this was retrieved just now".
_GUIDANCE = (
    "Background from earlier work in this library — use it to aim retrieval. "
    "It is NOT evidence: never cite it and never state it as a finding."
)


def _clean(value: object) -> str:
    return str(value or "").strip()


def selected_profile_blocks(
    blocks: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """The rows this module will actually render, in render order.

    Exposed separately (rather than kept private inside ``render_profile_block``)
    because the caller has to report HOW MANY blocks the model was given in its
    trace step, and re-deriving "which rows count" at the call site is exactly
    the kind of second implementation that drifts: cleared blocks keep their row
    (``clear_block`` empties the value but preserves history), so
    ``len(read_blocks(...))`` is NOT the number of blocks the model received.

    Order: the fixed label order above, and within one label the shared base
    layer before the caller's own overlay — so the block reads the same way on
    every round regardless of which rows exist.
    """
    by_label: dict[str, list[Mapping[str, Any]]] = {}
    for block in blocks or ():
        label = _clean(block.get("label"))
        if label not in _LABEL_NAMES:
            continue
        if not _clean(block.get("value")):
            continue
        by_label.setdefault(label, []).append(block)
    selected: list[Mapping[str, Any]] = []
    for label in PROFILE_LABEL_ORDER:
        rows = by_label.get(label) or []
        # base ('') first, then the caller's overlay. ``read_blocks`` can only
        # ever return those two owner values for one caller (its SQL predicate
        # is ``owner_id IN ('', ?)``), so this is a two-way sort, not a scan.
        rows.sort(key=lambda row: _clean(row.get("owner_id")) != "")
        selected.extend(rows)
    return selected


def render_profile_block(blocks: Sequence[Mapping[str, Any]]) -> str:
    """Render the blocks as one prompt block, hard-capped at
    ``AGENT_PROFILE_BLOCK_MAX_CHARS``. Empty string when there is nothing to
    say — an empty header would spend prompt budget to tell the model nothing,
    and (unlike the collection map, where "zero formulas" is itself a fact) an
    absent understanding block has no informative zero to report.

    ``(shared)`` / ``(yours)`` marks each line's provenance: the model should
    weigh "this library is mostly datasheets" (everyone's view) differently
    from "this member keeps asking about timing closure" (one member's), and
    without the marker the two are indistinguishable in a flat list.
    """
    selected = selected_profile_blocks(blocks)
    if not selected:
        return ""
    lines = [_HEADER, _GUIDANCE]
    for block in selected:
        name = _LABEL_NAMES[_clean(block.get("label"))]
        scope = "shared" if not _clean(block.get("owner_id")) else "yours"
        value = _clean(block.get("value"))
        if len(value) > AGENT_PROFILE_VALUE_MAX_CHARS:
            value = value[: AGENT_PROFILE_VALUE_MAX_CHARS - 1] + "…"
        lines.append(f"- {name} ({scope}): {value}")
    text = "\n".join(lines)
    if len(text) > AGENT_PROFILE_BLOCK_MAX_CHARS:
        return text[: AGENT_PROFILE_BLOCK_MAX_CHARS - 1] + "…"
    return text
