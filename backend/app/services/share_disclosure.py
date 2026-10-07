"""Share disclosure: how much of the author's own Memory a public report or
conversation may carry, and whether it may be published at all (M4).

Ruling M4: publishing a report or a conversation that cites the author's own
Memory asks the author first, with the count.  This module is the single
server-side definition of that count and of the publish rule built on it; the
share dialogs show the number they are given and never count Memory
themselves.  The report half comes first; the conversation half (the
notebook-scoped and the global conversation alike) is at the end of the
module, under "Conversations".

The count
---------
The number of DISTINCT Memory entries of the author whose content the public
page may carry — cited or merely used — the union of:

* citations that ARE a Memory (``object_type == "memory"``, id in
  ``object_id``) — the author's confirmed Memory injected into the run;
* citations the report engine recorded at generation time as coming from the
  author's Memory projection (``memory_id`` + ``memory_owner_id`` equal to the
  author on the stored citation) — counted from the record, so deleting the
  Memory or its projection later does not hide them;
* the author's Memory recorded as USED while the report was produced
  (``report["memory_used"]``): the planner's Memory lines, each section's
  confirmed-Memory block, and the Memory behind every source retrieval handed
  the planning and generation runs (corpus map and probes, the section
  deep-dive agent's observations, the synthesis payload, the section
  contexts — cited or not; ``app.services.report_memory_use``).  The body can
  restate any of it without a citation marker, so the count covers it;
* for citations without a record (reports generated before recording
  existed): cited ``source_id``s that are, right now, the author's Memory
  sources, resolved by ``memory_store.memory_ids_for_source_ids(source_ids,
  author_id)`` on ``memory_sql.memory_source_readable``.

What a report generated before recording cannot tell: which Memory its planner
and drafting prompts used (only its citations can be recognised), and — once a
cited Memory source is gone — that the citation came from Memory at all.

Stored facts count as stored: the page publishes the stored excerpt whatever
has happened to the Memory since.

Another member's Memory
-----------------------
A report that cites another member's Memory source (recorded with
``memory_owner_id`` not the author, or — without a record — a cited source
that is right now a Memory source of another member,
``memory_store.foreign_memory_sources_for_source_ids``) is never published:
the other member was never asked — and a link issued before this rule stops
serving it: the anonymous page asks ``report_foreign_memory_ids`` on every
open.  ``foreign_memory_ids`` carries those
Memory ids; the author already reads the excerpts inside their own report, so
refusing reveals nothing new.  Body text restating another member's Memory
without a citation cannot be recognised in a report generated before the
retrieval fixes that stop such Memory from reaching a report at all.

The publish rule
----------------
* another member's Memory cited: refused (403 at the route), whatever the count.
* count 0: nothing to disclose; publishing is exactly what it was before M4.
* count > 0 and the requester is not the author: refused (403 at the route).
* count above the acknowledgement (an absent one counts as 0):
  ``ShareDisclosureRequired`` carrying the current count (409 at the route).
  A count at or below it is accepted: the author agreed to at least that much,
  and the count of a report generated before recording can only FALL (a cited
  Memory source removed), while its page still carries the excerpt the author
  saw counted.
* already public: re-sharing publishes nothing new; the existing link is
  returned without asking (another member's Memory is still refused).

The route checks this on the publishing request and then hands
``share_memory_guard`` to ``share_report``, which re-counts the live part in
the same transaction that sets the share token and refuses with the same
typed error if it ROSE in between — that upward direction is what the lock
and the in-transaction count protect.  The foreign check is not repeated there:
a cited source cannot become another member's Memory source (Memory source ids
are minted fresh and a Memory's creator never changes), it can only stop being
one, which makes publishing less restricted, never more.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from app.domain.share_disclosure import (
    REPORT_MEMORY_USED_FIELD,
    SHARE_DISCLOSURE_REQUIRED,
    ShareDisclosureRequired,
    ShareMemoryGuard,
    require_acknowledged,
)
from app.domain.conversation_public_view import MAX_TURNS
from app.repositories.ports import (
    ConversationHasNoShareableAnswer,
    ConversationShareWatermarkStale,
)

__all__ = [
    "MEMORY_TITLE_PREFIX",
    "SHARE_DISCLOSURE_REQUIRED",
    "ConversationShareDisclosure",
    "ForeignMemoryShareRefused",
    "MemoryIdReader",
    "MemorySourceMapReader",
    "NonAuthorShareRefused",
    "ShareDisclosure",
    "ShareDisclosureRequired",
    "ShareMemoryGuard",
    "author_memory_sources",
    "conversation_share_disclosure",
    "conversation_share_window",
    "public_memory_title",
    "reference_memory_id",
    "report_foreign_memory_ids",
    "report_share_disclosure",
    "require_conversation_acknowledged",
    "require_publishable",
    "share_memory_guard",
    "turn_references",
    "unresolved_source_ids",
]

MEMORY_OBJECT_TYPE = "memory"


class MemoryIdReader(Protocol):
    def memory_ids_for_source_ids(
        self, source_ids: Sequence[str], owner_id: str
    ) -> list[str]: ...

    def foreign_memory_sources_for_source_ids(
        self, source_ids: Sequence[str], member_id: str
    ) -> Mapping[str, tuple[str, str]]: ...


@dataclass(frozen=True)
class ShareDisclosure:
    """The author, the distinct Memory ids of the author a public page may
    carry, the parts the count was built from (stored facts vs. live-resolved
    sources), and the Memory ids of other members the report cites."""

    author_id: str
    memory_ids: tuple[str, ...]
    known_memory_ids: frozenset[str] = frozenset()
    live_source_ids: tuple[str, ...] = ()
    foreign_memory_ids: tuple[str, ...] = ()

    @property
    def memory_count(self) -> int:
        return len(self.memory_ids)

    @property
    def foreign_memory_count(self) -> int:
        return len(self.foreign_memory_ids)


class NonAuthorShareRefused(Exception):
    """Someone other than the author tried to publish the author's Memory.

    The user-facing sentence belongs to the route (``user_error`` literal)."""


class ForeignMemoryShareRefused(Exception):
    """The report cites another member's Memory; it is never published.

    The user-facing sentence belongs to the route (``user_error`` literal)."""


def report_foreign_memory_ids(
    memory_reader: MemoryIdReader, report: Mapping[str, Any]
) -> tuple[str, ...]:
    """Other members' Memory a stored report cites: recorded citations whose
    owner is not the author, and — for unrecorded citations — cited sources
    that are right now another member's Memory sources.

    The anonymous report page asks this on every open, so a link issued before
    the rule stops serving such a report (the same 404 as a revoked link)."""
    author_id, _known, foreign, live_sources = _classified_references(report)
    if live_sources and author_id:
        foreign.update(
            memory_id for memory_id, _owner in memory_reader
            .foreign_memory_sources_for_source_ids(live_sources, author_id).values()
        )
    return tuple(sorted(foreign))


def _classified_references(
    report: Mapping[str, Any],
) -> tuple[str, set[str], set[str], list[str]]:
    """(author, the author's known Memory ids, other members' recorded Memory
    ids, cited sources without a record) of a stored report."""
    author_id = str(report.get("created_by") or "")
    known: set[str] = {
        str(item) for item in report.get(REPORT_MEMORY_USED_FIELD) or ()
        if isinstance(item, str) and item
    }
    foreign: set[str] = set()
    live_sources: list[str] = []
    for reference in report.get("references") or ():
        if not isinstance(reference, Mapping):
            continue
        object_id = str(reference.get("object_id") or "")
        if object_id and str(reference.get("object_type") or "") == MEMORY_OBJECT_TYPE:
            known.add(object_id)
        recorded = str(reference.get("memory_id") or "")
        if recorded:
            if author_id and str(reference.get("memory_owner_id") or "") == author_id:
                known.add(recorded)
            else:
                foreign.add(recorded)
            continue
        source_id = str(reference.get("source_id") or "")
        if source_id:
            live_sources.append(source_id)
    return author_id, known, foreign, list(dict.fromkeys(live_sources))


def report_share_disclosure(
    memory_reader: MemoryIdReader, report: Mapping[str, Any]
) -> ShareDisclosure:
    """Count the author's Memory a stored report carries, and find any other
    member's Memory it cites."""
    author_id, known, foreign, live_sources = _classified_references(report)
    memory_ids = set(known)
    if live_sources and author_id:
        memory_ids.update(memory_reader.memory_ids_for_source_ids(live_sources, author_id))
        foreign.update(
            memory_id for memory_id, _owner in memory_reader
            .foreign_memory_sources_for_source_ids(live_sources, author_id).values()
        )
    return ShareDisclosure(
        author_id=author_id,
        memory_ids=tuple(sorted(memory_ids)),
        known_memory_ids=frozenset(known),
        live_source_ids=tuple(live_sources),
        foreign_memory_ids=tuple(sorted(foreign)),
    )


