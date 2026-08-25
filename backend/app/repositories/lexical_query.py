"""Backend-neutral lexical query decomposition.

The returned terms are candidates only: repository adapters keep their native
ranking, while retrieval services apply the authoritative fused score.

The one thing NOT decomposed here is a user-quoted span; `app.core.query_syntax`
owns that parse and both this layer and the scoring layer read it from there.
"""
from __future__ import annotations

import re
from typing import Sequence

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
_COMPACT_MODEL_NAME_RE = re.compile(
    r"(?i)(?<![a-z0-9])([a-z]{2,})([0-9][a-z]*)(?![a-z0-9])"
)
_SPACED_MODEL_NAME_RE = re.compile(
    r"(?i)(?<![a-z0-9])([a-z]{2,})[\s._-]+([0-9][a-z]*)(?![a-z0-9])"
)
_NUMBERED_PROSE_LABELS = frozenset({
    "book", "chapter", "example", "figure", "item", "level", "page",
    "part", "phase", "point", "rule", "section", "step", "table",
    "version", "volume",
})


def spaced_model_name_parts(text: str) -> list[tuple[str, str]]:
    """High-confidence separated product-name parts, excluding prose labels."""
    return [
        (match.group(1), match.group(2))
        for match in _SPACED_MODEL_NAME_RE.finditer(text or "")
        if match.group(1).casefold() not in _NUMBERED_PROSE_LABELS
    ]


def model_name_alias_terms(text: str) -> list[str]:
    """Return separator variants for mixed ASCII letter/digit model names."""
    aliases: list[str] = []
    for match in _COMPACT_MODEL_NAME_RE.finditer(text or ""):
        aliases.append(f"{match.group(1)} {match.group(2)}")
    for stem, suffix in spaced_model_name_parts(text):
        aliases.append(f"{stem}{suffix}")
    return aliases


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
    return _recall_terms_with_head(query)[0]


def _recall_terms_with_head(query: str) -> tuple[list[str], int]:
    """`lexical_recall_terms` plus the size of its protected priority head.

    One derivation, two views. The term list is exactly what
    `lexical_recall_terms` has always returned — this is not a second spelling
    of the decomposition — and the extra number is the count of leading terms
    the truncation logic below already refuses to evict (the user's quoted
    spans and the whole-sentence term). `corpus_gated_recall_terms` needs that
    boundary so a corpus filter can only ever reach the tail; deriving it a
    second time from the query is exactly the drift this returns to prevent.
    """
    needle = (query or "").strip()
    if len(needle) < 3:
        return [], 0

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
    # Candidate recall mirrors scoring's compact alias.  Both spellings enter
    # the bounded shared term budget; neither becomes an exact-section probe.
    raw_terms.extend(model_name_alias_terms(remainder))

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
    # `protected` is the true head size and is what the corpus gate reads.
    # The eviction loop below keeps its historical floor of 1 so its output
    # stays byte-identical; the two numbers differ only for a query with no
    # quotes whose sentence overflowed MAX_EXACT_PHRASE_CHARS, where "protect
    # slot 0" was always an eviction-loop guard rather than a claim that the
    # first term is user-declared.
    floor = max(1, protected)
    if len(terms) <= MAX_LEXICAL_TERMS:
        return terms, protected
    keep = terms[:MAX_LEXICAL_TERMS]
    if not ident_terms:
        # No identifiers injected → historical truncation, byte-identical.
        return keep, protected
    # Identifier terms sit ahead of the run terms, so on overflow they squeeze
    # the tail — which is where the CJK tri-gram terms live. A pasted command
    # list plus a Chinese question measurably lost 9 of 11 CJK terms; without
    # CJK terms the Chinese half of the question has no lexical recall path at
    # all. Reserve a bounded tail quota: pull overflowed CJK terms back in,
    # evicting the lowest-priority (last non-CJK, non-phrase) terms. Only
    # active when identifiers were injected — identifier-free queries keep the
    # historical output bit-for-bit.
    overflow_cjk = [t for t in terms[MAX_LEXICAL_TERMS:] if _has_cjk(t)]
    kept_cjk = sum(1 for t in keep if _has_cjk(t))
    need = min(len(overflow_cjk), max(0, CJK_RESERVED_TERMS - kept_cjk))
    for term in overflow_cjk[:need]:
        # Never evict the priority head: the whole-sentence term (slot 0 as
        # before) and, when the user quoted spans, the phrases ahead of it.
        for index in range(len(keep) - 1, floor - 1, -1):
            if not _has_cjk(keep[index]):
                del keep[index]
                keep.append(term)
                break
    return keep, protected


