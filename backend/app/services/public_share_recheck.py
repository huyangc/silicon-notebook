"""Live re-check of the mounted libraries a public page draws on (D-3).

A notebook conversation or a report can quote a library mounted on its notebook
(federated retrieval).  The anonymous page re-authorizes its creator on every
open (read access to the notebook itself), and the global branch re-checks every
library its snapshot drew on; a mounted library is the remaining gap: unmount
it, or let the mount stop being effective (the mounted library is re-tiered,
its owner changes, a borrowed grant is revoked, the borrowing notebook gets
shared), and the published excerpts from it would keep being served.

So every open also asks, as the share's creator, whether each library the
page's references come from -- other than the page's own notebook -- is still
an effective participant of that notebook.  Any one that is not makes the
whole page unreadable, the same as the global branch losing a library: the
same indistinguishable 404 an unknown token gets, and restoring the mount
revives the same link.

Today's mount predicate does not depend on who asks (``mount_sql``: the
mounting notebook's owner decides); ``creator_id`` is carried so the per-viewer
predicate (M3, PR-E6) is applied to the creator at this one call site when it
lands.  An empty creator fails closed.

Which libraries the references come from:

* a conversation turn names a library on every anchor and citation retrieved
  from a mounted one (``notebook_id``, empty for local evidence), so the
  stored turns answer it without a read;
* a report citation stores only its ``source_id``; the route resolves the
  cited sources' owning notebooks in ONE batched read
  (``visible_source_owners``).  A source that is gone resolves to nothing and
  is not counted: its library cannot be named any more.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from app.domain.conversation_public_view import MAX_TURNS


def conversation_library_ids(turns: Sequence[Any]) -> set[str]:
    """Every library named on an anchor or citation of the projected turns."""
    found: set[str] = set()
    for turn in list(turns)[:MAX_TURNS]:
        payload = turn.get("payload") if isinstance(turn, Mapping) else None
        if not isinstance(payload, Mapping):
            continue
        for key in ("anchors", "citations"):
            items = payload.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, Mapping) and item.get("notebook_id"):
                    found.add(str(item["notebook_id"]))
    return found


def mounts_still_effective(
    notebook_id: str,
    creator_id: str,
    library_ids: Iterable[str],
    participant_notebook_ids: Callable[[str], Iterable[str]],
) -> bool:
    """True when every library in ``library_ids`` other than ``notebook_id``
    is still an effective participant of ``notebook_id`` for its creator.

    Reads nothing when the page draws on no other library, so a page without
    mounted evidence costs what it cost before."""
    foreign = {str(item) for item in library_ids if item and item != notebook_id}
    if not foreign:
        return True
    if not creator_id:
        return False
    return foreign <= set(participant_notebook_ids(notebook_id))
