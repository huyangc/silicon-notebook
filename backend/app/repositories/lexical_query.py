"""Backend-neutral lexical query decomposition.

The returned terms are candidates only: repository adapters keep their native
ranking, while retrieval services apply the authoritative fused score.

The one thing NOT decomposed here is a user-quoted span; `app.core.query_syntax`
owns that parse and both this layer and the scoring layer read it from there.
"""
from __future__ import annotations

import re

# `MAX_EXACT_PHRASE_CHARS` / `MAX_QUOTED_PHRASES` / `exact_probe_query` are
# re-exported deliberately: this module is the lexical layer's facade, and the
# reasoning retriever already reaches its lexical bounds through it. Callers
# that need the parse itself (scoring, prompts) import `app.core.query_syntax`
# directly — there is one definition, not two spellings of it.
from app.core.query_syntax import (  # noqa: F401  (re-exported)
    MAX_EXACT_PHRASE_CHARS,
    MAX_QUOTED_PHRASES,
    exact_probe_query,
    split_quoted_phrases,
    strip_accepted_quote_markers,
)

MAX_LEXICAL_TERMS = 64
# Identifier terms share the 64-term budget with word/CJK runs; both bounds
# below keep them from monopolising it (review-measured regressions, not
# hypotheticals — see identifier_terms/lexical_recall_terms).
MAX_IDENTIFIER_TERMS = 16
CJK_RESERVED_TERMS = 8

# `_`/`-`/`.`-joined identifiers, e.g. `set_db`, `place_opt_design`,
# `config.yaml`, `state-of-the-art`. Trailing punctuation (like the period in
# "run set_db.") is never absorbed because it is not part of an alnum run.
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9]+(?:[._\-][A-Za-z0-9]+)+")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"[0-9]")


