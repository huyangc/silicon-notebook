"""Section outline projection and section-hint selection (pure, zero I/O).

The reasoning retriever's in-document navigation (``browse_sections`` /
``read_section`` and the vector section-hint block) reads a source's chunks as
an outline of *heading occurrences*. This module turns the rows the chunk store
returns into that outline, filters, ranks, addresses, resolves and renders it.
Everything is deterministic; nothing here reads the database, the vectors or
the run state -- the caller (the reasoning retriever) supplies rows,
similarities and the run's chunk ids.

Input rows
    ``((chunk_id, section_path, navigation), ...)`` in document order, plus
    ``truncated`` (the scan stopped at its row bound) -- exactly what
    ``ChunkStorePort.source_outline_rows`` returns.

Structure (generic; no label word lists, no TOC-line patterns -- user ruling
2026-10-21)
    * **Occurrence**: a run of consecutive rows with the same ``section_path``.
      The same label later in the document is a separate occurrence
      (duplicates are never merged). An occurrence whose chunks are all
      navigation pages is dropped; navigation chunks of a mixed occurrence are
      left out of its ``chunk_ids``.
    * **Numbering** is read with the detector's parser
      (``navigation_chunks.heading_numbering``) on the label's last
      breadcrumb segment (the whole label in a flat source, see Kinds):
      ``5.7.2.1`` -> depth by segments, ``A.2`` likewise. The parser does not
      read a number followed by a dot (``1. Install``, ``5.7.``) -- such
      labels stay unnumbered here exactly as in the detector.
      A dotted number of two segments or more is taken as parsed. A
      one-segment reading -- a bare number (``5 Electrical``, but also
      ``16 GT/s Data Rate`` / ``2种低功耗模式``), a lone capital letter
      (``A Compliance``) or the chapter of a bare word-enumerated title
      (``Chapter 5`` / ``第5章`` / ``Appendix A``) -- counts only with
      structural corroboration, read off the numbering sequence (no word
      lists): the next dotted number starts with it (``5`` -> ``5.1``), or it
      is the current top-level chapter + 1 (``B`` after ``A``) and the
      current chapter does not continue after it (``6`` after ``5.9`` with no
      ``5.x`` later). Otherwise it is a leaf, so a ``16 GT/s`` heading inside
      ``5.7.3`` neither ends 5.7.3 nor rebuilds the virtual ``5`` / ``5.7``.
      A caption word additionally counts as a chapter word only when EVERY
      occurrence of that word (casefolded) is corroborated: with
      document-wide table numbering some ``Table N`` is always out of
      sequence, so ``Table 2 Timing`` right before ``2 Electrical`` stays a
      leaf, while ``Chapter N`` headings follow the sequence everywhere.
      (Chosen over "the corroborating number must be an ``N.x`` subsection":
      that still accepts ``Table 2`` right before ``2.1`` when chapter 2 has
      no heading of its own; the per-word rule rejects it whenever any other
      table breaks the sequence. Known residual: a document whose only
      ``Table N`` sits right before ``N.1``.) An item caption (``Figure
      5-1.``, ``Table 5-18``) is always a leaf.
    * **Kinds**: a source is *breadcrumbed* when two different
      multi-segment labels share their first segment, or a multi-segment
      label's parent path is itself a label. Flat headings whose text happens
      to contain `` > `` (``VIN > VOUT behaviour``, ``A > B compare``) meet
      neither, and the source is then read flat, each label one segment
      (residual: a breadcrumb source whose every top heading has no body and
      a single child also reads flat). In a breadcrumbed
      source every titled occurrence is structural: a *breadcrumb* node
      (depth from its parent chain), or a *numbered* node for a numbered
      one-segment label. In a flat source a numbered label is a *numbered*
      node and everything else (captions, ``Notes``, a title page) is a
      *leaf*; the preamble ``""`` is always a leaf. Structural nodes
      (breadcrumb / numbered / virtual) open a subtree; leaves do not end the
      enclosing section.
    * **Parent**: X is a child of an open node P when X's segments extend P's,
      or when both are numbered under the same breadcrumb parent and X's
      number extends P's (``5.7`` -> ``5.7.x``; ``5.70`` is NOT under ``5.7``).
    * **Virtual parents**: a numbered occurrence whose number's prefixes have
      no open occurrence gets synthesised parents (``5.7.1`` alone -> virtual
      ``5`` and ``5.7``) placed right before it; a virtual node owns no chunks
      and its range is all its descendants, so ``read_section("5.7")`` works.
    * **Range**: a structural node's range runs from it to the next
      structural occurrence that is not its descendant (= the next
      same-or-higher level heading); a leaf's range is the leaf alone. A
      document of leaves only therefore gets no subtree expansion.
      ``Occurrence.range_chunk_ids`` is the range's chunks in document order.
    * **Open ranges**: when the scan was cut (``truncated``), every occurrence
      whose range reaches the scan's end -- the last occurrence and every
      node still open there, virtual parents included -- is
      ``range_open``: its section may continue past the scanned rows. The
      flag travels into ``SectionAddress.incomplete`` and every rendered
      ``（N 段）`` note reads ``（N 段，可能未完）``.

Public API (T6 wires these)
    ``build_outline(rows, truncated=False) -> Outline``
        ``Outline.occurrences[i].index == i`` (document order, virtual nodes
        included). ``Outline.section_count`` = real (non-virtual) occurrences.
        ``Outline.numbered`` tells whether ``under`` is usable.
    ``filter_outline(outline, under="") -> OutlineView``
        ``under`` empty: every occurrence. Otherwise ``under`` is parsed with
        the same numbering reading (``5.7`` / ``A`` / ``Chapter 5``) and the
        view is the subtrees of occurrences numbered ``under`` or
        ``under.*`` (their caption/``Notes`` leaves included).
        ``under`` is read after NFKC with trailing punctuation dropped
        (``5.7.``). ``status`` tells the two refusals apart --
        ``UNDER_NO_NUMBERING`` (the document has none) and ``UNDER_UNPARSED``
        (``under`` names no number) -- both with ``entries`` empty;
        ``under_supported`` is ``status == UNDER_OK``.
    ``rank_sections(outline, chunk_sims, *, exclude_chunk_ids=frozenset(),
    entries=None) -> list[RankedSection]``
        An occurrence is *covered*, and skipped, when any of its OWN chunks
        is in ``exclude_chunk_ids`` (the run's chunks). Score = max similarity
        over the occurrence's range chunks that are neither in
        ``exclude_chunk_ids`` nor own chunks of a covered occurrence (so a
        covered ``1.4`` does not lift ``1``); chunks without a similarity are
        ignored and an occurrence with none is not ranked.
        Ties by a descendant are collapsed: walking the ranking (score desc;
        ties: structural before leaf, deeper first, smaller range, document
        order) an occurrence whose best chunk is already the best chunk of a
        kept one is dropped -- that can only be an ancestor (or the leaf)
        scoring through the kept occurrence's chunk. So a hit in ``5.7.2.1``
        yields ``5.7.2.1``, not also ``5.7.2`` / virtual ``5.7`` / ``5``, and a
        caption leaf inside ``5.7.3`` yields ``5.7.3``; an ancestor that scores
        with a chunk of its own stays, and so do its other subsections (a
        chapter introduction never swallows the chapter). ``entries``
        restricts ranking to a view (``filter_outline(...).entries``) for
        ``browse_sections(query=...)``; ``limit > 0`` stops after that many.
    ``select_hints(per_source_ranked, *, max_entries, relative_floor,
    best_chunk_sim) -> list[SectionHint]``
        ``per_source_ranked``: ``SourceRanking`` per source in first-seen
        order. Keeps entries with ``score >= best_chunk_sim * relative_floor``,
        merges sources by rank (all rank-1 entries, then rank-2 ...; within a
        rank by score desc, then source order) and caps at ``max_entries``.
        With ``best_chunk_sim <= 0`` the floor admits everything; whether to
        show hints at all then is the caller's decision.
    ``AddressBook``
        Run-local ids ``s1, s2, ...``: ``assign(notebook_id, source_id,
        occurrence)`` returns the id for that (notebook, source, occurrence
        index), assigning the next one on first sight; ``get(sid)`` ->
        ``SectionAddress(notebook_id, source_id, chunk_ids, label,
        occurrence)`` where ``chunk_ids`` is the occurrence's range;
        ``parse_section_id("[S3]") -> "s3"``. Stability relies on two caller
        rules: each source's outline is built once per run (occurrence
        indexes are positions in that outline), and ``notebook_id`` is passed
        in one convention per run (``""`` for the run's notebook everywhere,
        or its id everywhere -- mixing them issues two ids for one section).
    ``resolve_section(outline, text, run_chunk_ids) -> Resolved | Ambiguous |
    NotFound``
        Matches the full label or its last breadcrumb segment (whitespace and
        case folded, NFKC); when nothing matches, the dotted or digits-only
        number the text starts with (``5.7.3`` / ``5.7.3 Standard Package`` /
        ``５.７``) -- never a caption's chapter (``Table 7``) or a lone letter. One match -> ``Resolved``;
        several -> the one whose range holds a run chunk; still not one ->
        ``Ambiguous`` (the run-holding candidates when two or more hold one,
        else all).
    ``render_outline(entries, *, max_chars, truncated_scan, scan_rows) ->
    RenderedOutline``
        ``entries``: ``(sid, occurrence)`` pairs in display order. Lines
        ``[sN] <title>（N 段）`` indented two spaces per depth below the
        shallowest entry (a breadcrumb node shows its last segment; N = range
        chunks). Over ``max_chars`` the list is cut and「另有 K 条未列出，可用
        under/query 收窄」appended; a truncated scan appends「大纲只覆盖本篇前 N
        段」. ``max_chars <= 0`` = no limit.
    ``render_ranked(entries, *, max_chars, truncated_scan, scan_rows) ->
    RenderedOutline``
        The ``browse_sections(query=...)`` form: entries in rank order, one
        unindented line each with the full label (``display_label``).
        Both renderers clip the first line's title when even it does not
        fit and never exceed ``max_chars``; below ``RENDER_MIN_CHARS`` the
        notes themselves may be cut.
    ``render_hints(hints) -> str``
        ``hints``: ``(sid, SectionHint)`` pairs. Title「相关章节线索（可用
        read_section 按章节读取原文）」then ``[s3]《来源标题》 5.7.2.1 Loss and
        Crosstalk Mask（3 段）`` per hint; ``""`` when there is none (no block).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from app.services.navigation_chunks import heading_numbering

#: Separator of breadcrumb segments in ``chunks.section_path``
#: (``chunking.build_chunks``).
CRUMB_SEPARATOR = " > "
#: Display name of the occurrence before the first heading (empty label).
#: Display only: its ``label`` stays ``""`` and resolution never matches it.
UNTITLED = "（无标题）"
#: Smallest ``max_chars`` at which a cut listing still shows its notes and
#: at least one (clipped) line; ``render_*`` never exceed ``max_chars`` below
#: it either, but may then drop notes. T6 gives the browse Settings this floor.
RENDER_MIN_CHARS = 200
#: Marker on a range the outline scan cut (``Occurrence.range_open``), inside
#: the ``（N 段…）`` size note of every listing, hint and candidate line.
OPEN_RANGE_NOTE = "，可能未完"
#: Title of the hint block (model-facing).
HINT_BLOCK_TITLE = "相关章节线索（可用 read_section 按章节读取原文）"

KIND_NUMBERED = "numbered"
KIND_BREADCRUMB = "breadcrumb"
KIND_LEAF = "leaf"
KIND_VIRTUAL = "virtual"

UNDER_OK = "ok"
UNDER_NO_NUMBERING = "no_numbering"
UNDER_UNPARSED = "unparsed"

_SECTION_ID = re.compile(r"^\s*\[?\s*[sS](\d+)\s*\]?\s*$")
_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------- data types


@dataclass(frozen=True)
class Occurrence:
    """One heading occurrence of the outline.

    ``index``: position in ``Outline.occurrences`` (document order, virtual
    nodes included); the address key. ``label``: the stored ``section_path``
    (a virtual node: its number). ``chunk_ids``: its own non-navigation chunks.
    ``range_end``: exclusive end index of its range in ``Outline.occurrences``;
    ``range_chunk_ids``: the range's chunks in document order. ``parent``:
    index of the enclosing structural node, or None at top level.
    ``range_open``: the outline scan was cut and this range runs to the scan's
    end (the last occurrence and every node still open there, virtual parents
    included) -- the section may continue past what was read."""

    index: int
    label: str
    kind: str
    depth: int
    number: str
    crumbs: tuple[str, ...]
    chunk_ids: tuple[str, ...]
    range_end: int
    range_chunk_ids: tuple[str, ...]
    parent: "int | None" = None
    range_open: bool = False

    @property
    def virtual(self) -> bool:
        return self.kind == KIND_VIRTUAL

    @property
    def structural(self) -> bool:
        return self.kind != KIND_LEAF

    @property
    def title(self) -> str:
        """Display title in a tree listing: the last breadcrumb segment."""
        return self.crumbs[-1] if self.crumbs else (self.label or UNTITLED)

    @property
    def display_label(self) -> str:
        """The full label (``UNTITLED`` for the preamble)."""
        return self.label or UNTITLED


@dataclass(frozen=True)
class Outline:
    occurrences: tuple[Occurrence, ...]
    truncated: bool = False
    scanned_rows: int = 0

    @property
    def numbered(self) -> bool:
        """Whether any occurrence carries numbering (``under`` usable)."""
        return any(occ.number for occ in self.occurrences)

    @property
    def section_count(self) -> int:
        """Real (non-virtual) occurrences."""
        return sum(1 for occ in self.occurrences if not occ.virtual)


@dataclass(frozen=True)
class OutlineView:
    """``status``: ``UNDER_OK`` (also for an empty ``under``),
    ``UNDER_NO_NUMBERING`` (the document has no numbering) or
    ``UNDER_UNPARSED`` (``under`` names no section number)."""

    entries: tuple[Occurrence, ...]
    status: str = "ok"

    @property
    def under_supported(self) -> bool:
        return self.status == UNDER_OK


@dataclass(frozen=True)
class RankedSection:
    occurrence: Occurrence
    score: float
    best_chunk_id: str


@dataclass(frozen=True)
class SourceRanking:
    """One source's ranked sections, input of ``select_hints``."""

    notebook_id: str
    source_id: str
    source_title: str
    ranked: tuple[RankedSection, ...]


