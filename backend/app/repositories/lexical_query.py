"""Backend-neutral lexical query decomposition.

The returned terms are candidates only: repository adapters keep their native
ranking, while retrieval services apply the authoritative fused score.
"""
from __future__ import annotations


MAX_LEXICAL_TERMS = 64
MAX_EXACT_PHRASE_CHARS = 256


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
        if len(terms) >= MAX_LEXICAL_TERMS:
            break
    return terms


def sqlite_fts_match_expression(query: str) -> str:
    """Quote every recall term so user input cannot become FTS5 syntax."""
    return " OR ".join(
        '"' + term.replace('"', '""') + '"'
        for term in lexical_recall_terms(query)
    )
