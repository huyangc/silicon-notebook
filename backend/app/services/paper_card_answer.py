"""Answer-side glue for the paper tools (spec 2026-10-28 §7.2 / §7.3, plan T6).

``reasoning_retrieval`` drives ``find_sources`` / ``get_paper_cards`` /
``extract_from_sources`` and hands their outcomes to synthesis on the run
result (``PaperToolsSnapshot`` on the draft input); it renders no answer text.
This module is the seam between those outcomes and the answer, the way
``document_read_answer`` is for ``read_document``:

* **registration** (``build_paper_card_evidence``) -- every element a verified
  card field or an extracted value cites is read back once (through the
  caller's ``read_citations``: ``EvidenceContextService.collection_item_citations``,
  one bounded hydration grouped by the owning library, with the run's
  participant / ceiling checks and the read attestation) and given a key in
  the ``PAPER_CARD_KEY_BASE`` band (k9001..k9999).  The keys enter ``id_map``
  and the citation cards OUTSIDE the structured prompt budget, like external
  evidence: the table cites far more elements than the prompt could carry;
* **digest** (``paper_digest_block``) -- what the writing model reads: per
  paper a compact block (title, year / venue, tl;dr, two contributions, three
  metrics, extracted values, each line under its own ``kN``; about
  ``DIGEST_PAPER_CHARS``), inside the structured-side budget, whole papers only;
* **table** (``render_paper_table`` / ``coverage_line``) -- when the run's
  ``answer_layout`` is ``paper_table`` the answer is composed by code: the
  coverage line, the model's overview and comparison, the deterministic table
  (only keys registered above carry a marker) and the list of related papers
  that were not expanded.

Everything here is pure except the one injected reader.  A failure anywhere in
``build_paper_card_evidence`` is the caller's to swallow: then no key, no
digest and no table exist -- never a table whose markers bind to nothing.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from app.services.citation_markers import neutralize_markers
from app.services.context_spans import Glue, concat, entry_lines, joined
from app.services.reasoning_paper_tools import (
    ANSWER_LAYOUT_PAPER_TABLE,
    ANSWER_LAYOUT_PROSE,
    card_has_content,
    find_coverage_gaps,
)

# The paper-card evidence band (spec §7.2): k9001..k9999.  Mirrored (not
# imported) by ``AskService._PAPER_CARD_KEY_BASE``, ``ask_evidence`` and
# ``reasoning_retrieval.PAPER_CARD_KEY_BASE`` (whose ``read_document`` gate keeps
# its own band below this one); ``test_paper_card_answer`` pins all four equal.
PAPER_CARD_KEY_BASE = 9000
# 40 cards x 24 anchors = 960 < 999: the band holds the exhaustive tier whole.
PAPER_CARD_MAX_KEYS = 999
# Per-paper digest budget (spec §7.2, "about 600 characters").
DIGEST_PAPER_CHARS = 600
DIGEST_CONTRIBUTIONS = 2
DIGEST_METRICS = 3
# Table shape (spec §7.3).
TABLE_CELL_CHARS = 240
TABLE_METRICS = 3
TABLE_CONTRIBUTIONS = 2
# Extracted fields the table shows (in its one ``EXTRA_FIELDS_HEADER`` column);
# one extraction call takes at most four fields (``EXTRACT_MAX_FIELDS``) and
# the table keeps that many across calls -- the rest are named in the coverage
# line.
TABLE_EXTRA_FIELDS = 4
EXTRA_FIELDS_HEADER = "补充字段"
UNEXPANDED_TITLES = 100
# ``REASONING_MAX_DOCUMENT_READS x DOCUMENT_OVERVIEW_MAX_ELEMENTS`` above this
# pushes ``read_document``'s band (k7001+) into this one, which closes the
# action (``ReasoningRetriever.document_read_active``); startup warns.
DOCUMENT_READ_BAND_WIDTH = PAPER_CARD_KEY_BASE - 7000

PAPER_DIGEST_GUIDANCE = (
    "[Paper key-point digests, generated from the cached paper cards and the "
    "values extracted this turn. Every line carries the [kN] of the original "
    "passage it was verified against; cite those markers like any other "
    "evidence. A digest is a summary, not the paper: what it does not state "
    "is not established.]"
)

_EMPTY_CELL = "—"
_NEWLINES = re.compile(r"[\r\n]+")


@dataclass(frozen=True)
class PaperRow:
    """One paper that has a card with content or at least one extracted value."""

    notebook_id: str
    source_id: str
    title: str
    year: str
    venue: str
    card: Mapping[str, Any]
    # ``(field, FieldValue)`` with a value, in the run's field order.
    values: tuple = ()


@dataclass(frozen=True)
class PaperCardEvidence:
    """What one run's paper tools contribute to answer assembly.

    ``id_map`` / ``citations`` are the registration (every key, prompt or
    not); ``keys`` maps an element id to its key.  ``coverage`` and ``table``
    are pre-rendered and empty unless the layout is ``paper_table``.
    """

    layout: str
    rows: tuple
    id_map: Mapping[str, Mapping[str, Any]]
    keys: Mapping[str, str]
    citations: Mapping[str, Any]
    coverage: str = ""
    table: str = ""
    extra_fields: tuple = ()
    # Extracted fields past ``TABLE_EXTRA_FIELDS``: named in the coverage line.
    overflow_fields: tuple = ()
    unexpanded: tuple = ()
    table_rows: int = 0
    # The run called ``find_sources``: the coverage line states the topic's
    # coverage from it (``roster_omitted_for``).
    searched: bool = False

    @property
    def paper_table(self) -> bool:
        return self.layout == ANSWER_LAYOUT_PAPER_TABLE and bool(self.table)


#: The enumeration collection a paper-table answer leaves out of synthesis.
ROSTER_COLLECTION = "sources"


def roster_omitted_for(paper: Optional[PaperCardEvidence]) -> frozenset:
    """Collections the enumeration preview leaves out for this answer.

    A paper-table answer whose papers came from ``find_sources`` leads with a
    coverage line computed from it; a document-roster preview cut by the
    shared budget (``previewed 32`` of 40) would have the model write its own,
    contradicting coverage claim, and the roster is not the topic's candidate
    set anyway.  Without ``find_sources`` the roster may be the only list the
    run holds, so it stays."""
    if paper is not None and paper.paper_table and paper.searched:
        return frozenset({ROSTER_COLLECTION})
    return frozenset()


def document_read_band_overflow(max_reads: int, max_elements: int) -> bool:
    """True when ``read_document``'s band would cross into this one."""
    return int(max_reads) * int(max_elements) > DOCUMENT_READ_BAND_WIDTH


