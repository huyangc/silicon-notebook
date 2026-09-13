"""Point-specific contracts for plugin-provided reflect actions.

One contribution at ``ask.reflect_action`` is ONE action — not a provider that
offers several.  Availability, per-action call budgets and trace accounting are
all per action, so one contribution per action is the shape with the fewest
moving parts.

A contributor receives no core port of any kind: the call context carries the
question, the already-validated arguments the model wrote, a cancellation token
and a wall-clock deadline.  It sees no candidate text, no sub-queries, no gap
phrases and no identifier of any kind — the egress surface is exactly those two
strings-and-arguments, and the host records the arguments verbatim in the run
trace so an operator can read what left the deployment.

The value types and the character rails are re-exported from
``app.domain.reflect_action`` so a plugin manifest and the core projection read
the same constants instead of restating them.  :class:`ReflectActionResult` is
the one contract type defined HERE rather than in the domain module, because it
carries ``ExtensionResultStatus``/``ExtensionFailure`` and ``app.domain`` may
not import the Extension SDK.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.domain.reflect_action import (
    EXTERNAL_EVIDENCE_CONTEXT_CHARS,
    EXTERNAL_EVIDENCE_EXCERPT_MAX_CHARS,
    EXTERNAL_EVIDENCE_LOCATION_LABEL_MAX_CHARS,
    EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL,
    EXTERNAL_EVIDENCE_REFLECT_BLOCK_CHARS,
    EXTERNAL_EVIDENCE_SOURCE_LABEL_MAX_CHARS,
    EXTERNAL_EVIDENCE_TITLE_MAX_CHARS,
    EXTERNAL_EVIDENCE_URL_MAX_CHARS,
    REFLECT_ACTION_ARGUMENT_MAX_CHARS,
    REFLECT_ACTION_DESCRIPTION_MAX_CHARS,
    REFLECT_ACTION_ENUM_VALUES_MAX,
    REFLECT_ACTION_NAME_MAX_CHARS,
    REFLECT_ACTION_NOTE_MAX_CHARS,
    REFLECT_ACTION_PARAM_DESCRIPTION_MAX_CHARS,
    REFLECT_ACTION_PARAMETERS_MAX,
    ReflectActionDescriptor,
    ReflectActionItem,
    ReflectActionParameter,
)
# ``ExternalEvidence`` and ``ReflectActionSpec`` are deliberately NOT re-exported
# here.  Both are core-internal projections — the spec pairs a descriptor with
# its plugin id, the evidence carries a core-minted key — and a plugin neither
# builds nor receives either.  Same line ``extension_sdk/gap_consult.py`` draws
# around ``GapConsultCallContext``/``GapConsultHostPort``.
from app.extension_sdk.contracts import (
    CancellationToken,
    ExtensionFailure,
    ExtensionResultStatus,
)


ASK_REFLECT_ACTION_POINT = "ask.reflect_action"


@dataclass(frozen=True, slots=True)
class ReflectActionAvailabilityContext:
    """I/O-free live availability input, mirroring the gap-consult one."""

    contribution_id: str
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class ReflectActionCallContext:
    """Everything one invocation of a plugin action is given.

    Note what is absent, as at ``ask.gap_consult``: no notebook, actor, source,
    evidence, retrieval scope or core port.  ``question`` is wording the user
    actually saw — the synthesized ``research_question`` string is never sent
    outward — and ``arguments`` are the model's own, already checked against the
    descriptor (an unknown enum value has been cleared, a text value clamped to
    ``REFLECT_ACTION_ARGUMENT_MAX_CHARS``, an omitted parameter is an empty
    string, so every declared parameter is present as a key).

    ``max_items`` is what this call may still contribute — the run-level
    remainder, so it can be smaller than
    ``EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL``.
    """

    question: str
    arguments: Mapping[str, str]
    cancellation: CancellationToken | None
    deadline_monotonic: float
    max_items: int


@dataclass(frozen=True, slots=True)
class ReflectActionResult:
    """What ``invoke`` answers.

    RULING (design document §二 left this open, T1 closes it): this type carries
    ``status`` and ``failure`` itself — the two fields ``ContributorResult``
    would have contributed — alongside ``note``.  ``invoke`` returns it
    directly and the host does NOT wrap it in a ``ContributorResult``.  There is
    exactly one return shape at this point, so no host or plugin has to know
    which of two envelopes it is looking at.

    ``note`` is a one-sentence observation shown to the model on the next
    reflect turn ("only reviews found, no primary data"), bounded by
    ``REFLECT_ACTION_NOTE_MAX_CHARS``.
    """

    items: tuple[ReflectActionItem, ...] = ()
    note: str = ""
    status: ExtensionResultStatus = ExtensionResultStatus.AVAILABLE
    failure: ExtensionFailure | None = None


class ReflectActionContributor(Protocol):
    """One lent function: a fixed descriptor plus a bounded invocation."""

    descriptor: ReflectActionDescriptor

    def invoke(self, context: ReflectActionCallContext) -> ReflectActionResult: ...


__all__ = [
    "ASK_REFLECT_ACTION_POINT",
    "EXTERNAL_EVIDENCE_CONTEXT_CHARS",
    "EXTERNAL_EVIDENCE_EXCERPT_MAX_CHARS",
    "EXTERNAL_EVIDENCE_LOCATION_LABEL_MAX_CHARS",
    "EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL",
    "EXTERNAL_EVIDENCE_REFLECT_BLOCK_CHARS",
    "EXTERNAL_EVIDENCE_SOURCE_LABEL_MAX_CHARS",
    "EXTERNAL_EVIDENCE_TITLE_MAX_CHARS",
    "EXTERNAL_EVIDENCE_URL_MAX_CHARS",
    "REFLECT_ACTION_ARGUMENT_MAX_CHARS",
    "REFLECT_ACTION_DESCRIPTION_MAX_CHARS",
    "REFLECT_ACTION_ENUM_VALUES_MAX",
    "REFLECT_ACTION_NAME_MAX_CHARS",
    "REFLECT_ACTION_NOTE_MAX_CHARS",
    "REFLECT_ACTION_PARAMETERS_MAX",
    "REFLECT_ACTION_PARAM_DESCRIPTION_MAX_CHARS",
    "ReflectActionAvailabilityContext",
    "ReflectActionCallContext",
    "ReflectActionContributor",
    "ReflectActionDescriptor",
    "ReflectActionItem",
    "ReflectActionParameter",
    "ReflectActionResult",
]
