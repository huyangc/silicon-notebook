"""Core-owned contracts for the ``ask.gap_consult`` extension point.

Gap consultation asks deployment plugins for *pointers to material outside the
notebook* when this run's own retrieval came up thin or left a confirmed
direction uncovered.  What comes back is never evidence: it is not retrieved,
not cited, not bound to an anchor, and never enters answer synthesis.

Two properties are structural rather than documented-and-hoped-for:

* **The egress surface is one object.**  Everything a plugin can ever see about
  the request is :class:`GapConsultQuery` — a bounded question string and at
  most :data:`GAP_CONSULT_MAX_GAP_PHRASES` short gap phrases.  Auditing "what
  leaves the deployment" is therefore reading one dataclass, not tracing a
  call graph.  :class:`GapConsultCallContext` deliberately holds no notebook
  id, actor id, source id, evidence, or scope: privacy here is guaranteed by
  the field set, not by a filter someone has to remember to apply.

* **The port has no terminal semantics.**  ``consult`` takes a call context and
  answers suggestions.  Nothing in this signature says "called once, at the end
  of a run", so a later version can offer it as a reflect-loop action without
  reshaping the contract.

This module lives beside ``domain/extensions.py`` rather than inside it because
``app.models.ask`` imports the character limits below as the single source of
truth for its wire-level ``max_length`` values, and pulling the wire layer into
the import graph of a pure host-port protocol module would be a step backwards.

It must keep importing nothing from ``app.*`` and nothing third-party.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


# How many uncovered-direction phrases may accompany the question.  This is an
# egress rail, not a budget: each phrase is text the deployment sends outward.
GAP_CONSULT_MAX_GAP_PHRASES = 2
# The most suggestions one run may accept, across every contributor together.
GAP_CONSULT_MAX_SUGGESTIONS = 5
GAP_CONSULT_QUESTION_MAX_CHARS = 300
GAP_CONSULT_PHRASE_MAX_CHARS = 60
GAP_SUGGESTION_TITLE_MAX_CHARS = 200
GAP_SUGGESTION_SUMMARY_MAX_CHARS = 400
GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS = 40
GAP_SUGGESTION_URL_MAX_CHARS = 2048


@dataclass(frozen=True, slots=True)
class GapConsultQuery:
    """Everything that leaves the deployment for one gap consultation."""

    question: str
    gaps: tuple[str, ...]
    max_suggestions: int


@dataclass(frozen=True, slots=True)
class GapSuggestion:
    """One pointer to material outside the notebook.

    Four fields on purpose.  A date field would be the fifth, and a plugin's
    idea of "published on" is unverifiable here — the core never fetches the
    URL — so it would render as authority the core cannot stand behind.
    """

    title: str
    url: str
    summary: str = ""
    source_label: str = ""


@dataclass(frozen=True, slots=True)
class GapConsultCallContext:
    """Core-only call state.

    Note what is absent: no notebook, actor, source, evidence, or frozen
    retrieval scope.  A plugin cannot be handed identity it was never given.
    """

    query: GapConsultQuery
    cancellation: Any
    connection_probe: Any
    deadline_monotonic: float


class GapConsultHostPort(Protocol):
    """The application-facing view of the frozen gap-consult host."""

    def has_contributions(self) -> bool: ...

    def consult(
        self,
        call_context: GapConsultCallContext,
        *,
        event_sink: Any | None = None,
    ) -> tuple[GapSuggestion, ...]: ...


def gap_consult_host_is_dormant(host: object) -> bool:
    """True when ``host`` has nothing registered for ``ask.gap_consult``.

    Mirrors the defensive read in ``domain.extensions.lane_is_dormant`` but is
    deliberately a separate function: this point has no invocation dimension
    and no per-call capability set, so sharing that signature would mean
    passing placeholders through a contract that does not have those axes.

    The safety direction runs INTO the host.  A host predating this query
    (probe missing or not callable), a probe that raises, and a probe that
    answers anything other than the literal ``False`` being tested for all
    return False here, so the caller enters the host exactly as before.  The
    answer is compared by identity against ``False`` rather than truthiness
    tested, so a malformed reply can never be mistaken for "nothing is
    registered here".
    """
    probe = getattr(host, "has_contributions", None)
    if not callable(probe):
        return False
    try:
        return probe() is False
    except Exception:  # noqa: BLE001 — a probe failure must not skip the host
        return False


__all__ = [
    "GAP_CONSULT_MAX_GAP_PHRASES",
    "GAP_CONSULT_MAX_SUGGESTIONS",
    "GAP_CONSULT_PHRASE_MAX_CHARS",
    "GAP_CONSULT_QUESTION_MAX_CHARS",
    "GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS",
    "GAP_SUGGESTION_SUMMARY_MAX_CHARS",
    "GAP_SUGGESTION_TITLE_MAX_CHARS",
    "GAP_SUGGESTION_URL_MAX_CHARS",
    "GapConsultCallContext",
    "GapConsultHostPort",
    "GapConsultQuery",
    "GapSuggestion",
    "gap_consult_host_is_dormant",
]
