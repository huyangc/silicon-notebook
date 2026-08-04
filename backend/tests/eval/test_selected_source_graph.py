from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import yaml

from app.eval.selected_source_graph import (
    REQUIRED_SCENARIOS,
    ModelSamplingContract,
    RetrievalCost,
    SelectedSourceObservation,
    evaluate_selected_source_rollout,
    load_golden_cases,
    observation_from_dict,
    verify_attestation,
)
from app.services.source_graph_rollout import decide_source_graph_rollout
from app.services.source_graph_quality import quality_attestation_digest


MODEL = ModelSamplingContract(
    provider="fixture",
    model="fixed-model",
    prompt_version="selected-source-v1",
    temperature=0.0,
    top_p=1.0,
    seed=17,
)


def _paired():
    cases = load_golden_cases()
    baseline = []
    shadow = []
    for case in cases:
        selected = tuple(f"source-{alias}" for alias in case.selected_source_aliases)
        base = tuple(f"source-{alias}" for alias in case.mounted_base_aliases)
        alias_to_id = {
            alias: source_id
            for alias, source_id in zip(
                (*case.selected_source_aliases, *case.mounted_base_aliases),
                (*selected, *base),
            )
        }
        evidence_sources = tuple(
            alias_to_id[alias] for alias in case.gold_evidence_source_aliases
        )
        common = dict(
            case_id=case.case_id,
            model=MODEL,
            corpus_signature="fixture-corpus-v1",
            selected_source_ids=selected,
            selected_source_aliases=case.selected_source_aliases,
            source_alias_to_id=alias_to_id,
            mounted_base_source_ids=base,
            selected_source_chunk_count=(
                10_000 if case.scenario == "oversized_source" else 100
            ),
            evidence_ids=case.gold_evidence_ids,
            evidence_source_ids=evidence_sources,
            citation_anchors={"k1": case.gold_evidence_ids[0]},
            citation_source_ids=(selected[0],),
            baseline_manifest=f"manifest-{case.case_id}",
            baseline_candidate_ids=tuple(f"{case.case_id}-b-{i}" for i in range(20)),
            baseline_kg_candidate_count=20,
            groundable_sentences=4,
            cited_sentences=4,
            citations_total=1,
            valid_citations=1,
            grounded_sentences=4,
            supported_grounded_sentences=4,
            answer_nonempty=True,
            outline_sections=3,
            outline_dropped_sections=0,
        )
        baseline.append(
            SelectedSourceObservation(
                **common,
                cost=RetrievalCost(100, 1_000, 1_000, 100, 1),
            )
        )
        shadow.append(
            SelectedSourceObservation(
                **common,
                graph_actions=case.required_graph_actions,
                cost=RetrievalCost(120, 1_200, 1_500, 200, 2),
            )
        )
    return cases, baseline, shadow


def _replace_case(rows, case_id, **changes):
    return [replace(row, **changes) if row.case_id == case_id else row for row in rows]


def test_golden_set_covers_required_scenarios_and_energ_aizer_regression():
    cases = load_golden_cases()
    assert {case.scenario for case in cases} == REQUIRED_SCENARIOS
    energ = next(case for case in cases if case.case_id == "energ_aizer_single_pdagent")
    assert energ.required_graph_actions == ("expand_graph",)
    assert energ.min_baseline_kg_candidates == 20
    oversized = next(case for case in cases if case.scenario == "oversized_source")
    assert oversized.min_selected_source_chunks >= 10_000
    same_name = next(
        case for case in cases if case.scenario == "cross_source_same_name"
    )
    assert same_name.gold_evidence_source_aliases == ("same-name-a", "same-name-b")


def test_clean_pair_preserves_baseline_and_passes_quality_gate():
    cases, baseline, shadow = _paired()
    result = evaluate_selected_source_rollout(cases, baseline, shadow)
    assert result.approved
    assert not result.hard_failures
    assert not result.quality_failures
    assert not result.cost_failures


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"evidence_source_ids": ("outside",)}, "unauthorized_evidence"),
        ({"citation_source_ids": ("outside",)}, "unauthorized_citation"),
        ({"baseline_manifest": "changed"}, "baseline_manifest_changed"),
        ({"baseline_candidate_ids": ("changed",)}, "baseline_candidates_changed"),
        ({"baseline_evicted_count": 1}, "baseline_evicted"),
        ({"citation_anchors": {"k1": "changed"}}, "citation_key_changed"),
        ({"baseline_kg_candidate_count": 19}, "baseline_kg_count_changed"),
        ({"graph_actions": ()}, "required_graph_action_missing"),
    ],
)
def test_hard_invariants_block_rollout(changes, failure):
    cases, baseline, shadow = _paired()
    case_id = "energ_aizer_single_pdagent"
    result = evaluate_selected_source_rollout(
        cases, baseline, _replace_case(shadow, case_id, **changes)
    )
    assert not result.approved
    assert f"{case_id}:{failure}" in result.hard_failures


