"""Navigation-page detection: ordered heading self-reference (pure, zero I/O).

A table of contents and a list of figures / tables share properties that do
not depend on what they are called: their text is mostly made of the titles
of OTHER parts of the same document, those titles appear in document order,
they cover a whole stretch of the document rather than a sample of it, and the
page itself sits outside what it lists. This module measures exactly that,
from the source's own structure only -- no label word lists (``Contents`` /
``目录`` ...), no TOC-line patterns, no language rules (user rulings
2026-10-21 / 2026-10-22: no targeted rules, generic structural conditions).

Inputs are what chunk construction already has in hand: the source's elements
(``source_elements_for_chunking``; only the heading elements are used, including
headings whose section has no body such as ``5`` / ``5.7``) and the chunk list
as ``(section_path, text)`` pairs. The result flags each chunk.

Method, per chunk:

* **Normalisation** (titles and chunk text alike): NFKC, casefold, markup tags
  dropped, then only letters, digits and CJK characters kept -- one character
  stream. A MinerU table cell split (``Voltage T | ransfer Function``), dot
  leaders and page-number separators therefore do not affect matching, and
  coverage is measured on this stream.
* **Title text**: the heading text without its leading enumerator (a run of
  digits, ``.``, ``-`` and brackets, preceded by at most one word: ``5.7.2.1``,
  ``Figure 1-1.``, ``表 3-2``, ``A.2``). When the stripped title is shorter than
  ``NAV_MIN_TITLE_CHARS`` the full title is used; when that is still shorter
  the heading does not take part (``Notes``, ``Overview``, ``概述``).
* **Hits**: titles found in the chunk's stream (the chunk's ``[label] `` prefix
  removed first), excluding the chunk's own section and its ancestors; an
  occurrence wholly inside a longer occurrence of another title is not counted
  again.
* The **chain**: the longest strictly increasing run of the hits' document
  order, taken in the order the hits appear in the text (a title shared by
  several headings stands for one of them per occurrence; the chain is then
  re-pinned to the latest such heading that keeps it in order, so it spans
  only what the chunk lists).
  ``ordered_refs`` = its length, capped at the number of distinct titles hit;
  ``ratio`` = ``ordered_refs / hits``.
* ``letters_coverage`` = share of the stream's non-digit characters inside a
  hit (``coverage``, the share of the whole stream, is kept for diagnosis).
* ``locality`` = share of the chain inside the chunk's own section subtree or
  its parent's (a parent that is the document's common root does not count).
  The chunk's own section is its heading (found by label) read with the same
  parsing as every heading. Numbering compares segment prefixes -- ``Figure
  5-1`` and ``Chapter 5`` / ``第5章`` belong to chapter 5, ``Figure A-1`` and
  ``Appendix A`` to appendix A -- and takes a parent only from a number of two
  segments or more; breadcrumbs compare crumb prefixes; both readings apply
  (a numbered section can have unnumbered breadcrumb children). 0 when the
  chunk's section has neither.
* ``density`` = headings on the chain / participating headings of the same
  kind from the chain's first to its last heading in document order (numbered
  titles down to the chain's deepest numbering level, captions by their
  leading word, breadcrumb titles down to the chain's deepest level).
* **Decision** (all five): ``ordered_refs >= NAV_MIN_REFERENCES``,
  ``ratio >= NAV_MIN_ORDER_RATIO``, ``letters_coverage >=
  NAV_MIN_LETTER_COVERAGE``, ``locality < NAV_MAX_LOCALITY`` and ``density >=
  NAV_MIN_DENSITY``. Each condition separates one family of look-alikes: order
  (tables of section titles in another order), letters (prose that walks the
  sections), locality (a section's own overview or summary of its subtree),
  density (cross-chapter tables citing a few sections per chapter). Known
  residual: a summary that lists WHOLE subtrees of other chapters (an appendix
  register summary) meets all five and is flagged.

Performance: a title is only substring-checked when every one of its tokens
(character trigrams of the normalised title -- word boundaries are not reliable
here: MinerU splits words and CJK has none) occurs in the chunk; titles are
indexed under their rarest trigram, so each chunk costs one pass over its own
stream plus the candidates it actually shares grams with. A source with more
than ``NAV_MAX_TITLES`` titles is not judged at all (every chunk stays
unflagged) and the caller records ``navigation_detection_skipped``.
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

#: Minimum weighted length of a normalised title for it to take part. A CJK
#: character weighs ``NAV_CJK_CHAR_WEIGHT`` (one ideograph carries about as much
#: as a short Latin syllable), everything else 1. 10 keeps generic one-word
#: headings out (``overview`` = 8, ``summary`` = 7, ``概述`` = 6) -- they occur in
#: ordinary prose and would let body text look like a list of titles -- while a
#: two-ideograph numbered CJK title still qualifies through its full form
#: (``5.7.2.1损耗`` -> ``5721损耗`` = 4 + 6 = 10).
NAV_MIN_TITLE_CHARS = 10
#: Weight of one CJK (East Asian wide) character in ``NAV_MIN_TITLE_CHARS``.
NAV_CJK_CHAR_WEIGHT = 3
# The five structural conditions. User ruling 2026-10-22: generic structure
# only; every value below is calibrated on the synthetic documents of
# tests/test_navigation_chunks.py (measured values quoted for the chunks that
# reach the reference floor).
#
#: (1) Minimum length of the in-order run of referenced titles (the chain).
#: A table of contents page or a list of figures lists far more than six
#: consecutive titles (a 600-character chunk holds 10-15 entries: measured
#: 6-19); body text that cross-references a few sections (3), a four-entry
#: TOC (4) and the short tail chunk of a TOC stay below it.
NAV_MIN_REFERENCES = 6
#: (2) Minimum ``ordered_refs / hits``: how much of what the chunk references
#: lies on that one run. Navigation pages measured 1.00, a 20-entry TOC with
#: two entries out of place 0.86-0.92; a table whose rows are section titles in
#: random order measured 0.43-0.54 at 20 and 40 rows (20 seeds each, never
#: above 0.75).
NAV_MIN_ORDER_RATIO = 0.75
#: (3) Minimum share of the chunk's NON-DIGIT characters inside hits (digits
#: are page numbers and enumerators). Navigation pages measured 0.82-1.00, and
#: the shortest-caption list of tables 0.67 (the uncovered word ``Table``
#: weighs a third of each entry); a CJK TOC of short titles with three-digit
#: pages measured 1.00 here although only 0.46 of its stream by length. Prose
#: that names every section in order, one sentence each, measured 0.42-0.43;
#: revision-history lines with ``Updated Section N.N`` around the title 0.47.
NAV_MIN_LETTER_COVERAGE = 0.65
#: (4) Locality must stay BELOW this: the share of the chain inside the
#: chunk's own section subtree or its parent's. Navigation pages sit outside
#: what they list (0.00); a chapter's opening overview, a register map of its
#: own subsections, an in-order parameter summary of its own chapter and a
#: breadcrumb summary of its own part all measured 1.00.
NAV_MAX_LOCALITY = 0.5
#: (5) Minimum density: chain headings / same-kind participating headings
#: between the chain's first and last heading. Navigation pages measured 1.00
#: (a TOC that lists three of four levels too: the denominator stops at the
#: chain's deepest level), the near-ordered TOC 0.75-0.80; cross-chapter
#: tables that cite one section per chapter (revision histories in five
#: spellings, a front-matter summary) measured 0.19, shuffled tables
#: 0.15-0.55.
NAV_MIN_DENSITY = 0.6
#: Sources with more titles than this are not judged (cost bound on index
#: size; the largest production specification has about 700). The caller emits
#: ``navigation_detection_skipped``; every chunk of the source stays unflagged.
NAV_MAX_TITLES = 5000
#: Reason code carried by ``NavigationDetection.skipped`` and the event.
SKIPPED_TOO_MANY_TITLES = "too_many_titles"

# Character n-gram size of the prefilter tokens. Every participating title is
# at least four characters long (weighted length >= 10 with CJK weight 3), so
# each has at least two trigrams.
_GRAM = 3
_MARKUP_TAG = re.compile(r"<[^<>\n]{0,256}>")
# Leading enumerator: at most one word (letters only, e.g. ``Figure`` / ``表`` /
# ``A``), optionally followed by ``.``, then a digit run with ``.``/``-``
# separators, optional brackets around it and one trailing punctuation mark.
_ENUMERATOR = re.compile(
    r"^\s*(?:[^\W\d_]+\.?\s*)?"
    r"[\(\[（【]?\d+(?:[.\-–—]\d+)*[\)\]）】]?"
    r"[.:：、]?\s*"
)
# Dotted section number at the start of a title, for numbering ancestry
# (``5.7.2.1`` -> ``5.7.2`` / ``5.7`` / ``5``; ``A.2`` -> ``A``).
_SECTION_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)*|[A-Z](?:\.\d+)*)(?![.\dA-Za-z])")
# A word-enumerated title (``Figure 5-1.``, ``表 3-2``, ``Chapter 5``,
# ``第5章``, ``Appendix A``, ``Figure A-1``): the word is its kind for density,
# the enumerator (a digit run, or one capital letter as in the appendix
# numbering of ``_SECTION_NUMBER``) its chapter for locality. The enumerator
# must end at ``-`` / ``.`` / punctuation / whitespace / a non-ASCII character
# / the end, and a Latin word must not touch it: ``DDR4 ...``, ``I2C ...`` and
# ``UART1 ...`` are names, not enumerated captions (``_caption`` checks the
# separator).
_CAPTION_WORD = re.compile(
    r"^\s*([^\W\d_]+?)(\.?\s*)[\(\[（【]?(\d+|[A-Z])"
    r"(?=[-–—.:：、\s\)\]）】]|$|[^\x00-\x7f])"
)


@dataclass(frozen=True)
class ChunkNavigationMetrics:
    """Per-chunk measurements. ``coverage`` (whole stream) is reported for
    diagnosis only; the decision reads ``letters_coverage``. ``density`` is
    measured only for a chunk that reaches ``NAV_MIN_REFERENCES`` (0
    otherwise)."""

    ordered_refs: int
    coverage: float
    hits: int
    letters_coverage: float = 0.0
    locality: float = 0.0
    density: float = 0.0

    @property
    def ratio(self) -> float:
        return self.ordered_refs / self.hits if self.hits else 0.0


@dataclass(frozen=True)
class NavigationDetection:
    """``flags[i]`` is True when chunk ``i`` is a navigation page.

    ``skipped`` is ``""`` when the source was judged, else a reason code
    (``SKIPPED_TOO_MANY_TITLES``); a skipped source has every flag False and no
    metrics."""

    flags: tuple[bool, ...]
    title_count: int
    skipped: str = ""
    metrics: tuple[ChunkNavigationMetrics, ...] = ()


@dataclass(frozen=True)
class _Heading:
    order: int
    label: str
    key: str
    number: str
    crumbs: tuple[str, ...] = ()
    # The section the heading belongs to for locality: its own numbering, or
    # for a word-enumerated caption (``Figure 5-1.``) the chapter digit.
    chapter: str = ""
    # (kind, depth) for density: ("num", segments), ("cap:<word>", 0),
    # ("bc", crumbs) or ("plain", 0).
    kind: tuple[str, int] = ("plain", 0)

    @property
    def number_parts(self) -> tuple[str, ...]:
        return tuple(self.number.split(".")) if self.number else ()

    @property
    def position(self) -> list[str]:
        """Numbering segments for locality: the dotted number, else the
        caption's chapter, else nothing."""
        return list(self.number_parts) or ([self.chapter] if self.chapter else [])


