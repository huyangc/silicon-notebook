"""Admit an analysis snapshot's dependencies without silently clipping them.

Only already-retrieved values are eligible. Native renderers retain source
scope and citation semantics; this assembler owns whole-entry admission and
section-local handles, never retrieval or model calls.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.analysis_handoff import (
    AnalysisEvidence,
    AnalysisHandoff,
    analysis_evidence_keys,
)
from app.services.context_spans import Glue, concat

# The free band between sampled-document evidence (7000) and paper cards
# (9000), shifted by the same per-section offset as every other evidence kind.
ANALYSIS_EVIDENCE_KEY_BASE = 8000
_DEPENDENCY_HEADING = "Analysis dependency evidence\n"
_RENDERABLE_KINDS = frozenset({"knowledge", "chunk", "element"})


@dataclass(frozen=True, slots=True)
class AnalysisEvidenceAssembly:
    context_block: str
    id_map: dict[str, Any]
    bindings: dict[str, str]
    missing: tuple[str, ...]
    added_chars: int


def _bindings(id_map: Mapping[str, Any]) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for handle, entry in id_map.items():
        if (not isinstance(entry, Mapping)
                or entry.get("analysis_projection_complete") is False):
            continue
        for field in ("object_id", "element_id"):
            key = entry.get(field)
            if isinstance(key, str) and key:
                bindings.setdefault(key, handle)
    return bindings


def _complete_passage(dependency: AnalysisEvidence, context: str) -> bool:
    if dependency.kind not in {"chunk", "element"}:
        return True
    text = getattr(dependency.value, "text", None)
    return isinstance(text, str) and bool(text.strip()) and text in context


def assemble_analysis_evidence(
    context_block: str,
    id_map: Mapping[str, Any],
    analysis: AnalysisHandoff,
    evidence: Sequence[AnalysisEvidence],
    *,
    budget_chars: int,
    key_offset: int,
    render: Callable[..., tuple[str, Mapping[str, Any]]],
    aliases: Mapping[str, str] | None = None,
) -> AnalysisEvidenceAssembly:
    """Reuse complete bindings, then admit missing dependencies atomically.

    ``budget_chars`` bounds only the additional evidence partition, including
    its heading and separators. A renderer's partial or oversized result is
    rejected as a whole. The missing list is in first-use order, so callers
    can disclose every unsupported assessment without reinterpreting the text.
    """
    wanted = analysis_evidence_keys(analysis)
    pool = {item.key: item for item in evidence}
    output_map = dict(id_map)
    existing = _bindings(id_map)
    # External evidence uses public, run-stable xN identities in reflection;
    # its internal key contains a plugin id and must never enter a prompt.
    # Aliases bind only when the underlying item was actually admitted.
    for alias, target in (aliases or {}).items():
        if target in existing:
            existing[alias] = existing[target]
    bindings: dict[str, str] = {}
    added_chars = 0
    heading_written = False
    budget = max(0, budget_chars)
    for index, key in enumerate(wanted):
        dependency = pool.get(key)
        if key in existing and (
            dependency is None or _complete_passage(dependency, context_block)
        ):
            bindings[key] = existing[key]
            continue
        if dependency is None or dependency.kind not in _RENDERABLE_KINDS:
            continue
        separator = "\n\n" if context_block else ""
        prefix = separator + ("" if heading_written else _DEPENDENCY_HEADING)
        remaining = budget - added_chars - len(prefix)
        if remaining <= 0:
            continue
        block, block_map = render(
            dependency.kind, dependency.value,
            id_offset=key_offset + ANALYSIS_EVIDENCE_KEY_BASE + index,
            budget_chars=remaining,
        )
        candidate_bindings = _bindings(block_map)
        if (
            not block or block == "(none)" or len(block) > remaining
            or key not in candidate_bindings
            or not _complete_passage(dependency, block)
            or output_map.keys() & block_map.keys()
        ):
            continue
        context_block = concat(context_block, Glue(prefix), block)
        added_chars += len(prefix) + len(block)
        output_map.update(block_map)
        bindings[key] = candidate_bindings[key]
        heading_written = True
    return AnalysisEvidenceAssembly(
        context_block=context_block, id_map=output_map, bindings=bindings,
        missing=tuple(key for key in wanted if key not in bindings),
        added_chars=added_chars,
    )