def identifier_terms(text: str) -> list[str]:
    """Extract separator-joined identifiers, deduplicated in appearance order.

    This is the WIDE, recall-side definition; the exact-lookup channel gates on
    the narrower `exact_probe_terms` view below, because one extra OR-ed recall
    term is free while one extra exact probe is not.

    An identifier must contain an ASCII letter and be at least 4 chars long:
    numeric/abbreviation matches like `2.1`, `0.01`, `e.g`, `a-b` are exactly
    the low-selectivity substrings that ballooned a measured trigram-FTS probe
    from 0.7ms/3 hits to 22ms/200 hits ("第 2.1 节讲了什么") with zero recall
    benefit. The phrase cap also applies — a pathological joined run must not
    outgrow the bound the exact phrase itself obeys. At most
    MAX_IDENTIFIER_TERMS survive so pasted command lists cannot monopolise the
    shared MAX_LEXICAL_TERMS budget.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for match in _IDENTIFIER_RE.finditer(text or ""):
        value = match.group(0)
        if (
            len(value) < 4
            or len(value) > MAX_EXACT_PHRASE_CHARS
            or value in seen
            or not _ASCII_LETTER_RE.search(value)
        ):
            continue
        seen.add(value)
        terms.append(value)
        if len(terms) >= MAX_IDENTIFIER_TERMS:
            break
    return terms


def exact_probe_terms(text: str, *, honor_quotes: bool = True) -> list[str]:
    """The NARROWER gate: which of those identifiers deserve an exact probe.

    A user-quoted phrase always passes it. The whole gate exists because an
    INCIDENTAL name costs a probe it did not earn — the report engine's
    per-section question contains `real-time` whether or not anybody wanted it
    looked up. A `"..."` span is never incidental: the user typed two
    characters whose only purpose is to ask for this exact string, so the cost
    argument that rejects `state-of-the-art` does not apply to it.

    `honor_quotes=False` is for terms that came from the MODEL rather than the
    user. The reflect loop's `exact_term` is already stripped of wrapping
    quotes, so this only closes the smuggling shape (`x "的方法" y`), which
    would let a planner reopen the low-selectivity substring probe this gate was
    measured shut. The user's own quotes reach the channel through the seed
    pass, so nothing legitimate needs the action path to honour them.

    `identifier_terms` is a *recall* definition — adding one more OR-ed term to
    a lexical expression costs almost nothing, so `state-of-the-art` riding
    along there is a deliberate, harmless trade. The exact channel is a
    different contract: every surviving term buys a real substring probe, and a
    hit promotes a whole section into the evidence budget. Under that contract
    the same word is a measured regression on both axes — a 20k-chunk library
    answered `state-of-the-art` in 16ms with 50 hits, and a report engine whose
    per-section question reliably contains one of `state-of-the-art` /
    `real-time` / `end-to-end` paid that probe once per section for nothing;
    worse, when `real-time` matched a chapter *heading*, the whole 12-chunk
    chapter entered the evidence set at relevance 1.0.

    So: keep anything joined by `_` or `.` (`set_db`, `config.yaml` — prose does
    not spell words that way), and require a digit from the hyphen-only forms,
    which is exactly the line between a name (`GPT-4`, `v1-2`) and an ordinary
    English compound adjective (`state-of-the-art`, `real-time`, `high-level`).
    A digit is a shape test, not a heuristic about meaning: hyphenated English
    words do not contain digits, and versioned/model names essentially always
    do.
    """
    phrases, remainder = (
        split_quoted_phrases(text) if honor_quotes else ([], text or "")
    )
    candidates = [
        *phrases,
        *(
            term for term in identifier_terms(remainder)
            if "_" in term or "." in term or _DIGIT_RE.search(term)
        ),
    ]
    # Deduplicate ACROSS the two producers, not just within each: quoting a name
    # that also appears bare (`"set_db" 与 set_db 的区别`) masks only the quoted
    # occurrence, so the same name arrives from both sides. Each duplicate costs
    # a real probe and one of the very few `EXACT_LOOKUP_MAX_IDENTIFIERS` slots,
    # so a genuinely distinct third name can be dropped for a repeat of the
    # first. Folded, because the probes themselves are case-insensitive (FTS5
    # trigram case-folds; both adapters use ILIKE) — two casings are one probe.
    # (codex #410 round-2 P2)
    terms: list[str] = []
    seen: set[str] = set()
    for term in candidates:
        folded = term.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        terms.append(term)
    return terms


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2FA1F
    )


def lexical_recall_terms(query: str) -> list[str]:
    """Return an exact phrase plus independent Latin/number and CJK terms.

    User-quoted spans lead the list and are never decomposed: they are the one
    part of the query the user has said must stay whole. Everything else keeps
    its historical treatment, and a query without quotes produces byte-identical
    output to the pre-change implementation.
    """
    needle = (query or "").strip()
    if len(needle) < 3:
        return []

    phrases, remainder = split_quoted_phrases(needle)
    # Quoted spans are the most precise terms in the query, so they take the
    # front of the list, where the MAX_LEXICAL_TERMS cut and the CJK reserve
    # below cannot reach them.
    raw_terms: list[str] = list(phrases)
    # The whole-sentence term has to lose the quote characters: no document
    # contains them, so keeping them would spend a term (and, on PostgreSQL, a
    # real per-term probe) on a string that cannot match anything.
    sentence = strip_accepted_quote_markers(needle).strip() if phrases else needle
    if sentence and len(sentence) <= MAX_EXACT_PHRASE_CHARS:
        raw_terms.append(sentence)
    priority_count = len(raw_terms)
    ident_terms = identifier_terms(remainder)
    raw_terms.extend(ident_terms)

    run: list[str] = []
    run_kind = ""

    def flush() -> None:
        nonlocal run, run_kind
        if not run:
            return
        value = "".join(run)
        if run_kind == "cjk":
            if len(value) >= 3:
                raw_terms.append(value)
                raw_terms.extend(
                    value[offset:offset + 3]
                    for offset in range(len(value) - 2)
                )
        elif len(value) >= 3:
            raw_terms.append(value)
        run = []
        run_kind = ""

    # Run decomposition reads the REMAINDER, not the query: re-emitting a quoted
    # phrase's own words here is precisely the splitting the quotes forbid.
    # Without quotes the remainder is the query, character for character.
    for char in remainder:
        kind = "cjk" if _is_cjk(char) else "word" if char.isalnum() else ""
        if not kind:
            flush()
            continue
        if run and kind != run_kind:
            flush()
        run_kind = kind
        run.append(char)
    flush()

    terms: list[str] = []
    seen: set[str] = set()
    protected = 0
    for index, term in enumerate(raw_terms):
        normalized = term.strip()
        if len(normalized) < 3 or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
        if index < priority_count:
            # Counted after dedup so it indexes `terms`, not `raw_terms`: a
            # query that is nothing but one quoted phrase yields the same string
            # twice and must protect one slot, not two.
            protected += 1
    protected = max(1, protected)
    if len(terms) <= MAX_LEXICAL_TERMS:
        return terms
    keep = terms[:MAX_LEXICAL_TERMS]
    if not ident_terms:
        # No identifiers injected → historical truncation, byte-identical.
        return keep
    # Identifier terms sit ahead of the run terms, so on overflow they squeeze
    # the tail — which is where the CJK tri-gram terms live. A pasted command
    # list plus a Chinese question measurably lost 9 of 11 CJK terms; without
    # CJK terms the Chinese half of the question has no lexical recall path at
    # all. Reserve a bounded tail quota: pull overflowed CJK terms back in,
    # evicting the lowest-priority (last non-CJK, non-phrase) terms. Only
    # active when identifiers were injected — identifier-free queries keep the
    # historical output bit-for-bit.
    def _has_cjk(term: str) -> bool:
        return any(_is_cjk(char) for char in term)

    overflow_cjk = [t for t in terms[MAX_LEXICAL_TERMS:] if _has_cjk(t)]
    kept_cjk = sum(1 for t in keep if _has_cjk(t))
    need = min(len(overflow_cjk), max(0, CJK_RESERVED_TERMS - kept_cjk))
    for term in overflow_cjk[:need]:
        # Never evict the priority head: the whole-sentence term (slot 0 as
        # before) and, when the user quoted spans, the phrases ahead of it.
        for index in range(len(keep) - 1, protected - 1, -1):
            if not _has_cjk(keep[index]):
                del keep[index]
                keep.append(term)
                break
    return keep


def sqlite_fts_match_expression(query: str) -> str:
    """Quote every recall term so user input cannot become FTS5 syntax."""
    return " OR ".join(
        '"' + term.replace('"', '""') + '"'
        for term in lexical_recall_terms(query)
    )