def normalise(text: str) -> str:
    """NFKC + casefold, markup tags dropped, then letters/digits/CJK only."""
    folded = unicodedata.normalize("NFKC", _MARKUP_TAG.sub(" ", text or "")).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _weighted_length(normalised: str) -> int:
    return sum(
        NAV_CJK_CHAR_WEIGHT if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in normalised
    )


def title_key(title: str) -> str:
    """The normalised match string of one heading, or ``""`` when the heading
    is too short to take part."""
    stripped = _ENUMERATOR.sub("", title or "", count=1)
    key = normalise(stripped)
    if _weighted_length(key) >= NAV_MIN_TITLE_CHARS:
        return key
    key = normalise(title)
    return key if _weighted_length(key) >= NAV_MIN_TITLE_CHARS else ""


def _crumbs(section_path: str) -> list[str]:
    return [seg.strip() for seg in (section_path or "").split(" > ") if seg.strip()]


def _is_heading(element: Mapping) -> bool:
    return (element.get("element_type") or element.get("type") or "").lower() == "heading"


def _caption(text: str) -> "tuple[str, str] | None":
    """``(word, chapter)`` of a word-enumerated title, else None."""
    match = _CAPTION_WORD.match(text)
    if match is None:
        return None
    word, separator, chapter = match.groups()
    if not separator and word[-1].isascii():
        return None  # DDR4 / UART1: a Latin word glued to its digits
    return word, chapter


