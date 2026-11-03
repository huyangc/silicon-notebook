"""Request-owned marks. Guard admission and committed branches are distinct."""
from contextvars import ContextVar
from dataclasses import replace

from starlette.requests import Request

from app.domain.collaboration import CollabMark, PulseActor

CURRENT_COLLAB_SCOPE: ContextVar[dict | None] = ContextVar("collaboration_http_scope", default=None)


def current_request():
    scope = CURRENT_COLLAB_SCOPE.get()
    return Request(scope) if scope is not None else None


def append_mark(request, notebook_id, capability, owner_id, user, *, source):
    actor = PulseActor(user.id, str(user.display_name or "").strip() or str(user.username or "").strip(),
                       "owner" if owner_id == user.id else "editor")
    mark = CollabMark(source, notebook_id, capability, owner_id, actor)
    request.scope.setdefault("state", {}).setdefault("collab_marks", []).append(mark)
    return mark


def guarded_owner(request, notebook_id, user_id):
    return any(mark.source == "guard" and mark.notebook_id == notebook_id
               and mark.owner_id == user_id and mark.actor.user_id == user_id
               for mark in request.scope.get("state", {}).get("collab_marks", ()))


def committed_result(request, notebook_id, capability, result):
    """Only a branch which has committed calls this; previews never do."""
    request = request or current_request()
    if request is not None:
        marks = request.scope.setdefault("state", {}).setdefault("collab_marks", [])
        guard = next((mark for mark in reversed(marks) if mark.source == "guard"
                      and mark.notebook_id == notebook_id and mark.capability == capability), None)
        if guard is not None:
            marks.append(replace(guard, source="explicit"))
    return result
