"""The exact numbers behind the conversation share dialog, for a GLOBAL
conversation (``GET /global-ask/conversations/{id}/share/counts``).

The dialog used to page the whole conversation into the browser and count
there (``frontend/app/conversation-share-disclosure.ts``); past the page cap
it could only say "not counted". The server now counts, over the SAME window
the public page shows (``public_window``: the newest ``MAX_TURNS`` of the
published prefix), and with the browser's algorithm unchanged, so a
conversation of ordinary length reads exactly as before.

Every set the dialog talks about is a PREFIX of the conversation's completed
turns in the canonical ``(created_at, id)`` order: the turns up to the
boundary a share would pin, the turns up to the link's watermark, all of
them. So the store answers with prefix SIZES from one SQL aggregate
(``ShareRanks``), this module works out which prefixes the dialog describes
(``share_prefixes``), the store reads the newest ``MAX_TURNS`` rows of each
(at most two windows), and ``share_counts`` turns sizes and per-turn image
counts into the response. The mapping mirrors the browser's
``resolveShareBoundary`` / ``summarizeShareDisclosure`` /
``summarizeShareUpdate`` term by term; ``tests/fixtures/share_counts_golden.json``
holds both sides to the same expected numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.domain.conversation_public_view import MAX_TURNS


@dataclass(frozen=True)
class ShareRanks:
    """Prefix sizes over one conversation's completed turns.

    ``total``: every completed turn. ``boundary``: the prefix ending at the
    boundary turn -- the one the dialog asked about (``bounded``), or the
    newest when it asked about none; ``None`` when the asked one is not a
    completed turn of the conversation. ``shared``: the prefix the link serves
    now (``None`` = not shared); ``mark``: the same size when the watermark
    turn itself resolves, ``None`` when it does not (the page then falls back
    to the watermark instant, and so does ``shared``).
    """

    total: int
    boundary: int | None
    bounded: bool
    shared: int | None
    mark: int | None


@dataclass(frozen=True)
class SharePrefixes:
    """The prefixes the dialog describes. ``current``: the one whose newest
    ``MAX_TURNS`` turns its disclosure talks about (what the link has now, or
    what sharing would publish while there is no link); ``scope``: what
    sharing or updating publishes; ``current_scope``: the part of ``scope``
    the link already covers."""

    disclosure: int
    current: int
    scope: int
    current_scope: int
    ahead: bool
    behind: bool
    unknown: bool


def share_prefixes(ranks: ShareRanks) -> SharePrefixes | None:
    """``None`` when the asked boundary does not resolve: nothing can be
    counted (the browser's ``boundary.unresolved``)."""
    if ranks.boundary is None:
        return None
    scope = ranks.boundary
    unknown = ranks.bounded and ranks.shared is not None and ranks.mark is None
    ahead = ranks.bounded and ranks.mark is not None and ranks.mark > scope
    behind = ranks.bounded and ranks.mark is not None and ranks.mark < scope
    # Past the boundary (or unknowable) the link shows more than the clicked
    # answer, so its content is described over the whole conversation.
    disclosure = ranks.total if (ahead or unknown) else scope
    covered = disclosure if ranks.shared is None else ranks.shared
    return SharePrefixes(
        disclosure=disclosure,
        current=min(disclosure, covered),
        scope=scope,
        current_scope=min(scope, covered),
        ahead=ahead,
        behind=behind,
        unknown=unknown,
    )


def window_size(prefix: int) -> int:
    """How many turns of a prefix the public page shows."""
    return min(prefix, MAX_TURNS)


def share_counts(
    ranks: ShareRanks,
    *,
    through_id: str,
    current_images: Sequence[int],
    scope_images: Sequence[int],
) -> dict[str, int | bool | str]:
    """The response of the counts endpoint.

    ``current_images`` / ``scope_images``: the image count of every turn of
    the window of ``current`` / ``scope`` (oldest first) -- each turn's
    anchors and citations, images deduplicated by ``asset_id`` per turn, the
    browser's conservative upper bound (the page shows a subset).
    ``through_id``: the boundary turn's id (the asked one, or the newest).
    """
    prefixes = share_prefixes(ranks)
    if prefixes is None:
        return {
            "total_turns": ranks.total, "through_id": "", "boundary_index": -1,
            "boundary_unresolved": True, "watermark_ahead": False, "ahead_count": 0,
            "watermark_behind": False, "watermark_unknown": False,
            "new_turns": 0, "published_turns": 0, "earlier_turns": 0, "image_count": 0,
            "update_image_count": 0, "update_new_image_count": 0, "update_earlier_turns": 0,
        }
    current = window_size(prefixes.current)
    scope = window_size(prefixes.scope)
    if len(current_images) != current or len(scope_images) != scope:
        raise ValueError("share count windows do not match their prefixes")
    # The scope window's turns the link's current window does not show yet:
    # ranks above both the scope window's floor and the covered part.
    first_rank = prefixes.scope - scope + 1
    newly_shown = sum(
        images for offset, images in enumerate(scope_images)
        if first_rank + offset > prefixes.current_scope
    )
    return {
        "total_turns": ranks.total,
        "through_id": through_id,
        "boundary_index": prefixes.scope - 1 if ranks.bounded else -1,
        "boundary_unresolved": False,
        "watermark_ahead": prefixes.ahead,
        "ahead_count": (ranks.mark or 0) - prefixes.scope if prefixes.ahead else 0,
        "watermark_behind": prefixes.behind,
        "watermark_unknown": prefixes.unknown,
        "new_turns": prefixes.disclosure - prefixes.current,
        "published_turns": current,
        "earlier_turns": prefixes.current - current,
        "image_count": sum(current_images),
        "update_image_count": sum(scope_images),
        "update_new_image_count": newly_shown,
        "update_earlier_turns": prefixes.scope - scope,
    }
