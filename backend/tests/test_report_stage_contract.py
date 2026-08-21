from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.application.report_pipeline import (
    FinalizedReportArtifact,
    GeneratedReportSections,
    PlannedReportOutline,
    ReportAuditFacts,
    ReportFinalAuditInput,
    ReportGenerationInput,
    ReportPlanningInput,
    ReportStageBoundaryError,
    ReportStageRuntime,
    execute_report_final_audit_stage,
    execute_report_generation_stage,
    execute_report_planning_stage,
)


class _Executor:
    def run_planning_stage(self, stage, runtime):
        return PlannedReportOutline.create(stage, [{"title": "A"}])

    def run_generation_stage(self, stage, runtime):
        return GeneratedReportSections.create(
            stage, [{"title": "A", "markdown": "body"}]
        )

    def run_final_audit_stage(self, stage, runtime):
        generated = stage.generated
        return FinalizedReportArtifact(
            generation=generated.generation,
            sections=generated.sections,
            content_md="# report",
            gaps=(),
            references=(),
            audit_facts=ReportAuditFacts(
                1, 1, 0, 0, 0, 0, 0, 0, 8, "not_requested"
            ),
        )


def _planning() -> ReportPlanningInput:
    return ReportPlanningInput.create(
        notebook_id="nb",
        report_id="rep",
        question="q",
        history="",
        actor_id="actor",
        confirmed_intent={"resolved_question": "q"},
    )


def _generation() -> ReportGenerationInput:
    return ReportGenerationInput.create(
        notebook_id="nb",
        report_id="rep",
        original_question="q",
        display_question="q",
        research_question="q",
        depth=2,
        actor_id="actor",
        understanding={"resolved_question": "q"},
        outline=[{"title": "A"}],
    )


def _runtime() -> ReportStageRuntime:
    return ReportStageRuntime(None, object(), None, object(), "actor")


def test_report_stage_envelopes_are_frozen_and_transfer_typed_ownership():
    planning = _planning()
    with pytest.raises(FrozenInstanceError):
        planning.question = "changed"
    with pytest.raises(TypeError):
        planning.confirmed_intent["resolved_question"] = "changed"

    executor = _Executor()
    planned = execute_report_planning_stage(executor, planning, _runtime())
    generated = execute_report_generation_stage(
        executor, _generation(), _runtime()
    )
    finalized = execute_report_final_audit_stage(
        executor, ReportFinalAuditInput(generated), _runtime()
    )
    assert planned.outline[0]["title"] == "A"
    assert generated.successful_count == 1
    assert finalized.content_md == "# report"


def test_report_control_plane_is_recursively_snapshotted_without_aliases():
    intent = {
        "mandatory_topics": [{"id": "m1", "queries": ["q1"]}],
        "source_scope": {"source_ids": ["s1"]},
    }
    planning = ReportPlanningInput.create(
        notebook_id="nb",
        report_id="rep",
        question="q",
        history="",
        actor_id="actor",
        confirmed_intent=intent,
    )
    intent["mandatory_topics"][0]["queries"].append("evil")
    intent["source_scope"]["source_ids"].append("s2")
    assert planning.confirmed_intent["mandatory_topics"][0]["queries"] == ("q1",)
    assert planning.confirmed_intent["source_scope"]["source_ids"] == ("s1",)
    with pytest.raises(TypeError):
        planning.confirmed_intent["mandatory_topics"][0]["id"] = "evil"

    outline = [{"title": "A", "sub_queries": ["q1"]}]
    generated = ReportGenerationInput.create(
        notebook_id="nb",
        report_id="rep",
        original_question="q",
        display_question="q",
        research_question="q",
        depth=2,
        actor_id="actor",
        understanding=intent,
        outline=outline,
    )
    outline[0]["sub_queries"].append("evil")
    assert generated.outline[0]["sub_queries"] == ("q1",)

    with pytest.raises(ReportStageBoundaryError, match="JSON-shaped"):
        ReportPlanningInput.create(
            notebook_id="nb",
            report_id="rep",
            question="q",
            history="",
            actor_id="actor",
            confirmed_intent={"mutable_leaf": {"not", "json"}},
        )


@pytest.mark.parametrize(
    "method,call",
    [
        ("run_planning_stage", lambda e: execute_report_planning_stage(e, _planning(), _runtime())),
        ("run_generation_stage", lambda e: execute_report_generation_stage(e, _generation(), _runtime())),
        ("run_final_audit_stage", lambda e: execute_report_final_audit_stage(
            e,
            ReportFinalAuditInput(
                GeneratedReportSections.create(
                    _generation(), [{"title": "A", "markdown": "body"}]
                )
            ),
            _runtime(),
        )),
    ],
)
def test_report_stage_wrappers_reject_forged_output(method, call):
    class Forged(_Executor):
        pass

    setattr(Forged, method, lambda *_args: object())
    with pytest.raises(ReportStageBoundaryError):
        call(Forged())


def test_report_stage_wrappers_reject_exact_outputs_from_another_owner():
    planning = _planning()
    generation = _generation()

    class WrongOwner(_Executor):
        def run_planning_stage(self, _stage, _runtime):
            return PlannedReportOutline.create(_planning(), [{"title": "A"}])

        def run_generation_stage(self, _stage, _runtime):
            return GeneratedReportSections.create(
                _generation(), [{"title": "A", "markdown": "body"}]
            )

        def run_final_audit_stage(self, stage, _runtime):
            return FinalizedReportArtifact(
                generation=_generation(),
                sections=stage.generated.sections,
                content_md="# report",
                gaps=(),
                references=(),
                audit_facts=ReportAuditFacts(
                    1, 1, 0, 0, 0, 0, 0, 0, 8, "not_requested"
                ),
            )

    with pytest.raises(ReportStageBoundaryError):
        execute_report_planning_stage(WrongOwner(), planning, _runtime())
    with pytest.raises(ReportStageBoundaryError):
        execute_report_generation_stage(WrongOwner(), generation, _runtime())
    generated = GeneratedReportSections.create(
        generation, [{"title": "A", "markdown": "body"}]
    )
    with pytest.raises(ReportStageBoundaryError):
        execute_report_final_audit_stage(
            WrongOwner(), ReportFinalAuditInput(generated), _runtime()
        )


def test_generated_sections_keep_baseline_order_and_do_not_deepcopy_evidence():
    evidence = object()
    source = [
        {"title": "A", "markdown": "first", "evidence": evidence},
        {"title": "B", "markdown": "second", "failed": True},
    ]
    generated = GeneratedReportSections.create(_generation(), source)
    assert [row["title"] for row in generated.sections] == ["A", "B"]
    assert generated.sections[0]["evidence"] is evidence
    assert generated.successful_count == 1