def _numbering(text: str) -> tuple[str, "tuple[str, str] | None"]:
    """The two readings of a title's position: its dotted section number
    (``5.7.2.1`` / ``A.2``) and, when it has none, its caption enumeration."""
    match = _SECTION_NUMBER.match(text)
    number = match.group(1) if match else ""
    return number, (None if number else _caption(text))


@dataclass(frozen=True)
class HeadingNumbering:
    """How a heading title states its position -- the one numbering parser
    shared by this detector and the section outline
    (``app.services.section_outline``).

    ``number``: the dotted section number at the start of the title
    (``5.7.2.1``, ``A.2``, ``5``; a lone capital letter such as ``A`` is
    reported as the parser reads it), ``""`` when there is none. When there is
    none and the title is word-enumerated (``Figure 5-1.``, ``Table 5-18``,
    ``Chapter 5``, ``第5章``, ``Appendix A``, ``Figure A-1``):
    ``caption_word`` is the word (``Figure`` / ``第``) and ``caption_chapter``
    the enumerator's first part (``5`` / ``A``); ``caption_item`` is True when
    the enumerator continues past it (``5-1``, ``A-1``, ``5.1``), i.e. the title
    names an item inside that chapter rather than the chapter itself. A Latin
    word glued to its digits (``DDR4``, ``UART1``) is a name, not a caption."""

    number: str = ""
    caption_word: str = ""
    caption_chapter: str = ""
    caption_item: bool = False


