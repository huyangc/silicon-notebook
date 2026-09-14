"""Core-owned contracts for the ``ask.reflect_action`` extension point.

A reflect action is a *function a deployment plugin lends to the retrieval
agent*.  One contribution declares one action; its name, its one-paragraph
description and its parameter descriptions are projected into the reflect
prompt, into the reflect schema hint and into the action whitelist, and the
model then decides for itself whether to call it after the in-library channels
have come back empty.  What comes back is external evidence: it enters
synthesis, it is citable with ``[k]``, and it carries a URL.

Design document: ``docs/superpowers/specs/2026-09-13-reflect-plugin-action-design_zh.md``.

Three properties of this module are structural rather than merely documented:

* **The descriptor is deployment configuration, not user data.**  Every rail
  below is therefore a *loud* rail: a description that is too long, or that
  carries a newline or another control character, fails registration
  (``ExtensionRegistryError``, i.e. startup) instead of being clipped.  Silently
  clamping would show the model a different description than the deployment
  wrote — and a newline surviving into the prompt would let plugin text
  masquerade as a new template rule.  The counterpart rail on *plugin results*
  (``EXTERNAL_EVIDENCE_*``) is a different matter: that is runtime data from
  outside and the host bounds it per call.

* **The return shape is settled, once.**  ``invoke()`` returns
  :class:`~app.extension_sdk.reflect_action.ReflectActionResult`, which carries
  ``status`` and ``failure`` itself — the two fields ``ContributorResult`` would
  have contributed — plus ``note``.  The host does **not** wrap it in a second
  ``ContributorResult``.  The design document left this as a two-way choice
  (§二); this is the ruling, and there is exactly one return shape.
  ``ReflectActionResult`` is the one contract type that lives in the SDK module
  rather than here, because ``ExtensionResultStatus``/``ExtensionFailure`` live
  in ``app.extension_sdk.contracts`` and ``app.domain`` may not import the SDK
  (``scripts/check_architecture_boundaries.py`` :: ``FORBIDDEN_DOMAIN_PREFIXES``).

* **The model never sees a plugin id.**  :class:`ReflectActionSpec` is the
  core-internal projection that pairs a descriptor with its origin; the prompt,
  the schema, the candidate summary and the synthesis block carry only the
  action name and the ``source_label``.  ``plugin_id`` reaches the trace detail
  and the citation provenance, nowhere else.

This module must keep importing nothing from ``app.*`` and nothing third-party.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, Literal, Protocol


# --- Descriptor-side rails (what a plugin may DECLARE) ----------------------
# Every one of these is checked LOUDLY at registration: over the rail is a
# startup failure, never a clamp.  See the module docstring for why.
REFLECT_ACTION_NAME_MAX_CHARS = 32
REFLECT_ACTION_DESCRIPTION_MAX_CHARS = 600
REFLECT_ACTION_PARAMETERS_MAX = 4
REFLECT_ACTION_PARAM_DESCRIPTION_MAX_CHARS = 200
REFLECT_ACTION_ENUM_VALUES_MAX = 8

# --- Runtime clamp rails (what is CUT DOWN mid-run, not refused) ------------
# The opposite discipline to the block above, and the reason these two do not
# live in it: both bound text produced while a run is in flight -- the model's
# own argument (clamped by the reflect parser) and the plugin's one-sentence
# note (clamped by the host) -- and neither is deployment configuration anyone
# reviewed at boot.  Refusing them would fail a live question over a stray
# character; clamping is right here and wrong for a descriptor.
REFLECT_ACTION_ARGUMENT_MAX_CHARS = 300
REFLECT_ACTION_NOTE_MAX_CHARS = 300

# --- Result-side rails (what one call may bring back) -----------------------
# Runtime data from outside the deployment; bounded per call by the host.
EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL = 5
EXTERNAL_EVIDENCE_TITLE_MAX_CHARS = 200
EXTERNAL_EVIDENCE_EXCERPT_MAX_CHARS = 800
EXTERNAL_EVIDENCE_URL_MAX_CHARS = 2048
EXTERNAL_EVIDENCE_SOURCE_LABEL_MAX_CHARS = 40
EXTERNAL_EVIDENCE_LOCATION_LABEL_MAX_CHARS = 60
EXTERNAL_EVIDENCE_REFLECT_BLOCK_CHARS = 1600
EXTERNAL_EVIDENCE_CONTEXT_CHARS = 4000


# The action id the model types: ``^[a-z][a-z0-9_]{2,31}$``.  At least three
# characters, because a one- or two-letter action reads as a typo beside
# ``answer``/``expand_graph`` and is far easier to collide with by accident.
# The upper bound is INTERPOLATED from ``REFLECT_ACTION_NAME_MAX_CHARS`` rather
# than re-typed, so the named rail and the pattern cannot drift apart.
REFLECT_ACTION_NAME_RE = re.compile(
    rf"^[a-z][a-z0-9_]{{2,{REFLECT_ACTION_NAME_MAX_CHARS - 1}}}$"
)
# Parameter names are shorter on purpose: they are rendered inside the prompt
# line as ``<action>.<param>`` and inside a nested schema object, so a long one
# costs twice.
REFLECT_ACTION_PARAMETER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
REFLECT_ACTION_ENUM_VALUE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# ``name`` and ``reason`` are the two words a model most readily writes at the
# top level of a reflect object; a parameter spelled either way inside the
# action's nested object invites it to fill the outer field instead.
RESERVED_REFLECT_PARAMETER_NAMES = frozenset({"name", "reason"})

# Anything in this class inside a descriptor string is a registration failure,
# never a strip.  The set is wider than "control characters" in the ASCII sense
# on purpose -- see ``contains_control_characters``.
_CONTROL_CHARACTERS_RE = re.compile(
    "["
    "\x00-\x1f"        # C0, newline and carriage return included
    "\x7f-\x9f"        # DEL and the C1 block
    "  "     # LINE SEPARATOR, PARAGRAPH SEPARATOR
    "‪-‮"    # bidi embedding/override
    "⁦-⁩"    # bidi isolates
    "]"
)

# The same class as one or more consecutive characters, for the folding
# counterpart below.  Derived from the pattern above rather than re-typed, so
# "what must never reach the prompt" has exactly one definition.
_CONTROL_CHARACTER_RUN_RE = re.compile(_CONTROL_CHARACTERS_RE.pattern + "+")

# Every action id the core itself offers in the reflect loop, with every gate
# open.  A plugin action that took one of these names would be shadowed by (or
# would shadow) a core action in the whitelist and the dispatch chain.
CORE_REFLECT_ACTION_IDS = frozenset({
    "answer",
    "add_subquery",
    "search_elements",
    "exact_lookup",
    "expand_graph",
    "ppr_retrieve",
    "expand_community",
    "follow_chain",
    "search_chunks",
    "enumerate_elements",
    "enumerate_kg_objects",
    "update_outline",
    "consult_memory",
})

# Every TOP-LEVEL field name of the reflect schema hint, with every gate open.
# A plugin action's parameters arrive as one nested object keyed by the action
# name, so an action named after a schema field would make the model write its
# arguments into a core slot — ``"expand": {...}`` is a graph traversal, not a
# plugin call, and the parser would read it as one.
REFLECT_SCHEMA_TOP_LEVEL_FIELDS = frozenset({
    "sufficient",
    "next_action",
    "reason",
    "expand",
    "new_sub_query",
    "follow_chain",
    "enumerate",
    "outline",
    "community_focal",
    "elements_query",
    "ppr_query",
    "exact_term",
    "chunks_query",
})

# The union is what registration rejects.  It lives in ``app.domain`` rather
# than beside ``reflect_schema_hint`` in ``app.services.prompts`` for a hard
# reason, not a stylistic one: the check runs in
# ``app.extensions.registry``, and ``app.extensions.*`` may not import
# ``app.services.*`` (architecture boundary guard).  Domain is the one layer
# both the registry and the reflect projection may read.  The set is kept
# honest against prompt drift by a reflective test that re-derives it from
# ``reflect_schema_hint`` with every gate open
# (``backend/tests/test_reflect_action_contract.py``).
RESERVED_REFLECT_KEYS: frozenset[str] = (
    CORE_REFLECT_ACTION_IDS | REFLECT_SCHEMA_TOP_LEVEL_FIELDS
)


def contains_control_characters(value: str) -> bool:
    """True when ``value`` holds a character that must never reach the prompt.

    Wider than the ASCII control range, because the threat is not "an unusual
    byte" but "text that stops behaving like one plain line of a template":

    * **C0 (U+0000-U+001F) and DEL (U+007F).**  A ``\\n`` here is the direct
      attack — a descriptor that ends a prompt line and starts what reads as
      another core rule.
    * **C1 (U+0080-U+009F).**  Includes NEL (U+0085), which several tokenizers
      and renderers treat as a line break exactly like ``\\n``.
    * **U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR.**  These *are* line
      breaks to most renderers and to JavaScript's own string grammar; a rail
      that only knew ``\\n`` would be trivially stepped around with one of them.
    * **Bidi controls (U+202A-U+202E, U+2066-U+2069).**  An unterminated
      override reverses the visual order of everything after it, so the line an
      operator reviews in the descriptor and the line a human sees rendered can
      say different things.

    Used on descriptor strings, where True means "refuse to start", not "strip
    and carry on".
    """

    return _CONTROL_CHARACTERS_RE.search(value) is not None


def fold_control_characters(value: str) -> str:
    """Collapse every run of the characters above into a single space.

    The *runtime-data* counterpart of :func:`contains_control_characters`, and
    the reason the two live side by side rather than one calling the other: a
    descriptor is deployment configuration reviewed at boot, so a control
    character there is a refusal; a plugin's **result** arrives mid-question
    from outside, and refusing a live answer over a stray newline in someone
    else's abstract would be the wrong trade.

    The threat is different too, and sharper.  Evidence blocks are rendered one
    item per line, ``k7: [external · …] …``, and the reverse binding is read
    back off exactly that shape.  An excerpt carrying ``\\nk1: [chunk][personal]
    …`` would therefore render as a SECOND evidence line the core never wrote —
    a fabricated notebook citation, attributed to the user's own library, sitting
    in the middle of the model's evidence.  Folding is done once here, at the
    render contract, and independently of whatever the host already rejected:
    the invariant is a property of the block format, so it belongs to whoever
    owns the format.

    A run of several such characters folds to ONE space (``"a\\r\\n\\r\\nb"`` ->
    ``"a b"``) rather than one space each: the point is a single readable line,
    not a faithful byte count of what was stripped.
    """

    return _CONTROL_CHARACTER_RUN_RE.sub(" ", value)


@dataclass(frozen=True, slots=True)
class ReflectActionParameter:
    """One argument the model may write for a plugin action.

    ``kind`` is ``"text"`` or ``"enum"`` and there is deliberately no boolean
    and no integer: the reflect argument parser reads every value as a string
    (a model answering ``"true"`` or ``"yes"`` is routine), and a string
    example is what the schema hint can express faithfully. The shared shape
    walk (``model_json._collect_shape_deviations``) only reports a bool
    mismatch nowadays, but the projection still spells choices as ``a|b``
    strings so the hint and the parser describe the same contract.
    """

    name: str
    description: str
    kind: Literal["text", "enum"]
    values: tuple[str, ...] = ()
    required: bool = False


@dataclass(frozen=True, slots=True)
class ReflectActionDescriptor:
    """Everything a plugin declares about the function it lends the agent.

    ``description`` and the parameter descriptions are the only plugin-authored
    text that reaches the model.  The four fixed sentences around them — that
    the material is external, that it is citable, when to reach for it, and
    that arguments must not be copied out of the candidates — are core template
    text a plugin cannot edit.
    """

    name: str
    description: str
    source_label: str
    parameters: tuple[ReflectActionParameter, ...] = ()
    max_calls_per_run: int = 1


@dataclass(frozen=True, slots=True)
class ReflectActionItem:
    """One piece of material a plugin declares as quotable.

    ``excerpt`` is shown and fed to synthesis verbatim — the core never
    re-summarizes it, so the citation card shows exactly what the plugin put
    here.
    """

    title: str
    excerpt: str
    url: str
    location_label: str = ""


@dataclass(frozen=True, slots=True)
class ReflectActionSpec:
    """Core-internal projection of one registered, currently offerable action.

    ``plugin_id`` is present so the trace and the citation provenance can name
    the origin; it never reaches the prompt, the schema, the candidate summary
    or the synthesis block (design document §九 invariant 7).
    """

    contribution_id: str
    plugin_id: str
    descriptor: ReflectActionDescriptor


@dataclass(frozen=True, slots=True)
class ReflectActionCall:
    """Core-only call state for one plugin action invocation.

    The CORE-facing half of the pair, exactly as ``GapConsultCallContext`` is
    to ``GapConsultExtensionContext``: the reflect loop builds this, the host
    translates it into the SDK's ``ReflectActionCallContext`` on the worker
    thread.  Two types rather than one because ``app.services`` may not import
    the Extension SDK at all (``scripts/check_architecture_boundaries.py``
    keeps SDK imports inside the composition roots and the plugins), so the
    loop needs a shape that lives in a layer it is allowed to read.

    Note what is absent, as at ``ask.gap_consult``: no notebook, actor, source,
    candidate text, sub-query, gap phrase, retrieval scope or core port.
    ``question`` is wording the user actually saw — never the synthesized
    ``research_question`` (design document §九 invariant 1) — and ``arguments``
    are the model's own, already checked against the descriptor.
    """

    question: str
    arguments: Mapping[str, str]
    cancellation: Any
    deadline_monotonic: float
    max_items: int


@dataclass(frozen=True, slots=True)
class ReflectActionOutcome:
    """What the host answers the reflect loop for one attempted call.

    The host's product, not the plugin's: ``items`` have already been
    type-checked, length-clamped, scheme-checked and de-duplicated.
    ``failure_code`` non-empty means nothing was admitted and the loop records
    a skip step carrying it.  It is USUALLY a core-minted code
    (``plugin_action_failed``/``_timeout``/``_cancelled``/``_invalid_result``/
    ``_unavailable``); the one exception is a contributor that declared itself
    UNAVAILABLE and supplied its own failure code, which is passed through
    after the same shape gate every plugin string goes through (lower-case
    ``^[a-z][a-z0-9_]*$``, length-bounded).  Either way it is a stable code
    safe to put in a trace detail — never free text.

    ``truncated`` says the plugin offered more than this call's remaining
    admission slots — the loop discloses it in the trace so "the plugin found
    two things" and "the plugin found forty and we took two" are not the same
    line.  Keys are deliberately absent: minting ``ext:{plugin_id}:{n}`` is
    run-scoped, so only the loop can do it (design document §6.1).
    """

    items: tuple[ReflectActionItem, ...] = ()
    note: str = ""
    failure_code: str = ""
    truncated: bool = False


class ReflectActionHostPort(Protocol):
    """The seat ``ReasoningRetriever`` is given, if any.

    Both halves take core types only: :class:`ReflectActionCall` in, and
    :class:`ReflectActionOutcome` out.  The SDK-facing shapes never cross this
    seam, which is what lets ``app.services`` hold the seat at all.
    """

    def specs(
        self,
        deadline_monotonic: float,
        *,
        cancellation: Any = None,
    ) -> tuple["ReflectActionSpec", ...]: ...

    def invoke(
        self, spec: "ReflectActionSpec", call: ReflectActionCall
    ) -> ReflectActionOutcome: ...


@dataclass(frozen=True, slots=True)
class ExternalEvidence:
    """One admitted external item, keyed by the core.

    ``key`` is minted per run as ``ext:{plugin_id}:{n}`` and is used as the
    ``object_id`` of the resulting anchor/citation.  Design document §6.1.
    """

    key: str
    plugin_id: str
    action: str
    source_label: str
    title: str
    excerpt: str
    url: str
    location_label: str = ""


__all__ = [
    "CORE_REFLECT_ACTION_IDS",
    "EXTERNAL_EVIDENCE_CONTEXT_CHARS",
    "EXTERNAL_EVIDENCE_EXCERPT_MAX_CHARS",
    "EXTERNAL_EVIDENCE_LOCATION_LABEL_MAX_CHARS",
    "EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL",
    "EXTERNAL_EVIDENCE_REFLECT_BLOCK_CHARS",
    "EXTERNAL_EVIDENCE_SOURCE_LABEL_MAX_CHARS",
    "EXTERNAL_EVIDENCE_TITLE_MAX_CHARS",
    "EXTERNAL_EVIDENCE_URL_MAX_CHARS",
    "ExternalEvidence",
    "REFLECT_ACTION_ARGUMENT_MAX_CHARS",
    "REFLECT_ACTION_DESCRIPTION_MAX_CHARS",
    "REFLECT_ACTION_ENUM_VALUES_MAX",
    "REFLECT_ACTION_ENUM_VALUE_RE",
    "REFLECT_ACTION_NAME_MAX_CHARS",
    "REFLECT_ACTION_NAME_RE",
    "REFLECT_ACTION_NOTE_MAX_CHARS",
    "REFLECT_ACTION_PARAMETERS_MAX",
    "REFLECT_ACTION_PARAMETER_NAME_RE",
    "REFLECT_ACTION_PARAM_DESCRIPTION_MAX_CHARS",
    "REFLECT_SCHEMA_TOP_LEVEL_FIELDS",
    "RESERVED_REFLECT_KEYS",
    "RESERVED_REFLECT_PARAMETER_NAMES",
    "ReflectActionCall",
    "ReflectActionDescriptor",
    "ReflectActionHostPort",
    "ReflectActionItem",
    "ReflectActionOutcome",
    "ReflectActionParameter",
    "ReflectActionSpec",
    "contains_control_characters",
    "fold_control_characters",
]
