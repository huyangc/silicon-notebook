"""Typed application-pipeline contracts.

This package owns immutable stage envelopes.  Concrete orchestration remains
in the feature services until its behaviour has been characterised and moved;
the envelopes deliberately import neither repositories nor service
implementations.
"""

from app.application.ask_reasoning import (
    CommittedReasoningAnswer,
    ConnectionHoldProbe,
    PreparedReasoningAsk,
    ReasoningEvidenceSnapshot,
    ReasoningIntentProjection,
    ReasoningResponseDraft,
    ReasoningRetrievalRuntime,
    ReasoningRunInput,
    RetrievedReasoningAsk,
    StageBoundaryError,
    execute_reasoning_retrieval_stage,
)

__all__ = [
    "CommittedReasoningAnswer",
    "ConnectionHoldProbe",
    "PreparedReasoningAsk",
    "ReasoningEvidenceSnapshot",
    "ReasoningIntentProjection",
    "ReasoningResponseDraft",
    "ReasoningRetrievalRuntime",
    "ReasoningRunInput",
    "RetrievedReasoningAsk",
    "StageBoundaryError",
    "execute_reasoning_retrieval_stage",
]