@dataclass(frozen=True)
class SectionHint:
    notebook_id: str
    source_id: str
    source_title: str
    occurrence: Occurrence
    score: float
    rank: int  # 1-based rank inside its source


@dataclass(frozen=True)
class SectionAddress:
    """A surfaced section: ``chunk_ids`` = the range, in document order;
    ``notebook_id`` = the rows' owner (``""`` = the run's notebook);
    ``incomplete`` = the occurrence's ``range_open`` (the scan stopped inside
    this range, so ``chunk_ids`` may be only its scanned prefix)."""

    notebook_id: str
    source_id: str
    chunk_ids: tuple[str, ...]
    label: str
    occurrence: int = -1
    incomplete: bool = False


@dataclass(frozen=True)
class Resolved:
    occurrence: Occurrence


@dataclass(frozen=True)
class Ambiguous:
    candidates: tuple[Occurrence, ...]


@dataclass(frozen=True)
class NotFound:
    pass


@dataclass(frozen=True)
class RenderedOutline:
    text: str
    listed: int
    omitted: int


# ---------------------------------------------------------------- building


@dataclass
class _Draft:
    label: str
    crumbs: tuple[str, ...]
    chunk_ids: list[str] = field(default_factory=list)
    any_content: bool = False
    raw_number: str = ""
    caption_word: str = ""
    caption_chapter: str = ""
    caption_item: bool = False
    number: str = ""
    kind: str = KIND_LEAF


