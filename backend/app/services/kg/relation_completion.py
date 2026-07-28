"""Bounded, post-extraction relation completion.

This module never extracts nodes and never scans a notebook.  Its caller
hydrates one bounded source-local repository window, this module samples a
bounded cross-window candidate set, lets the configured models choose only
from server-issued ids, and returns validated proposals for a generation-CAS
write owned by the caller.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app.services.kg.edge_schema import EDGE_SPECS, canonical_edge_endpoints
from app.services.kg.json_utils import safe_json
from app.services.retrieval import _tokens


COMPLETION_SCHEMA_VERSION = 1
COMPLETION_EVIDENCE_PER_OBJECT = 6


@dataclass(frozen=True)
class CompletionCandidate:
    candidate_id: str
    source_object_id: str
    target_object_id: str
    source_type: str
    target_type: str
    source_name: str
    target_name: str
    allowed_edge_types: tuple[str, ...]
    allowed_element_ids: tuple[str, ...]
    excerpts: tuple[dict, ...]
    score: float


@dataclass(frozen=True)
class CompletionProposal:
    candidate: CompletionCandidate
    edge_type: str
    evidence_element_ids: tuple[str, ...]
    confidence: float


def completion_mode_for_notebook(settings: Any, notebook_id: str) -> str:
    mode = str(getattr(settings, "kg_relation_completion_mode", "off") or "off").lower()
    if mode not in {"off", "shadow", "write"} or mode == "off":
        return "off"
    allowlist = {
        item.strip() for item in str(
            getattr(settings, "kg_relation_completion_notebook_allowlist", "") or ""
        ).split(",") if item.strip()
    }
    if "*" in allowlist or notebook_id in allowlist:
        return mode
    percent = max(0, min(100, int(
        getattr(settings, "kg_relation_completion_rollout_percent", 0) or 0
    )))
    bucket = int(hashlib.sha256(notebook_id.encode("utf-8")).hexdigest()[:8], 16) % 100
    return mode if bucket < percent else "off"


def _name(obj: dict) -> str:
    return str((obj.get("payload") or {}).get("name") or "").strip()


def _section(obj: dict) -> str:
    payload_section = str((obj.get("payload") or {}).get("section_path") or "").strip()
    if payload_section:
        return payload_section
    for evidence in obj.get("evidence") or ():
        if isinstance(evidence, dict) and evidence.get("location_label"):
            return str(evidence["location_label"])
    return "<unknown>"


def _spread_sample(objects: Sequence[dict], run_id: str, limit: int) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for obj in objects:
        if obj.get("_oid") and _name(obj):
            groups.setdefault(_section(obj), []).append(obj)
    for section, rows in groups.items():
        rows.sort(key=lambda obj: hashlib.sha256(
            f"{run_id}\x00{section}\x00{obj['_oid']}".encode("utf-8")
        ).digest())
    sections = sorted(groups, key=lambda section: hashlib.sha256(
        f"{run_id}\x00{section}".encode("utf-8")
    ).digest())
    sampled: list[dict] = []
    offset = 0
    while sections and len(sampled) < max(0, limit):
        remaining = []
        for section in sections:
            rows = groups[section]
            if offset < len(rows):
                sampled.append(rows[offset])
                if len(sampled) >= limit:
                    break
            if offset + 1 < len(rows):
                remaining.append(section)
        sections = remaining
        offset += 1
    return sampled


def generate_candidates(
    objects: Sequence[dict],
    relations: Sequence[dict],
    elements: Sequence[Any],
    *,
    run_id: str,
    max_objects: int,
    max_pairs: int,
    excerpt_chars: int,
    anchor_ids: Iterable[str] | None = None,
    section_quota: int = 0,
) -> list[CompletionCandidate]:
    """Generate a deterministic, section-spread, bounded directed pair set."""
    sampled = _spread_sample(objects, run_id, max_objects)
    local_to_oid = {obj.get("local_id"): obj.get("_oid") for obj in objects}
    existing = set()
    for rel in relations:
        source_id = local_to_oid.get(rel.get("source_local_id")) or rel.get("source_object_id")
        target_id = local_to_oid.get(rel.get("target_local_id")) or rel.get("target_object_id")
        edge_type = str(rel.get("edge_type") or "")
        if source_id and target_id and edge_type in EDGE_SPECS:
            source_id, target_id = canonical_edge_endpoints(
                edge_type, str(source_id), str(target_id)
            )
            existing.add((source_id, target_id, edge_type))
    anchors = {str(value) for value in anchor_ids or () if value}
    element_map = {str(element.id): element for element in elements}
    ranked = []
    for left_index, left in enumerate(sampled):
        for right in sampled[left_index + 1:]:
            left_id, right_id = str(left["_oid"]), str(right["_oid"])
            if anchors and left_id not in anchors and right_id not in anchors:
                continue
            left_elements = {
                str(item.get("element_id")) for item in left.get("evidence") or ()
                if isinstance(item, dict) and item.get("element_id") in element_map
            }
            right_elements = {
                str(item.get("element_id")) for item in right.get("evidence") or ()
                if isinstance(item, dict) and item.get("element_id") in element_map
            }
            # Existing window extraction already saw same-element pairs.
            if not left_elements or not right_elements or left_elements & right_elements:
                continue
            left_name, right_name = _name(left), _name(right)
            common = set(_tokens(left_name)) & set(_tokens(right_name))
            left_quotes = " ".join(str(item.get("quoted_span") or "") for item in left.get("evidence") or () if isinstance(item, dict))
            right_quotes = " ".join(str(item.get("quoted_span") or "") for item in right.get("evidence") or () if isinstance(item, dict))
            mention = int(left_name.lower() in right_quotes.lower()) + int(
                right_name.lower() in left_quotes.lower()
            )
            score = float(
                len(common) + 2 * mention
                + max(float(left.get("_completion_score") or 0.0),
                      float(right.get("_completion_score") or 0.0))
            )
            if score <= 0:
                continue
            for source, target, source_elements, target_elements in (
                (left, right, left_elements, right_elements),
                (right, left, right_elements, left_elements),
            ):
                source_type = str(source.get("object_type") or "").lower()
                target_type = str(target.get("object_type") or "").lower()
                allowed = tuple(sorted(
                    edge_type for edge_type, spec in EDGE_SPECS.items()
                    if spec.category == "reasoning"
                    and (source_type, target_type) in spec.allowed_pairs
                ))
                if not allowed:
                    continue
                allowed = tuple(
                    edge_type for edge_type in allowed
                    if canonical_edge_endpoints(
                        edge_type, str(source["_oid"]), str(target["_oid"])
                    ) + (edge_type,) not in existing
                )
                if not allowed:
                    continue
                evidence_ids = tuple(sorted(source_elements | target_elements))[:6]
                excerpts = []
                for element_id in evidence_ids:
                    element = element_map[element_id]
                    excerpts.append({
                        "element_id": element_id,
                        "location_label": str(element.location_label or ""),
                        "text": str(element.text or "")[:max(1, excerpt_chars)],
                    })
                stable = hashlib.sha256(
                    f"{run_id}\x00{source['_oid']}\x00{target['_oid']}".encode("utf-8")
                ).hexdigest()[:16]
                priority = int(left.get("_completion_priority") or 0) + int(
                    right.get("_completion_priority") or 0
                )
                ranked.append((
                    -score, -priority, stable, _section(source),
                    CompletionCandidate(
                        candidate_id=f"c-{stable}",
                        source_object_id=str(source["_oid"]),
                        target_object_id=str(target["_oid"]),
                        source_type=source_type,
                        target_type=target_type,
                        source_name=_name(source),
                        target_name=_name(target),
                        allowed_edge_types=allowed,
                        allowed_element_ids=evidence_ids,
                        excerpts=tuple(excerpts),
                        score=score,
                    ),
                ))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    selected = []
    per_section: dict[str, int] = {}
    for item in ranked:
        section = item[3]
        if section_quota > 0 and per_section.get(section, 0) >= section_quota:
            continue
        selected.append(item[4])
        per_section[section] = per_section.get(section, 0) + 1
        if len(selected) >= max(0, max_pairs):
            break
    return selected


def _candidate_payload(candidate: CompletionCandidate) -> dict:
    return {
        "candidate_id": candidate.candidate_id,
        "source_object_id": candidate.source_object_id,
        "target_object_id": candidate.target_object_id,
        "source_type": candidate.source_type,
        "target_type": candidate.target_type,
        "source_name": candidate.source_name,
        "target_name": candidate.target_name,
        "allowed_edge_types": list(candidate.allowed_edge_types),
        "allowed_evidence": list(candidate.excerpts),
    }


def propose_batch(client: Any, candidates: Sequence[CompletionCandidate]) -> list[CompletionProposal]:
    if not candidates or not getattr(client, "configured", False):
        return []
    payload = [_candidate_payload(candidate) for candidate in candidates]
    raw = client.chat_json([
        {"role": "system", "content": (
            "Select only direct relations explicitly stated by the supplied excerpts. "
            "Use only issued candidate/object/edge/element ids. Similarity or co-occurrence "
            "alone is not a relation. Return an empty relations list when uncertain."
        )},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ], '{"relations":[{"candidate_id":"","source_object_id":"","target_object_id":"","edge_type":"","evidence_element_ids":[""],"confidence":0.0}]}')
    data = safe_json(raw)
    if not isinstance(data, dict) or set(data) != {"relations"} or not isinstance(
        data.get("relations"), list
    ):
        raise ValueError("invalid relation completion proposal envelope")
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    proposals = []
    for item in data.get("relations") or ():
        if not isinstance(item, dict):
            continue
        if set(item) != {
            "candidate_id", "source_object_id", "target_object_id", "edge_type",
            "evidence_element_ids", "confidence",
        }:
            continue
        candidate = by_id.get(str(item.get("candidate_id") or ""))
        if candidate is None:
            continue
        if (item.get("source_object_id"), item.get("target_object_id")) != (
            candidate.source_object_id, candidate.target_object_id
        ):
            continue
        edge_type = str(item.get("edge_type") or "")
        if edge_type not in candidate.allowed_edge_types:
            continue
        raw_evidence_ids = item.get("evidence_element_ids")
        if not isinstance(raw_evidence_ids, list) or not all(
            isinstance(value, str) and value for value in raw_evidence_ids
        ) or len(set(raw_evidence_ids)) != len(raw_evidence_ids):
            continue
        evidence_ids = tuple(raw_evidence_ids)
        if not evidence_ids or not set(evidence_ids) <= set(candidate.allowed_element_ids):
            continue
        try:
            raw_confidence = item.get("confidence")
            if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
                continue
            confidence = max(0.0, min(1.0, float(raw_confidence)))
        except (TypeError, ValueError):
            continue
        proposals.append(CompletionProposal(
            candidate, edge_type, evidence_ids, confidence
        ))
    return proposals


def verify_batch(client: Any, proposals: Sequence[CompletionProposal]) -> list[CompletionProposal]:
    if not proposals or not getattr(client, "configured", False):
        return []
    raw = client.chat_json([
        {"role": "system", "content": (
            "Verify whether each proposed directed relation is explicitly supported by its "
            "excerpts. Reject mere co-occurrence, topical similarity, or inference."
        )},
        {"role": "user", "content": json.dumps([
            {**_candidate_payload(proposal.candidate),
             "allowed_evidence": [
                 excerpt for excerpt in proposal.candidate.excerpts
                 if excerpt.get("element_id") in proposal.evidence_element_ids
             ],
             "proposed_edge_type": proposal.edge_type,
             "evidence_element_ids": list(proposal.evidence_element_ids)}
            for proposal in proposals
        ], ensure_ascii=False)},
    ], '{"verdicts":[{"candidate_id":"","valid":true,"reason":""}]}')
    data = safe_json(raw)
    if not isinstance(data, dict) or set(data) != {"verdicts"} or not isinstance(
        data.get("verdicts"), list
    ):
        raise ValueError("invalid relation completion verifier envelope")
    valid_ids = {
        str(item.get("candidate_id") or "")
        for item in data.get("verdicts") or ()
        if isinstance(item, dict)
        and set(item) == {"candidate_id", "valid", "reason"}
        and isinstance(item.get("candidate_id"), str)
        and isinstance(item.get("valid"), bool)
        and isinstance(item.get("reason"), str)
        and item.get("valid") is True
    }
    return [proposal for proposal in proposals if proposal.candidate.candidate_id in valid_ids]


def candidate_batches(
    candidates: Sequence[CompletionCandidate], *, pair_limit: int,
    char_limit: int, max_batches: int
) -> list[list[CompletionCandidate]]:
    """Deterministically pack complete serialized candidate payloads under rails."""
    batches: list[list[CompletionCandidate]] = []
    current: list[CompletionCandidate] = []
    pair_limit = max(1, int(pair_limit))
    char_limit = max(512, int(char_limit))
    for candidate in candidates:
        trial = [*current, candidate]
        size = len(json.dumps(
            [_candidate_payload(item) for item in trial], ensure_ascii=False,
            separators=(",", ":"),
        ))
        if size > char_limit or len(trial) > pair_limit:
            if current:
                batches.append(current)
                if len(batches) >= max(0, max_batches):
                    return batches
                current = []
            single_size = len(json.dumps(
                [_candidate_payload(candidate)], ensure_ascii=False,
                separators=(",", ":"),
            ))
            if single_size > char_limit:
                continue
        current.append(candidate)
    if current and len(batches) < max(0, max_batches):
        batches.append(current)
    return batches


def stable_relation_id(
    notebook_id: str, run_id: str, proposal: CompletionProposal
) -> str:
    source, target = canonical_edge_endpoints(
        proposal.edge_type,
        proposal.candidate.source_object_id,
        proposal.candidate.target_object_id,
    )
    digest = hashlib.sha256(
        f"{notebook_id}\x00{run_id}\x00{source}\x00{target}\x00"
        f"{proposal.edge_type}\x00{COMPLETION_SCHEMA_VERSION}".encode("utf-8")
    ).hexdigest()
    return f"relc-{digest[:32]}"


__all__ = [
    "COMPLETION_EVIDENCE_PER_OBJECT", "COMPLETION_SCHEMA_VERSION",
    "CompletionCandidate", "CompletionProposal",
    "candidate_batches", "completion_mode_for_notebook", "generate_candidates", "propose_batch",
    "stable_relation_id", "verify_batch",
]