def require_publishable(
    disclosure: ShareDisclosure,
    *,
    requester_id: str,
    acknowledged: int | None,
    already_shared: bool = False,
) -> None:
    """Raise unless ``requester_id`` may publish now with ``acknowledged``.

    ``already_shared``: the report is public already, so re-sharing publishes
    nothing new and the acknowledgement is not checked here; the store hands
    back the existing link, and if the link was revoked in the meantime it
    checks the acknowledgement itself (``share_memory_guard``).  Another
    member's Memory is refused either way.
    """
    if disclosure.foreign_memory_ids:
        raise ForeignMemoryShareRefused()
    if disclosure.memory_count == 0:
        return
    if not requester_id or requester_id != disclosure.author_id:
        raise NonAuthorShareRefused()
    if not already_shared:
        require_acknowledged(disclosure.memory_count, acknowledged)


def share_memory_guard(
    disclosure: ShareDisclosure, acknowledged: int | None
) -> ShareMemoryGuard:
    """What ``share_report`` re-checks inside its transaction."""
    return ShareMemoryGuard(
        author_id=disclosure.author_id,
        known_memory_ids=disclosure.known_memory_ids,
        live_source_ids=disclosure.live_source_ids,
        acknowledged=acknowledged,
    )


# --- Conversations ----------------------------------------------------------
#
# A conversation share publishes a watermark-bounded prefix of the
# conversation's completed turns (the notebook-scoped ``answers`` rows, or the
# global conversation's ``done`` jobs), so its count is taken over exactly that
# prefix: the turns up to and including the boundary the share would pin
# (``expected_through_id``; empty = the newest completed turn) in the same
# canonical order and with the same keyset the public snapshot uses, cut at the
# ``MAX_TURNS`` the public page projects.
#
# The count is the number of DISTINCT Memory entries of the author the page may
# carry, read off every anchor and citation of those turns (not only the
# references the page selects: the answer body can restate an anchor the page
# does not list), as the union of
#
# * citations that carry a ``memory_id`` (the author's confirmed Memory cited
#   directly; a run only ever reads the asker's own Memory);
# * anchors that ARE a Memory (``object_type == "memory"``, id in ``object_id``);
# * anchors and citations whose ``source_id`` is, right now, one of the
#   author's Memory sources (a Memory projection hit: an element or a knowledge
#   graph object derived from the Memory), resolved in ONE batch by
#   ``memory_store.memory_sources_for_source_ids(source_ids, author_id)`` (the
#   statement behind ``memory_ids_for_source_ids``, on
#   ``memory_sql.memory_source_readable``) for the window and the published
#   snapshot together.
#
# ``new_memory_count`` is how many of them are not already on the page the link
# serves now (the snapshot behind the current watermark): 0 when nothing new is
# published -- in particular when the boundary IS the current watermark -- and
# all of them when the conversation is not shared yet.  It is never derived from
# the acknowledgement: that is the report's "above what was acknowledged"
# reading (``require_acknowledged``), which a conversation does not use.
#
# The publish rule: a count above 0 needs ``acknowledged_memory_count`` equal
# to it (``!=`` refuses, an absent acknowledgement included); a count of 0
# publishes exactly as before.  Only the conversation's creator reaches either
# endpoint (the share routes' row-level gate), so there is no non-author case.