def _crumbs(label: str) -> tuple[str, ...]:
    return tuple(seg.strip() for seg in label.split(CRUMB_SEPARATOR) if seg.strip())


def _confident(number: str) -> bool:
    """A dotted number of two segments or more: needs no corroboration."""
    return "." in number


def _fold(rows: Iterable[Sequence]) -> tuple[list[_Draft], int]:
    drafts: list[_Draft] = []
    count = 0
    previous: "str | None" = None
    for row in rows:
        chunk_id, section_path, navigation = row[0], row[1], row[2]
        count += 1
        label = (section_path or "").strip()
        if previous is None or label != previous:
            drafts.append(_Draft(label=label, crumbs=_crumbs(label)))
            previous = label
        draft = drafts[-1]
        if not navigation:
            draft.any_content = True
            draft.chunk_ids.append(str(chunk_id))
    return [d for d in drafts if d.any_content], count


def _breadcrumbed(drafts: Sequence[_Draft]) -> bool:
    """Whether the source's labels are breadcrumbs: two multi-segment labels
    share their first segment, or a multi-segment label's parent path is
    itself a label. Flat headings whose text happens to contain `` > ``
    (``VIN > VOUT behaviour``, ``A > B compare``) meet neither."""
    multi = [d for d in drafts if len(d.crumbs) >= 2]
    heads: dict[str, set[str]] = {}
    for draft in multi:
        heads.setdefault(draft.crumbs[0], set()).add(draft.label)
    if any(len(labels) >= 2 for labels in heads.values()):
        return True
    labels = {CRUMB_SEPARATOR.join(d.crumbs) for d in drafts if d.crumbs}
    return any(CRUMB_SEPARATOR.join(d.crumbs[:-1]) in labels for d in multi)