# ----------------------------------------------------------- rows

def _text(value: Any) -> str:
    return str(value or "").strip()


def _find_index(finds: Sequence[Any]) -> dict:
    """``(notebook_id, source_id) -> FoundSource`` (first sighting)."""
    index: dict = {}
    for outcome in finds:
        for item in getattr(outcome.result, "items", ()) or ():
            index.setdefault((item.notebook_id, item.source_id), item)
    return index


def _all_fields(extractions: Sequence[Any]) -> tuple:
    fields: list[str] = []
    for outcome in extractions:
        for name in outcome.fields:
            if name not in fields:
                fields.append(name)
    return tuple(fields)


def paper_rows(tools: Any) -> tuple[tuple, tuple]:
    """``(rows, extra_fields)``: cards in first-request order, then the papers
    that only have extracted values, in extraction order."""
    found = _find_index(tools.finds)
    fields = _all_fields(tools.extractions)[:TABLE_EXTRA_FIELDS]
    values: dict = {}
    titles: dict = {}
    for outcome in tools.extractions:
        for item in outcome.items:
            result = item.result
            key = (result.notebook_id, result.source_id)
            titles.setdefault(key, item.title)
            for value in result.fields:
                if value.value and value.field in fields:
                    # The run's latest value for a field wins.
                    values.setdefault(key, {})[value.field] = value
    rows: list[PaperRow] = []
    seen: set = set()

    def add(key, title, card) -> None:
        item = found.get(key)
        fields_here = values.get(key) or {}
        rows.append(PaperRow(
            notebook_id=key[0], source_id=key[1],
            title=_text(title) or _text(getattr(item, "title", "")) or key[1],
            year=str(getattr(item, "pub_year", "") or ""),
            venue=_text(getattr(item, "venue", "")),
            card=card,
            values=tuple((name, fields_here[name]) for name in fields if name in fields_here),
        ))
        seen.add(key)

    for outcome in tools.cards:
        result = outcome.result
        key = (result.notebook_id, result.source_id)
        if key in seen:
            continue
        if card_has_content(outcome):
            add(key, outcome.title, result.card)
        elif key in values:
            add(key, outcome.title, {})
    for key in values:
        if key not in seen:
            add(key, titles.get(key, ""), {})
    return tuple(rows), fields