MEMORY_TITLE_PREFIX = "Memory · "
"""The label prefix the Ask engine gives a direct Memory citation
(``AskService._memory_citations``).  An authenticated reader sees it as the
citation's label; the public projections drop it and carry ``is_memory``
instead, so the page names the author's personal memory in its own words, for
every stored answer, old ones included."""


class MemorySourceMapReader(Protocol):
    def memory_sources_for_source_ids(
        self, source_ids: Sequence[str], owner_id: str
    ) -> Mapping[str, str]: ...


def reference_memory_id(
    reference: Any, memory_by_source: Mapping[str, str]
) -> str:
    """The Memory id a stored reference (a ``Citation``, an ``AnswerAnchor`` or
    a report reference) stands for, or ``""``.

    The one definition the conversation count and both public projections
    share: a recorded ``memory_id``, else a reference that IS a Memory
    (``object_type == "memory"``), else a ``source_id`` that
    ``memory_by_source`` (the author's Memory sources, ``{source_id:
    memory_id}``) maps."""
    if not isinstance(reference, Mapping):
        return ""
    recorded = str(reference.get("memory_id") or "")
    if recorded:
        return recorded
    object_id = str(reference.get("object_id") or "")
    if object_id and str(reference.get("object_type") or "") == MEMORY_OBJECT_TYPE:
        return object_id
    return str(memory_by_source.get(str(reference.get("source_id") or ""), "") or "")


def unresolved_source_ids(references: Sequence[Any]) -> list[str]:
    """Distinct ``source_id``s of the references whose Memory identity is not a
    stored fact -- the only ones the batched source lookup has to answer."""
    wanted: list[str] = []
    for reference in references:
        if not isinstance(reference, Mapping) or reference_memory_id(reference, {}):
            continue
        source_id = str(reference.get("source_id") or "")
        if source_id:
            wanted.append(source_id)
    return list(dict.fromkeys(wanted))


