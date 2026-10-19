"""SQL fragments that let a title search see a promotion source under the
CURRENT wording (「收录自：…」) while its stored title still says 「晋升自：…」.

Stored titles are never rewritten (2026-10-19 wording ruling); readers map them
(``app.domain.promotion_source.presented_source_title``).  A search that
matches the stored title would therefore miss a query for 「收录自」.  These
fragments add a branch that matches the MAPPED title, for promotion sources only
(type ``promotion`` or a ``src-promo-`` id), identically on both backends.

The branch is added ONLY when the needle can tell the two spellings apart --
it contains one of the characters that differ between the current and the legacy
prefix (``PROMOTION_WORDING_CHARS``, 「收」「录」).  Any other needle matches the
mapped title exactly when it matches the stored one (the rest of the title is
identical), so for every ordinary query the SQL text is byte-identical to what it
was before, and so are its plans and indexes (trigram GIN on PostgreSQL).
"""
from __future__ import annotations

from app.domain.promotion_source import (
    LEGACY_MEMORY_PROMOTION_TITLE_PREFIX,
    LEGACY_PROMOTION_TITLE_PREFIX,
    MEMORY_PROMOTION_TITLE_PREFIX,
    PROMOTION_SOURCE_ID_PREFIX,
    PROMOTION_SOURCE_TYPE,
    PROMOTION_TITLE_PREFIX,
)

#: Characters of the current prefixes that the legacy ones do not have.
PROMOTION_WORDING_CHARS = frozenset(
    (set(PROMOTION_TITLE_PREFIX) | set(MEMORY_PROMOTION_TITLE_PREFIX))
    - (set(LEGACY_PROMOTION_TITLE_PREFIX) | set(LEGACY_MEMORY_PROMOTION_TITLE_PREFIX))
)


def needle_names_current_wording(needle: str) -> bool:
    """True when a title search for ``needle`` needs the mapped-title branch."""
    return any(char in PROMOTION_WORDING_CHARS for char in str(needle or ""))


def promotion_gate_sql(type_column: str, id_column: str) -> str:
    """Predicate: the row is a promotion source (a literal-only fragment, so it
    needs no placeholder and no ``%`` escaping on either driver)."""
    return (
        f"({type_column} = '{PROMOTION_SOURCE_TYPE}' OR "
        f"substr({id_column}, 1, {len(PROMOTION_SOURCE_ID_PREFIX)}) = "
        f"'{PROMOTION_SOURCE_ID_PREFIX}')"
    )


def mapped_title_sql(title_column: str) -> str:
    """The title in today's spelling: the leading legacy prefix replaced, the
    rest untouched (``current_promotion_title`` in SQL; ``substr`` and ``||``
    are the same on SQLite and PostgreSQL)."""
    t = title_column
    memory_n = len(LEGACY_MEMORY_PROMOTION_TITLE_PREFIX)
    plain_n = len(LEGACY_PROMOTION_TITLE_PREFIX)
    return (
        f"(CASE WHEN substr({t}, 1, {memory_n}) = "
        f"'{LEGACY_MEMORY_PROMOTION_TITLE_PREFIX}' THEN "
        f"'{MEMORY_PROMOTION_TITLE_PREFIX}' || substr({t}, {memory_n + 1}) "
        f"WHEN substr({t}, 1, {plain_n}) = '{LEGACY_PROMOTION_TITLE_PREFIX}' THEN "
        f"'{PROMOTION_TITLE_PREFIX}' || substr({t}, {plain_n + 1}) "
        f"ELSE {t} END)"
    )
