"""Versioned, content-free quality attestation contract for graph rollout."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping

SELECTED_SOURCE_GRAPH_SUITE_VERSION = 1
# SHA-256 of the canonical serialized golden cases.  The evaluator recomputes
# this from the loaded suite; production accepts only this pinned value, so a
# custom ``--golden`` file can diagnose a corpus but cannot authorize rollout.
SELECTED_SOURCE_GRAPH_GOLDEN_DIGEST = (
    "f32334c6125feaecfc755154ea7125bf975d235e7824d0744eea24037a3571a7"
)
REQUIRED_SCENARIOS = frozenset(
    {
        "single_source",
        "few_sources",
        "oversized_source",
        "mounted_base",
        "cross_source_same_name",
    }
)


@dataclass(frozen=True)
class SelectedSourceGatePolicy:
    recall_k: int = 20
    max_latency_ratio: float = 1.50
    max_database_rows_ratio: float = 1.50
    max_peak_memory_ratio: float = 2.00
    max_added_prompt_tokens_per_case: int = 4_000
    max_added_model_calls_per_case: int = 1

    def __post_init__(self) -> None:
        if self.recall_k <= 0:
            raise ValueError("recall_k must be positive")
        if any(
            value < 1.0
            for value in (
                self.max_latency_ratio,
                self.max_database_rows_ratio,
                self.max_peak_memory_ratio,
            )
        ):
            raise ValueError("cost ratios must be at least 1")
        if self.max_added_prompt_tokens_per_case < 0:
            raise ValueError("prompt token budget must be non-negative")
        if self.max_added_model_calls_per_case < 0:
            raise ValueError("model-call budget must be non-negative")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def quality_attestation_digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


_RATE_FIELDS = {
    "evidence_recall_at_k",
    "citation_coverage",
    "citation_validity",
    "grounded_sentence_coverage",
    "answer_nonempty_rate",
    "no_answer_rate",
    "erroneous_refusal_rate",
    "outline_drop_rate",
}
_STRUCTURAL_FIELDS = {
    "groundable_sentences",
    "citations_total",
    "grounded_sentences",
    "outline_sections",
}
_COST_FIELDS = {
    "latency_ms",
    "database_rows",
    "peak_memory_bytes",
    "prompt_tokens",
    "model_calls",
}
_POSITIVE_QUALITY_FIELDS = {
    "evidence_recall_at_k",
    "citation_coverage",
    "citation_validity",
    "grounded_sentence_coverage",
    "answer_nonempty_rate",
}
_NEGATIVE_QUALITY_FIELDS = {
    "no_answer_rate",
    "erroneous_refusal_rate",
    "outline_drop_rate",
}
_ATTESTATION_FIELDS = {
    "suite_version",
    "run_id",
    "corpus_signature",
    "golden_digest",
    "model",
    "approved",
    "hard_failures",
    "quality_failures",
    "cost_failures",
    "case_count",
    "cases",
    "scenario_counts",
    "policy",
    "baseline",
    "shadow",
}


def _validate_metrics(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != (
        _RATE_FIELDS | _STRUCTURAL_FIELDS | _COST_FIELDS
    ):
        raise ValueError("selected-source quality attestation metrics mismatch")
    if any(
        isinstance(value[name], bool)
        or not isinstance(value[name], (int, float))
        or not math.isfinite(float(value[name]))
        or not 0 <= float(value[name]) <= 1
        for name in _RATE_FIELDS
    ) or any(
        isinstance(value[name], bool)
        or not isinstance(value[name], int)
        or value[name] < 0
        for name in (_STRUCTURAL_FIELDS | _COST_FIELDS)
    ):
        raise ValueError("selected-source quality attestation metrics invalid")
    return value


def _assert_no_regression(
    baseline: Mapping[str, Any],
    shadow: Mapping[str, Any],
    *,
    policy: SelectedSourceGatePolicy,
    additive_multiplier: int,
) -> None:
    for name in _POSITIVE_QUALITY_FIELDS:
        if float(shadow[name]) + 1e-12 < float(baseline[name]):
            raise ValueError("selected-source quality attestation regression")
    for name in _NEGATIVE_QUALITY_FIELDS:
        if float(shadow[name]) > float(baseline[name]) + 1e-12:
            raise ValueError("selected-source quality attestation regression")
    for name in _STRUCTURAL_FIELDS:
        if int(baseline[name]) > 0 and int(shadow[name]) == 0:
            raise ValueError(
                "selected-source quality attestation structural regression"
            )

    def over_ratio(name: str, ratio: float) -> bool:
        before = int(baseline[name])
        after = int(shadow[name])
        return after > 0 if before <= 0 else after > before * ratio

    if (
        over_ratio("latency_ms", policy.max_latency_ratio)
        or over_ratio("database_rows", policy.max_database_rows_ratio)
        or over_ratio("peak_memory_bytes", policy.max_peak_memory_ratio)
        or int(shadow["prompt_tokens"]) - int(baseline["prompt_tokens"])
        > policy.max_added_prompt_tokens_per_case * additive_multiplier
        or int(shadow["model_calls"]) - int(baseline["model_calls"])
        > policy.max_added_model_calls_per_case * additive_multiplier
    ):
        raise ValueError("selected-source quality attestation cost regression")


def verify_attestation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate integrity and the versioned activation shape.

    The digest detects accidental file mutation; it is deliberately not a
    signature. Deployment must load attestations only from its trusted artifact
    path and pin the expected corpus/model contract at rollout time.
    """
    payload = dict(value)
    digest = str(payload.pop("attestation_digest", ""))
    if not digest or not hmac.compare_digest(
        digest, quality_attestation_digest(payload)
    ):
        raise ValueError("selected-source quality attestation digest mismatch")
    if int(payload.get("suite_version") or 0) != SELECTED_SOURCE_GRAPH_SUITE_VERSION:
        raise ValueError("selected-source quality attestation version mismatch")
    if payload.get("approved") is not True or any(
        payload.get(key)
        for key in ("hard_failures", "quality_failures", "cost_failures")
    ):
        raise ValueError("selected-source quality gate is not approved")
    if any(
        not isinstance(payload.get(key), list)
        for key in ("hard_failures", "quality_failures", "cost_failures")
    ):
        raise ValueError("selected-source quality attestation failures malformed")
    if set(payload) != _ATTESTATION_FIELDS:
        raise ValueError("selected-source quality attestation shape mismatch")
    if (
        not str(payload["run_id"]).strip()
        or not str(payload["corpus_signature"]).strip()
    ):
        raise ValueError("selected-source quality attestation identity missing")
    if payload["golden_digest"] != SELECTED_SOURCE_GRAPH_GOLDEN_DIGEST:
        raise ValueError("selected-source quality attestation golden mismatch")
    if payload.get("policy") != asdict(SelectedSourceGatePolicy()):
        raise ValueError("selected-source quality attestation policy mismatch")
    model = payload.get("model")
    if (
        not isinstance(model, Mapping)
        or set(model)
        != {"provider", "model", "prompt_version", "temperature", "top_p", "seed"}
        or not all(
            str(model[key]).strip() for key in ("provider", "model", "prompt_version")
        )
        or isinstance(model["temperature"], bool)
        or not isinstance(model["temperature"], (int, float))
        or not math.isfinite(float(model["temperature"]))
        or float(model["temperature"]) < 0
        or isinstance(model["top_p"], bool)
        or not isinstance(model["top_p"], (int, float))
        or not math.isfinite(float(model["top_p"]))
        or not 0 < float(model["top_p"]) <= 1
        or isinstance(model["seed"], bool)
        or not isinstance(model["seed"], int)
    ):
        raise ValueError("selected-source quality attestation model mismatch")
    scenarios = payload.get("scenario_counts")
    if (
        not isinstance(scenarios, Mapping)
        or set(scenarios) != set(REQUIRED_SCENARIOS)
        or any(not isinstance(value, int) or value <= 0 for value in scenarios.values())
        or not isinstance(payload.get("case_count"), int)
        or payload["case_count"] < len(REQUIRED_SCENARIOS)
        or sum(scenarios.values()) != payload["case_count"]
    ):
        raise ValueError("selected-source quality attestation coverage mismatch")
    policy = SelectedSourceGatePolicy(**dict(payload["policy"]))
    baseline = _validate_metrics(payload["baseline"])
    shadow = _validate_metrics(payload["shadow"])
    _assert_no_regression(
        baseline,
        shadow,
        policy=policy,
        additive_multiplier=int(payload["case_count"]),
    )
    case_rows = payload.get("cases")
    if not isinstance(case_rows, list) or len(case_rows) != payload["case_count"]:
        raise ValueError("selected-source quality attestation case metrics mismatch")
    case_ids: set[str] = set()
    case_scenarios: dict[str, int] = {name: 0 for name in REQUIRED_SCENARIOS}
    for row in case_rows:
        if not isinstance(row, Mapping) or set(row) != {
            "case_id",
            "scenario",
            "baseline",
            "shadow",
        }:
            raise ValueError("selected-source quality attestation case shape mismatch")
        case_id = str(row["case_id"]).strip()
        scenario = str(row["scenario"])
        if not case_id or case_id in case_ids or scenario not in REQUIRED_SCENARIOS:
            raise ValueError(
                "selected-source quality attestation case identity mismatch"
            )
        case_ids.add(case_id)
        case_scenarios[scenario] += 1
        _assert_no_regression(
            _validate_metrics(row["baseline"]),
            _validate_metrics(row["shadow"]),
            policy=policy,
            additive_multiplier=1,
        )
    if case_scenarios != dict(scenarios):
        raise ValueError("selected-source quality attestation case coverage mismatch")
    return payload
