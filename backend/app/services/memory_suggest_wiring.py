"""The single wiring predicates of auto user-memory suggestions.

Every consumer — the ask-completed observer port, the job, the
``PATCH /me/memory-mode`` route and ``GET /system/config`` — asks THESE
functions, never ``settings`` directly. One predicate per question is what
keeps a kill switch from leaving a half-off state behind ("the switch is
hidden but the job still runs", "the job stopped but the route still enables
it").

A settings object missing a flag reads as OFF (fail-closed): a stand-in
that predates this feature must not switch it on.

Deliberately NOT consulted by the expiry sweep (plan G11): with the
deployment switch off, already-written suggestions stay on the memory page
for the user and still expire on schedule.
"""
from __future__ import annotations

from app.domain.memory_suggest_contracts import (
    MEMORY_SUGGEST_WORKLOAD,
    SIGNAL_CLARIFIED,
    SIGNAL_CORRECTED,
    SIGNAL_REPEATED,
    SuggestedSignal,
)

_SIGNAL_FLAGS: tuple[tuple[SuggestedSignal, str], ...] = (
    (SIGNAL_CLARIFIED, "auto_memory_suggest_clarified_enabled"),
    (SIGNAL_REPEATED, "auto_memory_suggest_repeated_enabled"),
    (SIGNAL_CORRECTED, "auto_memory_suggest_corrected_enabled"),
)


def memory_suggest_enabled_signals(settings: object) -> frozenset[SuggestedSignal]:
    """The signal classes whose own switch is on (D11 / G26).

    Independent of the deployment switch: callers that must also honour it
    combine the two through ``memory_suggest_wiring_active``.
    """
    return frozenset(
        signal
        for signal, flag in _SIGNAL_FLAGS
        if bool(getattr(settings, flag, False))
    )


def memory_suggest_wiring_active(settings: object) -> bool:
    """Whether suggestions can run at all on this deployment.

    Off when the deployment switch is off, and also when every signal class is
    switched off — a run could then never produce anything, so the observer
    port, the job and the "turn on" route treat it exactly like the
    deployment switch being off (zero writes, G26).
    """
    return bool(getattr(settings, "auto_memory_suggest_enabled", False)) and bool(
        memory_suggest_enabled_signals(settings)
    )


def memory_suggest_model_ready(models: object | None) -> bool:
    """Whether a chat service is bound to the ``memory_suggest`` workload.

    ``models`` is the runtime provider (``configured(workload_id)``); ``None``
    (repository not built yet) and any provider failure read as not ready —
    this only drives an in-place hint, never a decision to write.
    """
    if models is None:
        return False
    configured = getattr(models, "configured", None)
    if not callable(configured):
        return False
    try:
        return bool(configured(MEMORY_SUGGEST_WORKLOAD))
    except Exception:  # readiness hint is fail-closed
        return False
