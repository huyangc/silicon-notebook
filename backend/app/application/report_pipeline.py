"""Immutable ownership envelopes for the Deep Report application pipeline.

The existing Report engine remains the concrete orchestrator.  These contracts
make its real confirmed-planning -> section generation -> core final audit ->
terminal commit hand-offs explicit without exposing repositories, model
clients, retrieval ports, mutable plugin contexts, or a new leaf-I/O owner.
Large evidence graphs are transferred by exclusive ownership; they are never
serialized or recursively copied merely to cross a stage boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping, Protocol

from app.domain.cancellation import CancelEvent


def freeze_report_control(value: object) -> object:
    """Recursively snapshot the JSON-shaped Report control plane.

    Evidence graphs deliberately do not use this helper: they cross the later
    generation/final-audit boundary by exclusive typed ownership so citation
    and domain-object identity remains intact.
    """
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ReportStageBoundaryError("Report control keys must be strings")
        return MappingProxyType(
            {key: freeze_report_control(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_report_control(item) for item in value)
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ReportStageBoundaryError("Report control value is not JSON-shaped")


def thaw_report_control(value: object) -> object:
    """Restore frozen JSON control data to the legacy dict/list shape."""
    if isinstance(value, Mapping):
        return {key: thaw_report_control(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_report_control(item) for item in value]
    return value


class ReportStageBoundaryError(RuntimeError):
    """A core Report authority or ownership invariant was violated."""


class ReportConnectionHoldProbe(Protocol):
    def is_connection_held(self) -> bool: ...


class ReportStageExecutor(Protocol):
    def run_planning_stage(
        self,
        stage: "ReportPlanningInput",
        runtime: "ReportStageRuntime",
    ) -> "PlannedReportOutline | None": ...

    def run_generation_stage(
        self,
        stage: "ReportGenerationInput",
        runtime: "ReportStageRuntime",
    ) -> "GeneratedReportSections": ...

    def run_final_audit_stage(
        self,
        stage: "ReportFinalAuditInput",
        runtime: "ReportStageRuntime",
    ) -> "FinalizedReportArtifact": ...


@dataclass(frozen=True, slots=True)
class ReportStageRuntime:
    """Point-specific authorities for one planning or generation run.

    ``retrieval_run`` owns the request-local embedding single-flight and leaf
    semaphore.  Stage wrappers validate its identity but never acquire an
    outer slot.  ``scope`` is the exact active source-scope object (or ``None``
    for the backward-compatible whole-scope path), not a reconstructed copy.
    """

    scope: object | None
    retrieval_run: object
    cancellation: CancelEvent
    connection_probe: ReportConnectionHoldProbe
    actor_id: str


@dataclass(frozen=True, slots=True)
class ReportPlanningInput:
    notebook_id: str
    report_id: str
    question: str
    history: str
    actor_id: str
    confirmed_intent: Mapping[str, object] | None

    @classmethod
    def create(
        cls,
        *,
        notebook_id: str,
        report_id: str,
        question: str,
        history: str,
        actor_id: str,
        confirmed_intent: Mapping[str, object] | None,
    ) -> "ReportPlanningInput":
        intent = freeze_report_control(confirmed_intent)
        return cls(
            notebook_id,
            report_id,
            question,
            history,
            actor_id,
            intent,
        )


@dataclass(frozen=True, slots=True)
class PlannedReportOutline:
    planning: ReportPlanningInput
    outline: tuple[Mapping[str, object], ...]

    @classmethod
    def create(
        cls,
        planning: ReportPlanningInput,
        outline: list[dict[str, object]],
    ) -> "PlannedReportOutline":
        return cls(
            planning,
            tuple(freeze_report_control(section) for section in outline),
        )


@dataclass(frozen=True, slots=True)
class ReportGenerationInput:
    notebook_id: str
    report_id: str
    original_question: str
    display_question: str
    research_question: str
    depth: int
    actor_id: str
    understanding: Mapping[str, object]
    outline: tuple[Mapping[str, object], ...]

    @classmethod
    def create(
        cls,
        *,
        notebook_id: str,
        report_id: str,
        original_question: str,
        display_question: str,
        research_question: str,
        depth: int,
        actor_id: str,
        understanding: Mapping[str, object],
        outline: list[dict[str, object]],
    ) -> "ReportGenerationInput":
        return cls(
            notebook_id,
            report_id,
            original_question,
            display_question,
            research_question,
            depth,
            actor_id,
            freeze_report_control(understanding),
            tuple(freeze_report_control(section) for section in outline),
        )


@dataclass(frozen=True, slots=True)
class GeneratedReportSections:
    generation: ReportGenerationInput
    sections: tuple[Mapping[str, object], ...]
    successful_count: int
    synthesis_status: str

    @classmethod
    def create(
        cls,
        generation: ReportGenerationInput,
        sections: list[dict[str, object]],
    ) -> "GeneratedReportSections":
        successful_count = sum(
            bool(str(section.get("markdown") or "").strip())
            and not bool(section.get("failed"))
            for section in sections
        )
        synthesis_status = next(
            (
                str(section.get("_synthesis_status") or "")
                for section in sections
                if section.get("_synthesis_status")
            ),
            "not_requested",
        )
        return cls(
            generation,
            tuple(MappingProxyType(section) for section in sections),
            successful_count,
            synthesis_status,
        )


@dataclass(frozen=True, slots=True)
class ReportFinalAuditInput:
    generated: GeneratedReportSections


@dataclass(frozen=True, slots=True)
class ReportAuditFacts:
    section_count: int
    successful_section_count: int
    failed_section_count: int
    reference_count: int
    gap_count: int
    claim_ledgers_available: int
    claim_ledgers_partial: int
    unsupported_high_risk_assertions: int
    content_chars: int
    synthesis_status: str


@dataclass(frozen=True, slots=True)
class FinalizedReportArtifact:
    generation: ReportGenerationInput
    sections: tuple[Mapping[str, object], ...]
    content_md: str
    gaps: tuple[str, ...]
    references: tuple[Mapping[str, object], ...]
    audit_facts: ReportAuditFacts


@dataclass(frozen=True, slots=True)
class CommittedReport:
    notebook_id: str
    report_id: str
    actor_id: str
    audit_facts: ReportAuditFacts


def execute_report_planning_stage(
    executor: ReportStageExecutor,
    stage: ReportPlanningInput,
    runtime: ReportStageRuntime,
) -> PlannedReportOutline | None:
    runner = getattr(executor, "run_planning_stage", None)
    if not callable(runner):
        raise ReportStageBoundaryError("Report planner has no typed stage runner")
    result = runner(stage, runtime)
    if result is not None:
        if (
            type(result) is not PlannedReportOutline
            or result.planning is not stage
        ):
            raise ReportStageBoundaryError("invalid Report planning stage output")
    return result


def execute_report_generation_stage(
    executor: ReportStageExecutor,
    stage: ReportGenerationInput,
    runtime: ReportStageRuntime,
) -> GeneratedReportSections:
    runner = getattr(executor, "run_generation_stage", None)
    if not callable(runner):
        raise ReportStageBoundaryError("Report generator has no typed stage runner")
    result = runner(stage, runtime)
    if (
        type(result) is not GeneratedReportSections
        or result.generation is not stage
    ):
        raise ReportStageBoundaryError("invalid Report generation stage output")
    return result


def execute_report_final_audit_stage(
    executor: ReportStageExecutor,
    stage: ReportFinalAuditInput,
    runtime: ReportStageRuntime,
) -> FinalizedReportArtifact:
    runner = getattr(executor, "run_final_audit_stage", None)
    if not callable(runner):
        raise ReportStageBoundaryError("Report final auditor has no typed stage runner")
    result = runner(stage, runtime)
    if (
        type(result) is not FinalizedReportArtifact
        or result.generation is not stage.generated.generation
    ):
        raise ReportStageBoundaryError("invalid Report final-audit stage output")
    return result


__all__ = [
    "CommittedReport",
    "FinalizedReportArtifact",
    "GeneratedReportSections",
    "PlannedReportOutline",
    "ReportAuditFacts",
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