def _has_cjk(term: str) -> bool:
    return any(_is_cjk(char) for char in term)


def _is_all_cjk(term: str) -> bool:
    """Every character is CJK — the exact shape the run decomposer emits.

    Deliberately stricter than `_has_cjk`, because the corpus gate below needs
    a PROOF of zero hits, not a likelihood. Pad an all-CJK term for trigram
    extraction and every one of its trigrams still contains a CJK character, so
    against a corpus holding no CJK character at all the trigram intersection
    is empty (`similarity` is 0, `%` is false) and a substring match is
    impossible. A *mixed* term such as `abc时序` shares the `abc` trigram and
    therefore carries no such proof, so it is never dropped.
    """
    return bool(term) and all(_is_cjk(char) for char in term)


def corpus_language_drops_cjk(corpus_langs: Sequence[str] | None) -> bool:
    """Would the corpus gate drop CJK terms for this notebook's languages?

    `None`/empty means "unknown", which never gates: the caller has to have
    actually probed the corpus for a term to be discarded.
    """
    return bool(corpus_langs) and "zh" not in corpus_langs


def corpus_gated_recall_terms(
    query: str, corpus_langs: Sequence[str] | None = None
) -> list[str]:
    """`lexical_recall_terms` minus the probes the corpus cannot possibly answer.

    A long Chinese question decomposes into ~60 CJK tri-gram terms. Every one
    of them is a real per-term probe on PostgreSQL, and against a corpus with
    no CJK character each probe scans the notebook's chunks, computes a
    trigram similarity per row, and returns nothing: 7,026 chunks × 64 terms
    measured at 29.7s for 26 rows, all 26 of which came from the two Latin
    terms. Four report sections in parallel then blow through
    POSTGRES_STATEMENT_TIMEOUT_SECONDS and the whole lexical arm dies
    fail-open — so the cost of these guaranteed-empty probes is not slowness,
    it is the silent loss of the lexical hits that *would* have matched.

    Three deliberate asymmetries:

    * Only the CJK direction. Dropping Latin terms against a Chinese-only
      corpus would be the mirror-image argument, and it is wrong in practice:
      Latin identifiers (`set_db`, `config.yaml`, product names, formulas) are
      ordinary content inside Chinese documents, so "the sampled chunks held
      no Latin letter" is a far weaker claim than "held no CJK character" —
      and `_notebook_langs` returns ["en"] for an empty/unknown corpus, which
      would turn the mirror rule into a filter that fires on a guess.
    * Only the tail. The priority head — the user's `"..."` spans and the
      whole-sentence term — is never filtered. A quoted span is the one part of
      the query the user explicitly declared must be matched verbatim
      (`app.core.query_syntax`), and the gate must not become a second, quieter
      truncation of the slots the existing MAX_LEXICAL_TERMS cut already
      refuses to touch.
    * Only provably-empty terms (`_is_all_cjk`), never merely CJK-containing
      ones.

    The corpus language is a SAMPLED probe (`_notebook_langs`, head ∪ tail of
    the chunk table), so a notebook whose only Chinese source sits in the
    unsampled middle loses CJK *lexical* recall until the sample sees it again;
    ANN/semantic recall is untouched, the same probe already decides whether
    Chinese keywords get minted at all for that notebook, and
    `LEXICAL_LANGUAGE_GATE_ENABLED=0` turns the whole thing off.
    """
    terms, head = _recall_terms_with_head(query)
    if not corpus_language_drops_cjk(corpus_langs):
        return terms
    return [
        term for index, term in enumerate(terms)
        if index < head or not _is_all_cjk(term)
    ]


def sqlite_fts_match_expression(
    query: str, corpus_langs: Sequence[str] | None = None
) -> str:
    """Quote every recall term so user input cannot become FTS5 syntax.

    The corpus gate applies here too. FTS5 answers a guaranteed-empty trigram
    term cheaply, so this side is about determinism rather than cost: both
    backends must probe the SAME term set, or one notebook's recall would
    depend on which adapter served it.
    """
    return " OR ".join(
        '"' + term.replace('"', '""') + '"'
        for term in corpus_gated_recall_terms(query, corpus_langs)
    )
