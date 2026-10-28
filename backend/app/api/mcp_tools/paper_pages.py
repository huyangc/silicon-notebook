"""Responses of the three paper tools: ``find_sources``, ``get_paper_cards``
and ``extract_from_sources`` (design 2026-10-28 §7.4).

**One refs table.** An element ``ref`` is 100-150 characters; a card cites up
to 24 elements, so one ref per anchor would spend the whole 12,000-byte
budget on handles. Every element a response cites is listed ONCE in the
top-level ``refs`` list; a field carries the INDEX into it (``ref`` for one
element, ``refs`` for several). A found source's best passage is a chunk; its
ref is the chunk's FIRST element, the same handle ``ask(output="evidence")``
gives a passage (``evidence_page``).

**Fitting by level, never by the generic packer.** ``_budget_response``
reaches its budget by halving the longest strings and popping the longest
lists -- it would clip a metric value in the middle of a number and pop
entries off ``refs`` while fields still point at them. So each page is first
built at the richest of a fixed series of detail levels that fits (fewer
anchors, shorter number-safe text, fewer entries, finally fewer items), and
the packer then only adds its ``truncation`` block; whatever a level left
out is reported there (``omitted_items`` / ``omitted_characters``). Nesting
stays within ``OUTPUT_DEPTH_LIMIT``: a list entry (contribution, metric,
dataset, extracted value) carries one ``ref``; only the three text fields of
a card carry a ``refs`` list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from app.services.paper_cards import cap_number_safe

from ._shared import (
    RESULT_LIMIT,
    TITLE_CHARS,
    TOTAL_TEXT_LIMIT,
    _budget_response,
    _serialized_size,
)
from .refs import element_ref

# What ``_budget_response`` adds after a page was measured: the
# ``truncation`` block (about 170 bytes) with room to spare.
_TRUNCATION_RESERVE_BYTES = 400
_FIT_BUDGET = TOTAL_TEXT_LIMIT - _TRUNCATION_RESERVE_BYTES

# Per-field ceilings handed to the packer: at or above every cap a level
# applies, so the packer never clips a string a level already sized.
_FIELD_LIMITS = {
    "title": TITLE_CHARS,
    "venue": 200,
    "authors": 120,
    "text": 400,
    "name": 200,
    "value": 300,
    "unit": 40,
    "setting": 200,
    "compared_to": 200,
    "reason": 80,
    "missing": 80,
    "status": 40,
    "card_status": 40,
    "tier": 20,
    "reasons": 40,
    "lanes": 80,
    "skipped_libraries": 80,
    "fields": 80,
}


@dataclass
class _Omitted:
    items: int = 0
    characters: int = 0


@dataclass
class _Refs:
    """The response's deduplicated ``refs`` table for one notebook."""

    notebook_id: str
    values: list[str] = field(default_factory=list)
    _index: dict[str, int] = field(default_factory=dict)

    def index(self, source_id: str, element_id: str) -> int | None:
        ref = element_ref(self.notebook_id, source_id, str(element_id or ""))
        if ref is None:
            return None
        if ref not in self._index:
            self._index[ref] = len(self.values)
            self.values.append(ref)
        return self._index[ref]