# The enumerator continues past the chapter part: ``5-1`` / ``A-1`` / ``5.1``
# (``Chapter 5. Electrical`` does not -- a space follows the dot).
_ENUMERATOR_CONTINUES = re.compile(r"[-–—.][0-9A-Za-z]")


def heading_numbering(text: str) -> HeadingNumbering:
    """Public reading of ``_numbering`` (same parse, no behaviour change)."""
    number, caption = _numbering(text or "")
    if number:
        return HeadingNumbering(number=number)
    if caption is None:
        return HeadingNumbering()
    match = _CAPTION_WORD.match(text)
    rest = text[match.end():] if match is not None else ""
    return HeadingNumbering(
        caption_word=caption[0],
        caption_chapter=caption[1],
        caption_item=bool(_ENUMERATOR_CONTINUES.match(rest)),
    )


def _headings(elements: Iterable[Mapping]) -> list[_Heading]:
    """Heading elements in document order, labelled the way ``build_chunks``
    labels the chunks that follow them (full breadcrumb, else the text)."""
    output: list[_Heading] = []
    for element in elements:
        if not _is_heading(element):
            continue
        text = (element.get("text") or "").strip()
        if not text:
            continue
        crumbs = _crumbs(str(element.get("section_path") or ""))
        label = " > ".join(crumbs) if crumbs else text
        number, caption = _numbering(text)
        label_crumbs = tuple(_crumbs(label))
        if number:
            kind = ("num", len(number.split(".")))
        elif caption:
            kind = ("cap:" + caption[0].casefold(), 0)
        elif len(label_crumbs) >= 2:
            kind = ("bc", len(label_crumbs))
        else:
            kind = ("plain", 0)
        output.append(
            _Heading(
                order=len(output),
                label=label,
                key=title_key(text),
                number=number,
                crumbs=label_crumbs,
                chapter=caption[1] if caption else "",
                kind=kind,
            )
        )
    return output


def _number_ancestors(number: str) -> set[str]:
    parts = number.split(".")
    return {".".join(parts[:end]) for end in range(1, len(parts))}


def _strip_label_prefix(section_path: str, text: str) -> str:
    """Drop the ``[label] `` prefix ``build_chunks`` puts on chunk text (the
    last two breadcrumb segments), so a chunk is not credited with its own
    section's title."""
    crumbs = _crumbs(section_path)
    candidates = []
    if crumbs:
        candidates.append(" > ".join(crumbs[-2:]))
    if section_path:
        candidates.append(section_path)
    for prefix in candidates:
        head = f"[{prefix}] "
        if text.startswith(head):
            return text[len(head):]
    return text


