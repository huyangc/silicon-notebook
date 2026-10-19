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

#: Every promotion source id starts with this (content-addressed ids:
#: ``promotion_provenance.promotion_source_id``, 0068, v88), and no other
#: source id does -- so a reader holding only ids can tell one apart.
PROMOTION_SOURCE_ID_PREFIX = "src-promo-"


def is_promotion_source_id(source_id: object) -> bool:
    return str(source_id or "").startswith(PROMOTION_SOURCE_ID_PREFIX)

#: What a new promotion source is titled (2026-10-19 ruling: user-facing words
#: say 「收录」, never 「晋升」; ``docs/ui-vocabulary.md``).
PROMOTION_TITLE_PREFIX = "收录自："
MEMORY_PROMOTION_TITLE_PREFIX = "收录自个人记忆："

#: What promotion sources written before that ruling are titled IN STORAGE
#: (#826: the runtime path, SQLite v88 and PostgreSQL 0068 -- the two sealed
#: migrations keep writing this spelling).  Stored titles are never rewritten;
#: every reader maps them (``presented_source_title``), and the sealed
#: migrations pin these constants.
LEGACY_PROMOTION_TITLE_PREFIX = "晋升自："
LEGACY_MEMORY_PROMOTION_TITLE_PREFIX = "晋升自个人记忆："
_LEGACY_STEM = "晋升自"  # what both legacy prefixes start with (cheap pre-check)

#: Neither legacy prefix is a prefix of the other (the fourth character
#: differs); the pairs are still ordered Memory-first so no reader has to check.
_LEGACY_TO_CURRENT = (
    (LEGACY_MEMORY_PROMOTION_TITLE_PREFIX, MEMORY_PROMOTION_TITLE_PREFIX),
    (LEGACY_PROMOTION_TITLE_PREFIX, PROMOTION_TITLE_PREFIX),
)


def promotion_source_title(origin_title: str) -> str:
    return PROMOTION_TITLE_PREFIX + str(origin_title or "")


def memory_promotion_source_title(memory_title: str) -> str:
    return MEMORY_PROMOTION_TITLE_PREFIX + str(memory_title or "")


def current_promotion_title(title: object) -> str:
    """A promotion source title in today's spelling: a stored title that still
    carries a legacy prefix is mapped, anything else is returned as is.  Only
    the LEADING prefix is replaced -- a body that mentions 「晋升」 is untouched.
    Not gated on the source's type; use ``presented_source_title`` on a source
    row."""
    value = str(title or "")
    for legacy, current in _LEGACY_TO_CURRENT:
        if value.startswith(legacy):
            return current + value[len(legacy):]
    return value


def presented_source_title(
    title: object, *, source_type: object = None, source_id: object = None
) -> str:
    """THE one reader-side mapping of a stored source title to the title the
    user sees: a PROMOTION source (by type, or by its content-addressed id)
    whose stored title still carries a legacy prefix is shown with today's
    prefix; every other source -- including an uploaded document that happens
    to be titled 「晋升自：…」 -- is untouched.  Every surface that names a source
    from its stored title goes through here (``source_display_title``, the
    source models, global search, the activity list, the share preview)."""
    value = str(title or "")
    if not value.startswith(_LEGACY_STEM):
        return value
    if is_promotion_source_type(source_type) or is_promotion_source_id(source_id):
        return current_promotion_title(value)
    return value


def promotion_origin_title(title: str) -> str:
    """The original's title inside a promotion source title (what a citation
    card shows); a title without any promotion prefix (renamed) is returned as
    is.  Both spellings are recognised: stored titles are not rewritten."""
    value = str(title or "")
    for prefix in (
        MEMORY_PROMOTION_TITLE_PREFIX, PROMOTION_TITLE_PREFIX,
        LEGACY_MEMORY_PROMOTION_TITLE_PREFIX, LEGACY_PROMOTION_TITLE_PREFIX,
    ):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def is_promotion_source_type(source_type: object) -> bool:
    return str(source_type or "") == PROMOTION_SOURCE_TYPE


class PromotionSourceNotExtractable(ValueError):
    """KG extraction refused for a promotion source: it has no document, and
    re-extracting it would first delete the promoted objects its elements
    support (``SourceIngestionService.run_extraction`` is the one gate every
    extraction caller -- reextract CLI, ``extract_source``, jobs -- passes)."""

    def __init__(self, source_id: str) -> None:
        super().__init__(
            f"source {source_id} holds content promoted into a public library "
            "and is never extracted"
        )
