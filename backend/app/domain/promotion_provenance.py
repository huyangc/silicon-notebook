"""Promotion provenance (PR-E8, ledger B-12): a knowledge object promoted into a
public library carries the public library's OWN provenance.

Before this, an approved promotion copied the personal object's evidence as it
was: every entry still pointed at a source and an element of the promoter's
private notebook.  A mounted public library only opens its VISIBLE sources to a
run's ceiling (plan D1), so such an object was pruned from every mounted or
global run (B-12), and its citation could only be shown as a pointer-free
snapshot (B-11, ``domain.citation_origin.owned_by_another_library``).

The rule, defined once here and applied by both backends' approval paths
(``GovernanceStore.approve_promotion_in_transaction`` /
``approve_memory_promotion_in_transaction``) and by the stored-data migration
(PostgreSQL ``0068_promotion_provenance.sql`` / SQLite ``_migration_88``):

* An evidence entry is FOREIGN when its ``source_id`` is not a source of the
  public library itself (another notebook's source, or one that no longer
  exists).  An entry that already names one of the library's own sources -- a
  native document, or a promotion source written earlier -- is kept as it is,
  which is what makes the rewrite idempotent.
* Each foreign entry is grouped by its ORIGINAL (``source_origin_key``: the
  original source id; a Memory promotion: ``memory_origin_key`` of the Memory),
  and every group gets ONE synthetic source in the public library,
  ``source_type = 'promotion'`` (visible: it is listed, enters the ceiling of a
  mounted run, can be deleted, cannot be re-parsed), titled
  ``晋升自：<original title>`` (a Memory promotion: ``晋升自个人记忆：<Memory
  title>``; approval publishes it, by design).
* Each foreign entry becomes one element of that source.  Its text is the
  original element's CURRENT text when the same transaction can still read it
  (the element exists, belongs to the source the entry names, and that source
  is not a member's Memory -- the stores never read a Memory element here),
  otherwise the entry's stored ``quoted_span``; an entry with neither is
  dropped (an object
  left without evidence is then dropped by every ceiling: fail closed).
* The entry is rewritten to point at the new source and element; the original
  ``source_id`` and its notebook stay on the entry only as display keys
  (``origin_source_id`` / ``origin_notebook_id``); the stored ``source_title``
  (the original title, which citation cards show) is kept.
* Ids are content-addressed (``promotion_source_id`` / ``promotion_element_id``)
  so the runtime path and the migration of either backend produce the same rows,
  and a second promotion from the same original reuses them (``INSERT`` that
  ignores an existing id).

Only dependency-free helpers live here; the stores do the reads and writes.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import AbstractSet, Any, Mapping, Optional, Sequence, Tuple

#: ``sources.source_type`` of a public library's synthetic promotion source.
PROMOTION_SOURCE_TYPE = "promotion"

PROMOTION_TITLE_PREFIX = "晋升自："
MEMORY_PROMOTION_TITLE_PREFIX = "晋升自个人记忆："

#: Display keys a rewritten evidence entry keeps for its original.
ORIGIN_SOURCE_KEY = "origin_source_id"
ORIGIN_NOTEBOOK_KEY = "origin_notebook_id"

#: ``source_elements.metadata`` key of a promotion element.
ELEMENT_METADATA_KEY = "promotion"

#: A rewritten entry without a stored excerpt quotes this many characters of the
#: element text (the same bound ``safe_memory_evidence`` applies).
EXCERPT_CHARS = 500

#: Element type of an entry that names none.
DEFAULT_ELEMENT_TYPE = "paragraph"

_KEY_SEPARATOR = "|"


def _digest(*parts: str) -> str:
    return hashlib.md5(_KEY_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


def source_origin_key(origin_source_id: str) -> str:
    """Group key of a generic promotion: the original source id."""
    return str(origin_source_id or "")


def memory_origin_key(memory_id: str) -> str:
    """Group key of a Memory promotion: the Memory itself is the original."""
    return f"memory:{memory_id}"


def promotion_source_id(base_notebook_id: str, origin_key: str) -> str:
    """``src-promo-`` + md5(``<library>|<origin key>``)."""
    return "src-promo-" + _digest(str(base_notebook_id), str(origin_key))


def promotion_element_id(source_id: str, origin_element_id: str, text: str) -> str:
    """``el-promo-`` + md5(``<promotion source>|<original element>|<text>``)."""
    return "el-promo-" + _digest(str(source_id), str(origin_element_id), str(text))


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


@dataclass(frozen=True)
class OriginElement:
    """A live element read in the approving transaction."""

    source_id: str
    text: str


@dataclass(frozen=True)
class PromotionSourceRow:
    id: str
    title: str


@dataclass(frozen=True)
class PromotionElementRow:
    id: str
    source_id: str
    element_type: str
    location_label: str
    text: str
    metadata: dict


@dataclass(frozen=True)
class PromotionPlan:
    """The rewritten evidence plus the rows it needs (first-seen order)."""

    evidence: list
    sources: Tuple[PromotionSourceRow, ...]
    elements: Tuple[PromotionElementRow, ...]
    #: foreign entries rewritten / dropped (neither live text nor a quote)
    rewritten: int = 0
    dropped: int = 0


def evidence_source_ids(evidence: Sequence[Any]) -> list[str]:
    """Distinct non-empty ``source_id`` values of the dict entries, in order."""
    out: dict[str, None] = {}
    for item in evidence or ():
        if isinstance(item, dict):
            value = str(item.get("source_id") or "")
            if value:
                out.setdefault(value, None)
    return list(out)


def foreign_element_ids(
    evidence: Sequence[Any], own_source_ids: AbstractSet[str]
) -> list[str]:
    """Distinct non-empty ``element_id`` values of the foreign dict entries."""
    out: dict[str, None] = {}
    for item in evidence or ():
        if not isinstance(item, dict):
            continue
        if str(item.get("source_id") or "") in own_source_ids:
            continue
        value = str(item.get("element_id") or "")
        if value:
            out.setdefault(value, None)
    return list(out)


def plan_promotion_evidence(
    base_notebook_id: str,
    evidence: Sequence[Any],
    *,
    own_source_ids: AbstractSet[str],
    source_notebooks: Mapping[str, str],
    origin_elements: Mapping[str, OriginElement],
    fallback_origin_notebook_id: str = "",
    memory: Optional[Tuple[str, str]] = None,
) -> PromotionPlan:
    """Rewrite ``evidence`` for ``base_notebook_id`` (module docstring).

    ``own_source_ids``: the entries' source ids that belong to the library.
    ``source_notebooks``: live source id -> its notebook (the origin notebook).
    ``origin_elements``: live element id -> its source and current text.
    ``memory``: ``(memory_id, memory_title)`` for a Memory promotion.
    Non-dict items and own entries are kept unchanged, in place.
    """
    rewritten: list = []
    sources: dict[str, PromotionSourceRow] = {}
    elements: dict[str, PromotionElementRow] = {}
    kept = dropped = 0
    for item in evidence or ():
        if not isinstance(item, dict):
            rewritten.append(item)
            kept += 1
            continue
        origin_source_id = str(item.get("source_id") or "")
        if origin_source_id in own_source_ids:
            rewritten.append(item)
            kept += 1
            continue
        origin_element_id = str(item.get("element_id") or "")
        live = origin_elements.get(origin_element_id) if origin_element_id else None
        live_text = (
            live.text if live is not None and live.source_id == origin_source_id
            else ""
        )
        stored_span = item.get("quoted_span")
        stored_span = stored_span if isinstance(stored_span, str) else ""
        text = live_text or stored_span
        if not text:
            dropped += 1
            continue
        if memory is not None:
            source_id = promotion_source_id(base_notebook_id, memory_origin_key(memory[0]))
            title = memory_promotion_source_title(memory[1])
        else:
            source_id = promotion_source_id(
                base_notebook_id, source_origin_key(origin_source_id)
            )
            stored_title = item.get("source_title")
            title = promotion_source_title(
                stored_title if isinstance(stored_title, str) else ""
            )
        sources.setdefault(source_id, PromotionSourceRow(source_id, title))
        origin_notebook_id = (
            source_notebooks.get(origin_source_id) or fallback_origin_notebook_id or ""
        )
        element_id = promotion_element_id(source_id, origin_element_id, text)
        element_type = item.get("element_type")
        location_label = item.get("location_label")
        elements.setdefault(element_id, PromotionElementRow(
            id=element_id,
            source_id=source_id,
            element_type=(
                element_type if isinstance(element_type, str) and element_type
                else DEFAULT_ELEMENT_TYPE
            ),
            location_label=location_label if isinstance(location_label, str) else "",
            text=text,
            metadata={ELEMENT_METADATA_KEY: {
                ORIGIN_SOURCE_KEY: origin_source_id,
                "origin_element_id": origin_element_id,
                ORIGIN_NOTEBOOK_KEY: origin_notebook_id,
            }},
        ))
        rewritten.append({
            **item,
            "source_id": source_id,
            "element_id": element_id,
            "quoted_span": stored_span or text[:EXCERPT_CHARS],
            ORIGIN_SOURCE_KEY: origin_source_id,
            ORIGIN_NOTEBOOK_KEY: origin_notebook_id,
        })
    return PromotionPlan(
        evidence=rewritten,
        sources=tuple(sources.values()),
        elements=tuple(elements.values()),
        rewritten=len(rewritten) - kept,
        dropped=dropped,
    )