def _anchors(entry: Any, name: str = "anchors") -> list[str]:
    if not isinstance(entry, Mapping):
        return []
    raw = entry.get(name)
    return [str(a) for a in (raw if isinstance(raw, list) else [raw]) if a]


def _visible_anchor_ids(row: PaperRow, *, table: bool) -> list[str]:
    """The anchors this row can show, in display order: the digest's (the
    first anchor of the tl;dr and of each of the first two contributions, the
    first three metrics, the extracted values) and, with ``table``, the
    table's (a contribution cell shows up to two anchors).  Nothing else is
    registered: a key no answer text can carry would only be a card that
    nothing cites."""
    card = row.card
    per_contribution = TABLE_CONTRIBUTIONS if table else 1
    ids = _anchors(card.get("tldr"))[:1]
    for entry in (card.get("contributions") or [])[:DIGEST_CONTRIBUTIONS]:
        ids.extend(_anchors(entry)[:per_contribution])
    for entry in (card.get("metrics") or [])[:max(TABLE_METRICS, DIGEST_METRICS)]:
        ids.extend(_anchors(entry, "anchor"))
    ids.extend(value.element_id for _name, value in row.values if value.element_id)
    return ids


def _requested_anchors(rows: Sequence[PaperRow], *, table: bool) -> list[tuple[str, PaperRow]]:
    """The elements to register: what the table (``table``) and the digest of
    EVERY row can show -- the digest covers rows past the table's inline
    limit -- capped at the band."""
    wanted: dict[str, PaperRow] = {}
    for row in rows:
        for element_id in _visible_anchor_ids(row, table=table):
            wanted.setdefault(element_id, row)
    return list(wanted.items())[:PAPER_CARD_MAX_KEYS]


# ----------------------------------------------------------- registration

def build_paper_card_evidence(
    tools: Any,
    *,
    read_citations: Callable[[list], Mapping[str, Any]],
    tier_of: Callable[[list], Mapping[str, str]],
    table_rows: int,
    for_answer: bool = True,
) -> Optional[PaperCardEvidence]:
    """Register the run's paper evidence; ``None`` when there is nothing to show.

    ``read_citations(items)`` resolves ``{element_id: Citation}`` for items of
    the enumeration-row shape (``object_id`` + ``evidence_element_ids`` +
    ``notebook_id`` + ``tier``); an element is kept only when its citation names
    the very source the card or the value belongs to.  ``tier_of(notebook_ids)``
    is the library tier map.

    ``for_answer=False`` (the retrieval-only output: there is no answer and so
    no table) registers only what the digest can show, and renders no table.
    """
    if tools is None:
        return None
    rows, fields = paper_rows(tools)
    if not rows:
        return None
    layout = str(getattr(tools, "answer_layout", "") or "")
    table = for_answer and layout == ANSWER_LAYOUT_PAPER_TABLE
    requested = _requested_anchors(rows, table=table)
    tiers = dict(tier_of(sorted({row.notebook_id for _eid, row in requested})) or {}) \
        if requested else {}
    items = [
        SimpleNamespace(
            object_id=element_id, evidence_element_ids=(element_id,),
            notebook_id=row.notebook_id, tier=tiers.get(row.notebook_id) or "personal",
        )
        for element_id, row in requested
    ]
    resolved = dict(read_citations(items) or {}) if items else {}
    id_map: dict[str, dict] = {}
    keys: dict[str, str] = {}
    citations: dict[str, Any] = {}
    for element_id, row in requested:
        citation = resolved.get(element_id)
        if citation is None or citation.source_id != row.source_id \
                or citation.element_id != element_id:
            continue
        key = f"k{PAPER_CARD_KEY_BASE + len(keys) + 1}"
        keys[element_id] = key
        citations[element_id] = citation
        id_map[key] = {
            "object_id": element_id, "object_type": "element",
            "name": citation.label or row.title, "definition": None,
            "snippet": citation.quoted_span, "source_title": row.title,
            "source_id": citation.source_id, "element_id": element_id,
            "location_label": citation.location_label, "tier": citation.tier,
            # Copied from the citation, which normalised it once
            # (``foreign_notebook_id``): "" for the active library.
            "notebook_id": citation.notebook_id,
            "relevance": 1.0,
        }
    evidence = PaperCardEvidence(
        layout=layout if table else ANSWER_LAYOUT_PROSE,
        rows=rows, id_map=id_map, keys=keys, citations=citations,
        extra_fields=fields,
        overflow_fields=_all_fields(tools.extractions)[TABLE_EXTRA_FIELDS:],
        table_rows=max(0, int(table_rows)),
        searched=bool(tools.finds),
    )
    if not table:
        return evidence
    unexpanded = _unexpanded_titles(tools, rows, evidence.table_rows)
    evidence = dataclasses.replace(evidence, unexpanded=unexpanded)
    return dataclasses.replace(
        evidence,
        coverage=coverage_line(tools, evidence),
        table=render_paper_table(evidence),
    )


