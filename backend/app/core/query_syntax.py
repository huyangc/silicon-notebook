"""The one syntax a user may write into a query: `"..."` means "keep this whole".

Everything else in retrieval decomposes the question — the lexical layer splits
it into phrase / identifier / word / CJK-trigram terms, and the scoring layer
splits it into content tokens. A span the user wrapped in ASCII double quotes is
the exception: it is a constraint about matching, not material to be broken up,
so both layers ask this module which parts of the text they are not allowed to
touch.

It lives in `core` because it sits BELOW both of them: `app.repositories`
(candidate generation) and `app.services.retrieval` (scoring) import each other
through the ports module, so a shared leaf is the only place a single definition
can live. It imports nothing from the app and holds no state.

The frontend mirrors this parse in `frontend/app/query-syntax.ts` so the
composer can show the user which phrases were actually recognised; the two are
pinned to the same cases by tests on both sides.
"""
from __future__ import annotations

import re
from typing import Iterable


# A single exact phrase (and therefore a quoted span) may not exceed this.
MAX_EXACT_PHRASE_CHARS = 256
# A user may declare at most this many atomic phrases per query — and a text
# carrying MORE DISTINCT quoted spans than this has none of them honoured at
# all. Distinct, because the internal research query legitimately repeats one
# phrase across the objective, the normalized question and every mandatory
# topic; see the counting comment in `split_quoted_phrases`.
#
# The threshold is not a budget, it is a shape test: quoting two or three things
# is a constraint, quoting a dozen is punctuation in pasted material. The
# concrete case in this codebase is knowhow smart completion, whose "query" is a
# JSON envelope; every key and every cell value is a quoted span there, so
# honouring them would turn machine scaffolding into search constraints AND strip
# the CJK cell values of their tri-gram recall path (the one regression this
# layer has measured before). Honouring a PREFIX of a dozen spans would be worse
# than either option — it would silently pick a few at random and still strip
# the rest — so past the threshold the syntax simply does not apply and the text
# is tokenized exactly as it was before this feature existed.
MAX_QUOTED_PHRASES = 4
# SQLite's FTS5 trigram tokenizer cannot index anything shorter, so a term below
# this length has no lexical recall path at all on the shipped backend.
MIN_LEXICAL_TERM_CHARS = 3

# ASCII double quotes ONLY, and never across a line break.
#
# `“…”` is deliberately excluded: Chinese prose spends the typographic pair on
# ordinary quotation and emphasis (他说“不行”), so honouring it would silently
# turn a large share of existing questions into phrase-constrained ones. A
# straight `"` inside a Chinese question is a deliberate act — which is exactly
# what a search constraint has to be. Excluding `\n` keeps one unbalanced quote
# in a pasted block from swallowing the paragraph after it.
_QUOTED_RE = re.compile(r'"([^"\n]+)"')

# Confirmed-intent planning keeps the reviewed contract attached to each
# direction so later orchestration can audit it.  That envelope is not search
# material: feeding it to lexical decomposition / keyword coverage dilutes a
# short direction with the same boilerplate on every query.  Retrieval leaves
# the public/trace string intact and reads only the prefix before one of these
# service-owned separators.
_RETRIEVAL_CONTRACT_MARKERS = (
    "\n\n检索必须服从以下已确认问题契约：",
    "\n\n用户确认的补充信息与问题契约：",
)


def retrieval_query_head(text: str) -> str:
    """Return the actual search direction, excluding intent-contract text.

    The markers are service-generated exact separators, not user syntax.  An
    ordinary question that merely mentions the same words without the blank
    line + full marker stays unchanged.  Empty/malformed envelopes fail open to
    the original text so this helper can never erase a query.
    """
    source = text or ""
    cut = len(source)
    for marker in _RETRIEVAL_CONTRACT_MARKERS:
        index = source.find(marker)
        if index >= 0:
            cut = min(cut, index)
    head = source[:cut].strip()
    return head or source.strip()


def split_quoted_phrases(text: str) -> tuple[list[str], str]:
    """Split user-declared atomic phrases off the rest of the query.

    Returns `(phrases, remainder)` where `remainder` is the input with exactly
    the accepted spans blanked out — so the caller can decompose what is left
    without emitting a phrase's own words a second time as independent terms.
    "Don't tokenize what I quoted" is the whole contract: `"static timing
    analysis"` must mean that string, not `static` OR `timing` OR `analysis`.

    A span is accepted only when it can actually become a term: at least
    MIN_LEXICAL_TERM_CHARS (below that SQLite's trigram index cannot match it at
    all) and no longer than MAX_EXACT_PHRASE_CHARS. Rejected spans are left in
    the remainder rather than dropped — masking a span the caller then discards
    would delete those words from the query entirely, turning a precision
    request into a recall hole.

    More than MAX_QUOTED_PHRASES spans in the text disables the syntax for that
    text entirely (see the constant): `("", text)`, i.e. the exact pre-feature
    behaviour.

    Duplicate spans fold onto their first occurrence (case-insensitively) but
    every occurrence is still masked, since the surviving phrase covers it.
    Blanking preserves length and offsets, so the remainder stays aligned with
    the original text for anything that reports positions.

    Known boundary (codex #410 round-1 P2, deliberately not fixed here). The
    phrase is whitespace-normalized, but the two consumers differ in how
    tolerant they can be about the DOCUMENT's whitespace:

    * Scoring (`KeywordBasis`, `bm25_scores`) normalizes the haystack too, so a
      phrase broken across a line break still counts.
    * Candidate generation cannot: an FTS5 trigram phrase and an escaped `ILIKE`
      pattern are literal, contiguous matches against stored text. A document
      that writes `static   timing\nanalysis` is therefore scored as a match if
      something else surfaced it, but the phrase alone will not surface it.

    Closing that gap needs a whitespace-normalized indexed column (schema
    migration + backfill); an unindexed regex scan over chunk text would be the
    unbounded per-notebook scan this layer must never do. In practice the rest
    of the query still produces candidates — the quote-stripped whole-sentence
    term and every non-quoted run are still in the term list — and ANN recall is
    unaffected.
    """
    phrases, spans = _accepted_spans(text or "")
    pieces: list[str] = []
    cursor = 0
    for start, end, _value in spans:
        pieces.append((text or "")[cursor:start])
        pieces.append(" " * (end - start))
        cursor = end
    pieces.append((text or "")[cursor:])
    return phrases, "".join(pieces)


