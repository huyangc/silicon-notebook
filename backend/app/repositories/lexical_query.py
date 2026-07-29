"""Backend-neutral lexical query decomposition.

The returned terms are candidates only: repository adapters keep their native
ranking, while retrieval services apply the authoritative fused score.
"""
from __future__ import annotations

import re


MAX_LEXICAL_TERMS = 64
MAX_EXACT_PHRASE_CHARS = 256
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


def exact_probe_terms(text: str) -> list[str]:
    """The NARROWER gate: which of those identifiers deserve an exact probe.

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
    return [
        term for term in identifier_terms(text)
        if "_" in term or "." in term or _DIGIT_RE.search(term)
    ]


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2FA1F
    )


def lexical_recall_terms(query: str) -> list[str]:
    """Return an exact phrase plus independent Latin/number and CJK terms."""
    needle = (query or "").strip()
    if len(needle) < 3:
        return []

    raw_terms: list[str] = []
    if len(needle) <= MAX_EXACT_PHRASE_CHARS:
        raw_terms.append(needle)
    ident_terms = identifier_terms(needle)
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

    for char in needle:
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
    for term in raw_terms:
        normalized = term.strip()
        if len(normalized) < 3 or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
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
        for index in range(len(keep) - 1, 0, -1):   # never evict slot 0 (phrase)
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