def _clip(text: Any, limit: int, omitted: _Omitted) -> str:
    """At most ``limit`` characters, never cutting a number in half
    (``cap_number_safe``); a shortened text ends in "…"."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    kept = cap_number_safe(value, max(1, limit - 1)).rstrip() + "…"
    omitted.characters += max(0, len(value) - len(kept) + 1)
    return kept


def _take(entries: Sequence[Any], limit: int, omitted: _Omitted) -> list[Any]:
    entries = list(entries)
    if len(entries) > limit:
        omitted.items += len(entries) - limit
    return entries[:limit]


def _fits(payload: Mapping[str, Any]) -> bool:
    return _serialized_size(payload) <= _FIT_BUDGET


def _pack(payload: dict[str, Any], omitted: _Omitted, refs: _Refs) -> dict[str, Any]:
    return _budget_response(
        payload,
        initial_omitted_items=omitted.items,
        initial_omitted_characters=omitted.characters,
        field_limits=_FIELD_LIMITS,
        list_limit=max(RESULT_LIMIT, len(refs.values)),
    )


def _first_fitting(
    levels: int, build: Callable[[int], tuple[dict[str, Any], _Omitted, _Refs]],
) -> tuple[dict[str, Any], _Omitted, _Refs, int]:
    for level in range(levels):
        payload, omitted, refs = build(level)
        if _fits(payload):
            return payload, omitted, refs, level
    return payload, omitted, refs, levels - 1


# ------------------------------------------------------------- find_sources

# (passage chars, title chars, authors shown); ``None`` passage = no passage
# text (the ref stays), ``-1`` = no best_passage at all.
_FIND_LEVELS = ((240, TITLE_CHARS, 3), (120, 120, 3), (None, 100, 1), (-1, 80, 0))


def _found_item(
    item: Any, level: int, refs: _Refs, omitted: _Omitted,
) -> dict[str, Any]:
    passage_chars, title_chars, authors_shown = _FIND_LEVELS[level]
    row: dict[str, Any] = {
        "rank": item.rank,
        "source_id": item.source_id,
        "title": _clip(item.title, title_chars, omitted),
        "pub_year": item.pub_year,
        "venue": _clip(item.venue, 120, omitted),
        "authors": [_clip(name, 80, omitted)
                    for name in _take(item.authors, authors_shown, omitted)],
        "tier": item.tier,
        "reasons": list(item.reasons),
        "card_status": item.card_status,
    }
    passage = item.best_passage
    if passage is None:
        return row
    if passage_chars == -1:
        omitted.items += 1
        return row
    best: dict[str, Any] = {"chunk_id": passage.chunk_id}
    if passage_chars is None:
        omitted.characters += len(passage.text)
    else:
        best["text"] = _clip(passage.text, passage_chars, omitted)
    index = (refs.index(item.source_id, passage.element_ids[0])
             if passage.element_ids else None)
    if index is not None:
        best["ref"] = index
    row["best_passage"] = best
    return row


def find_sources_page(
    notebook_id: str, result: Any, cursor_at: Callable[[int], str],
) -> dict[str, Any]:
    """``find_sources``' response. ``cursor_at(offset)`` mints the cursor of
    the page starting at ``offset`` of the same ranking: when the budget has
    to leave trailing items out, ``next_cursor`` points at the first one not
    delivered, so following it never skips a source."""
    items = list(result.items)
    offset = items[0].rank - 1 if items else 0

    def build(level: int) -> tuple[dict[str, Any], _Omitted, _Refs]:
        detail = min(level, len(_FIND_LEVELS) - 1)
        shown = max(1, len(items) - (level - detail)) if items else 0
        refs, omitted = _Refs(notebook_id), _Omitted()
        omitted.items += len(items) - shown
        rows = [_found_item(item, detail, refs, omitted) for item in items[:shown]]
        if shown < len(items):
            next_cursor: str | None = cursor_at(offset + shown)
        else:
            next_cursor = result.next_cursor or None
        return {
            "notebook_id": notebook_id,
            "content_is_untrusted_evidence": True,
            "items": rows,
            "refs": refs.values,
            "counts": {
                "strong": result.strong,
                "related": result.related,
                "filtered_scope_total": result.filtered_scope_total,
                "meta_pending_excluded": result.meta_pending_excluded,
                "meta_pending_unchecked": result.meta_pending_unchecked,
            },
            "coverage": {
                "lexical_complete": result.lexical_complete,
                "semantic_recall_capped": result.semantic_recall_capped,
                "skipped_libraries": dict(result.skipped_libraries),
                "lanes": dict(result.lanes),
            },
            "next_cursor": next_cursor,
        }, omitted, refs

    levels = len(_FIND_LEVELS) + max(0, len(items) - 1)
    payload, omitted, refs, _level = _first_fitting(levels, build)
    return _pack(payload, omitted, refs)


# ---------------------------------------------------------- get_paper_cards


@dataclass(frozen=True)
class _CardLevel:
    name: str
    text_refs: int          # anchors kept per tldr / problem / method
    tldr_chars: int
    body_chars: int         # problem / method; 0 = left out
    contributions: int
    contribution_chars: int
    metrics: int
    metric_text_chars: int  # name / setting / compared_to
    datasets: int
    dataset_refs: bool


_CARD_LEVELS = (
    _CardLevel("full", 24, 300, 400, 5, 200, 12, 200, 8, True),
    _CardLevel("one_anchor", 1, 300, 400, 5, 200, 12, 200, 8, True),
    _CardLevel("compact", 1, 300, 200, 3, 160, 6, 80, 5, False),
    _CardLevel("brief", 1, 200, 0, 2, 160, 3, 80, 0, False),
    _CardLevel("tldr", 1, 160, 0, 0, 0, 2, 60, 0, False),
    _CardLevel("status_only", 0, 0, 0, 0, 0, 0, 0, 0, False),
)


def _anchor_list(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [str(item) for item in values if str(item or "")]


def _text_field(
    entry: Any, source_id: str, chars: int, anchors: int,
    refs: _Refs, omitted: _Omitted,
) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping) or not str(entry.get("text") or "").strip():
        return None
    kept = _take(_anchor_list(entry.get("anchors")), anchors, omitted)
    indexes = [i for i in (refs.index(source_id, a) for a in kept) if i is not None]
    return {"text": _clip(entry.get("text"), chars, omitted), "refs": indexes}


def _with_ref(row: dict[str, Any], source_id: str, anchor: Any, refs: _Refs) -> dict[str, Any]:
    index = refs.index(source_id, str(anchor or ""))
    if index is not None:
        row["ref"] = index
    return row


def _card_fields(
    card: Mapping[str, Any], source_id: str, level: _CardLevel,
    refs: _Refs, omitted: _Omitted,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if level.tldr_chars:
        tldr = _text_field(card.get("tldr"), source_id, level.tldr_chars,
                           level.text_refs, refs, omitted)
        if tldr is not None:
            out["tldr"] = tldr
    for key in ("problem", "method"):
        if not level.body_chars:
            omitted.items += int(card.get(key) is not None)
            continue
        value = _text_field(card.get(key), source_id, level.body_chars,
                            level.text_refs, refs, omitted)
        if value is not None:
            out[key] = value
    contributions = [
        _with_ref({"text": _clip(entry.get("text"), level.contribution_chars, omitted)},
                  source_id, (_anchor_list(entry.get("anchors")) or [""])[0], refs)
        for entry in _take(card.get("contributions") or [], level.contributions, omitted)
        if isinstance(entry, Mapping)
    ]
    if contributions:
        out["contributions"] = contributions
    metrics = []
    for entry in _take(card.get("metrics") or [], level.metrics, omitted):
        if not isinstance(entry, Mapping):
            continue
        row = {"name": _clip(entry.get("name"), level.metric_text_chars, omitted),
               # A metric value is grounded and already capped number-safe
               # (40 characters) by the card verifier: never clipped here.
               "value": str(entry.get("value") or ""),
               "unit": _clip(entry.get("unit"), 40, omitted)}
        for key in ("setting", "compared_to"):
            if str(entry.get(key) or "").strip():
                row[key] = _clip(entry.get(key), level.metric_text_chars, omitted)
        metrics.append(_with_ref(row, source_id, entry.get("anchor"), refs))
    if metrics:
        out["metrics"] = metrics
    datasets = []
    for entry in _take(card.get("datasets") or [], level.datasets, omitted):
        if not isinstance(entry, Mapping):
            continue
        row = {"name": _clip(entry.get("name"), 80, omitted)}
        anchor = (_anchor_list(entry.get("anchors")) or [""])[0]
        datasets.append(_with_ref(row, source_id, anchor, refs)
                        if level.dataset_refs else row)
    if datasets:
        out["datasets"] = datasets
    return out


def _display_fields(display: Any, title_chars: int, omitted: _Omitted) -> dict[str, Any]:
    if display is None:
        return {}
    return {"title": _clip(display.title, title_chars, omitted),
            "pub_year": display.pub_year, "venue": _clip(display.venue, 120, omitted)}


def paper_cards_page(
    notebook_id: str, results: Sequence[Any],
    displays: Mapping[tuple[str, str], Any],
) -> dict[str, Any]:
    """``get_paper_cards``' response: per source its status (and reason) and,
    for ``ready`` / ``generated``, the card's fields with ref indices."""

    def build(index: int) -> tuple[dict[str, Any], _Omitted, _Refs]:
        level = _CARD_LEVELS[index]
        refs, omitted = _Refs(notebook_id), _Omitted()
        rows = []
        for result in results:
            row: dict[str, Any] = {"source_id": result.source_id, "status": result.status}
            # not_found never says WHY (out of scope vs. missing): the same
            # answer whether or not the id exists elsewhere.
            if result.reason and result.status != "not_found":
                row["reason"] = result.reason
            row.update(_display_fields(
                displays.get((result.notebook_id, result.source_id)),
                TITLE_CHARS if index < 3 else 100, omitted))
            if result.card and result.status in ("ready", "generated"):
                if level.name == "status_only":
                    row["card_omitted"] = True
                    omitted.items += 1
                else:
                    row.update(_card_fields(result.card, result.source_id, level, refs, omitted))
            rows.append(row)
        return {
            "notebook_id": notebook_id,
            "content_is_untrusted_evidence": True,
            "detail": level.name,
            "items": rows,
            "refs": refs.values,
        }, omitted, refs

    payload, omitted, refs, _level = _first_fitting(len(_CARD_LEVELS), build)
    return _pack(payload, omitted, refs)