def registered_keys(paper: Optional[PaperCardEvidence]) -> set:
    """Exact-evidence keys for ``classify_evidence`` (the registration)."""
    return set(paper.id_map) if paper is not None else set()


def with_registration(id_map: dict, paper: Optional[PaperCardEvidence]) -> dict:
    """``id_map`` plus every registered key (the prompt's own entries win)."""
    if paper is None or not paper.id_map:
        return id_map
    return {**paper.id_map, **id_map}


# ----------------------------------------------------------- digest

def _clean(text: Any) -> str:
    """One line of model-facing or table text: no newline, no fake marker."""
    return neutralize_markers(_NEWLINES.sub(" ", str(text or ""))).strip()


# Units written flush against the number: symbols, never words.  ``x`` / ``X``
# is the multiplication sign spelled in ASCII (``3.5x``).
_FLUSH_UNITS = frozenset({"x", "X"})


def value_with_unit(value: str, unit: str) -> str:
    """``21.4 AP`` / ``79 GPU days`` / ``26 FPS``, but ``85.2%`` / ``4×`` /
    ``90°``: a space only before a unit that starts with a letter or digit."""
    if not unit:
        return value
    if not value:
        return unit
    if unit[0].isalnum() and unit not in _FLUSH_UNITS:
        return f"{value} {unit}"
    return f"{value}{unit}"


def _name_ends_with_unit(name: str, unit: str) -> bool:
    """Name ``GPU days`` with unit ``GPU days``: the name already says the unit
    (case-insensitive, on a word boundary -- ``mAP`` does not end with the
    unit ``AP``)."""
    if not unit or not name.casefold().endswith(unit.casefold()):
        return False
    before = name[:len(name) - len(unit)]
    return not before or not (before[-1].isalnum() and unit[0].isalnum())


def _compared_text(against: str, unit: str, shown_unit: str) -> str:
    """The ``compared_to`` text with each ``<number><unit>`` written the way
    the paper's own value is (``SchNet 1.8M`` beside ``2.1 M`` reads
    ``SchNet 1.8 M``; the unit is dropped there too when the name carries
    it)."""
    if not unit or not against:
        return against
    pattern = re.compile(
        r"(?<![\w.])(\d+(?:[.,]\d+)*)\s*" + re.escape(unit) + r"(?!\w)")
    return pattern.sub(lambda m: value_with_unit(m.group(1), shown_unit), against)


def _metric_text(metric: Mapping[str, Any], *, compared: bool = False) -> str:
    """``name value unit（setting）`` and, with ``compared``, `` vs compared_to``
    (the card prompt keeps ``metrics`` to the paper's own method and puts the
    baseline it is measured against in ``compared_to``).  A name that already
    ends with the unit does not repeat it (``GPU days 79``)."""
    name = _clean(metric.get("name"))
    value = _clean(metric.get("value"))
    unit = _clean(metric.get("unit"))
    shown_unit = "" if _name_ends_with_unit(name, unit) else unit
    setting = _clean(metric.get("setting"))
    text = f"{name} {value_with_unit(value, shown_unit)}".strip()
    text = f"{text}（{setting}）" if setting else text
    against = _clean(metric.get("compared_to")) if compared else ""
    against = _compared_text(against, unit, shown_unit)
    return f"{text} vs {against}" if against else text


def _year_venue(row: PaperRow) -> str:
    parts = [part for part in (row.year, _clean(row.venue)) if part]
    return " / ".join(parts)