def test_mounted_base_is_authorized_but_must_remain_frozen():
    cases, baseline, shadow = _paired()
    case_id = "mounted_base_contradiction"
    assert evaluate_selected_source_rollout(cases, baseline, shadow).approved
    changed = _replace_case(shadow, case_id, mounted_base_source_ids=())
    result = evaluate_selected_source_rollout(cases, baseline, changed)
    assert f"{case_id}:mounted_base_scope_mismatch" in result.hard_failures
    assert f"{case_id}:mounted_base_scope_missing" in result.hard_failures


def test_golden_aliases_and_structural_metrics_are_hard_contracts():
    cases, baseline, shadow = _paired()
    case_id = "few_sources_compare_isolation"
    wrong_scope = _replace_case(shadow, case_id, selected_source_aliases=("wrong",))
    result = evaluate_selected_source_rollout(cases, baseline, wrong_scope)
    assert f"{case_id}:golden_scope_mismatch" in result.hard_failures

    malformed = _replace_case(shadow, case_id, citations_total=2)
    result = evaluate_selected_source_rollout(cases, baseline, malformed)
    assert f"{case_id}:unauthorized_citation" in result.hard_failures

    same_name = "cross_source_same_name"
    row = next(item for item in shadow if item.case_id == same_name)
    polluted = _replace_case(
        shadow,
        same_name,
        evidence_source_ids=(row.selected_source_ids[0], row.selected_source_ids[0]),
    )
    result = evaluate_selected_source_rollout(cases, baseline, polluted)
    assert f"{same_name}:gold_evidence_source_mismatch" in result.hard_failures

    duplicate_evidence = _replace_case(
        shadow,
        same_name,
        evidence_ids=(row.evidence_ids[0], row.evidence_ids[0]),
    )
    result = evaluate_selected_source_rollout(cases, baseline, duplicate_evidence)
    assert f"{same_name}:unauthorized_evidence" in result.hard_failures

    collapsed_aliases = {
        **row.source_alias_to_id,
        "same-name-b": row.selected_source_ids[0],
    }
    collapsed_scope = _replace_case(
        shadow,
        same_name,
        selected_source_ids=(row.selected_source_ids[0], row.selected_source_ids[0]),
        source_alias_to_id=collapsed_aliases,
    )
    result = evaluate_selected_source_rollout(cases, baseline, collapsed_scope)
    assert f"{same_name}:golden_source_binding_mismatch" in result.hard_failures
    assert f"{same_name}:invalid_frozen_scope" in result.hard_failures

    oversized = "oversized_source_bounded_graph"
    undersized = _replace_case(shadow, oversized, selected_source_chunk_count=9_999)
    result = evaluate_selected_source_rollout(cases, baseline, undersized)
    assert f"{oversized}:scenario_scale_missing" in result.hard_failures


def test_one_case_quality_regression_cannot_be_hidden_by_other_cases():
    cases, baseline, shadow = _paired()
    case_id = "few_sources_compare_isolation"
    degraded = _replace_case(shadow, case_id, valid_citations=0)
    result = evaluate_selected_source_rollout(cases, baseline, degraded)
    assert not result.approved
    assert f"{case_id}:citation_validity_regressed" in result.quality_failures


def test_one_case_cost_regression_blocks_even_when_aggregate_is_bounded():
    cases, baseline, shadow = _paired()
    case_id = "cross_source_same_name"
    expensive = _replace_case(
        shadow,
        case_id,
        cost=RetrievalCost(151, 1_200, 1_500, 200, 2),
    )
    result = evaluate_selected_source_rollout(cases, baseline, expensive)
    assert not result.approved
    assert f"{case_id}:latency_budget_exceeded" in result.cost_failures


def test_energ_aizer_must_expand_without_losing_twenty_baseline_kg_candidates():
    cases, baseline, shadow = _paired()
    case_id = "energ_aizer_single_pdagent"
    before = next(row for row in baseline if row.case_id == case_id)
    after = next(row for row in shadow if row.case_id == case_id)
    assert len(before.baseline_candidate_ids) == 20
    assert after.baseline_candidate_ids == before.baseline_candidate_ids
    assert after.baseline_kg_candidate_count == 20
    assert "expand_graph" in after.graph_actions
    assert evaluate_selected_source_rollout(cases, baseline, shadow).approved


