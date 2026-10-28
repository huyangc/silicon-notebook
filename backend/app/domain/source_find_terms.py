"""Literal-term matching for ``find_sources``' metadata leg (pure stdlib).

The metadata leg runs one SQL prefilter per library (``lower(col) LIKE`` with the
``%`` / ``_`` / backslash metacharacters escaped) and then re-checks every row it
returned here, in Python, with the very same case fold SQL applies
(``ascii_lower``: A-Z only, on both backends).  The re-check is the arbiter: it
names WHICH field matched and whether the match sits on a word boundary, and it
drops a prefilter hit that is not a real match.  So SQL may only ever
over-match, never under-match, and both backends answer identically.
``keywords`` is matched one list element at a time on both backends
(``jsonb_array_elements`` / ``json_each``), never on the JSON text, so a term
containing a quote or a backslash matches the keyword as written.

Word boundary (design §5.2): a short ASCII term such as ``GAN`` matching inside
``organ`` is a substring match, not a word match.  For an ASCII alphanumeric
term of at most ``WORD_BOUNDARY_TERM_MAX_CHARS`` characters the match counts as
word-boundary only when the characters on both sides are not ASCII letters or
digits (a trailing plural ``s`` is allowed: ``GAN`` matches ``GANs`` as a word,
``organ`` stays a substring; ``es`` is not -- ``GAT`` must not match ``gates``); any other term (non-ASCII, contains punctuation/space, or longer)
counts every substring match as a boundary match -- CJK has no case and no
spaces, and a long term rarely hides inside an unrelated word.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from app.domain.sql_text_fold import ascii_lower

#: Fields the metadata leg matches, in the order reasons are reported.
FIND_SOURCES_FIELDS = (
    "title", "paper_title", "keywords", "summary", "author", "venue",
)
#: Fields whose word-boundary match makes a source ``strong`` (design §5.3).
STRONG_FIELDS = frozenset({"title", "paper_title", "keywords"})
#: Short ASCII terms are judged by word boundary (see the module docstring).
WORD_BOUNDARY_TERM_MAX_CHARS = 6

_ASCII_ALNUM = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


#: A filtered scope counts as REALLY narrowed only when it is non-empty and at
#: most this share of the library's visible sources.  Two consumers, one rule:
#: the metadata leg binds its term legs to the scope's ids only then
#: (``find_sources_rows.bind_term_legs``), and ``find_sources`` attests its
#: semantic leg's list as a producer's own narrow universe
#: (``producer_explicit``, which turns the lexical corpus-language gate off)
#: only then.  Measured on the 49k-source bench
#: (``docs/superpowers/plans/2026-10-28-find-sources-measurement.md``): a 49-id
#: scope cuts the term legs' UNION ALL from 21 ms to 1-2 ms while a 26% scope
#: makes it slower (21 -> 52 ms); and a near-whole-library scan with the
#: language gate off is the ASK-1 pathology (64 probe terms, 29.7 s cold).
NARROW_SCOPE_MAX_SHARE = 0.1


def really_narrowed(scope_size: int, visible_total: int) -> bool:
    """Is a scope of ``scope_size`` out of ``visible_total`` visible sources
    narrow enough to be treated as bounded (see ``NARROW_SCOPE_MAX_SHARE``)?"""
    return 0 < scope_size < visible_total and (
        scope_size <= visible_total * NARROW_SCOPE_MAX_SHARE
    )


def fold_term(term: str) -> str:
    """The folded form a term is matched with (SQL ``lower()``'s fold)."""
    return ascii_lower(str(term or "").strip())


def like_pattern(folded_term: str) -> str:
    """``%term%`` with LIKE's metacharacters escaped by backslash.

    PostgreSQL's LIKE escapes with backslash by default; the SQLite statements
    spell ``ESCAPE '\\'`` explicitly.  The backslash itself is escaped first."""
    escaped = (
        folded_term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _needs_boundary(folded_term: str) -> bool:
    return (
        0 < len(folded_term) <= WORD_BOUNDARY_TERM_MAX_CHARS
        and all(char in _ASCII_ALNUM for char in folded_term)
    )


def term_match(folded_term: str, value: object) -> bool | None:
    """``None`` when ``folded_term`` does not occur in ``value``; otherwise
    whether some occurrence is a word-boundary match."""
    if not folded_term or value is None:
        return None
    text = ascii_lower(str(value))
    start = text.find(folded_term)
    if start < 0:
        return None
    if not _needs_boundary(folded_term):
        return True
    width = len(folded_term)
    while start >= 0:
        before = text[start - 1] if start > 0 else ""
        if before not in _ASCII_ALNUM and _ends_a_word(text, start + width):
            return True
        start = text.find(folded_term, start + 1)
    return False


def _ends_a_word(text: str, end: int) -> bool:
    """Does the match ending at ``end`` end a word, allowing a plural ``s``?

    ``GAN`` in ``GANs`` is still the term (a short acronym is most often
    written in the plural), so a trailing ``s`` followed by a non-alphanumeric
    is a boundary too.  ``es`` is deliberately NOT accepted: it turns ordinary
    words into false strong hits (``GAT`` in ``gates``, ``nod`` in ``nodes``).
    ``organ``-style hits stay substring matches because the character BEFORE
    them is a letter."""
    for suffix in ("", "s"):
        if suffix and not text.startswith(suffix, end):
            continue
        after = end + len(suffix)
        if after >= len(text) or text[after] not in _ASCII_ALNUM:
            return True
    return False


def field_match(folded_terms: Sequence[str], values: Iterable[object]) -> bool | None:
    """Best match of any term in any of ``values``: ``None`` (no match),
    ``False`` (substring only) or ``True`` (a word-boundary match)."""
    best: bool | None = None
    for value in values:
        for term in folded_terms:
            hit = term_match(term, value)
            if hit is True:
                return True
            if hit is False:
                best = False
    return best


def merge_match(
    matches: dict[str, bool], field: str, hit: bool | None,
) -> None:
    """Record ``hit`` for ``field``: a boundary match anywhere wins."""
    if hit is None:
        return
    matches[field] = bool(matches.get(field, False) or hit)


def ordered_fields(matches: Mapping[str, bool]) -> tuple[str, ...]:
    """Matched fields in the canonical reporting order."""
    return tuple(field for field in FIND_SOURCES_FIELDS if field in matches)