def _digest_lines(row: PaperRow, keys: Mapping[str, str]) -> list[tuple[str, str]]:
    """``(key, text)`` per digest line, display order; unregistered dropped."""
    card = row.card
    out: list[tuple[str, str]] = []

    def put(label: str, text: str, anchors: list[str]) -> None:
        key = next((keys[a] for a in anchors if a in keys), "")
        if key and text:
            out.append((key, f"{label}: {text}"))

    tldr = card.get("tldr")
    if isinstance(tldr, Mapping):
        put("TL;DR", _clean(tldr.get("text")), _anchors(tldr))
    for entry in (card.get("contributions") or [])[:DIGEST_CONTRIBUTIONS]:
        if isinstance(entry, Mapping):
            put("Contribution", _clean(entry.get("text")), _anchors(entry))
    for entry in (card.get("metrics") or [])[:DIGEST_METRICS]:
        if isinstance(entry, Mapping):
            text = _metric_text(entry)
            compared = _clean(entry.get("compared_to"))
            put("Metric", f"{text}; compared to {compared}" if compared else text,
                _anchors(entry, "anchor"))
    for name, value in row.values:
        put(f"Extracted «{_clean(name)}»", _clean(value.value), [value.element_id])
    return out


def _digest_entry(row: PaperRow, keys: Mapping[str, str]) -> tuple[str, list[str]]:
    """One paper's digest (header + keyed lines, at most ``DIGEST_PAPER_CHARS``)
    and the keys it shows; ``("", [])`` when no line can be cited."""
    facts = _year_venue(row)
    header = f"《{_clean(row.title)}》" + (f" · {facts}" if facts else "")
    lines = [header]
    used = len(header)
    shown: list[str] = []
    for key, text in _digest_lines(row, keys):
        line = f"{key}: {text}"
        room = DIGEST_PAPER_CHARS - used - 1
        if len(line) > room:
            if room - len(key) - 3 < 40:
                break
            line = line[:room - 1] + "…"
        lines.append(line)
        used += len(line) + 1
        shown.append(key)
    if not shown:
        return "", []
    return entry_lines(lines, starts=list(range(1, len(lines)))), shown


def paper_digest_block(
    paper: Optional[PaperCardEvidence], budget_chars: int,
) -> tuple[str, dict]:
    """The digest block for ``budget_chars`` and the id_map entries it shows.

    Whole papers only, in row order, stopping at the first that does not fit:
    a paper left out still appears in the table, never half-digested.
    ``("", {})`` when not even one paper fits (the block is all or nothing).
    """
    if paper is None or budget_chars <= 0:
        return "", {}
    blocks: list[str] = []
    shown: list[str] = []
    used = len(PAPER_DIGEST_GUIDANCE)
    for row in paper.rows:
        text, keys = _digest_entry(row, paper.keys)
        if not text:
            continue
        if used + 2 + len(text) > budget_chars:
            break
        blocks.append(text)
        shown.extend(keys)
        used += 2 + len(text)
    if not blocks:
        return "", {}
    body = joined(blocks, "\n\n")
    return (concat(PAPER_DIGEST_GUIDANCE, Glue("\n"), body),
            {key: dict(paper.id_map[key]) for key in dict.fromkeys(shown)})


