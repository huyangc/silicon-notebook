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
* a report citation stores its ``source_id`` / ``object_id`` and, since the
  engine recorded it, ``from_reference_library`` (whether its evidence came
  from a mounted library, decided by the owning notebook at generation).  The
  route reads ownership only for citations that say so or predate the field
  (a report of local citations reads nothing): the cited sources in ONE
  batched read (``visible_source_owners``), then the cited knowledge objects
  whose source did not resolve -- a knowledge object can be cited with no
  source -- in ONE batched read (``knowledge.object_owners``).  A citation
  marked as coming from a mounted library whose library can no longer be
  named (its source and object are gone) fails closed: the page is not
  served, since nothing can show the library is still mounted.  A legacy
  citation without the field that cannot be resolved is not counted.
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


REFERENCE_LIBRARY_FLAG = "from_reference_library"


def report_references_needing_owner(references: Sequence[Any]) -> list[Mapping[str, Any]]:
    """The report citations whose library has to be read: those marked as
    coming from a mounted library, and legacy ones written before the mark."""
    return [
        reference for reference in references
        if isinstance(reference, Mapping)
        and (REFERENCE_LIBRARY_FLAG not in reference
             or reference.get(REFERENCE_LIBRARY_FLAG) is True)
    ]


def report_source_ids(references: Sequence[Mapping[str, Any]]) -> list[str]:
    return list(dict.fromkeys(
        str(reference.get("source_id")) for reference in references
        if reference.get("source_id")
    ))


def report_unresolved_object_ids(
    references: Sequence[Mapping[str, Any]], source_owners: Mapping[str, str]
) -> list[str]:
    """Object ids of the citations whose source did not name a library."""
    return list(dict.fromkeys(
        str(reference.get("object_id")) for reference in references
        if reference.get("object_id")
        and not source_owners.get(str(reference.get("source_id") or ""))
    ))


def report_library_ids(
    references: Sequence[Mapping[str, Any]],
    source_owners: Mapping[str, str],
    object_owners: Mapping[str, str],
) -> set[str] | None:
    """The libraries the citations come from, or ``None`` (fail closed) when a
    citation marked as coming from a mounted library names none any more."""
    found: set[str] = set()
    for reference in references:
        library = (
            source_owners.get(str(reference.get("source_id") or ""))
            or object_owners.get(str(reference.get("object_id") or ""))
        )
        if library:
            found.add(str(library))
        elif reference.get(REFERENCE_LIBRARY_FLAG) is True:
            return None
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