def author_memory_sources(
    reader: MemorySourceMapReader, source_ids: Sequence[str], author_id: str
) -> dict[str, str]:
    """``{source_id: memory_id}`` for the given sources that are the author's
    Memory sources: ONE batched read, and none when there is nothing to ask."""
    wanted = list(dict.fromkeys(str(item) for item in source_ids if item))
    if not wanted or not author_id:
        return {}
    return dict(reader.memory_sources_for_source_ids(wanted, author_id))


def public_memory_title(title: str) -> str:
    """A Memory reference's title as a public page shows it: without the
    engine's ``Memory · `` label prefix (the page marks it with ``is_memory``)."""
    if title.startswith(MEMORY_TITLE_PREFIX):
        return title[len(MEMORY_TITLE_PREFIX):].strip()
    return title


def turn_references(payload: Any) -> list[Mapping[str, Any]]:
    """Every anchor and citation of one stored answer (an ``AskResponse``
    payload; a global turn's legacy ``response`` has the same two lists)."""
    row = payload if isinstance(payload, Mapping) else {}
    found: list[Mapping[str, Any]] = []
    for key in ("anchors", "citations"):
        items = row.get(key)
        if isinstance(items, list):
            found.extend(item for item in items if isinstance(item, Mapping))
    return found


@dataclass(frozen=True)
class ConversationShareDisclosure:
    """The author's distinct Memory ids in the snapshot about to be published,
    and those the currently published snapshot already carries."""

    memory_ids: frozenset[str]
    published_memory_ids: frozenset[str] = frozenset()

    @property
    def memory_count(self) -> int:
        return len(self.memory_ids)

    @property
    def new_memory_count(self) -> int:
        """Relative to the current watermark: the entries this share adds to
        the page the link serves now.  Never above ``memory_count``."""
        return len(self.memory_ids - self.published_memory_ids)


def conversation_share_disclosure(
    reader: MemorySourceMapReader,
    author_id: str,
    payloads: Sequence[Any],
    published_payloads: Sequence[Any] = (),
) -> ConversationShareDisclosure:
    """Count the author's Memory in the turns about to be published
    (``payloads``) and in the turns the link serves now
    (``published_payloads``), with one batched source lookup for both."""
    window = [ref for payload in payloads for ref in turn_references(payload)]
    published = [
        ref for payload in published_payloads for ref in turn_references(payload)
    ]
    memory_by_source = author_memory_sources(
        reader, unresolved_source_ids(window + published), author_id
    )

    def ids(references: list[Mapping[str, Any]]) -> frozenset[str]:
        return frozenset(
            memory_id for reference in references
            if (memory_id := reference_memory_id(reference, memory_by_source))
        )

    return ConversationShareDisclosure(
        memory_ids=ids(window), published_memory_ids=ids(published)
    )


def require_conversation_acknowledged(
    disclosure: ConversationShareDisclosure, acknowledged: int | None
) -> None:
    """Raise ``ShareDisclosureRequired`` unless a conversation carrying the
    author's Memory comes with exactly the count as its acknowledgement
    (``!=`` refuses, an absent one included).  A count of 0 never asks."""
    if disclosure.memory_count and acknowledged != disclosure.memory_count:
        raise ShareDisclosureRequired(
            disclosure.memory_count, disclosure.new_memory_count
        )


def conversation_share_window(
    turns: Sequence[tuple[str, Any]],
    conversation_id: str,
    through_id: str = "",
    published_through_id: str = "",
) -> tuple[str, list[Any]]:
    """``(boundary turn id, payloads of the turns the share would publish)``.

    ``turns`` are every completed turn of the conversation as ``(id, payload)``
    in the canonical order the public snapshot uses; the window is the prefix
    ending at ``through_id`` (empty = the newest), cut at ``MAX_TURNS`` like
    the public page.  It refuses exactly as the share write does, with the same
    exceptions: a boundary that does not resolve, or that sorts before the
    published watermark ``published_through_id`` (the watermark only
    advances; a watermark turn that no longer resolves is not compared, as
    the store does not compare it), is ``ConversationShareWatermarkStale``; a
    conversation without a completed turn is
    ``ConversationHasNoShareableAnswer``."""
    if not turns:
        raise ConversationHasNoShareableAnswer(conversation_id)
    positions = {turn_id: position for position, (turn_id, _payload) in enumerate(turns)}
    if through_id:
        index = positions.get(through_id)
        if index is None:
            raise ConversationShareWatermarkStale(through_id)
    else:
        index = len(turns) - 1
    published = positions.get(published_through_id) if published_through_id else None
    if published is not None and index < published:
        raise ConversationShareWatermarkStale(through_id or turns[index][0])
    window = [payload for _turn_id, payload in turns[: index + 1]][:MAX_TURNS]
    return turns[index][0], window
