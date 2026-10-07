"""The promotion source type and its titles (PR-E8, ledger B-12).

A public library's synthetic ``source_type = 'promotion'`` source -- one per
original of an approved promotion -- is described in
``app.domain.promotion_provenance`` (the rewrite rule, the ids). This leaf
module holds only what READERS need (the type value, the title spelling and its
inverse), so a reader such as the citation-card title rule does not import the
write-side rewrite planner.
"""
from __future__ import annotations

#: ``sources.source_type`` of a public library's synthetic promotion source.
PROMOTION_SOURCE_TYPE = "promotion"

PROMOTION_TITLE_PREFIX = "晋升自："
MEMORY_PROMOTION_TITLE_PREFIX = "晋升自个人记忆："


def promotion_source_title(origin_title: str) -> str:
    return PROMOTION_TITLE_PREFIX + str(origin_title or "")


def memory_promotion_source_title(memory_title: str) -> str:
    return MEMORY_PROMOTION_TITLE_PREFIX + str(memory_title or "")


def promotion_origin_title(title: str) -> str:
    """The original's title inside a promotion source title (what a citation
    card shows); a title without either prefix (renamed) is returned as is."""
    value = str(title or "")
    for prefix in (MEMORY_PROMOTION_TITLE_PREFIX, PROMOTION_TITLE_PREFIX):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def is_promotion_source_type(source_type: object) -> bool:
    return str(source_type or "") == PROMOTION_SOURCE_TYPE
