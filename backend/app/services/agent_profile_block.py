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

from app.models.agent_profile import PROFILE_LABEL_ORDER

# PROFILE_LABEL_ORDER: the five app-layer labels, in render order. Canonical
# definition lives in ``app.models.agent_profile`` (a lower layer this module
# can import forward without creating the reverse ``app.models -> app.services``
# edge it used to be) -- see ``scripts/architecture_boundary_baseline.json`` ::
# core_models_service_imports (now empty). This tuple is also the whitelist: a
# row whose label is not here renders nothing at all. Dropping beats
# rendering-at-the-end because the block's whole point is a fixed, predictable
# shape the planning model sees identically on every round of every run; an
# unknown label can only come from a future writer that has not been taught
# what this block means yet, and a stray line is worse than a missing one.

# Human-readable name per label. A mismatch against ``PROFILE_LABEL_ORDER`` is
# NOT a KeyError at runtime in either direction: a label present only in
# ``PROFILE_LABEL_ORDER`` is silently dropped by ``selected_profile_blocks``
# (it filters on ``_LABEL_NAMES`` membership), and a label present only here
# is silently dropped by ``render_profile_block``'s final loop (which only
# walks ``PROFILE_LABEL_ORDER``). The assert right below the dict is what
# turns "added a label in one place but not the other" into a hard failure
# at import time instead of a quietly missing prompt line.
_LABEL_NAMES: dict[str, str] = {
    "corpus_shape": "corpus shape",
    "key_entities": "key entities",
    "corpus_gaps": "corpus gaps",
    "retrieval_notes": "retrieval notes",
    "usage_gaps": "usage gaps",
}
assert set(PROFILE_LABEL_ORDER) == set(_LABEL_NAMES), (
    "PROFILE_LABEL_ORDER and _LABEL_NAMES must name exactly the same labels"
)

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

#: Page size for resolving Agent id → display name (``list_agent_profiles``'s
#: ``(offset, limit)`` pair). One deployment-wide account's Agent roster is
#: what both consumers below page through in full (offset 0, this limit) to
#: build an in-memory id→name map — a single point constant rather than each
#: call site inlining its own ``100`` literal, so the two stay the same
#: number instead of drifting into "why is the MCP tool's roster page one
#: size and the API route's another" the day someone tunes one of them.
#: Shared by ``app.api.mcp_server._profile_names`` (the MCP read surface) and
#: ``app.api.agent_profile_routes._observation_agent_names`` (the "Agent 记录"
#: read endpoint) — see each function's own docstring for why the two are
#: the same lookup by construction.
AGENT_PROFILE_NAME_PAGE = 100


def resolve_agent_profile_names(list_profiles, owner_id: str) -> dict:
    """Full id→name roster for ONE owner, paging until the roster runs dry.

    codex #535 R2 P2: the previous single-page ``(0, AGENT_PROFILE_NAME_PAGE)``
    read silently dropped every profile past the first page — an owner with
    more than ``AGENT_PROFILE_NAME_PAGE`` Agent profiles saw observations from
    the older ones attributed to the unknown-Agent fallback. There is no
    protocol cap on an owner's profile count, so a fixed result-changing page
    was exactly the numeric-limit shape CLAUDE.md's red line forbids. Paging
    is bounded by the owner's own roster size (each page issues one bounded
    query); both consumers (``mcp_server._profile_names`` and
    ``agent_profile_routes._observation_agent_names``) call THIS helper so the
    two stay one lookup by construction.
    """
    names: dict = {}
    offset = 0
    while True:
        page = list(list_profiles(owner_id, offset, AGENT_PROFILE_NAME_PAGE))
        for profile in page:
            names[profile.id] = profile.name
        if len(page) < AGENT_PROFILE_NAME_PAGE:
            return names
        offset += AGENT_PROFILE_NAME_PAGE


def _clean(value: object) -> str:
    """Strip + collapse all internal whitespace (incl. newlines) to single
    spaces (mirrors ``reasoning_retrieval._outline_text`` for model-authored
    free text). A multi-line value that only got its ends stripped could
    forge a fake second ``- name (scope): value`` row — or even a fake
    ``[Collections in scope]``-style header — inside the rendered block by
    embedding literal newlines; collapsing to one line before this string
    ever reaches an ``f"- {name} ({scope}): {value}"`` line makes that
    forgery structurally impossible, not just discouraged."""
    return " ".join(str(value or "").split())


#: Public alias for ``_clean``. Agentic Memory P3 (T3-T5 fix round):
#: ``agent_profile_job.render_usage_block`` renders one more untrusted,
#: model-independent free-text field this module never sees —
#: ``agent_observations.text`` (an external Agent's own words, written via
#: the ``add_observation`` MCP tool) — into a line of that same
#: ``f"- [{label}] {text}"`` shape this docstring describes the forgery risk
#: for. The risk is identical: an observation whose text contains a literal
#: newline could forge a fake blank line followed by a fabricated
#: ``[End of untrusted...]``/``[Verified system note...]``-style header,
#: and the untrusted-instruction framing around the section (this module's
#: own docstring's whole point) would not save a reader who only skims the
#: rendered block — the forged header would just look like the next thing
#: the system said. One implementation, not a second copy that could drift:
#: the alias is exported rather than reimplemented so both call sites answer
#: "is this text safe to drop into one rendered line" with the exact same
#: function.
collapse_prompt_line = _clean


def clip_block_value(value: object) -> str:
    """One line, capped at ``AGENT_PROFILE_VALUE_MAX_CHARS``, "…" on overflow.

    The ONE implementation of "make this text safe and small enough to be a
    block value", shared by this renderer and by the consolidation job's write
    path (``agent_profile_job``). Two copies drifted apart trivially — the job
    would store a 400-character value that the renderer then re-clipped to 399,
    and the value a user sees in the panel would differ from the value the
    model was shown — so the collapse and the cap live together, once.
    """
    text = _clean(value)
    if len(text) > AGENT_PROFILE_VALUE_MAX_CHARS:
        return text[: AGENT_PROFILE_VALUE_MAX_CHARS - 1] + "…"
    return text


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
        lines.append(f"- {name} ({scope}): {clip_block_value(block.get('value'))}")
    text = "\n".join(lines)
    if len(text) > AGENT_PROFILE_BLOCK_MAX_CHARS:
        return text[: AGENT_PROFILE_BLOCK_MAX_CHARS - 1] + "…"
    return text


def rendered_row_count(rendered: str) -> int:
    """How many ``- name (scope): value`` rows are actually present in an
    already-rendered block string.

    NOT the same as ``len(selected_profile_blocks(...))``: the whole-block
    cap in ``render_profile_block`` can truncate away entire trailing rows
    (five oversized blocks routinely collapse the whole-block char budget
    down to two or three rows actually delivered), so counting the
    pre-truncation selection over-reports how many rows the model saw. The
    caller (the trace step's ``blocks`` field) wants the delivered count.
    """
    return sum(1 for line in rendered.splitlines() if line.startswith("- "))