def paper_digest_reserve(paper: Optional[PaperCardEvidence], chunk_context_chars: int) -> int:
    """Characters the enumeration preview leaves the digest (computed before
    the preview renders, like ``document_read_block_reserve``): the whole
    digest plus its joiner, at most a quarter of the partition so a long paper
    list cannot starve the roster."""
    if paper is None:
        return 0
    text, _map = paper_digest_block(paper, int(chunk_context_chars))
    return min(len(text) + 2, int(chunk_context_chars) // 4) if text else 0


# ----------------------------------------------------------- table

def _cell_escape(text: Any) -> str:
    """Cell text: one line, no marker, and ``|`` mapped to the full-width
    ``｜`` -- a backslash escape would be undone by a backslash in the text
    itself (``\\|W\\|_F``) and split the cell."""
    return _clean(text).replace("|", "｜")


def _markers(anchors: Iterable[str], keys: Mapping[str, str], limit: int = 2) -> str:
    found = [keys[a] for a in dict.fromkeys(anchors) if a in keys][:limit]
    return "".join(f"[{key}]" for key in found)


def _cell(items: Sequence[tuple[str, str]]) -> str:
    """Join ``(text, markers)`` items with ``；`` within ``TABLE_CELL_CHARS``;
    a marker is never cut, only the text before it."""
    out = ""
    for text, markers in items:
        text = _cell_escape(text)
        if not text:
            continue
        piece = f"{text} {markers}" if markers else text
        sep = "；" if out else ""
        room = TABLE_CELL_CHARS - len(out) - len(sep)
        if len(piece) <= room:
            out += sep + piece
            continue
        if not out:
            keep = room - (len(markers) + 1 if markers else 0) - 1
            if keep > 0:
                cut = text[:keep].rstrip("\\")
                out = f"{cut}… {markers}" if markers else f"{cut}…"
        break
    return out or _EMPTY_CELL


def _title_cell(row: PaperRow, keys: Mapping[str, str]) -> str:
    marker = _markers(_anchors(row.card.get("tldr"))[:1], keys, 1)
    title = _cell_escape(row.title)[:TABLE_CELL_CHARS - len(marker) - 2]
    return f"{title} {marker}" if marker else title


def _row_line(row: PaperRow, paper: PaperCardEvidence, *, extra_column: bool) -> str:
    keys = paper.keys
    metrics = [
        (_metric_text(entry, compared=True), _markers(_anchors(entry, "anchor"), keys))
        for entry in (row.card.get("metrics") or [])[:TABLE_METRICS]
        if isinstance(entry, Mapping)
    ]
    contributions = [
        (str(entry.get("text") or ""), _markers(_anchors(entry), keys))
        for entry in (row.card.get("contributions") or [])[:TABLE_CONTRIBUTIONS]
        if isinstance(entry, Mapping)
    ]
    cells = [
        _title_cell(row, keys),
        _cell_escape(_year_venue(row))[:TABLE_CELL_CHARS] or _EMPTY_CELL,
        _cell(metrics), _cell(contributions),
    ]
    if extra_column:
        cells.append(_cell([
            (f"{_clean(name)}: {_clean(value.value)}", _markers([value.element_id], keys))
            for name, value in row.values
        ]))
    return "| " + " | ".join(cells) + " |"


def render_paper_table(paper: PaperCardEvidence) -> str:
    """The table (first ``table_rows`` rows) and the not-expanded list."""
    inline = paper.rows[:paper.table_rows]
    if not inline:
        return ""
    # Extracted values share ONE column, ``field: value [kN]`` per entry: a
    # column per field is mostly empty (a field is usually extracted for the
    # one or two papers whose card lacks it).  No inline row with a value, no
    # column.
    extra_column = any(row.values for row in inline)
    headers = ["论文", "年份 / 会议", "主要技术指标", "关键创新点",
               *((EXTRA_FIELDS_HEADER,) if extra_column else ())]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
        *(_row_line(row, paper, extra_column=extra_column) for row in inline),
    ]
    table = "\n".join(lines)
    if paper.unexpanded:
        titles, total = paper.unexpanded
        tail = f" 等共 {total} 篇" if total > len(titles) else ""
        table += "\n\n其余相关论文（未展开）：" + "、".join(titles) + tail
    return table


def _unexpanded_titles(tools: Any, rows: Sequence[PaperRow], table_rows: int) -> tuple:
    """``(titles, total)`` of the related papers not shown as a row: rows past
    the inline limit, then every found paper with no row; ``()`` when none."""
    inline = {(row.notebook_id, row.source_id) for row in rows[:table_rows]}
    titles: list[str] = []
    seen = set(inline)
    for row in rows[table_rows:]:
        seen.add((row.notebook_id, row.source_id))
        titles.append(_clean(row.title))
    for outcome in tools.finds:
        for item in getattr(outcome.result, "items", ()) or ():
            key = (item.notebook_id, item.source_id)
            if key in seen:
                continue
            seen.add(key)
            titles.append(_clean(item.title) or item.source_id)
    if not titles:
        return ()
    return tuple(titles[:UNEXPANDED_TITLES]), len(titles)


def _find_groups(finds: Sequence[Any]) -> list:
    """The last page of each distinct ``find_sources`` request (cursor aside)."""
    groups: dict = {}
    for outcome in finds:
        request = outcome.request
        try:
            identity = dataclasses.replace(request, cursor="")
        except TypeError:
            identity = id(request)
        groups.setdefault(identity, []).append(outcome)
    return list(groups.values())


def _tier_rank(tier: str) -> int:
    return 0 if tier == "strong" else 1