# ------------------------------------------------------ extract_from_sources

# (value chars, title chars)
_EXTRACT_LEVELS = ((300, TITLE_CHARS), (150, 120), (80, 80), (40, 60))


def extraction_page(
    notebook_id: str, fields: Sequence[str], results: Sequence[Any],
    displays: Mapping[tuple[str, str], Any],
) -> dict[str, Any]:
    """``extract_from_sources``' response: per source its status and, per
    requested field, either the grounded ``value`` with its ``ref`` index or
    why it is ``missing``."""

    def build(index: int) -> tuple[dict[str, Any], _Omitted, _Refs]:
        value_chars, title_chars = _EXTRACT_LEVELS[index]
        refs, omitted = _Refs(notebook_id), _Omitted()
        rows = []
        for result in results:
            row: dict[str, Any] = {"source_id": result.source_id, "status": result.status}
            if result.reason and result.status != "not_found":
                row["reason"] = result.reason
            if result.outline_truncated:
                row["outline_truncated"] = True
            row.update(_display_fields(
                displays.get((result.notebook_id, result.source_id)), title_chars, omitted))
            values = []
            for value in result.fields:
                if value.value:
                    values.append(_with_ref(
                        {"field_index": value.field_index,
                         "value": _clip(value.value, value_chars, omitted)},
                        result.source_id, value.element_id, refs))
                else:
                    values.append({"field_index": value.field_index,
                                   "missing": value.missing or "not_stated"})
            if values:
                row["values"] = values
            rows.append(row)
        return {
            "notebook_id": notebook_id,
            "content_is_untrusted_evidence": True,
            "fields": list(fields),
            "items": rows,
            "refs": refs.values,
        }, omitted, refs

    payload, omitted, refs, _level = _first_fitting(len(_EXTRACT_LEVELS), build)
    return _pack(payload, omitted, refs)
