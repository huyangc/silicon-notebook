"""Quality-gated rollout decision for selected-source graph enrichment.

This service owns no retrieval wiring.  It decides whether a later Ask/Report
consumer may run shadow-only or user-visible enrichment after verifying the
content-free evaluation attestation produced by ``app.eval.selected_source_graph``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from app.services.source_graph_quality import verify_attestation

SourceGraphRolloutMode = Literal["off", "shadow", "allowlist", "rollout", "on"]


@dataclass(frozen=True)
class SourceGraphRolloutDecision:
    enabled: bool
    shadow_only: bool
    reason: str


def load_quality_attestation(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("selected-source quality attestation must be an object")
    # Validate the deployment-owned file, but retain its signed digest.  The
    # rollout decision verifies the object again at the point of use; returning
    # ``verify_attestation``'s normalized payload would drop that digest and
    # make every valid file fail the second verification.
    raw = dict(value)
    verify_attestation(raw)
    return raw


def _stable_rollout_bucket(notebook_id: str) -> int:
    digest = hashlib.sha256(notebook_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def decide_source_graph_rollout(
    *,
    notebook_id: str,
    mode: SourceGraphRolloutMode | str,
    attestation: Mapping[str, Any] | None,
    notebook_allowlist: frozenset[str] = frozenset(),
    rollout_percent: float = 0.0,
    expected_corpus_signature: str | None = None,
    expected_model: Mapping[str, Any] | None = None,
) -> SourceGraphRolloutDecision:
    normalized = str(mode or "off").strip().lower()
    if normalized not in {"off", "shadow", "allowlist", "rollout", "on"}:
        return SourceGraphRolloutDecision(False, False, "invalid_mode")
    if normalized == "off":
        return SourceGraphRolloutDecision(False, False, "disabled")

    # Shadow never changes user-visible evidence.  It is the data-collection
    # lane that produces an attestation and therefore cannot require one.
    if normalized == "shadow":
        return SourceGraphRolloutDecision(True, True, "shadow")

    if attestation is None:
        return SourceGraphRolloutDecision(False, False, "quality_gate_missing")
    if not expected_corpus_signature or expected_model is None:
        return SourceGraphRolloutDecision(False, False, "quality_gate_pin_missing")
    try:
        verified = verify_attestation(attestation)
    except (TypeError, ValueError):
        return SourceGraphRolloutDecision(False, False, "quality_gate_rejected")
    if verified.get("corpus_signature") != expected_corpus_signature:
        return SourceGraphRolloutDecision(False, False, "quality_gate_corpus_mismatch")
    if expected_model is not None and verified.get("model") != dict(expected_model):
        return SourceGraphRolloutDecision(False, False, "quality_gate_model_mismatch")

    if normalized == "allowlist":
        allowed = "*" in notebook_allowlist or notebook_id in notebook_allowlist
        return SourceGraphRolloutDecision(
            allowed, False, "allowlisted" if allowed else "not_allowlisted"
        )
    if normalized == "rollout":
        if not 0.0 <= rollout_percent <= 100.0:
            return SourceGraphRolloutDecision(False, False, "invalid_rollout_percent")
        enabled = _stable_rollout_bucket(notebook_id) < round(rollout_percent * 100)
        return SourceGraphRolloutDecision(
            enabled, False, "rollout" if enabled else "rollout_excluded"
        )
    return SourceGraphRolloutDecision(True, False, "quality_approved")