def test_attestation_rejects_tampering_and_active_rollout_requires_approval():
    cases, baseline, shadow = _paired()
    result = evaluate_selected_source_rollout(cases, baseline, shadow)
    attestation = result.attestation(run_id="run-1")
    assert verify_attestation(attestation)["approved"] is True
    tampered = {**attestation, "case_count": 999}
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_attestation(tampered)
    internally_inconsistent = replace(
        result,
        shadow=replace(result.shadow, citation_validity=0.5),
    ).attestation(run_id="run-1")
    with pytest.raises(ValueError, match="regression"):
        verify_attestation(internally_inconsistent)

    assert decide_source_graph_rollout(
        notebook_id="n1", mode="shadow", attestation=None
    ).shadow_only
    assert not decide_source_graph_rollout(
        notebook_id="n1", mode="on", attestation=None
    ).enabled
    assert not decide_source_graph_rollout(
        notebook_id="n1", mode="on", attestation=tampered
    ).enabled
    assert not decide_source_graph_rollout(
        notebook_id="n1", mode="on", attestation=attestation
    ).enabled
    assert decide_source_graph_rollout(
        notebook_id="n1",
        mode="on",
        attestation=attestation,
        expected_corpus_signature="fixture-corpus-v1",
        expected_model=asdict(MODEL),
    ).enabled
    assert not decide_source_graph_rollout(
        notebook_id="n1",
        mode="on",
        attestation=attestation,
        expected_corpus_signature="other-corpus",
    ).enabled
    assert not decide_source_graph_rollout(
        notebook_id="n1",
        mode="on",
        attestation=attestation,
        expected_model={**asdict(MODEL), "model": "other-model"},
    ).enabled


def test_attestation_identity_is_frozen_from_measured_observations():
    cases, baseline, shadow = _paired()
    result = evaluate_selected_source_rollout(cases, baseline, shadow)
    attestation = result.attestation(run_id="run-identity")
    assert attestation["corpus_signature"] == baseline[0].corpus_signature
    assert attestation["model"] == asdict(baseline[0].model)
    with pytest.raises(TypeError):
        result.attestation(  # type: ignore[call-arg]
            run_id="run-spoofed",
            corpus_signature="production-corpus",
            model=replace(MODEL, model="production-model"),
        )


def test_allowlist_and_hash_rollout_are_deterministic():
    cases, baseline, shadow = _paired()
    result = evaluate_selected_source_rollout(cases, baseline, shadow)
    attestation = result.attestation(run_id="run-1")
    assert decide_source_graph_rollout(
        notebook_id="n1",
        mode="allowlist",
        attestation=attestation,
        notebook_allowlist=frozenset({"n1"}),
        expected_corpus_signature="fixture-corpus-v1",
        expected_model=asdict(MODEL),
    ).enabled
    assert not decide_source_graph_rollout(
        notebook_id="n2",
        mode="allowlist",
        attestation=attestation,
        notebook_allowlist=frozenset({"n1"}),
        expected_corpus_signature="fixture-corpus-v1",
        expected_model=asdict(MODEL),
    ).enabled
    first = decide_source_graph_rollout(
        notebook_id="stable",
        mode="rollout",
        attestation=attestation,
        rollout_percent=37.5,
        expected_corpus_signature="fixture-corpus-v1",
        expected_model=asdict(MODEL),
    )
    second = decide_source_graph_rollout(
        notebook_id="stable",
        mode="rollout",
        attestation=attestation,
        rollout_percent=37.5,
        expected_corpus_signature="fixture-corpus-v1",
        expected_model=asdict(MODEL),
    )
    assert first == second
    assert decide_source_graph_rollout(
        notebook_id="stable",
        mode="rollout",
        attestation=attestation,
        rollout_percent=100,
        expected_corpus_signature="fixture-corpus-v1",
        expected_model=asdict(MODEL),
    ).enabled
    assert not decide_source_graph_rollout(
        notebook_id="stable",
        mode="rollout",
        attestation=attestation,
        rollout_percent=0,
        expected_corpus_signature="fixture-corpus-v1",
        expected_model=asdict(MODEL),
    ).enabled