class _Ancestry:
    """Keys of a chunk's own section and its ancestors: breadcrumb prefixes of
    its label and, for numbered titles, numbering prefixes (``5.7.2.1`` ->
    ``5.7.2`` / ``5.7`` / ``5``)."""

    def __init__(self, headings: Sequence[_Heading]) -> None:
        self.by_label: dict[str, list[_Heading]] = {}
        self.keys_by_number: dict[str, set[str]] = {}
        for heading in headings:
            self.by_label.setdefault(heading.label, []).append(heading)
            if heading.number and heading.key:
                self.keys_by_number.setdefault(heading.number, set()).add(heading.key)
        self._memo: dict[str, frozenset[str]] = {}

    def excluded(self, section_path: str) -> frozenset[str]:
        memo = self._memo.get(section_path)
        if memo is not None:
            return memo
        crumbs = _crumbs(section_path)
        path = " > ".join(crumbs) or (section_path or "").strip()
        labels = {" > ".join(crumbs[:end]) for end in range(1, len(crumbs) + 1)}
        labels.add(path)
        excluded: set[str] = set()
        for label in labels:
            for heading in self.by_label.get(label, ()):
                if heading.key:
                    excluded.add(heading.key)
                if label == path and heading.number:
                    for number in _number_ancestors(heading.number):
                        excluded |= self.keys_by_number.get(number, set())
        result = frozenset(excluded)
        self._memo[section_path] = result
        return result


def _grams(text: str) -> set[str]:
    return {text[i : i + _GRAM] for i in range(len(text) - _GRAM + 1)}


class _TitleIndex:
    """Trigram-token prefilter over the participating titles: each title is
    indexed under its rarest trigram (a necessary condition for a substring
    match), and its full trigram set must be present before the substring
    check runs."""

    def __init__(self, headings: Sequence[_Heading]) -> None:
        orders: dict[str, list[int]] = {}
        for heading in headings:
            if heading.key:
                orders.setdefault(heading.key, []).append(heading.order)
        self.orders = {key: tuple(values) for key, values in orders.items()}
        # order -> key, built once: the chain re-pin walks it per node.
        self.key_of = {order: key for key, values in self.orders.items() for order in values}
        self.grams = {key: _grams(key) for key in self.orders}
        frequency: dict[str, int] = {}
        for grams in self.grams.values():
            for gram in grams:
                frequency[gram] = frequency.get(gram, 0) + 1
        self.by_gram: dict[str, list[str]] = {}
        for key, grams in self.grams.items():
            anchor = min(grams, key=lambda gram: (frequency[gram], gram))
            self.by_gram.setdefault(anchor, []).append(key)

    def occurrences(self, stream: str, excluded: frozenset[str]) -> list[tuple[int, int, str]]:
        present = _grams(stream)
        found: list[tuple[int, int, str]] = []
        for gram in present:
            for key in self.by_gram.get(gram, ()):
                if key in excluded or not self.grams[key] <= present:
                    continue
                start = stream.find(key)
                while start >= 0:
                    found.append((start, start + len(key), key))
                    start = stream.find(key, start + 1)
        return found