def _next_letter(letter: str) -> str:
    return chr(ord(letter) + 1) if len(letter) == 1 and "A" <= letter < "Z" else ""


def _corroborate(drafts: list[_Draft]) -> None:
    """Set ``draft.number``: dotted numbers as parsed; one-segment readings
    only with structural corroboration (module docstring, Numbering)."""
    # First segment of the next dotted (two-segment-or-more) number.
    following: list[str] = [""] * len(drafts)
    upcoming = ""
    for i in range(len(drafts) - 1, -1, -1):
        following[i] = upcoming
        if _confident(drafts[i].raw_number):
            upcoming = drafts[i].raw_number.split(".")[0]
    rejected: set[str] = set()  # caption words not read as chapter words
    while True:
        failed = _assign_numbers(drafts, following, rejected)
        if not failed - rejected:
            return
        rejected |= failed


def _assign_numbers(drafts: list[_Draft], following: list[str], rejected: set[str]) -> set[str]:
    """One forward pass; returns the caption words with an uncorroborated
    occurrence."""
    failed: set[str] = set()
    digit, letter = 0, ""  # the current top-level chapter, by kind

    def corroborated(candidate: str, nxt: str) -> bool:
        if nxt == candidate:
            return True  # its own subsection follows (5 -> 5.1)
        if candidate.isdigit():
            # The next chapter in sequence, and the current chapter does not
            # continue after it.
            return int(candidate) == digit + 1 and nxt != str(digit)
        return bool(letter) and candidate == _next_letter(letter) and nxt != letter

    for i, draft in enumerate(drafts):
        draft.number = ""
        raw, nxt = draft.raw_number, following[i]
        if _confident(raw):
            draft.number = raw
        elif raw:
            if corroborated(raw, nxt):
                draft.number = raw
        elif draft.caption_chapter and not draft.caption_item:
            word = draft.caption_word.casefold()
            if not corroborated(draft.caption_chapter, nxt):
                failed.add(word)
            elif word not in rejected:
                draft.number = draft.caption_chapter
        if draft.number:
            head = draft.number.split(".")[0]
            if head.isdigit():
                digit = int(head)
            else:
                letter = head
    return failed


