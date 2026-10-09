"""Where each ``kN`` entry of a synthesis context starts and ends, recorded by
the code that writes it.

The retrieval-only ask output (``output="evidence"``) hands an Agent the
synthesis context cut back into one item per key.  Finding those cuts again
in the finished string is guessing: a chunk whose own text has a line that
starts with another admitted key (``k2: ...``), or ends in a blank line plus
a bracketed line, looks exactly like an entry boundary or a section heading.
So the boundaries are recorded where they are known -- in the renderers that
write the entries and in the assemblers that join blocks -- and never
re-discovered from the text.

Recording is off unless ``recording_spans()`` is active (only the evidence
path turns it on).  Off, every helper returns exactly the string the inline
expression it replaced returned (``sep.join``, ``"".join``, ``text[:limit]``),
so the answer path's prompt is byte for byte what it was.  On, each helper
also files ``text -> spans`` in a per-run registry keyed by the string it
returned; a later helper that receives that string as a part shifts its spans
into the new string.  A part nobody registered is one keyless span: its text
is still delivered, but none of its keys is attributed (they count as
omitted) -- it is never split by looking at its text.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, NamedTuple, Sequence


class Span(NamedTuple):
    """``[start, end)`` of one piece of a context string.

    ``key`` is the entry's ``kN`` (``""`` for keyless content); ``glue`` marks
    text an assembler inserted between blocks -- a separator or a section
    heading -- which is not anybody's evidence."""

    key: str
    start: int
    end: int
    glue: bool = False


class Glue(str):
    """Text an assembler puts between blocks (separator / section heading)."""

    __slots__ = ()


# The renderer-written head of a keyed entry: ``k12: ...``.  Read only at a
# recorded entry start, never searched for.
_ENTRY_KEY = re.compile(r"k\d+(?=:)")

_RECORDER: ContextVar["dict[str, tuple[Span, ...]] | None"] = ContextVar(
    "context_span_recorder", default=None
)


@contextmanager
def recording_spans() -> Iterator[None]:
    """Record context boundaries for the duration (re-entrant)."""
    if _RECORDER.get() is not None:
        yield
        return
    token = _RECORDER.set({})
    try:
        yield
    finally:
        _RECORDER.reset(token)


def recorded_spans(text: str) -> "tuple[Span, ...] | None":
    """The spans filed for ``text``, or ``None`` (not recording / unknown)."""
    registry = _RECORDER.get()
    return None if registry is None else registry.get(text)


def entry_lines(
    lines: Sequence[str], *, sep: str = "\n", empty: str = "(none)",
    starts: "Sequence[int] | None" = None,
) -> str:
    """``sep.join(lines)`` (``empty`` when there are none) for a renderer whose
    list items are its entries.

    Without ``starts`` every item is one entry; its key is read from its own
    head.  With ``starts`` (indices of the items that open an entry) the items
    up to the next start belong to that entry -- for a renderer that writes
    an entry as several items, e.g. a workbook result and its rows; items
    before the first start are keyless.
    """
    if not lines:
        return empty
    text = sep.join(lines)
    registry = _RECORDER.get()
    if registry is None:
        return text
    opens = set(range(len(lines))) if starts is None else set(starts)
    heads = sorted(opens | {0})
    spans: list[Span] = []
    offset = 0
    for position, head in enumerate(heads):
        tail = heads[position + 1] if position + 1 < len(heads) else len(lines)
        if position:
            spans.append(Span("", offset, offset + len(sep), True))
            offset += len(sep)
        unit = sep.join(lines[head:tail])
        match = _ENTRY_KEY.match(unit) if head in opens else None
        spans.append(Span(match.group(0) if match else "", offset, offset + len(unit)))
        offset += len(unit)
    registry[text] = tuple(spans)
    return text


def concat(*parts: str) -> str:
    """``"".join(parts)``; ``Glue`` parts are recorded as assembler glue."""
    text = "".join(parts)
    registry = _RECORDER.get()
    if registry is None or not text:
        return text
    spans: list[Span] = []
    offset = 0
    for part in parts:
        if not part:
            continue
        end = offset + len(part)
        inner = None if isinstance(part, Glue) else registry.get(part)
        if inner is not None:
            spans.extend(span._replace(start=span.start + offset, end=span.end + offset)
                         for span in inner)
        else:
            spans.append(Span("", offset, end, isinstance(part, Glue)))
        offset = end
    registry[text] = tuple(spans)
    return text


def joined(parts: Sequence[str], sep: str) -> str:
    """``sep.join(parts)`` of whole blocks (``sep`` is glue)."""
    pieces: list[str] = []
    for index, part in enumerate(parts):
        if index:
            pieces.append(Glue(sep))
        pieces.append(part)
    return concat(*pieces)


def clip(text: str, limit: int) -> str:
    """``text[:limit]``; a cut entry keeps its key over the part that stayed."""
    out = text[:limit]
    registry = _RECORDER.get()
    if registry is None or not out or out == text:
        return out
    inner = registry.get(text)
    if inner is not None:
        registry[out] = tuple(
            span._replace(end=min(span.end, len(out)))
            for span in inner if span.start < len(out)
        )
    return out
