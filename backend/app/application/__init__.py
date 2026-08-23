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
from app.application.report_pipeline import (
    CommittedReport,
    FinalizedReportArtifact,
    GeneratedReportSections,
    PlannedReportOutline,
    ReportFinalAuditInput,
    ReportGenerationInput,
    ReportPlanningInput,
    ReportStageBoundaryError,
    ReportStageRuntime,
    execute_report_final_audit_stage,
    execute_report_generation_stage,
    execute_report_planning_stage,
    freeze_report_control,
    thaw_report_control,
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
    "CommittedReport",
    "FinalizedReportArtifact",
    "GeneratedReportSections",
    "PlannedReportOutline",
    "ReportFinalAuditInput",
    "ReportGenerationInput",
    "ReportPlanningInput",
    "ReportStageBoundaryError",
    "ReportStageRuntime",
    "execute_report_final_audit_stage",
    "execute_report_generation_stage",
    "execute_report_planning_stage",
    "freeze_report_control",
    "thaw_report_control",
]
