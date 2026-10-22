"""One mount edge as the mounting notebook's owner sees it (both backends).

``list_mount_edges`` reads every edge, the failing ones included: a failing
edge stays on screen, greyed out, with the reason it fails. The row carries
the store's booleans -- ``ok`` (``MOUNT_VALID_EXPR``), ``gate_closed``
(``MOUNT_GATE_CLOSED_EXPR``), ``base_not_live`` (``MOUNT_BASE_NOT_LIVE_EXPR``,
derived from the same lifecycle term as ``MOUNT_VALID_EXPR``) and
``same_owner`` -- plus ``base_status`` and ``base_owner``; ``viewer_id`` is
who is looking. The reason only states what is true now, for that viewer:

* the mounted notebook is not live: being deleted (``'deleting'``) -> "it has
  been deleted"; inside its deletion window and the viewer is the mounted
  notebook's own owner -> "you deleted it, cancelling the deletion brings it
  back"; inside its window for anyone else (the mounting notebook's group
  admins included), or being copied / imported -> a neutral "temporarily
  unavailable" -- nothing about tiers or ownership, which would not be the
  cause;
* the unshared gate closed a borrowed edge: how to reopen it;
* otherwise the edge lost its grant (a public library demoted, a share
  withdrawn).

The name of a failing edge is masked unless the mounting owner may still read
the notebook (same owner, or the gate case): a name seen at mount time is not
a licence to keep reading later renames.
"""
from __future__ import annotations

from typing import Any

from app.domain.notebook_delete_grace import PENDING_DELETE_STATUS

# The six-phase delete's status (``request()`` / the promotion write it).
NOTEBOOK_DELETING_STATUS = "deleting"

MASKED_MOUNT_NAME = "已不可用的知识库"
MOUNT_OWN_PENDING_DELETE_REASON = "你已删除这本笔记本，撤销删除即可恢复"
MOUNT_BASE_UNAVAILABLE_REASON = "该参考库暂时不可用"
MOUNT_BASE_DELETED_REASON = "该参考库已被删除"
MOUNT_GATE_CLOSED_REASON = (
    "本笔记本已共享，借来的参考库暂停参与检索；取消本笔记本的共享即可恢复"
)
MOUNT_ACCESS_LOST_REASON = "该库已不是公共知识库，且不属于你"


def mount_edge_view(row: Any, viewer_id: str | None = None) -> dict:
    active = bool(row["ok"])
    gate_closed = bool(row["gate_closed"])
    same_owner = bool(row["same_owner"])
    if active:
        reason = ""
    elif row["base_not_live"]:
        if row["base_status"] == NOTEBOOK_DELETING_STATUS:
            reason = MOUNT_BASE_DELETED_REASON
        elif (
            row["base_status"] == PENDING_DELETE_STATUS
            and bool(viewer_id) and row["base_owner"] == viewer_id
        ):
            reason = MOUNT_OWN_PENDING_DELETE_REASON
        else:
            reason = MOUNT_BASE_UNAVAILABLE_REASON
    elif gate_closed:
        reason = MOUNT_GATE_CLOSED_REASON
    else:
        reason = MOUNT_ACCESS_LOST_REASON
    name_visible = active or same_owner or gate_closed
    return {
        "id": row["id"],
        "name": row["name"] if name_visible else MASKED_MOUNT_NAME,
        "tier": row["tier"] or "personal",
        "active": active,
        "inactive_reason": reason,
    }
