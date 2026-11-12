"""Whole ranking pages: preserve counts and cursor boundaries under the MCP cap."""
from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

from app.domain.concept_ranking_contracts import CONCEPT_RANK_CITATIONS_PER_ROW
from app.services.concept_ranking import ConceptRankPage
from app.services.concept_ranking_answer import ranking_notice

from ._shared import TOTAL_TEXT_LIMIT, AgentToolError, _serialized_size
from .refs import element_ref

# Display projection only; source names and all numeric counts remain intact.
CONCEPT_RANK_DISPLAY_NAME_CHARS = 200


def concept_rank_page(notebook_id: str, page: ConceptRankPage) -> dict[str, Any]:
    """Reduce evidence only; never silently drop ranked rows or clip a number."""
    coverage = page.computation.libraries[0].coverage
    notice, failures = ranking_notice(
        (SimpleNamespace(fingerprint=page.computation.fingerprint,
                         computation=page.computation, successful_pages=()),),
        cited_keys=frozenset(), key_owner={}, query_candidates=0, answer_follows=False,
    )
    for anchors in range(CONCEPT_RANK_CITATIONS_PER_ROW, 0, -1):
        refs: list[str] = []
        indexes: dict[tuple[str, str, str], int] = {}
        rows = []
        omitted = 0
        omitted_characters = 0
        for index, row in enumerate(page.page_rows() if coverage.status == "ok" else ()):
            row_refs = []
            omitted += max(0, len(row.evidence) - anchors)
            for evidence in row.evidence[:anchors]:
                key = (evidence.notebook_id, evidence.source_id, evidence.element_id)
                if key not in indexes:
                    indexes[key] = len(refs)
                    refs.append(element_ref(*key))
                if indexes[key] not in row_refs:
                    row_refs.append(indexes[key])
            name = row.name
            if len(name) > CONCEPT_RANK_DISPLAY_NAME_CHARS:
                omitted_characters += len(name) - CONCEPT_RANK_DISPLAY_NAME_CHARS
                name = name[:CONCEPT_RANK_DISPLAY_NAME_CHARS] + "…"
            rows.append({
                "rank": page.page_number * page.page_size + index + 1,
                "name": name, "source_count": row.source_count,
                "object_count": row.object_count, "same_name_group": row.same_name_group,
                "refs": row_refs,
            })
        payload = {
            "notebook_id": notebook_id, "query": page.computation.query,
            "path": coverage.path, "rows": rows, "refs": refs,
            "coverage": {**asdict(coverage), "next_cursor": page.next_cursor},
            "next_cursor": page.next_cursor,
        }
        if failures:
            payload["ranking_notice"] = notice
            payload["ranking_failures"] = [failure.model_dump() for failure in failures]
        if omitted or omitted_characters:
            payload["truncation"] = {"truncated": True, "omitted_items": omitted,
                                     "omitted_characters": omitted_characters}
        if _serialized_size(payload) <= TOTAL_TEXT_LIMIT:
            return payload
    raise AgentToolError(
        "internal", "结果超出 MCP 响应预算，请减小 limit、去掉 cursor 后从第一页重新排行"
    )