def test_citation_anchor_must_bind_to_evidence_from_the_declared_source():
    cases, baseline, shadow = _paired()
    case_id = "energ_aizer_single_pdagent"
    poisoned = _replace_case(
        shadow,
        case_id,
        citation_anchors={"k1": "outside-evidence"},
    )
    result = evaluate_selected_source_rollout(cases, baseline, poisoned)
    assert f"{case_id}:unauthorized_citation" in result.hard_failures


def test_empty_answer_shape_and_zero_denominators_cannot_pass():
    cases, baseline, shadow = _paired()
    case_id = "few_sources_compare_isolation"
    empty = _replace_case(
        shadow,
        case_id,
        groundable_sentences=0,
        cited_sentences=0,
        grounded_sentences=0,
        supported_grounded_sentences=0,
        answer_nonempty=False,
    )
    result = evaluate_selected_source_rollout(cases, baseline, empty)
    assert not result.approved
    assert f"{case_id}:answer_shape_invalid" in result.hard_failures
    assert f"{case_id}:groundable_sentences_missing" in result.quality_failures


def test_verifier_recomputes_aggregate_and_per_case_cost_rails():
    cases, baseline, shadow = _paired()
    attestation = evaluate_selected_source_rollout(cases, baseline, shadow).attestation(
        run_id="run-cost"
    )
    payload = dict(attestation)
    payload.pop("attestation_digest")
    payload["shadow"] = {**payload["shadow"], "latency_ms": 10**12}
    payload["cases"] = [dict(row) for row in payload["cases"]]
    payload["cases"][0] = {
        **payload["cases"][0],
        "shadow": {**payload["cases"][0]["shadow"], "database_rows": 10**12},
    }
    resigned = {**payload, "attestation_digest": quality_attestation_digest(payload)}
    with pytest.raises(ValueError, match="cost regression"):
        verify_attestation(resigned)


def test_custom_weakened_golden_cannot_authorize_activation(tmp_path: Path):
    canonical_path = (
        Path(__file__).resolve().parents[2]
        / "app"
        / "eval"
        / "selected_source_graph_gold.yaml"
    )
    rows = yaml.safe_load(canonical_path.read_text(encoding="utf-8"))
    for row in rows:
        row["required_graph_actions"] = []
        row["min_baseline_kg_candidates"] = 0
    custom_path = tmp_path / "weakened.yaml"
    custom_path.write_text(yaml.safe_dump(rows), encoding="utf-8")
    custom_cases = load_golden_cases(custom_path)
    _cases, baseline, shadow = _paired()
    result = evaluate_selected_source_rollout(custom_cases, baseline, shadow)
    assert result.approved
    attestation = result.attestation(run_id="run-custom")
    with pytest.raises(ValueError, match="golden mismatch"):
        verify_attestation(attestation)


def test_attestation_rejects_extra_content_or_source_identity_fields():
    cases, baseline, shadow = _paired()
    attestation = evaluate_selected_source_rollout(cases, baseline, shadow).attestation(
        run_id="run-extra"
    )
    payload = dict(attestation)
    payload.pop("attestation_digest")
    payload.update({"question": "sensitive text", "source_ids": ["source-secret"]})
    malformed = {
        **payload,
        "attestation_digest": quality_attestation_digest(payload),
    }
    with pytest.raises(ValueError, match="shape mismatch"):
        verify_attestation(malformed)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("answer_nonempty",), "false"),
        (("no_answer",), "false"),
        (("erroneous_refusal",), "false"),
        (("cost", "model_calls"), True),
        (("model", "seed"), True),
    ],
)
def test_observation_parser_rejects_truthy_strings_and_boolean_numbers(path, value):
    _cases, _baseline, shadow = _paired()
    row = asdict(shadow[0])
    target = row
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match="selected-source observation"):
        observation_from_dict(row)


def test_cli_writes_content_free_verified_attestation(tmp_path: Path):
    cases, baseline, shadow = _paired()
    input_path = tmp_path / "paired.json"
    output_path = tmp_path / "attestation.json"
    input_path.write_text(
        json.dumps(
            {
                "run_id": "run-cli",
                "baseline": [asdict(row) for row in baseline],
                "shadow": [asdict(row) for row in shadow],
            }
        ),
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "eval_selected_source_graph.py"),
            str(input_path),
            "--output",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    attestation = json.loads(output_path.read_text(encoding="utf-8"))
    verify_attestation(attestation)
    rendered = json.dumps(attestation, ensure_ascii=False)
    assert "question" not in rendered
    assert "evidence_ids" not in rendered
    assert "citation_anchors" not in rendered
