"""Frozen knowledge-domain contracts (Task 13).

Canonical home of the knowledge status vocabularies and the knowledge-graph
size guard. ``app.services.sqlite_repository`` re-exports every name here as
its frozen compatibility surface (the manifest pins those export sites), and
``app.repositories.sqlite.notebook_store`` re-exports ``USABLE_STATUSES`` for
its Task-8 consumers — all references resolve to the SAME objects.
"""
from __future__ import annotations

from dataclasses import dataclass

# Knowledge statuses that may be surfaced in answers/retrieval (§12 governance).
# 'deprecated' is excluded; 'conflict' is retrieved but flagged elsewhere.
USABLE_STATUSES = ("approved", "reviewed", "project_specific", "conflict")

# Every status a curator can stamp on a knowledge object.
KNOWLEDGE_STATUSES = ("approved", "reviewed", "deprecated", "conflict", "project_specific")


class KnowledgeGraphTooLargeError(Exception):
    """Raised by knowledge_graph() (legacy GET /notebooks/{id}/graph) when the
    notebook exceeds settings.viz_sync_build_max_objects — that endpoint has
    no bounded fallback (unlike unified_graph), so it refuses outright rather
    than materializing an unbounded in-memory graph. The route maps this to
    HTTP 413."""


@dataclass(frozen=True)
class PromotionApproval:
    """Outcome of the in-transaction promotion approval primitive
    (GovernanceStore.approve_promotion_in_transaction)."""

    candidate_id: str
    source_notebook_id: str
    source_object_id: str
    base_notebook_id: str
    base_object_id: str
    created_new_object: bool
