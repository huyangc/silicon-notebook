"""Immutable, bounded analysis transferred between retrieval and synthesis.

Findings describe the agent's current assessment, never a new evidence source.
References retain their retrieval identity until the synthesis assembler binds
them to the handles actually admitted in that particular prompt.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

# Protocol rails for model-produced snapshots. Reject the entire update on
# overflow: a clipped qualification or omitted finding can reverse its meaning.
ANALYSIS_FINDINGS_MAX = 12
ANALYSIS_TEXT_MAX_CHARS = 800
ANALYSIS_KEY_MAX_CHARS = 200
ANALYSIS_EVIDENCE_KEYS_MAX = 8
ANALYSIS_UNRESOLVED_MAX = 12
ANALYSIS_STATUSES = frozenset({"supported", "refuted", "uncertain"})
INVALID_ANALYSIS_UPDATE = "invalid_analysis_update"
_CITATION_HANDLE = re.compile(r"k[1-9][0-9]*\Z")


@dataclass(frozen=True, slots=True)
class AnalysisFinding:
    id: str
    aspect: str
    conclusion: str
    status: str
    evidence_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisHandoff:
    findings: tuple[AnalysisFinding, ...] = ()
    unresolved: tuple[str, ...] = ()
    stop_reason: str = "in_progress"
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnalysisEvidence:
    """One already-retrieved dependency; rendering remains a service concern."""

    kind: str
    key: str
    value: object


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(INVALID_ANALYSIS_UPDATE)
    return value


def _texts(value: object, count: int, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > count:
        raise ValueError(INVALID_ANALYSIS_UPDATE)
    return tuple(_text(item, limit) for item in value)


def parse_analysis_update(value: object, previous: AnalysisHandoff) -> AnalysisHandoff:
    """Accept a complete snapshot, or retain and explicitly mark its rejection.

    Missing optional output is backward compatible and leaves the last good
    snapshot untouched. A valid empty snapshot deliberately clears findings.
    The model cannot set the host-owned termination reason.
    """
    if value is None:
        return previous
    try:
        if not isinstance(value, dict):
            raise ValueError(INVALID_ANALYSIS_UPDATE)
        rows = value.get("findings")
        if not isinstance(rows, list) or len(rows) > ANALYSIS_FINDINGS_MAX:
            raise ValueError(INVALID_ANALYSIS_UPDATE)
        findings: list[AnalysisFinding] = []
        ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(INVALID_ANALYSIS_UPDATE)
            finding_id = _text(row.get("id"), ANALYSIS_KEY_MAX_CHARS)
            status = row.get("status")
            if (finding_id in ids or not isinstance(status, str)
                    or status not in ANALYSIS_STATUSES):
                raise ValueError(INVALID_ANALYSIS_UPDATE)
            ids.add(finding_id)
            findings.append(AnalysisFinding(
                id=finding_id,
                aspect=_text(row.get("aspect"), ANALYSIS_TEXT_MAX_CHARS),
                conclusion=_text(row.get("conclusion"), ANALYSIS_TEXT_MAX_CHARS),
                status=status,
                evidence_keys=_texts(row.get("evidence_keys"),
                                     ANALYSIS_EVIDENCE_KEYS_MAX, ANALYSIS_KEY_MAX_CHARS),
            ))
        unresolved = _texts(value.get("unresolved"), ANALYSIS_UNRESOLVED_MAX,
                            ANALYSIS_TEXT_MAX_CHARS)
    except ValueError:
        return replace(previous, issues=tuple(dict.fromkeys(
            (*previous.issues, INVALID_ANALYSIS_UPDATE),
        )))
    return replace(previous, findings=tuple(findings), unresolved=unresolved)


def analysis_evidence_keys(analysis: AnalysisHandoff) -> tuple[str, ...]:
    """Stable dependency identities in first-use order, with no duplicates."""
    return tuple(dict.fromkeys(
        key for finding in analysis.findings for key in finding.evidence_keys
    ))


def render_analysis_handoff(
    analysis: AnalysisHandoff, bindings: Mapping[str, str],
) -> str:
    """Render all findings and missing dependencies, using only current handles.

    Bounds are enforced when parsing the snapshot. No rendering-time prefix
    cut can silently discard an assessment, an unresolved issue or a qualifier.
    The caller owns evidence admission and supplies only handles actually
    present in its prompt, including the section's own namespace offset.
    """
    findings = []
    for finding in analysis.findings:
        available = []
        missing = []
        for key in finding.evidence_keys:
            handle = bindings.get(key)
            if isinstance(handle, str) and _CITATION_HANDLE.fullmatch(handle):
                available.append({"key": key, "citation": f"[{handle}]"})
            else:
                missing.append(key)
        findings.append({
            "id": finding.id, "aspect": finding.aspect,
            "conclusion": finding.conclusion, "status": finding.status,
            "available_evidence": available, "missing_evidence_keys": missing,
            "review_required": bool(missing) or not finding.evidence_keys,
        })
    payload = {
        "findings": findings, "unresolved": analysis.unresolved,
        "stop_reason": analysis.stop_reason, "issues": analysis.issues,
    }
    return (
        "[Analysis handoff: these are prior assessments, not evidence. Verify "
        "every conclusion against the actual evidence and preserve its scope "
        "and conditions. An uncertain assessment is not refuted. Missing "
        "dependencies require review and disclosure; do not present them as "
        "established. Cite only the available_evidence citations mapped here. "
        "Ending retrieval does not prove that the question is resolved.]\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
