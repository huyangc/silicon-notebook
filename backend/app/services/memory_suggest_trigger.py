"""Auto user-memory suggestions: the Ask-completed trigger (design 2026-10-28
§4.1 "端口内的判定", §4.3 "触发").

The fourth built-in completion observer (``builtin.ask_user_memory_suggest``)
delegates here through the runtime's ``memory_suggest`` port. It runs after the
answer was delivered, on the completion hook's own thread, so everything here
is fail-open: an exception is logged by class name and swallowed.

Cost contract, in order (each step only runs when the previous one passed):

1. **No store call at all** when the deployment wiring is off, every signal
   class is switched off (G26), the Ask was not a notebook-scope Ask, or it
   was not submitted from the web app (``submitted_via != "web"``): MCP and
   global Asks are never counted, so they can never drive a run.
2. **Fresh authentication hint**: a matching manual-mode request actor, read
   under the store's still-stable process epoch, stops with zero extra SQL.
   Every mode write invalidates these private hints before I/O and after
   commit/rollback; runtime replacement invalidates them too. There is no
   user cache or TTL. Missing, stale or different-actor hints use the original
   primary-key read (``user_state``); an absent/disabled switch stops there.
   Enabled users always take that real-time read.
3. **One write transaction** (``note_signal``): it re-checks the switch and
   that the owner can still read this notebook (live) INSIDE the same
   transaction as the upsert, which is design §4.1 step 3 ("复核读权") and
   step 4 ("upsert") folded together (plan B9). The trigger deliberately does
   NOT call ``user_can_read_notebook`` itself: a separate read would leave a
   window in which an access loss commits between the check and the upsert
   and the job row is recreated for someone who can no longer read the
   notebook. ``0`` back means nothing was counted (switch off, no read
   access): stop.
4. **Hand-off**: when the count reaches ``auto_memory_suggest_trigger`` (the
   5th web Ask by default), or the Ask carried clarification answers while the
   ``clarified`` class is on (D3(x)), ``start_job(notebook_id, owner_id)`` is
   called -- if one is wired. ``start_job`` must only enqueue (a queue put to
   the light pool, never a blocking wait); ``None`` means "count only" (T6
   ships the seat empty, T7b plugs the job in).

Repository access goes through ``MemorySuggestStorePort`` only (G28): this
module imports no backend.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from app.core.request_context import get_request_user
from app.domain.extensions import CompletedAskNotification
from app.domain.memory_suggest_contracts import SIGNAL_CLARIFIED
from app.services.memory_suggest_wiring import (
    memory_suggest_enabled_signals,
    memory_suggest_wiring_active,
)

if TYPE_CHECKING:
    from app.repositories.ports import MemorySuggestStorePort

_log = logging.getLogger("silicon_notebook.memory_suggest")

#: ``start_job(notebook_id, owner_id)``: enqueue one run, never block.
StartMemorySuggestJob = Callable[[str, str], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def had_clarification_answers(response: object) -> bool:
    """Whether a finished Ask's finalized understanding carried non-empty
    clarification answers -- ``response.intent.clarification_answers``, the
    same value the answer payload persists (``payload.intent``) and the job's
    clarified scan later reads. The two completion call sites (streaming
    coordinator and synchronous ``ask_current``) both ask this, so the
    ``had_clarification`` fact has one definition."""
    intent = getattr(response, "intent", None)
    return bool(getattr(intent, "clarification_answers", None))


class MemorySuggestTrigger:
    """Count one completed web Ask towards a (notebook, owner) suggestion run."""

    def __init__(
        self,
        store: "MemorySuggestStorePort",
        settings: object,
        start_job: StartMemorySuggestJob | None,
        *,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self._store = store
        self._settings = settings
        self._start_job = start_job
        self._clock = clock

    def note_ask_completed(self, notification: CompletedAskNotification) -> None:
        """Fail-open entry point for one completed Ask (see module docstring)."""
        try:
            self._note(notification)
        except Exception as exc:
            # Class name only: no exception text (it may quote ids or SQL).
            _log.warning(
                "memory suggestion ask-completed trigger failed: %s",
                type(exc).__name__,
            )

    def _note(self, notification: CompletedAskNotification) -> None:
        settings = self._settings
        if not memory_suggest_wiring_active(settings):
            return
        enabled = memory_suggest_enabled_signals(settings)
        if (
            not enabled
            or notification.scope != "notebook"
            or notification.submitted_via != "web"
        ):
            return
        notebook_id = notification.notebook_id
        owner_id = notification.actor_id
        if not notebook_id or not owner_id:
            return
        if self.has_fresh_manual_hint(owner_id):
            # Authentication already read this exact mode. Any mode write or
            # runtime replacement since then invalidates its private stamp;
            # absent/stale hints fall through to the original real-time read.
            return
        state = self._store.user_state(owner_id)
        if not state or not state.get("enabled"):
            return
        # Read access and the switch are re-checked inside this one write
        # transaction (plan B9); 0 = nothing counted.
        pending = self._store.note_signal(
            notebook_id,
            owner_id,
            ask_job_id=notification.ask_job_id,
            now=self._clock(),
        )
        if pending <= 0:
            return
        threshold = max(1, int(getattr(settings, "auto_memory_suggest_trigger", 5)))
        clarified_now = (
            notification.had_clarification and SIGNAL_CLARIFIED in enabled
        )
        if (pending >= threshold or clarified_now) and self._start_job is not None:
            self._start_job(notebook_id, owner_id)

    def has_fresh_manual_hint(self, owner_id: str) -> bool:
        """Core-only skip attestation at the completion linearization point.

        It changes no third-party access port; a plugin can still notify it,
        and that call independently checks the same epoch/live-switch gate.
        """
        actor = get_request_user()
        if actor is None or actor.id != owner_id or actor.memory_mode != "manual":
            return False
        epoch = actor._memory_mode_epoch
        return epoch is not None and epoch == self._store.mode_epoch()


__all__ = [
    "MemorySuggestTrigger",
    "StartMemorySuggestJob",
    "had_clarification_answers",
]