def _classify(drafts: list[_Draft]) -> None:
    breadcrumbed = _breadcrumbed(drafts)
    if not breadcrumbed:
        for draft in drafts:
            draft.crumbs = (draft.label,) if draft.label else ()
    for draft in drafts:
        reading = heading_numbering(draft.crumbs[-1] if draft.crumbs else "")
        draft.raw_number = reading.number
        draft.caption_word = reading.caption_word
        draft.caption_chapter = reading.caption_chapter
        draft.caption_item = reading.caption_item
    _corroborate(drafts)
    for draft in drafts:
        if draft.number and len(draft.crumbs) < 2:
            draft.kind = KIND_NUMBERED
        elif breadcrumbed and draft.crumbs:
            draft.kind = KIND_BREADCRUMB
        else:
            draft.kind = KIND_LEAF


@dataclass
class _Node:
    index: int
    label: str
    kind: str
    depth: int
    number: str
    crumbs: tuple[str, ...]
    chunk_ids: tuple[str, ...]
    parent: "int | None"
    range_end: int = -1

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.number.split(".")) if self.number else ()


def _extends(longer: Sequence[str], shorter: Sequence[str]) -> bool:
    return len(longer) > len(shorter) and tuple(longer[: len(shorter)]) == tuple(shorter)


def _is_parent(parent: _Node, crumbs: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    if parent.crumbs and _extends(crumbs, parent.crumbs):
        return True
    return bool(
        parent.number
        and parts
        and crumbs[:-1] == parent.crumbs[:-1]
        and _extends(parts, parent.parts)
    )


def build_outline(rows: Iterable[Sequence], truncated: bool = False) -> Outline:
    """Fold ``(chunk_id, section_path, navigation)`` rows (document order)
    into the outline; see the module docstring for the rules."""
    drafts, scanned = _fold(rows)
    _classify(drafts)
    nodes: list[_Node] = []
    stack: list[_Node] = []

    def add(label, kind, number, crumbs, chunk_ids) -> _Node:
        parent = stack[-1] if stack else None
        node = _Node(
            index=len(nodes),
            label=label,
            kind=kind,
            depth=parent.depth + 1 if parent else 0,
            number=number,
            crumbs=crumbs,
            chunk_ids=tuple(chunk_ids),
            parent=parent.index if parent else None,
        )
        nodes.append(node)
        return node

    for draft in drafts:
        if draft.kind == KIND_LEAF:
            leaf = add(draft.label, KIND_LEAF, draft.number, draft.crumbs, draft.chunk_ids)
            leaf.range_end = leaf.index + 1
            continue
        parts = tuple(draft.number.split(".")) if draft.number else ()
        while stack and not _is_parent(stack[-1], draft.crumbs, parts):
            stack.pop().range_end = len(nodes)
        if len(parts) >= 2:
            base = 0
            for open_node in stack:
                if open_node.number and _extends(parts, open_node.parts):
                    base = max(base, len(open_node.parts))
            for end in range(base + 1, len(parts)):
                number = ".".join(parts[:end])
                stack.append(
                    add(number, KIND_VIRTUAL, number, draft.crumbs[:-1] + (number,), ())
                )
        stack.append(add(draft.label, draft.kind, draft.number, draft.crumbs, draft.chunk_ids))
    for open_node in stack:
        open_node.range_end = len(nodes)
    occurrences = []
    for node in nodes:
        span = nodes[node.index : node.range_end]
        occurrences.append(
            Occurrence(
                index=node.index,
                label=node.label,
                kind=node.kind,
                depth=node.depth,
                number=node.number,
                crumbs=node.crumbs,
                chunk_ids=node.chunk_ids,
                range_end=node.range_end,
                range_chunk_ids=tuple(cid for member in span for cid in member.chunk_ids),
                parent=node.parent,
                range_open=bool(truncated) and node.range_end == len(nodes),
            )
        )
    return Outline(occurrences=tuple(occurrences), truncated=bool(truncated), scanned_rows=scanned)


# ---------------------------------------------------------------- filtering


_TRAILING_MARKS = ".。:：、,，;；)）]】 \t"


def _number_reading(text: str):
    """The numbering parse of free text (an ``under`` value, a section name):
    NFKC first (``５.７`` -> ``5.7``), trailing punctuation dropped
    (``5.7.`` -> ``5.7``)."""
    cleaned = unicodedata.normalize("NFKC", text or "").strip().rstrip(_TRAILING_MARKS)
    return heading_numbering(cleaned)


def _under_number(text: str) -> str:
    """The section an ``under`` value names: its number (``5.7`` / ``A``),
    or the chapter of a bare word-enumerated title (``Chapter 5`` -> ``5``)."""
    reading = _number_reading(text)
    if reading.number:
        return reading.number
    if reading.caption_chapter and not reading.caption_item:
        return reading.caption_chapter
    return ""


def _named_number(text: str) -> str:
    """The number ``resolve_section`` falls back to: a dotted number or a
    digits-only one only. A caption's chapter (``Table 7``) and a lone capital
    letter (``A Brief ...``) do not name a section."""
    number = _number_reading(text).number
    return number if _confident(number) or number.isdigit() else ""


def _under(number: str, prefix: str) -> bool:
    return number == prefix or number.startswith(prefix + ".")


def filter_outline(outline: Outline, under: str = "") -> OutlineView:
    """The occurrences to list; see the module docstring."""
    if not (under or "").strip():
        return OutlineView(entries=outline.occurrences)
    if not outline.numbered:
        return OutlineView(entries=(), status=UNDER_NO_NUMBERING)
    prefix = _under_number(under)
    if not prefix:
        return OutlineView(entries=(), status=UNDER_UNPARSED)
    matched: set[int] = set()
    entries = []
    for occ in outline.occurrences:  # parents precede children
        if (occ.number and _under(occ.number, prefix)) or (
            occ.parent is not None and occ.parent in matched
        ):
            matched.add(occ.index)
            entries.append(occ)
    return OutlineView(entries=tuple(entries))


# ---------------------------------------------------------------- ranking


def rank_sections(
    outline: Outline,
    chunk_sims: Mapping[str, float],
    *,
    exclude_chunk_ids: "frozenset[str] | set[str]" = frozenset(),
    entries: "Sequence[Occurrence] | None" = None,
    limit: int = 0,
) -> list[RankedSection]:
    """Rank occurrences by their best uncovered chunk; see the module
    docstring for covered / nested handling."""
    # A covered occurrence's chunks score nothing, not even for an ancestor:
    # the run already holds that section.
    covered = {
        occ.index
        for occ in outline.occurrences
        if any(cid in exclude_chunk_ids for cid in occ.chunk_ids)
    }
    seen = set(exclude_chunk_ids)
    for index in covered:
        seen.update(outline.occurrences[index].chunk_ids)
    candidates = outline.occurrences if entries is None else entries
    scored: list[RankedSection] = []
    for occ in candidates:
        if occ.index in covered:
            continue
        best_id, best = "", None
        for cid in occ.range_chunk_ids:
            if cid in seen:
                continue
            sim = chunk_sims.get(cid)
            if sim is None:
                continue
            if best is None or sim > best:
                best_id, best = cid, float(sim)
        if best is not None:
            scored.append(RankedSection(occurrence=occ, score=best, best_chunk_id=best_id))
    scored.sort(
        key=lambda item: (
            -item.score,
            0 if item.occurrence.structural else 1,
            -item.occurrence.depth,
            len(item.occurrence.range_chunk_ids),
            item.occurrence.index,
        )
    )
    # Two occurrences share a best chunk only when one contains the other
    # (a chunk belongs to one occurrence; the ranges holding it are that
    # occurrence and its ancestors), so a claimed-chunk set is the "ancestor
    # tied by a kept descendant" test in O(1).
    claimed: set[str] = set()
    kept: list[RankedSection] = []
    for item in scored:
        if item.best_chunk_id in claimed:
            continue
        claimed.add(item.best_chunk_id)
        kept.append(item)
        if limit > 0 and len(kept) >= limit:
            break
    return kept


def select_hints(
    per_source_ranked: Sequence[SourceRanking],
    *,
    max_entries: int,
    relative_floor: float,
    best_chunk_sim: float,
) -> list[SectionHint]:
    """Merge per-source rankings by rank under the relative floor and cap."""
    if max_entries <= 0:
        return []
    floor = float(best_chunk_sim) * float(relative_floor)
    pool: list[tuple[int, float, int, SectionHint]] = []
    for order, source in enumerate(per_source_ranked):
        rank = 0
        for item in source.ranked:
            if item.score < floor:
                continue
            rank += 1
            hint = SectionHint(
                notebook_id=source.notebook_id,
                source_id=source.source_id,
                source_title=source.source_title,
                occurrence=item.occurrence,
                score=item.score,
                rank=rank,
            )
            pool.append((rank, -item.score, order, hint))
    pool.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in pool[:max_entries]]


