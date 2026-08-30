"""Compatibility re-export shim (Task 13; definitions sunk to app.domain in B3).

The knowledge status vocabularies and the KG-analysis size/bucket contracts
now live in ``app.domain.knowledge_contracts`` (a leaf module with zero
``app.services``/``app.repositories`` dependencies, so ``app.repositories``
adapters can import it directly without a reverse dependency on
``app.services``). This module re-exports every name unchanged so existing
importers (``app.services.sqlite_repository``'s frozen compatibility
surface, ``app.repositories.sqlite.notebook_store``, and the many services
that still spell it as ``app.services.knowledge_contracts``) keep resolving
to the SAME objects without any call-site changes.
"""
from __future__ import annotations

from app.domain.knowledge_contracts import (
    CLUSTER_OBJECT_TYPE_GROUPS,
    CLUSTER_OBJECT_TYPES,
    CLUSTER_SIZE_BUCKETS,
    COMMUNITY_OVERVIEW_MAX,
    COMMUNITY_TOP_MEMBERS_MAX,
    CONCEPT_DETAIL_PAGE_MAX,
    EMPTY_CLUSTER_BUCKET,
    KG_COMMUNITY_EDGES_MAX,
    KG_SOURCE_PAGE_MAX,
    KNOWLEDGE_STATUSES,
    KnowledgeGraphTooLargeError,
    LARGEST_CLUSTERS_MAX,
    OTHER_OBJECT_TYPE_GROUP,
    PromotionApproval,
    RELATION_EXCLUSION_BUCKETS,
    RELATION_PROVENANCE_BUCKETS,
    USABLE_STATUSES,
)

__all__ = [
    "CLUSTER_OBJECT_TYPE_GROUPS",
    "CLUSTER_OBJECT_TYPES",
    "CLUSTER_SIZE_BUCKETS",
    "COMMUNITY_OVERVIEW_MAX",
    "COMMUNITY_TOP_MEMBERS_MAX",
    "CONCEPT_DETAIL_PAGE_MAX",
    "EMPTY_CLUSTER_BUCKET",
    "KG_COMMUNITY_EDGES_MAX",
    "KG_SOURCE_PAGE_MAX",
    "KNOWLEDGE_STATUSES",
    "KnowledgeGraphTooLargeError",
    "LARGEST_CLUSTERS_MAX",
    "OTHER_OBJECT_TYPE_GROUP",
    "PromotionApproval",
    "RELATION_EXCLUSION_BUCKETS",
    "RELATION_PROVENANCE_BUCKETS",
    "USABLE_STATUSES",
]
