"""Paired quality gate for selected-source graph-enrichment rollout.

The harness is deliberately independent from Ask/Report wiring.  A runner
records baseline and shadow observations under one frozen model/corpus
contract; this module verifies hard isolation/baseline invariants, computes
quality and cost deltas, and emits a content-free attestation for rollout.
Questions and answer text never enter the attestation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from app.services.source_graph_quality import (
    REQUIRED_SCENARIOS,
    SELECTED_SOURCE_GRAPH_SUITE_VERSION,
    SelectedSourceGatePolicy,
    quality_attestation_digest,
    verify_attestation as verify_quality_attestation,
)

# Compatibility export for evaluation callers; production imports the service
# contract directly and never depends on ``app.eval``.
verify_attestation = verify_quality_attestation


def _ratio(numerator: int, denominator: int, *, empty: float = 1.0) -> float:
    return numerator / denominator if denominator > 0 else empty


@dataclass(frozen=True)
class SelectedSourceGoldenCase:
    case_id: str
    scenario: str
    question: str
    selected_source_aliases: tuple[str, ...]
    mounted_base_aliases: tuple[str, ...]
    gold_evidence_ids: tuple[str, ...]
    gold_evidence_source_aliases: tuple[str, ...]
    required_graph_actions: tuple[str, ...] = ()
    min_baseline_kg_candidates: int = 0
    requires_mounted_base: bool = False
    min_selected_source_chunks: int = 0


@dataclass(frozen=True)
class ModelSamplingContract:
    provider: str
    model: str
    prompt_version: str
    temperature: float
    top_p: float
    seed: int


@dataclass(frozen=True)
class RetrievalCost:
    latency_ms: int = 0
    database_rows: int = 0
    peak_memory_bytes: int = 0
    prompt_tokens: int = 0
    model_calls: int = 0


@dataclass(frozen=True)
class SelectedSourceObservation:
    case_id: str
    model: ModelSamplingContract
    corpus_signature: str
    selected_source_ids: tuple[str, ...]
    selected_source_aliases: tuple[str, ...]
    source_alias_to_id: Mapping[str, str]
    mounted_base_source_ids: tuple[str, ...]
    selected_source_chunk_count: int
    evidence_ids: tuple[str, ...]
    evidence_source_ids: tuple[str, ...]
    citation_anchors: Mapping[str, str]
    citation_source_ids: tuple[str, ...]
    baseline_manifest: str
    baseline_candidate_ids: tuple[str, ...]
    baseline_kg_candidate_count: int
    baseline_evicted_count: int = 0
    graph_actions: tuple[str, ...] = ()
    groundable_sentences: int = 0
    cited_sentences: int = 0
    citations_total: int = 0
    valid_citations: int = 0
    grounded_sentences: int = 0
    supported_grounded_sentences: int = 0
    answer_nonempty: bool = False
    no_answer: bool = False
    erroneous_refusal: bool = False
    outline_sections: int = 0
    outline_dropped_sections: int = 0
    cost: RetrievalCost = field(default_factory=RetrievalCost)


@dataclass(frozen=True)
class LaneMetrics:
    evidence_recall_at_k: float
    citation_coverage: float
    citation_validity: float
    grounded_sentence_coverage: float
    answer_nonempty_rate: float
    no_answer_rate: float
    erroneous_refusal_rate: float
    outline_drop_rate: float
    groundable_sentences: int
    citations_total: int
    grounded_sentences: int
    outline_sections: int
    latency_ms: int
    database_rows: int
    peak_memory_bytes: int
    prompt_tokens: int
    model_calls: int


@dataclass(frozen=True)
class SelectedSourceGateResult:
    approved: bool
    hard_failures: tuple[str, ...]
    quality_failures: tuple[str, ...]
    cost_failures: tuple[str, ...]
    baseline: LaneMetrics
    shadow: LaneMetrics
    case_count: int
    scenario_counts: Mapping[str, int]
    golden_digest: str
    cases: tuple[Mapping[str, Any], ...]
    policy: SelectedSourceGatePolicy
    corpus_signature: str
    model: ModelSamplingContract
    suite_version: int = SELECTED_SOURCE_GRAPH_SUITE_VERSION

    def attestation(self, *, run_id: str) -> dict[str, Any]:
        payload = {
            "suite_version": self.suite_version,
            "run_id": run_id,
            "corpus_signature": self.corpus_signature,
            "golden_digest": self.golden_digest,
            "model": asdict(self.model),
            "approved": self.approved,
            "hard_failures": list(self.hard_failures),
            "quality_failures": list(self.quality_failures),
            "cost_failures": list(self.cost_failures),
            "case_count": self.case_count,
            "scenario_counts": dict(self.scenario_counts),
            "cases": [dict(row) for row in self.cases],
            "policy": asdict(self.policy),
            "baseline": asdict(self.baseline),
            "shadow": asdict(self.shadow),
        }
        return {
            **payload,
            "attestation_digest": quality_attestation_digest(payload),
        }


def load_golden_cases(
    path: str | Path | None = None,
) -> tuple[SelectedSourceGoldenCase, ...]:
    golden_path = (
        Path(path)
        if path
        else Path(__file__).with_name("selected_source_graph_gold.yaml")
    )
    rows = yaml.safe_load(golden_path.read_text(encoding="utf-8")) or []
    cases = tuple(
        SelectedSourceGoldenCase(
            case_id=str(row["id"]),
            scenario=str(row["scenario"]),
            question=str(row["question"]),
            selected_source_aliases=tuple(row.get("selected_source_aliases") or ()),
            mounted_base_aliases=tuple(row.get("mounted_base_aliases") or ()),
            gold_evidence_ids=tuple(row.get("gold_evidence_ids") or ()),
            gold_evidence_source_aliases=tuple(
                row.get("gold_evidence_source_aliases") or ()
            ),
            required_graph_actions=tuple(row.get("required_graph_actions") or ()),
            min_baseline_kg_candidates=int(row.get("min_baseline_kg_candidates") or 0),
            requires_mounted_base=bool(row.get("requires_mounted_base", False)),
            min_selected_source_chunks=int(row.get("min_selected_source_chunks") or 0),
        )
        for row in rows
    )
    ids = [case.case_id for case in cases]
    if not cases or len(set(ids)) != len(ids):
        raise ValueError("selected-source golden cases must have unique non-empty ids")
    missing = REQUIRED_SCENARIOS - {case.scenario for case in cases}
    if missing:
        raise ValueError(
            f"selected-source golden set missing scenarios: {sorted(missing)}"
        )
    if any(
        not case.question.strip() or not case.selected_source_aliases for case in cases
    ):
        raise ValueError(
            "every selected-source case needs a question and source aliases"
        )
    if any(
        len(case.gold_evidence_ids) != len(case.gold_evidence_source_aliases)
        or set(case.gold_evidence_source_aliases)
        - set((*case.selected_source_aliases, *case.mounted_base_aliases))
        or case.requires_mounted_base != bool(case.mounted_base_aliases)
        or case.min_selected_source_chunks < 0
        for case in cases
    ):
        raise ValueError("selected-source golden evidence provenance is invalid")
    return cases


def golden_suite_digest(cases: Sequence[SelectedSourceGoldenCase]) -> str:
    return quality_attestation_digest([asdict(case) for case in cases])


def observation_from_dict(row: Mapping[str, Any]) -> SelectedSourceObservation:
    model = row.get("model") or {}
    cost = row.get("cost") or {}

    def integer(values: Mapping[str, Any], key: str, default: int = 0) -> int:
        value = values.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"selected-source observation {key} must be an integer")
        return value

    def number(values: Mapping[str, Any], key: str) -> float:
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"selected-source observation {key} must be numeric")
        return float(value)

    def boolean(values: Mapping[str, Any], key: str) -> bool:
        value = values.get(key)
        if not isinstance(value, bool):
            raise ValueError(f"selected-source observation {key} must be boolean")
        return value

    return SelectedSourceObservation(
        case_id=str(row["case_id"]),
        model=ModelSamplingContract(
            provider=str(model["provider"]),
            model=str(model["model"]),
            prompt_version=str(model["prompt_version"]),
            temperature=number(model, "temperature"),
            top_p=number(model, "top_p"),
            seed=integer(model, "seed"),
        ),
        corpus_signature=str(row["corpus_signature"]),
        selected_source_ids=tuple(row.get("selected_source_ids") or ()),
        selected_source_aliases=tuple(row.get("selected_source_aliases") or ()),
        source_alias_to_id={
            str(alias): str(source_id)
            for alias, source_id in dict(row.get("source_alias_to_id") or {}).items()
        },
        mounted_base_source_ids=tuple(row.get("mounted_base_source_ids") or ()),
        selected_source_chunk_count=integer(row, "selected_source_chunk_count"),
        evidence_ids=tuple(row.get("evidence_ids") or ()),
        evidence_source_ids=tuple(row.get("evidence_source_ids") or ()),
        citation_anchors=dict(row.get("citation_anchors") or {}),
        citation_source_ids=tuple(row.get("citation_source_ids") or ()),
        baseline_manifest=str(row.get("baseline_manifest") or ""),
        baseline_candidate_ids=tuple(row.get("baseline_candidate_ids") or ()),
        baseline_kg_candidate_count=integer(row, "baseline_kg_candidate_count"),
        baseline_evicted_count=integer(row, "baseline_evicted_count"),
        graph_actions=tuple(row.get("graph_actions") or ()),
        groundable_sentences=integer(row, "groundable_sentences"),
        cited_sentences=integer(row, "cited_sentences"),
        citations_total=integer(row, "citations_total"),
        valid_citations=integer(row, "valid_citations"),
        grounded_sentences=integer(row, "grounded_sentences"),
        supported_grounded_sentences=integer(row, "supported_grounded_sentences"),
        answer_nonempty=boolean(row, "answer_nonempty"),
        no_answer=boolean(row, "no_answer"),
        erroneous_refusal=boolean(row, "erroneous_refusal"),
        outline_sections=integer(row, "outline_sections"),
        outline_dropped_sections=integer(row, "outline_dropped_sections"),
        cost=RetrievalCost(
            latency_ms=integer(cost, "latency_ms"),
            database_rows=integer(cost, "database_rows"),
            peak_memory_bytes=integer(cost, "peak_memory_bytes"),
            prompt_tokens=integer(cost, "prompt_tokens"),
            model_calls=integer(cost, "model_calls"),
        ),
    )


def _lane_metrics(
    cases: Sequence[SelectedSourceGoldenCase],
    rows: Sequence[SelectedSourceObservation],
    *,
    recall_k: int,
) -> LaneMetrics:
    by_case = {case.case_id: case for case in cases}
    recalls = []
    cited = groundable = valid = citations = grounded = supported = 0
    nonempty = no_answer = refusal = dropped = outline = 0
    latency = db_rows = peak = tokens = calls = 0
    for row in rows:
        gold = set(by_case[row.case_id].gold_evidence_ids)
        if gold:
            recalls.append(len(set(row.evidence_ids[:recall_k]) & gold) / len(gold))
        cited += row.cited_sentences
        groundable += row.groundable_sentences
        valid += row.valid_citations
        citations += row.citations_total
        supported += row.supported_grounded_sentences
        grounded += row.grounded_sentences
        nonempty += int(row.answer_nonempty)
        no_answer += int(row.no_answer)
        refusal += int(row.erroneous_refusal)
        dropped += row.outline_dropped_sections
        outline += row.outline_sections
        latency += row.cost.latency_ms
        db_rows += row.cost.database_rows
        peak = max(peak, row.cost.peak_memory_bytes)
        tokens += row.cost.prompt_tokens
        calls += row.cost.model_calls
    count = len(rows)
    return LaneMetrics(
        evidence_recall_at_k=sum(recalls) / len(recalls) if recalls else 1.0,
        citation_coverage=_ratio(cited, groundable),
        citation_validity=_ratio(valid, citations),
        grounded_sentence_coverage=_ratio(supported, grounded),
        answer_nonempty_rate=_ratio(nonempty, count, empty=0.0),
        no_answer_rate=_ratio(no_answer, count, empty=0.0),
        erroneous_refusal_rate=_ratio(refusal, count, empty=0.0),
        outline_drop_rate=_ratio(dropped, outline, empty=0.0),
        groundable_sentences=groundable,
        citations_total=citations,
        grounded_sentences=grounded,
        outline_sections=outline,
        latency_ms=latency,
        database_rows=db_rows,
        peak_memory_bytes=peak,
        prompt_tokens=tokens,
        model_calls=calls,
    )


def evaluate_selected_source_rollout(
    cases: Sequence[SelectedSourceGoldenCase],
    baseline: Sequence[SelectedSourceObservation],
    shadow: Sequence[SelectedSourceObservation],
    *,
    policy: SelectedSourceGatePolicy | None = None,
) -> SelectedSourceGateResult:
    policy = policy or SelectedSourceGatePolicy()
    cases_by_id = {case.case_id: case for case in cases}
    baseline_by_id = {row.case_id: row for row in baseline}
    shadow_by_id = {row.case_id: row for row in shadow}
    hard: list[str] = []
    quality: list[str] = []
    cost: list[str] = []

    if len(baseline_by_id) != len(baseline) or len(shadow_by_id) != len(shadow):
        hard.append("duplicate_case_observation")
    if set(cases_by_id) != set(baseline_by_id) or set(cases_by_id) != set(shadow_by_id):
        hard.append("case_set_mismatch")
    scenario_counts = {
        scenario: sum(case.scenario == scenario for case in cases)
        for scenario in sorted(REQUIRED_SCENARIOS)
    }
    if any(count == 0 for count in scenario_counts.values()):
        hard.append("scenario_coverage_incomplete")

    paired_ids = sorted(set(cases_by_id) & set(baseline_by_id) & set(shadow_by_id))
    suite_models = {row.model for row in (*baseline, *shadow)}
    suite_corpora = {row.corpus_signature for row in (*baseline, *shadow)}
    if len(suite_models) > 1:
        hard.append("suite_model_contract_mismatch")
    if len(suite_corpora) > 1:
        hard.append("suite_corpus_signature_mismatch")
    # Attestation identity comes only from the measured observations.  A
    # mixed suite is already a hard failure; keep its first baseline identity
    # solely so the rejected diagnostic artifact remains serializable.
    identity_row = baseline[0] if baseline else (shadow[0] if shadow else None)
    evaluated_model = (
        identity_row.model
        if identity_row is not None
        else ModelSamplingContract("", "", "", 0.0, 1.0, 0)
    )
    evaluated_corpus = identity_row.corpus_signature if identity_row is not None else ""
    for case_id in paired_ids:
        case = cases_by_id[case_id]
        before = baseline_by_id[case_id]
        after = shadow_by_id[case_id]
        prefix = f"{case_id}:"
        if before.model != after.model:
            hard.append(prefix + "model_contract_mismatch")
        if before.corpus_signature != after.corpus_signature:
            hard.append(prefix + "corpus_signature_mismatch")
        if before.selected_source_ids != after.selected_source_ids:
            hard.append(prefix + "selected_scope_mismatch")
        if (
            before.selected_source_aliases != case.selected_source_aliases
            or after.selected_source_aliases != case.selected_source_aliases
        ):
            hard.append(prefix + "golden_scope_mismatch")
        expected_aliases = (*case.selected_source_aliases, *case.mounted_base_aliases)
        if (
            before.source_alias_to_id != after.source_alias_to_id
            or set(after.source_alias_to_id) != set(expected_aliases)
            or len(set(after.source_alias_to_id.values()))
            != len(after.source_alias_to_id)
            or tuple(
                after.source_alias_to_id.get(alias, "")
                for alias in case.selected_source_aliases
            )
            != after.selected_source_ids
            or tuple(
                after.source_alias_to_id.get(alias, "")
                for alias in case.mounted_base_aliases
            )
            != after.mounted_base_source_ids
        ):
            hard.append(prefix + "golden_source_binding_mismatch")
        if before.mounted_base_source_ids != after.mounted_base_source_ids:
            hard.append(prefix + "mounted_base_scope_mismatch")
        if case.requires_mounted_base and not after.mounted_base_source_ids:
            hard.append(prefix + "mounted_base_scope_missing")
        if (
            before.selected_source_chunk_count != after.selected_source_chunk_count
            or after.selected_source_chunk_count < case.min_selected_source_chunks
        ):
            hard.append(prefix + "scenario_scale_missing")
        allowed = set(after.selected_source_ids) | set(after.mounted_base_source_ids)
        if (
            not after.selected_source_ids
            or len(set(after.selected_source_ids)) != len(after.selected_source_ids)
            or len(set(after.mounted_base_source_ids))
            != len(after.mounted_base_source_ids)
        ):
            hard.append(prefix + "invalid_frozen_scope")
        for lane in (before, after):
            if (
                len(lane.evidence_ids) != len(lane.evidence_source_ids)
                or len(set(lane.evidence_ids)) != len(lane.evidence_ids)
                or not set(lane.evidence_source_ids).issubset(allowed)
            ):
                hard.append(prefix + "unauthorized_evidence")
            if (
                len(lane.citation_anchors) != lane.citations_total
                or len(lane.citation_source_ids) != lane.citations_total
                or not set(lane.citation_source_ids).issubset(allowed)
            ):
                hard.append(prefix + "unauthorized_citation")
            counts = (
                lane.groundable_sentences,
                lane.cited_sentences,
                lane.citations_total,
                lane.valid_citations,
                lane.grounded_sentences,
                lane.supported_grounded_sentences,
                lane.outline_sections,
                lane.outline_dropped_sections,
                lane.cost.latency_ms,
                lane.cost.database_rows,
                lane.cost.peak_memory_bytes,
                lane.cost.prompt_tokens,
                lane.cost.model_calls,
            )
            if (
                any(value < 0 for value in counts)
                or lane.cited_sentences > lane.groundable_sentences
                or lane.valid_citations > lane.citations_total
                or lane.supported_grounded_sentences > lane.grounded_sentences
                or lane.outline_dropped_sections > lane.outline_sections
            ):
                hard.append(prefix + "metric_shape_invalid")
            evidence_sources = dict(zip(lane.evidence_ids, lane.evidence_source_ids))
            if any(
                evidence_sources.get(evidence_id) != citation_source_id
                for (_key, evidence_id), citation_source_id in zip(
                    lane.citation_anchors.items(), lane.citation_source_ids
                )
            ):
                hard.append(prefix + "unauthorized_citation")
            if any(
                evidence_id in evidence_sources
                and evidence_sources[evidence_id] != lane.source_alias_to_id.get(alias)
                for evidence_id, alias in zip(
                    case.gold_evidence_ids, case.gold_evidence_source_aliases
                )
            ):
                hard.append(prefix + "gold_evidence_source_mismatch")
            if not lane.answer_nonempty and not lane.no_answer:
                hard.append(prefix + "answer_shape_invalid")
        if (
            not before.baseline_manifest
            or not after.baseline_manifest
            or len(set(before.baseline_candidate_ids))
            != len(before.baseline_candidate_ids)
            or len(set(after.baseline_candidate_ids))
            != len(after.baseline_candidate_ids)
        ):
            hard.append(prefix + "baseline_manifest_invalid")
        if before.baseline_manifest != after.baseline_manifest:
            hard.append(prefix + "baseline_manifest_changed")
        if before.baseline_candidate_ids != after.baseline_candidate_ids:
            hard.append(prefix + "baseline_candidates_changed")
        if before.baseline_evicted_count != 0 or after.baseline_evicted_count != 0:
            hard.append(prefix + "baseline_evicted")
        for key, anchor in before.citation_anchors.items():
            if after.citation_anchors.get(key) != anchor:
                hard.append(prefix + "citation_key_changed")
                break
        if before.baseline_kg_candidate_count < case.min_baseline_kg_candidates:
            hard.append(prefix + "baseline_kg_floor_missing")
        if after.baseline_kg_candidate_count != before.baseline_kg_candidate_count:
            hard.append(prefix + "baseline_kg_count_changed")
        if not set(case.required_graph_actions).issubset(after.graph_actions):
            hard.append(prefix + "required_graph_action_missing")

    paired_cases = [cases_by_id[case_id] for case_id in paired_ids]
    paired_baseline = [baseline_by_id[case_id] for case_id in paired_ids]
    paired_shadow = [shadow_by_id[case_id] for case_id in paired_ids]
    before_metrics = _lane_metrics(
        paired_cases, paired_baseline, recall_k=policy.recall_k
    )
    after_metrics = _lane_metrics(paired_cases, paired_shadow, recall_k=policy.recall_k)
    positive_metrics = (
        "evidence_recall_at_k",
        "citation_coverage",
        "citation_validity",
        "grounded_sentence_coverage",
        "answer_nonempty_rate",
    )
    negative_metrics = (
        "no_answer_rate",
        "erroneous_refusal_rate",
        "outline_drop_rate",
    )
    for name in positive_metrics:
        if getattr(after_metrics, name) + 1e-12 < getattr(before_metrics, name):
            quality.append(name + "_regressed")
    for name in negative_metrics:
        if getattr(after_metrics, name) > getattr(before_metrics, name) + 1e-12:
            quality.append(name + "_regressed")

    # Aggregate equality alone is insufficient: a gain on one source may not
    # hide a regression on another.  Enforce the same rails case by case.
    for case_id in paired_ids:
        case_metrics_before = _lane_metrics(
            [cases_by_id[case_id]], [baseline_by_id[case_id]], recall_k=policy.recall_k
        )
        case_metrics_after = _lane_metrics(
            [cases_by_id[case_id]], [shadow_by_id[case_id]], recall_k=policy.recall_k
        )
        for name in positive_metrics:
            if getattr(case_metrics_after, name) + 1e-12 < getattr(
                case_metrics_before, name
            ):
                quality.append(f"{case_id}:{name}_regressed")
        for name in negative_metrics:
            if (
                getattr(case_metrics_after, name)
                > getattr(case_metrics_before, name) + 1e-12
            ):
                quality.append(f"{case_id}:{name}_regressed")
        for name in (
            "groundable_sentences",
            "citations_total",
            "grounded_sentences",
            "outline_sections",
        ):
            if (
                getattr(case_metrics_before, name) > 0
                and getattr(case_metrics_after, name) == 0
            ):
                quality.append(f"{case_id}:{name}_missing")

    def over_ratio(after_value: int, before_value: int, ratio: float) -> bool:
        if before_value <= 0:
            return after_value > 0
        return after_value > before_value * ratio

    if over_ratio(
        after_metrics.latency_ms,
        before_metrics.latency_ms,
        policy.max_latency_ratio,
    ):
        cost.append("latency_budget_exceeded")
    if over_ratio(
        after_metrics.database_rows,
        before_metrics.database_rows,
        policy.max_database_rows_ratio,
    ):
        cost.append("database_rows_budget_exceeded")
    if over_ratio(
        after_metrics.peak_memory_bytes,
        before_metrics.peak_memory_bytes,
        policy.max_peak_memory_ratio,
    ):
        cost.append("peak_memory_budget_exceeded")
    case_count = max(1, len(paired_ids))
    if (
        after_metrics.prompt_tokens - before_metrics.prompt_tokens
        > policy.max_added_prompt_tokens_per_case * case_count
    ):
        cost.append("prompt_token_budget_exceeded")
    if (
        after_metrics.model_calls - before_metrics.model_calls
        > policy.max_added_model_calls_per_case * case_count
    ):
        cost.append("model_call_budget_exceeded")
    for case_id in paired_ids:
        before_cost = baseline_by_id[case_id].cost
        after_cost = shadow_by_id[case_id].cost
        prefix = f"{case_id}:"
        if over_ratio(
            after_cost.latency_ms, before_cost.latency_ms, policy.max_latency_ratio
        ):
            cost.append(prefix + "latency_budget_exceeded")
        if over_ratio(
            after_cost.database_rows,
            before_cost.database_rows,
            policy.max_database_rows_ratio,
        ):
            cost.append(prefix + "database_rows_budget_exceeded")
        if over_ratio(
            after_cost.peak_memory_bytes,
            before_cost.peak_memory_bytes,
            policy.max_peak_memory_ratio,
        ):
            cost.append(prefix + "peak_memory_budget_exceeded")
        if (
            after_cost.prompt_tokens - before_cost.prompt_tokens
            > policy.max_added_prompt_tokens_per_case
        ):
            cost.append(prefix + "prompt_token_budget_exceeded")
        if (
            after_cost.model_calls - before_cost.model_calls
            > policy.max_added_model_calls_per_case
        ):
            cost.append(prefix + "model_call_budget_exceeded")

    case_metrics = tuple(
        {
            "case_id": case_id,
            "scenario": cases_by_id[case_id].scenario,
            "baseline": asdict(
                _lane_metrics(
                    [cases_by_id[case_id]],
                    [baseline_by_id[case_id]],
                    recall_k=policy.recall_k,
                )
            ),
            "shadow": asdict(
                _lane_metrics(
                    [cases_by_id[case_id]],
                    [shadow_by_id[case_id]],
                    recall_k=policy.recall_k,
                )
            ),
        }
        for case_id in paired_ids
    )
    failures = (*hard, *quality, *cost)
    return SelectedSourceGateResult(
        approved=not failures,
        hard_failures=tuple(dict.fromkeys(hard)),
        quality_failures=tuple(dict.fromkeys(quality)),
        cost_failures=tuple(dict.fromkeys(cost)),
        baseline=before_metrics,
        shadow=after_metrics,
        case_count=len(paired_ids),
        scenario_counts=scenario_counts,
        golden_digest=golden_suite_digest(cases),
        cases=case_metrics,
        policy=policy,
        corpus_signature=evaluated_corpus,
        model=evaluated_model,
    )