def coverage_line(tools: Any, paper: PaperCardEvidence) -> str:
    """The line that leads a paper-table answer (spec §7.3).

    Counts: each distinct request's last page (pages share their counts); a
    paper listed by two requests is counted once, under its best tier.
    "展开" counts the inline rows find_sources listed; rows read straight from
    a source are named apart.  Extracted fields past the table width are named
    rather than dropped silently."""
    inline = paper.rows[:paper.table_rows]
    finds = list(tools.finds)
    overflow = ""
    if paper.overflow_fields:
        overflow = (f"另有 {len(paper.overflow_fields)} 个临时字段未列入表："
                    + "、".join(_clean(name) for name in paper.overflow_fields) + "。")
    if not finds:
        return (f"> 本表展开了 {len(inline)} 篇文档的要点（本轮没有做文档级检索，"
                "不代表已列全）。" + overflow)
    groups = _find_groups(finds)
    strong = sum(int(group[-1].result.strong or 0) for group in groups)
    related = sum(int(group[-1].result.related or 0) for group in groups)
    sightings: dict = {}
    for group in groups:
        tiers: dict = {}
        for outcome in group:
            for item in outcome.result.items:
                tiers.setdefault((item.notebook_id, item.source_id), item.tier)
        for key, tier in tiers.items():
            sightings.setdefault(key, []).append(tier)
    for tiers in sightings.values():
        if len(tiers) < 2:
            continue
        # Take every sighting out of its own tier, then count the paper once
        # under the best one.
        for tier in tiers:
            if tier == "strong":
                strong -= 1
            else:
                related -= 1
        if min(_tier_rank(t) for t in tiers) == 0:
            strong += 1
        else:
            related += 1
    strong, related = max(0, strong), max(0, related)
    total = max(strong + related, len(sightings))
    expanded = sum(1 for row in inline if (row.notebook_id, row.source_id) in sightings)
    direct = len(inline) - expanded
    noun = "文档" if any(getattr(o.request, "kind", "") == "all" for o in finds) else "论文"
    gaps: list[str] = []
    for outcome in finds:
        for gap in find_coverage_gaps(outcome.result):
            if gap not in gaps:
                gaps.append(gap)
    if any(group[-1].result.next_cursor for group in groups):
        gaps.append("检索结果还有后续页没有取回")
    line = (f"> 共找到 {total} 篇相关{noun}（强相关 {strong} 篇、相关 {related} 篇），"
            f"其中 {expanded} 篇展开了要点"
            + (f"（另有 {direct} 篇直接按来源读取）" if direct else "") + "。")
    if gaps:
        line += "可能还有更多：" + "；".join(gaps) + "。"
    return line + overflow


# ----------------------------------------------------------- composition

def compose_paper_answer(answer: str, paper: Optional[PaperCardEvidence]) -> str:
    """The single-synthesis answer of a paper-table run: coverage line, the
    model's overview, the table.  Unchanged otherwise -- and for an empty model
    answer, so the empty-content retry still sees an empty answer."""
    if paper is None or not paper.paper_table or not answer:
        return answer
    return f"{paper.coverage}\n\n{answer}\n\n{paper.table}"


def coverage_line_rendered(answer: str, paper: Optional[PaperCardEvidence]) -> bool:
    """The answer carries the paper table and its coverage line.  That line is
    then the answer's single completeness statement -- it says "可能还有更多"
    / "不代表已列全" itself when coverage has gaps -- so the generic
    completeness warning is not appended after the table as well."""
    return bool(
        answer and paper is not None and paper.paper_table and paper.coverage
        and paper.coverage in answer and paper.table in answer)


def sectioned_paper_prefix(paper: Optional[PaperCardEvidence]) -> str:
    """What a sectioned answer starts with: the coverage line and the table,
    once, before the first section (``""`` when there is no table)."""
    if paper is None or not paper.paper_table:
        return ""
    return f"{paper.coverage}\n\n{paper.table}"


__all__ = [
    "DIGEST_PAPER_CHARS",
    "PAPER_CARD_KEY_BASE",
    "PAPER_CARD_MAX_KEYS",
    "PAPER_DIGEST_GUIDANCE",
    "PaperCardEvidence",
    "PaperRow",
    "build_paper_card_evidence",
    "compose_paper_answer",
    "coverage_line",
    "coverage_line_rendered",
    "document_read_band_overflow",
    "paper_digest_block",
    "paper_digest_reserve",
    "paper_rows",
    "registered_keys",
    "render_paper_table",
    "roster_omitted_for",
    "sectioned_paper_prefix",
    "value_with_unit",
    "with_registration",
]