# ---------------------------------------------------------------- addresses


def parse_section_id(text: str) -> str:
    """``"s3"`` for ``s3`` / ``S3`` / ``[s3]``; ``""`` when the text is not an
    address id."""
    match = _SECTION_ID.match(text or "")
    return f"s{int(match.group(1))}" if match else ""


class AddressBook:
    """Run-local section ids. Ids are issued in first-seen order and never
    reused; one (notebook_id, source_id, occurrence index) keeps one id."""

    def __init__(self) -> None:
        self._ids: dict[tuple[str, str, int], str] = {}
        self._addresses: dict[str, SectionAddress] = {}

    def assign(self, notebook_id: str, source_id: str, occurrence: Occurrence) -> str:
        key = (notebook_id or "", source_id, occurrence.index)
        sid = self._ids.get(key)
        if sid is None:
            sid = f"s{len(self._ids) + 1}"
            self._ids[key] = sid
            self._addresses[sid] = SectionAddress(
                notebook_id=notebook_id or "",
                source_id=source_id,
                chunk_ids=occurrence.range_chunk_ids,
                label=occurrence.label,
                occurrence=occurrence.index,
                incomplete=occurrence.range_open,
            )
        return sid

    def sid_of(self, notebook_id: str, source_id: str, occurrence_index: int) -> str:
        return self._ids.get((notebook_id or "", source_id, occurrence_index), "")

    def get(self, sid: str) -> "SectionAddress | None":
        return self._addresses.get(parse_section_id(sid) or sid)

    # 测试观察点:本 run 地址簿的只读视图观察点
    @property
    def addresses(self) -> Mapping[str, SectionAddress]:
        return MappingProxyType(self._addresses)

    def __contains__(self, sid: object) -> bool:
        return isinstance(sid, str) and self.get(sid) is not None

    def __len__(self) -> int:
        return len(self._addresses)


