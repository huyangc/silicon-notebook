"""Prepare directory evidence for overview answers using shared enumeration rails."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any

from app.core.ask_retrieval_policy import AskRetrievalLimits
from app.models.ask import Citation, TypedCollectionResult
from app.services.cancellation import CancelEvent, raise_if_cancelled
from app.services.collection_enumeration import EnumerationBudget, SourceItem
from app.services.collection_enumeration_answer import (
    apply_synthesis_preview_counts,
    delivered_outcomes,
    enumeration_prompt_block,
    typed_collection_results,
)
from app.services.reasoning_retrieval import CollectionEnumerationOutcome


_SUMMARY_GUIDANCE = (
    "[Directory overview: document rows contain stored summary excerpts, not "
    "verified full text. Explain each document only from its supplied summary "
    "or explicitly supplied supplemental original excerpts; "
    "a title alone does not establish its contents. Explicitly disclose missing "
    "summaries and that directory completeness does not mean full-text analysis.]"
)


@dataclass
class CatalogOverview:
    context_block: str
    id_map: dict[str, dict[str, Any]]
    citations: dict[str, Citation]
    result_sets: list[TypedCollectionResult]
    coverage_note: str
    items: list[SourceItem]
    active_notebook_id: str = ""


def catalog_coverage_note(result: TypedCollectionResult, *, attempted: bool = True) -> str:
    coverage = result.coverage
    total = str(coverage.total) if coverage.total is not None else "未知"
    synthesis = (f"本次合成展示 {result.synthesis_rows} 篇的目录信息。" if attempted
                 else "尚未调用模型生成文档介绍。")
    return (
        f"文档目录已返回 {coverage.returned_total}/{total} 篇，" + synthesis
        + "介绍依据已存摘要摘录；缺少摘要的文档不能仅凭标题判断正文内容。"
        "目录列全不等于已阅读全文。"
    )


def prepare_catalog_overview(
    enumeration: Any,
    evidence_context: Any,
    notebook_id: str,
    limits: AskRetrievalLimits,
    budget_chars: int,
    cancel_event: CancelEvent = None,
    *,
    local_only: bool = False,
) -> CatalogOverview:
    """List authorized sources and project the delivered list into bounded context.

    Enumeration owns scope and traversal; the answer projection owns transport
    trimming and preview counts. This helper only composes those existing seams.
    ``citations`` is keyed by source identity; ``id_map`` preserves the preview's
    k5001+ namespace for the answer citation binder.
    """
    raise_if_cancelled(cancel_event)
    listing = enumeration.enumerate_sources(
        notebook_id,
        budget=EnumerationBudget(
            page_size=limits.enum_page_size,
            max_rows=limits.enum_rows_per_run,
            max_pages=limits.enum_pages_per_run,
            max_payload_chars=limits.structured_payload_chars,
            excerpt_chars=limits.cell_excerpt_chars,
        ),
        cancel_event=cancel_event,
        local_only=local_only,
    )
    raise_if_cancelled(cancel_event)
    outcomes = [CollectionEnumerationOutcome(
        collection="sources", kind="", source_id="", local_only=local_only,
        items=list(listing.items), coverage=listing.coverage,
    )]
    citations = evidence_context.collection_item_citations(
        listing.items, active_notebook_id=notebook_id,
    )
    raise_if_cancelled(cancel_event)
    results = typed_collection_results(
        outcomes, payload_chars=limits.structured_payload_chars,
        citations_by_item_id=citations,
    )
    views = delivered_outcomes(outcomes, results)
    delivered_items = list(views[0].items)
    # Labels belong only to the synthesis view: retain stored card data intact.
    views = [replace(view, items=[
        replace(item, summary=(
            f"已存摘要摘录：{item.summary}" if item.summary.strip()
            else "暂无已存摘要，不能仅凭标题介绍正文内容。"
        )) for item in view.items
    ]) for view in views]
    wrapper = _SUMMARY_GUIDANCE + "\n\n"
    preview = enumeration_prompt_block(
        views, inline_rows=limits.inline_answer_rows,
        budget_chars=max(0, int(budget_chars) - len(wrapper)),
        citations_by_item_id=citations,
    )
    apply_synthesis_preview_counts(results, preview.shown_rows)
    result = results[0]
    note = catalog_coverage_note(result)
    raise_if_cancelled(cancel_event)
    return CatalogOverview(
        context_block=wrapper + preview.text if preview.text else "",
        id_map=preview.evidence_by_id,
        citations={item.item_id: citations[item.item_id]
                   for item in result.items if item.item_id in citations},
        result_sets=results,
        coverage_note=note,
        items=delivered_items,
        active_notebook_id=notebook_id,
    )


def supplement_missing_summaries(
    catalog: CatalogOverview, sources: Any, *, budget_chars: int,
    max_elements: int, generation_reader: Any, cancel_event: CancelEvent = None,
) -> None:
    """Share the remaining context and element budget across previewed empty rows.

    The directory remains the coverage authority; original excerpts never turn
    its stored summary or source card into a fabricated ingestion result.
    """
    from app.services.document_source_overview import prepare_source_overview

    preview_sources = {entry.get("source_id") for entry in catalog.id_map.values()}
    missing = [item for item in catalog.items
               if not item.summary.strip() and item.source_id in preview_sources]
    remaining_elements = max_elements
    for index, item in enumerate(missing):
        raise_if_cancelled(cancel_event)
        remaining_rows = len(missing) - index
        element_share = remaining_elements // remaining_rows
        directory_key = next(key for key, entry in catalog.id_map.items()
                             if entry.get("object_type") == "source" and entry.get("object_id") == item.source_id)
        header = f"\n\n[Supplemental original excerpts for document {directory_key}; bounded sampling, not a full reading] " + json.dumps(
            item.source_title, ensure_ascii=False,
        ).replace("[", "［").replace("]", "］") + "\n"
        char_share = (budget_chars - len(catalog.context_block)) // remaining_rows - len(header)
        if element_share <= 0 or char_share <= 0:
            continue
        key_offset = max((int(key[1:]) for key in catalog.id_map), default=0)
        original = prepare_source_overview(
            sources, item, char_share, element_share, cancel_event,
            generation_reader=generation_reader, key_offset=key_offset,
            active_notebook_id=catalog.active_notebook_id,
        )
        remaining_elements -= element_share
        if original.context_block:
            catalog.context_block += header + original.context_block
            catalog.id_map.update(original.id_map)
            catalog.citations.update({c.element_id: c for c in original.citations})
        catalog.coverage_note += "\n目录中缺少摘要的文档：" + original.coverage_note
