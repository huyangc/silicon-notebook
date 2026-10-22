"""Public-conversation-page constants sunk from app.services.conversation_public_view in B3.

``MAX_TURNS`` is the one name app.repositories (ask_state_store, both
backends, and the global store) imports directly (a safety ceiling on how
many turns one public page renders), and ``public_window`` is the one
definition of WHICH turns those are. This sink is scoped to exactly the names
repositories consume; sinking the rest of the public-conversation projection
module is a separate piece of work, not attempted here.
``app.services.conversation_public_view`` re-exports these names unchanged
for existing importers.
"""
from __future__ import annotations

from typing import Sequence, TypeVar

# Safety ceiling on how many turns one public page renders. A conversation's
# turn count is bounded by how many times a user asked, so this almost never
# binds; it exists so a pathological conversation cannot balloon one anonymous
# response. Truncation is disclosed (``omitted_earlier_turns`` /
# ``truncated_turns``) rather than silent.
MAX_TURNS = 500

_Turn = TypeVar("_Turn")


def public_window(turns: Sequence[_Turn]) -> list[_Turn]:
    """The turns one public page renders out of a published prefix (oldest
    first): its NEWEST ``MAX_TURNS``.

    The page ends at the watermark (or the boundary a share pins), so the turn
    the author shared is always on it; what a very long conversation leaves
    out is its oldest turns, and the page says how many
    (``omitted_earlier_turns``). Every reader of a published prefix -- the page
    projection, its image and Memory lookups, the live re-checks, the share
    disclosure and the share dialog's counts -- cuts through this one function,
    so none of them can describe a different window than the page shows.
    """
    items = list(turns)
    return items[-MAX_TURNS:] if len(items) > MAX_TURNS else items
