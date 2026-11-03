"""Frozen actor metadata crosses background submission without extra reads."""
import contextvars

from app.core.request_context import get_request_user
from app.domain.collaboration import PulseActor

_JOB_ACTOR: contextvars.ContextVar[PulseActor | None] = contextvars.ContextVar("collaboration_job_actor", default=None)


def _request_actor() -> PulseActor | None:
    # The original context-copy contract also admits legacy bare values and
    # id-only principals. Optional observer metadata must never block a job.
    try:
        user = get_request_user()
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, str) or not user_id:
            return None
        name = str(getattr(user, "display_name", "") or "").strip()
        name = name or str(getattr(user, "username", "") or "").strip()
        return PulseActor(user_id, name, "editor")
    except Exception:
        return None


def submission_context(actor=None):
    context = contextvars.copy_context()
    captured = actor if isinstance(actor, PulseActor) else _request_actor()
    if captured is not None:
        context.run(_JOB_ACTOR.set, captured)
    return context


def current_job_actor() -> PulseActor | None:
    actor = _JOB_ACTOR.get()
    if actor is not None:
        return actor
    return _request_actor()


def pulse_touch_for_job(callback, notebook_id: str, kind, actor_id=None, actor_name=None) -> None:
    try:
        if callback is not None:
            callback(notebook_id, kind, actor_id, actor_name)
    except Exception:
        # Post-terminal observer: never reverse a committed job result.
        pass


def pulse_touch_for_job_kinds(callback, notebook_id: str, kinds) -> None:
    # A terminal action is one primary activity with several derived hints.
    pulse_touch_for_job(callback, notebook_id, tuple(kinds))


class _QueueEntry(tuple):
    """Keep the legacy two-tuple's equality/serialization and attach metadata."""
    def __new__(cls, mode, at, actor):
        entry = super().__new__(cls, (mode, at))
        entry.actor = actor
        return entry


def job_queue_entry(mode, at, previous=None):
    return _QueueEntry(mode, at, current_job_actor() or getattr(previous, "actor", None))


def pulse_touch_for_mcp(repo, notebook_id, kind, principal, affected_kinds=()):
    try:
        repo._runtime.collaboration.touch_for_job(notebook_id, affected_kinds or kind, principal.owner_id, principal.profile_name)
    except Exception:
        pass