def _accepted_spans(source: str) -> tuple[list[str], list[tuple[int, int, str]]]:
    """The accepted phrases plus the `(start, end, value)` of every span they cover.

    One acceptance rule, used by both the masking split above and the
    marker-stripping below — a second copy of "which spans count" is exactly how
    the two would start disagreeing about the same query.
    """
    matches = _QUOTED_RE.findall(source)
    # DISTINCT spans, not raw occurrences. Repeating one phrase is not "many
    # constraints", and the internal research query is built by concatenating
    # the intent contract's objective, resolved question, entities and up to 16
    # mandatory topics — each of which the planner prompt explicitly tells the
    # model to carry the user's quoted span through verbatim. Counting
    # occurrences let that expansion push a perfectly ordinary one-phrase
    # question past the limit and silently switch the syntax off for the whole
    # reasoning/report run, i.e. exactly where it matters most (codex #410
    # round-4 P2). The JSON-envelope shape this gate exists for is unaffected:
    # its keys and cell values are all DIFFERENT strings.
    distinct = {" ".join(value.split()).lower() for value in matches}
    if not matches or len(distinct) > MAX_QUOTED_PHRASES:
        return [], []
    phrases: list[str] = []
    seen: set[str] = set()
    spans: list[tuple[int, int, str]] = []
    for match in _QUOTED_RE.finditer(source):
        value = " ".join(match.group(1).split())
        # `.lower()`, deliberately not `.casefold()`: this key has to agree with
        # the frontend mirror, and JavaScript has no casefold. `casefold()` maps
        # ß→ss, so `"straße"`/`"STRASSE"` would fold together here and stay two
        # phrases in the composer — the echo would then name a different set of
        # phrases than retrieval applies, which is the one thing the mirror
        # exists to prevent. Simple lowercase matches `toLowerCase()`.
        # (codex #410 round-2 P3)
        folded = value.lower()
        if (
            len(value) < MIN_LEXICAL_TERM_CHARS
            or len(value) > MAX_EXACT_PHRASE_CHARS
        ):
            continue
        if folded not in seen:
            seen.add(folded)
            phrases.append(value)
        spans.append((match.start(), match.end(), value))
    return phrases, spans


def quoted_phrases(text: str) -> list[str]:
    """The atomic phrases a user declared with `"..."`, in appearance order."""
    return split_quoted_phrases(text)[0]


def unquoted_remainder(text: str) -> str:
    """The query with its accepted `"..."` spans blanked out."""
    return split_quoted_phrases(text)[1]


def strip_accepted_quote_markers(text: str) -> str:
    """Drop the quote characters of ACCEPTED spans, keeping the words they wrapped.

    For an accepted span the markers are syntax, not content: no document
    contains them, so any matcher that consumes the whole string (the
    full-sentence recall term, the catalog's plain substring search, Memory's
    whole-query probe) has to see the text without them.

    Rejected spans — unpaired, too short, too long, or any span in a text the
    density gate switched the syntax off for — keep their quotes, because the
    contract everywhere else in this module is that a rejected span stays
    ordinary query content. Stripping unconditionally broke exactly the case the
    density gate exists for: a user searching the catalog for literal JSON or
    code could no longer match the quotes they typed (codex #410 round-5 P2).
    """
    source = text or ""
    _phrases, spans = _accepted_spans(source)
    if not spans:
        return source
    pieces: list[str] = []
    cursor = 0
    for start, end, value in spans:
        pieces.append(source[cursor:start])
        pieces.append(value)
        cursor = end
    pieces.append(source[cursor:])
    return "".join(pieces)


def strip_quote_markers(text: str) -> str:
    """Drop EVERY quote character. Only for building a probe term, never a query.

    `exact_probe_query` re-encodes each term as a quoted span, so a term that
    itself contained a `"` would break that encoding. This is a sanitizer for
    one value, not the query-level transform — that one is
    `strip_accepted_quote_markers` above, which honours the acceptance rule.
    """
    return (text or "").replace('"', "")


def exact_probe_query(terms: Iterable[str]) -> str:
    """Re-encode already-chosen probe terms so a re-parse yields them back.

    The exact-lookup channel re-derives its own terms from the string it is
    handed, so a caller that has already selected them must hand over a string
    that parses to exactly that list. Joining a multi-word phrase with spaces
    would not survive the round trip; quoting every term does, and single-word
    identifiers round-trip through the phrase branch unchanged.
    """
    parts = []
    for term in terms:
        cleaned = " ".join(strip_quote_markers(str(term)).split())
        if cleaned:
            parts.append(f'"{cleaned}"')
    return " ".join(parts)