def _drop_contained(occurrences: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """An occurrence lying wholly inside an earlier-starting (or same-start,
    longer) occurrence of another title is the shorter title repeated inside a
    longer one: drop it."""
    kept: list[tuple[int, int, str]] = []
    max_end = -1
    for start, end, key in sorted(occurrences, key=lambda item: (item[0], -item[1])):
        if end <= max_end:
            continue
        kept.append((start, end, key))
        max_end = end
    return kept


def _covered(occurrences: Sequence[tuple[int, int, str]]) -> int:
    total = 0
    current_start = current_end = -1
    for start, end, _key in occurrences:  # sorted by start
        if start > current_end:
            total += max(0, current_end - current_start)
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    total += max(0, current_end - current_start)
    return total


def _ordered_chain(
    occurrences: Sequence[tuple[int, int, str]],
    orders: Mapping[str, tuple[int, ...]],
    key_of: Mapping[int, str],
) -> list[int]:
    """The longest strictly increasing run of document order over the hits in
    text order, as the heading orders on it (patience sorting with predecessor
    pointers). A title shared by several headings may stand for any of them;
    its candidate orders are fed in descending order so at most one of them
    extends a given run.

    Patience sorting keeps the SMALLEST order that ends each run, so a shared
    title on the chain is pinned to its earliest heading in the whole document
    (a TOC chunk of chapters 7-8 that starts with ``Electrical
    Characteristics`` would start at chapter 1's). The chain is therefore
    re-pinned from its tail to its head: each node takes the LATEST heading
    of the same title that still lies after its predecessor and before its
    successor -- the same run, as tight as it can be, so density measures the
    stretch the chunk really lists."""
    tails: list[int] = []
    tail_nodes: list[tuple[int, object]] = []
    for _start, _end, key in occurrences:
        for order in sorted(orders[key], reverse=True):
            position = bisect.bisect_left(tails, order)
            node = (order, tail_nodes[position - 1] if position else None)
            if position == len(tails):
                tails.append(order)
                tail_nodes.append(node)
            else:
                tails[position] = order
                tail_nodes[position] = node
    chain: list[int] = []
    node: object = tail_nodes[-1] if tail_nodes else None
    while node is not None:
        order, node = node  # type: ignore[misc]
        chain.append(order)
    chain.reverse()
    for index in range(len(chain) - 2, -1, -1):
        low = chain[index - 1] if index else -1
        high = chain[index + 1]
        chain[index] = max(
            order for order in orders[key_of[chain[index]]] if low < order < high
        )
    return chain


def _letters_coverage(stream: str, occurrences: Sequence[tuple[int, int, str]]) -> float:
    """Share of the stream's non-digit characters inside a hit (0 when the
    stream is all digits). Digits are page numbers and enumerators: a TOC of
    short titles with three-digit pages is mostly digits by length but not by
    what it says."""
    letters = [0]
    for ch in stream:
        letters.append(letters[-1] + (0 if ch.isdigit() else 1))
    if not letters[-1]:
        return 0.0
    covered = 0
    current_start = current_end = -1
    for start, end, _key in occurrences:  # sorted by start
        if start > current_end:
            if current_end > current_start:
                covered += letters[current_end] - letters[current_start]
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end > current_start:
        covered += letters[current_end] - letters[current_start]
    return covered / letters[-1]


class _Structure:
    """What locality and density read about the headings: the breadcrumb depth
    every multi-segment label shares (the document's common root), each
    heading's kind/depth, and the participating headings in document order."""

    def __init__(self, headings: Sequence[_Heading], index: "_TitleIndex") -> None:
        self.headings = headings
        # Every heading that carries a chain node's title: locality asks
        # whether ANY of them is local (see ``locality``).
        self.orders = index.orders
        self.key_of = index.key_of
        multi = [h.crumbs for h in headings if len(h.crumbs) >= 2]
        depth = 0
        while (
            multi
            and all(len(c) > depth + 1 for c in multi)
            and len({c[depth] for c in multi}) == 1
        ):
            depth += 1
        self.root_depth = depth
        self.keyed = [h.order for h in headings if h.key]
        # A chunk's section_path is its heading's label (``build_chunks``):
        # the heading parsed once gives the chunk's own numbering.
        self.by_label: dict[str, _Heading] = {}
        for heading in headings:
            self.by_label.setdefault(heading.label, heading)

    def locality(self, section_path: str, chain: Sequence[int]) -> float:
        """Share of the chain inside the chunk's own section subtree or its
        parent's (a parent that is the document's common root does not
        count). Numbering compares segment prefixes (``Figure 5-1`` reads as
        chapter 5) and takes a parent only from a number of two segments or
        more; breadcrumbs compare crumb prefixes. 0 for a chunk whose section
        has neither.

        A chain node counts as local when ANY heading carrying its title is
        local, not only the one the chain was pinned to. Titles repeat across
        chapters (chapters 1 and 5 with the same seven subsection names): the
        text cannot say which chapter's copy a chunk means, and the chain's
        pin follows document order, not the chunk's position -- so chapter
        5's own overview would read as a list of chapter 1's sections. A
        table of contents is unaffected: its own section (``Contents``, the
        document root) has no subtree to be local to."""
        if not chain:
            return 0.0
        crumbs = _crumbs(section_path)
        own_heading = self.by_label.get(" > ".join(crumbs) or (section_path or "").strip())
        if own_heading is not None:
            own = own_heading.position
        elif crumbs:
            # A section path no heading carries (a plugin proposal): the same
            # parsing on its last segment.
            number, caption = _numbering(crumbs[-1])
            own = number.split(".") if number else ([caption[1]] if caption else [])
        else:
            own = []
        number_prefixes: list[list[str]] = []
        crumb_prefixes: list[list[str]] = []
        if own:
            number_prefixes.append(own)
            if len(own) >= 2:
                number_prefixes.append(own[:-1])
        # Both readings: a numbered section can have unnumbered breadcrumb
        # children (``9 Registers > 9.5 Register Map > UCIe Link Control``).
        if len(crumbs) >= 2:
            crumb_prefixes.append(crumbs)
            if len(crumbs) - 1 > self.root_depth:
                crumb_prefixes.append(crumbs[:-1])
        if not number_prefixes and not crumb_prefixes:
            return 0.0
        def inside(heading: _Heading) -> bool:
            parts = heading.position
            if parts and any(parts[: len(p)] == p for p in number_prefixes):
                return True
            return any(
                len(heading.crumbs) > len(p) and list(heading.crumbs[: len(p)]) == p
                for p in crumb_prefixes
            )

        local = 0
        for order in chain:
            if any(inside(self.headings[same]) for same in self.orders[self.key_of[order]]):
                local += 1
        return local / len(chain)

    def density(self, chain: Sequence[int]) -> float:
        """Headings on the chain / participating headings of the same kinds
        from the chain's first to its last heading in document order. Same
        kind: numbered titles down to the chain's deepest numbering level,
        captions by their leading word (leaves), breadcrumb titles down to the
        chain's deepest crumb level. The numerator counts heading POSITIONS on
        the chain (each is a different heading, the chain being strictly
        increasing), not distinct titles: that is the count the thresholds
        were calibrated on, and a TOC that lists a title shared by several
        chapters lists each of those headings."""
        if not chain:
            return 0.0
        deepest: dict[str, int] = {}
        for order in chain:
            kind, depth = self.headings[order].kind
            deepest[kind] = max(deepest.get(kind, 0), depth)
        low = bisect.bisect_left(self.keyed, chain[0])
        high = bisect.bisect_right(self.keyed, chain[-1])
        span = 0
        for order in self.keyed[low:high]:
            kind, depth = self.headings[order].kind
            if kind in deepest and depth <= deepest[kind]:
                span += 1
        return len(chain) / span if span else 0.0


def is_navigation(metrics: ChunkNavigationMetrics) -> bool:
    """The decision: all five structural conditions."""
    return (
        metrics.ordered_refs >= NAV_MIN_REFERENCES
        and metrics.ratio >= NAV_MIN_ORDER_RATIO
        and metrics.letters_coverage >= NAV_MIN_LETTER_COVERAGE
        and metrics.locality < NAV_MAX_LOCALITY
        and metrics.density >= NAV_MIN_DENSITY
    )


def detect_navigation_chunks(
    elements: Sequence[Mapping],
    chunks: Sequence[tuple[str, str]],
) -> NavigationDetection:
    """Flag the navigation pages among ``chunks`` (``(section_path, text)``,
    any order) of one source whose elements are ``elements`` (document
    order)."""
    headings = _headings(elements)
    if len(headings) > NAV_MAX_TITLES:
        return NavigationDetection(
            flags=tuple(False for _ in chunks),
            title_count=len(headings),
            skipped=SKIPPED_TOO_MANY_TITLES,
        )
    index = _TitleIndex(headings)
    ancestry = _Ancestry(headings)
    structure = _Structure(headings, index)
    flags: list[bool] = []
    metrics: list[ChunkNavigationMetrics] = []
    for section_path, text in chunks:
        stream = normalise(_strip_label_prefix(section_path or "", text or ""))
        if not stream or not index.orders:
            flags.append(False)
            metrics.append(ChunkNavigationMetrics(0, 0.0, 0))
            continue
        excluded = ancestry.excluded(section_path or "")
        kept = _drop_contained(index.occurrences(stream, excluded))
        distinct = len({key for _start, _end, key in kept})
        chain = _ordered_chain(kept, index.orders, index.key_of)
        refs = min(len(chain), distinct)
        measured = ChunkNavigationMetrics(
            ordered_refs=refs,
            coverage=round(_covered(kept) / len(stream), 4),
            hits=distinct,
            letters_coverage=round(_letters_coverage(stream, kept), 4),
            locality=round(structure.locality(section_path or "", chain), 4),
            density=(
                round(structure.density(chain), 4) if refs >= NAV_MIN_REFERENCES else 0.0
            ),
        )
        metrics.append(measured)
        flags.append(is_navigation(measured))
    return NavigationDetection(
        flags=tuple(flags), title_count=len(headings), metrics=tuple(metrics)
    )