# ---------------------------------------------------------------- resolving


def _fold_text(text: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text or "")).strip().casefold()


def resolve_section(
    outline: Outline,
    text: str,
    run_chunk_ids: "frozenset[str] | set[str]" = frozenset(),
) -> "Resolved | Ambiguous | NotFound":
    """Resolve a section named by label text or number; see the module
    docstring."""
    query = _fold_text(text)
    if not query:
        return NotFound()
    matches = [
        occ
        for occ in outline.occurrences
        if _fold_text(occ.label) == query or (occ.crumbs and _fold_text(occ.crumbs[-1]) == query)
    ]
    if not matches:
        number = _named_number(text)
        if number:
            matches = [occ for occ in outline.occurrences if occ.number == number]
    if not matches:
        return NotFound()
    if len(matches) == 1:
        return Resolved(matches[0])
    holding = [occ for occ in matches if any(cid in run_chunk_ids for cid in occ.range_chunk_ids)]
    if len(holding) == 1:
        return Resolved(holding[0])
    return Ambiguous(tuple(holding if holding else matches))


# ---------------------------------------------------------------- rendering


def _omitted_note(count: int) -> str:
    return f"另有 {count} 条未列出，可用 under/query 收窄"


def _scan_note(rows: int) -> str:
    return f"大纲只覆盖本篇前 {rows} 段"


