"""Dependency-light public contracts for deployment indexing-pipeline strategies.

The runtime-owned value objects live in ``app.domain.indexing_pipeline`` so
core services can validate plugin output without importing the SDK facade.
This module keeps the stable SDK import path for plugin authors.
"""
from __future__ import annotations

from app.domain.indexing_pipeline import (
    IndexingChunkContext,
    IndexingChunkProposal,
    IndexingKgEdgeProposal,
    IndexingKgEdgeType,
    IndexingKgElement,
    IndexingKgFragment,
    IndexingKgMessage,
    IndexingKgObjectProposal,
    IndexingKgPrompt,
    IndexingKgPromptContext,
    IndexingKgStepProposal,
    IndexingPipelineAvailabilityContext,
    IndexingPipelineDescriptor,
    IndexingPipelineProvider,
    IndexingSourceElement,
)


INDEXING_PIPELINE_POINT = "indexing.pipeline"


__all__ = [
    "INDEXING_PIPELINE_POINT",
    "IndexingChunkContext",
    "IndexingChunkProposal",
    "IndexingKgEdgeProposal",
    "IndexingKgEdgeType",
    "IndexingKgElement",
    "IndexingKgFragment",
    "IndexingKgMessage",
    "IndexingKgObjectProposal",
    "IndexingKgPrompt",
    "IndexingKgPromptContext",
    "IndexingKgStepProposal",
    "IndexingPipelineAvailabilityContext",
    "IndexingPipelineDescriptor",
    "IndexingPipelineProvider",
    "IndexingSourceElement",
]
