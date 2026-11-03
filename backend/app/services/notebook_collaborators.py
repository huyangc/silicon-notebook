"""Named sharing use cases (share-edit P1-T6): add an editor, remove a named
grant, a collaborator's own exit.

Thin orchestration over the seats that already own each half:
``groups`` (the grant rows, ``set_user_editor_grant`` deciding everything in
one write transaction), ``sharing`` (the read predicate and the per-member
state cleanup) and ``memory`` (the acknowledged exit that deletes the
leaver's Memory). Authorization is the routes' (``notebook:collaborate``,
owner-only); nothing here re-decides it.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from app.models.memory import MemberExitCounts, MemberExitOutcome

logger = logging.getLogger(__name__)

#: The bell event a new editor receives (``pending_bus``): the browser
#: refreshes its notebook list and access state on it.
COLLABORATOR_GRANTED_EVENT = "notebook_collaborator_granted"


class NotebookCollaboratorService:
    def __init__(
        self,
        *,
        groups: Any,
        sharing: Any,
        memory: Any,
        emit: Callable[[str, dict], None],
    ) -> None:
        self.groups = groups
        self.sharing = sharing
        self.memory = memory
        self.emit = emit

    def grant_editor(self, notebook_id: str, *, owner_id: str, target_user_id: str) -> dict:
        """Make ``target_user_id`` an editor. A real change notifies them once
        (fail-open: a dead bell never fails the request); an idempotent repeat
        notifies nobody."""
        result = self.groups.set_user_editor_grant(
            notebook_id, owner_id=owner_id, target_user_id=target_user_id
        )
        if result["changed"]:
            try:
                self.emit(target_user_id, {
                    "event": COLLABORATOR_GRANTED_EVENT, "notebook_id": notebook_id,
                })
            except Exception as exc:  # noqa: BLE001 - the grant has committed
                logger.warning("collaborator bell failed (%s)", type(exc).__name__)
        return result

    def revoke(self, notebook_id: str, user_id: str) -> bool:
        """Remove someone's named grant, whatever its role. When that was their
        last way to read the notebook their per-member state (understanding
        overlay, observations) is cleared, as on a membership removal; when they
        still read it (a membership row, a group grant) it is kept -- they keep
        using the notebook. Their Memory is never deleted here: an access change
        is not an exit."""
        removed = self.groups.delete_user_grant(notebook_id, user_id)
        if removed and not self.sharing.user_can_read_notebook(notebook_id, user_id):
            try:
                self.sharing.forget_member_state(notebook_id, user_id)
            except Exception as exc:  # noqa: BLE001 - the revocation has committed
                logger.warning("collaborator overlay cleanup failed (%s)", type(exc).__name__)
        return removed

    def exit_disclosure(self, notebook_id: str, user_id: str) -> MemberExitCounts:
        return self.memory.collaboration_exit_disclosure(notebook_id, user_id)

    def leave(self, notebook_id: str, user_id: str, acknowledged: int | None) -> MemberExitOutcome:
        return self.memory.leave_collaboration(
            notebook_id, user_id, acknowledged,
        )