def _size(parts: Sequence[str]) -> int:
    return sum(len(part) for part in parts) + max(len(parts) - 1, 0)


def _fit(
    lines: Sequence[tuple[str, str, str]],
    *,
    max_chars: int,
    truncated_scan: bool,
    scan_rows: int,
) -> RenderedOutline:
    """Join ``(head, title, tail)`` lines under ``max_chars``: cut the list
    with the omitted note, clip the first line's title when even it does not
    fit, and never exceed ``max_chars`` (``<= 0`` = no limit)."""
    full = [head + title + tail for head, title, tail in lines]
    notes = [_scan_note(scan_rows)] if truncated_scan else []
    if max_chars <= 0 or _size(full + notes) <= max_chars:
        return RenderedOutline(text="\n".join(full + notes), listed=len(full), omitted=0)
    reserve = _size([_omitted_note(len(full))] + notes) + 1
    kept: list[str] = []
    for (head, title, tail), line in zip(lines, full):
        if _size(kept + [line]) + reserve <= max_chars:
            kept.append(line)
            continue
        room = max_chars - reserve - len(head) - len(tail) - 1
        if not kept and room > 0:
            kept.append(head + title[:room] + "…" + tail)
        break
    omitted = len(full) - len(kept)
    body = kept + ([_omitted_note(omitted)] if omitted else []) + notes
    return RenderedOutline(text="\n".join(body)[:max_chars], listed=len(kept), omitted=omitted)


def _size_note(occ: Occurrence) -> str:
    """``（N 段）``, or ``（N 段，可能未完）`` for a range the scan cut."""
    return (f"（{len(occ.range_chunk_ids)} 段{OPEN_RANGE_NOTE}）" if occ.range_open
            else f"（{len(occ.range_chunk_ids)} 段）")


def render_outline(
    entries: Sequence[tuple[str, Occurrence]],
    *,
    max_chars: int,
    truncated_scan: bool,
    scan_rows: int,
) -> RenderedOutline:
    """Render a tree listing (``browse_sections`` without ``query``); see
    the module docstring."""
    shallowest = min((occ.depth for _sid, occ in entries), default=0)
    lines = [
        (
            f"{'  ' * (occ.depth - shallowest)}[{sid}] ",
            occ.title,
            _size_note(occ),
        )
        for sid, occ in entries
    ]
    return _fit(lines, max_chars=max_chars, truncated_scan=truncated_scan, scan_rows=scan_rows)


def render_ranked(
    entries: Sequence[tuple[str, Occurrence]],
    *,
    max_chars: int,
    truncated_scan: bool,
    scan_rows: int,
) -> RenderedOutline:
    """Render a ranked listing (``browse_sections`` with ``query``): one
    unindented line per entry in the given order, full label."""
    lines = [
        (f"[{sid}] ", occ.display_label, _size_note(occ))
        for sid, occ in entries
    ]
    return _fit(lines, max_chars=max_chars, truncated_scan=truncated_scan, scan_rows=scan_rows)


def render_hints(hints: Sequence[tuple[str, SectionHint]]) -> str:
    """The hint block, or ``""`` when there are no hints."""
    if not hints:
        return ""
    lines = [HINT_BLOCK_TITLE]
    for sid, hint in hints:
        occ = hint.occurrence
        lines.append(
            f"[{sid}]《{hint.source_title}》 {occ.display_label}{_size_note(occ)}"
        )
    return "\n".join(lines)
