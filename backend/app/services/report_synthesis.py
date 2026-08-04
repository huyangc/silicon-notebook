"""Deterministic contracts for report-wide synthesis.

The model may propose a frame, a synthesis blueprint, and a per-section claim
ledger.  This module is the fail-open boundary around those optional structures:
only bounded, evidence-keyed data crosses into report drafting.  It deliberately
has no repository or model dependency so malformed optional output can never make
the report job fail.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Iterable, Mapping, Sequence


FRAME_MAX_FACETS = 8
FRAME_MAX_VALUES = 12
FRAME_MAX_AXES = 8
BLUEPRINT_MAX_DEFINITIONS = 24
BLUEPRINT_MAX_CLAIMS = 60
BLUEPRINT_SECTION_MAX_CLAIMS = 12
CLAIM_LEDGER_MAX_CLAIMS = 24
SYNTHESIS_EVIDENCE_MAX_CHARS = 36_000
# The final editor remains deterministically bounded even though the deployment
# no longer declares an application-side total context window.  Provider/model
# bindings own the actual prompt+completion compatibility check.
EDITOR_CONTEXT_MAX_CHARS = 143_712

_CLAIM_TYPES = frozenset({
    "fact", "comparison", "trend", "inference", "general",
})
_STRONG_TREND = re.compile(
    r"(?:高置信|确定(?:会|将|性)|必(?:然|将)|已经?成为主流|全面替代|"
    r"high[- ]confidence|certain(?:ly)?|will inevitably|has become mainstream|"
    r"will replace)",
    re.IGNORECASE,
)


def _text(value: object, chars: int) -> str:
    return str(value or "").strip()[:chars]


def _strings(value: object, limit: int, chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        _text(item, chars) for item in value if _text(item, chars)
    ))[:limit]


def _confidence(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def normalize_report_frame(value: object, *, strict: bool = False) -> dict | None:
    """Return a bounded frame or ``None`` for absent/model-invalid output.

    ``strict`` is reserved for the user-confirmed PATCH surface: invalid user input
    is an actionable 422, while invalid model output silently disables the feature.
    """
    try:
        if value in (None, "", {}):
            return None
        if not isinstance(value, dict):
            raise ValueError("框架必须是对象")
        raw_facets = value.get("facets") or []
        raw_axes = value.get("axes") or []
        if not isinstance(raw_facets, list) or not isinstance(raw_axes, list):
            raise ValueError("分类维度和比较轴必须是列表")
        if len(raw_facets) > FRAME_MAX_FACETS or len(raw_axes) > FRAME_MAX_AXES:
            raise ValueError("框架维度数量超出上限")

        facets: list[dict] = []
        facet_ids: set[str] = set()
        for index, raw in enumerate(raw_facets, 1):
            if not isinstance(raw, dict):
                raise ValueError("分类维度格式无效")
            name = _text(raw.get("name"), 120)
            facet_id = _text(raw.get("id"), 80) or f"facet-{index}"
            values = _strings(raw.get("values"), FRAME_MAX_VALUES, 120)
            if not name or facet_id in facet_ids:
                raise ValueError("分类维度缺少名称或标识重复")
            facet_ids.add(facet_id)
            facets.append({
                "id": facet_id,
                "name": name,
                "values": values,
                "exclusive": bool(raw.get("exclusive", False)),
            })

        axes: list[dict] = []
        axis_ids: set[str] = set()
        for index, raw in enumerate(raw_axes, 1):
            if not isinstance(raw, dict):
                raise ValueError("比较轴格式无效")
            name = _text(raw.get("name"), 120)
            axis_id = _text(raw.get("id"), 80) or f"axis-{index}"
            if not name or axis_id in axis_ids:
                raise ValueError("比较轴缺少名称或标识重复")
            axis_ids.add(axis_id)
            axes.append({
                "id": axis_id,
                "name": name,
                "condition_fields": _strings(
                    raw.get("condition_fields"), 8, 120
                ),
            })

        frame = {
            "subject_kind": _text(value.get("subject_kind"), 160),
            "facets": facets,
            "axes": axes,
            "instance_policy": _text(value.get("instance_policy"), 600),
        }
        return frame if any((frame["subject_kind"], facets, axes,
                             frame["instance_policy"])) else None
    except (TypeError, ValueError):
        if strict:
            raise
        return None


def _evidence_id(item: object, *names: str) -> str:
    for name in names:
        value = _text(getattr(item, name, ""), 160)
        if value:
            return value
    return ""


def _item_card(item: object, evidence_id: str, *, kind: str) -> dict:
    payload = getattr(item, "payload", None)
    payload = payload if isinstance(payload, Mapping) else {}
    name = (
        _text(payload.get("name"), 240)
        or _text(getattr(item, "source_title", ""), 240)
        or _text(getattr(item, "section_path", ""), 240)
        or evidence_id
    )
    body = (
        _text(payload.get("definition"), 900)
        or _text(payload.get("evidence"), 900)
        or _text(getattr(item, "text", ""), 900)
    )
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "name": name,
        "body": body,
        "relevance": round(float(getattr(item, "relevance", 0.0) or
                                 getattr(item, "score", 0.0) or 0.0), 4),
    }


def synthesis_evidence_payload(
    outline: Sequence[dict], results: Sequence[object], *,
    max_chars: int = SYNTHESIS_EVIDENCE_MAX_CHARS,
) -> tuple[list[dict], set[str]]:
    """Build a fair, report-wide evidence view without grouping by paper."""
    sections: list[dict] = []
    legal: set[str] = set()
    if not outline or not results:
        return sections, legal
    # Preserve fair section shares without imposing a per-section minimum that
    # could exceed a caller-provided evidence ceiling.
    per_section = max_chars // max(1, min(len(outline), len(results)))
    for index, (section, result) in enumerate(zip(outline, results), 1):
        cards: list[dict] = []
        seen: set[str] = set()
        candidates: list[tuple[object, str, str]] = []
        candidates.extend(
            (item, _evidence_id(item, "object_id"), "knowledge")
            for item in list(getattr(result, "top_hits", None) or [])
        )
        candidates.extend(
            (item, _evidence_id(item, "element_id"), "source_element")
            for item in list(getattr(result, "elements", None) or [])
        )
        candidates.extend(
            (item, _evidence_id(item, "chunk_id"), "source_passage")
            for item in list(getattr(result, "chunks", None) or [])
        )
        used = 0
        for item, evidence_id, kind in candidates:
            if not evidence_id or evidence_id in seen:
                continue
            card = _item_card(item, evidence_id, kind=kind)
            encoded = json.dumps(card, ensure_ascii=False)
            if used + len(encoded) > per_section:
                continue
            used += len(encoded)
            seen.add(evidence_id)
            legal.add(evidence_id)
            cards.append(card)
        sections.append({
            "section_id": f"section-{index}",
            "title": _text(section.get("title"), 200),
            "scope": _text(section.get("scope"), 500),
            "intent_ids": _strings(section.get("intent_ids"), 16, 80),
            "evidence": cards,
        })
    return sections, legal


def normalize_synthesis_blueprint(
    value: object, *, outline: Sequence[dict], legal_evidence_ids: set[str],
    frame: dict | None,
) -> dict | None:
    """Validate a blueprint atomically; one illegal binding discards all of it."""
    try:
        if not isinstance(value, dict):
            return None
        section_ids = {f"section-{index}" for index in range(1, len(outline) + 1)}
        raw_sections = value.get("sections")
        raw_claims = value.get("claims")
        if not isinstance(raw_sections, list) or not isinstance(raw_claims, list):
            return None
        if len(raw_claims) > BLUEPRINT_MAX_CLAIMS:
            return None
        facet_ids = {
            _text(row.get("id"), 80) for row in ((frame or {}).get("facets") or [])
            if isinstance(row, dict)
        }

        claims: list[dict] = []
        claim_ids: set[str] = set()
        owners: dict[str, str] = {}
        for raw in raw_claims:
            if not isinstance(raw, dict):
                return None
            claim_id = _text(raw.get("id"), 80)
            statement = _text(raw.get("statement"), 1200)
            claim_type = _text(raw.get("type"), 24).lower()
            owner = _text(raw.get("owner_section_id"), 80)
            evidence_keys = _strings(raw.get("evidence_keys"), 16, 160)
            counter = _strings(raw.get("counterevidence_keys"), 12, 160)
            facet_id = _text(raw.get("facet_id"), 80)
            if (
                not claim_id or claim_id in claim_ids or not statement
                or claim_type not in _CLAIM_TYPES or owner not in section_ids
                or any(key not in legal_evidence_ids for key in evidence_keys + counter)
                or (facet_id and facet_id not in facet_ids)
            ):
                return None
            if claim_type in {"fact", "comparison", "trend"} and not evidence_keys:
                return None
            claim_ids.add(claim_id)
            owners[claim_id] = owner
            claims.append({
                "id": claim_id,
                "statement": statement,
                "type": claim_type,
                "facet_id": facet_id,
                "evidence_keys": evidence_keys,
                "counterevidence_keys": counter,
                "conditions": _strings(raw.get("conditions"), 8, 300),
                "owner_section_id": owner,
            })

        sections: list[dict] = []
        seen_sections: set[str] = set()
        for raw in raw_sections:
            if not isinstance(raw, dict):
                return None
            section_id = _text(raw.get("section_id"), 80)
            raw_claim_ids = raw.get("claim_ids")
            if (
                not isinstance(raw_claim_ids, list)
                or len(raw_claim_ids) > BLUEPRINT_SECTION_MAX_CLAIMS
            ):
                return None
            ids = _strings(raw_claim_ids, BLUEPRINT_SECTION_MAX_CLAIMS, 80)
            if (
                section_id not in section_ids or section_id in seen_sections
                or any(cid not in claim_ids or owners[cid] != section_id for cid in ids)
            ):
                return None
            seen_sections.add(section_id)
            sections.append({
                "section_id": section_id,
                "thesis": _text(raw.get("thesis"), 1000),
                "claim_ids": ids,
                "must_contrast": _strings(raw.get("must_contrast"), 8, 400),
                "handoff": _text(raw.get("handoff"), 500),
                "do_not_repeat": _strings(raw.get("do_not_repeat"), 24, 80),
            })
        if seen_sections != section_ids:
            return None
        assigned = {cid for row in sections for cid in row["claim_ids"]}
        if assigned != claim_ids:
            return None

        definitions: list[dict] = []
        raw_definitions = value.get("shared_definitions") or []
        if not isinstance(raw_definitions, list) or len(raw_definitions) > BLUEPRINT_MAX_DEFINITIONS:
            return None
        for raw in raw_definitions:
            if not isinstance(raw, dict):
                return None
            keys = _strings(raw.get("evidence_keys"), 12, 160)
            if any(key not in legal_evidence_ids for key in keys):
                return None
            term = _text(raw.get("term"), 160)
            definition = _text(raw.get("definition"), 800)
            if term and definition:
                definitions.append({
                    "term": term, "definition": definition,
                    "evidence_keys": keys,
                })
        return {
            "version": 1,
            "central_answer": _text(value.get("central_answer"), 1600),
            "shared_definitions": definitions,
            "claims": claims,
            "sections": sections,
        }
    except (TypeError, ValueError):
        return None


def blueprint_for_section(blueprint: dict | None, section_index: int) -> dict | None:
    if not blueprint:
        return None
    section_id = f"section-{section_index + 1}"
    section = next(
        (row for row in blueprint.get("sections") or []
         if row.get("section_id") == section_id),
        None,
    )
    if not section:
        return None
    wanted = set(section.get("claim_ids") or [])
    return {
        "central_answer": blueprint.get("central_answer", ""),
        "shared_definitions": list(blueprint.get("shared_definitions") or []),
        "section": section,
        "claims": [
            row for row in (blueprint.get("claims") or []) if row.get("id") in wanted
        ],
    }


def localize_blueprint_evidence(section_blueprint: dict | None,
                                id_map: Mapping[str, Mapping[str, object]]) -> dict | None:
    """Translate report-wide evidence ids to the local ``[k]`` ids a writer sees."""
    if not section_blueprint:
        return None
    by_evidence: dict[str, str] = {}
    for local_key, context in id_map.items():
        for field in ("object_id", "element_id", "chunk_id"):
            evidence_id = _text(context.get(field), 160)
            if evidence_id:
                by_evidence.setdefault(evidence_id, str(local_key))

    claims: list[dict] = []
    kept_ids: set[str] = set()
    for raw in section_blueprint.get("claims") or []:
        claim = dict(raw)
        claim["evidence_keys"] = [
            by_evidence[key] for key in raw.get("evidence_keys") or []
            if key in by_evidence
        ]
        claim["counterevidence_keys"] = [
            by_evidence[key] for key in raw.get("counterevidence_keys") or []
            if key in by_evidence
        ]
        if claim.get("type") in {"fact", "comparison", "trend"} and not claim["evidence_keys"]:
            continue
        claims.append(claim)
        kept_ids.add(str(claim.get("id") or ""))
    section = dict(section_blueprint.get("section") or {})
    section["claim_ids"] = [
        claim_id for claim_id in section.get("claim_ids") or [] if claim_id in kept_ids
    ]
    definitions = []
    for raw in section_blueprint.get("shared_definitions") or []:
        row = dict(raw)
        row["evidence_keys"] = [
            by_evidence[key] for key in raw.get("evidence_keys") or []
            if key in by_evidence
        ]
        definitions.append(row)
    return {
        "central_answer": section_blueprint.get("central_answer", ""),
        "shared_definitions": definitions,
        "section": section,
        "claims": claims,
    }


def _anchor_keys(text: str) -> set[str]:
    return set(re.findall(r"k\d+", text or ""))


def normalize_claim_ledger(
    value: object, *, markdown: str, legal_anchor_keys: set[str],
    blueprint_claim_ids: set[str] | None = None, frame: dict | None = None,
) -> tuple[list[dict], str]:
    """Bind claims to exact emitted prose and same-sentence citation markers."""
    if not isinstance(value, list):
        return [], "missing"
    if len(value) > CLAIM_LEDGER_MAX_CLAIMS:
        return [], "invalid"
    facet_map = {
        _text(row.get("id"), 80): row
        for row in ((frame or {}).get("facets") or []) if isinstance(row, dict)
    }
    out: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            return [], "invalid"
        claim_id = _text(raw.get("claim_id"), 80) or f"claim-{index}"
        statement = _text(raw.get("statement"), 1600)
        claim_type = _text(raw.get("type"), 24).lower()
        evidence_keys = _strings(raw.get("evidence_keys"), 16, 32)
        if (
            claim_id in seen or not statement or statement not in markdown
            or claim_type not in _CLAIM_TYPES
            or any(key not in legal_anchor_keys for key in evidence_keys)
            or any(key not in _anchor_keys(statement) for key in evidence_keys)
            or (blueprint_claim_ids is not None and claim_id not in blueprint_claim_ids)
        ):
            return [], "invalid"
        if claim_type in {"fact", "comparison", "trend"} and not evidence_keys:
            return [], "invalid"
        conditions = _strings(raw.get("conditions"), 8, 300)
        if claim_type == "comparison" and not conditions and not bool(
            raw.get("same_paper_baseline", False)
        ):
            return [], "invalid"
        assignments: dict[str, str] = {}
        raw_assignments = raw.get("frame_assignments") or {}
        if not isinstance(raw_assignments, dict):
            return [], "invalid"
        for facet_id, raw_value in raw_assignments.items():
            facet_id = _text(facet_id, 80)
            assigned = _text(raw_value, 120)
            facet = facet_map.get(facet_id)
            if not facet or (facet.get("values") and assigned not in facet["values"]):
                return [], "invalid"
            assignments[facet_id] = assigned
        seen.add(claim_id)
        out.append({
            "claim_id": claim_id,
            "statement": statement,
            "statement_hash": hashlib.sha256(statement.encode("utf-8")).hexdigest()[:16],
            "type": claim_type,
            "entities": _strings(raw.get("entities"), 12, 160),
            "evidence_keys": evidence_keys,
            "conditions": conditions,
            "same_paper_baseline": bool(raw.get("same_paper_baseline", False)),
            "confidence": _confidence(raw.get("confidence", 0.0)),
            "frame_assignments": assignments,
        })
    return out, "available"


def annotate_trend_evidence(
    claims: Sequence[dict], *, id_map: Mapping[str, Mapping[str, object]],
    family_by_source: Mapping[str, str],
    uncertain_source_ids: set[str] | None = None,
) -> list[dict]:
    """Cap trend confidence by the number of distinguishable cited documents.

    This is deliberately an annotation/audit, not a prose rewriter.  Missing
    source identity is uncertainty, never evidence that two anchors came from
    independent documents, so it caps the trend at the research tier.
    """
    out: list[dict] = []
    uncertain_source_ids = uncertain_source_ids or set()
    for claim in claims:
        row = dict(claim)
        if row.get("type") != "trend":
            out.append(row)
            continue
        families: set[str] = set()
        identity_unknown = False
        for key in row.get("evidence_keys") or []:
            context = id_map.get(str(key)) or {}
            source_id = _text(context.get("source_id"), 160)
            if source_id:
                if source_id in uncertain_source_ids or source_id not in family_by_source:
                    identity_unknown = True
                else:
                    families.add(str(family_by_source[source_id]))
            else:
                identity_unknown = True
        count = len(families)
        level = (
            "research" if identity_unknown
            else "high_confidence" if count >= 3
            else "developing" if count >= 2
            else "research"
        )
        row["independent_family_count"] = count
        row["source_identity_unknown"] = identity_unknown
        row["trend_level"] = level
        row["trend_wording_violation"] = bool(
            level != "high_confidence" and _STRONG_TREND.search(str(row.get("statement") or ""))
        )
        out.append(row)
    return out


def exclusive_frame_conflicts(sections: Iterable[dict], frame: dict | None) -> list[str]:
    exclusive = {
        _text(row.get("id"), 80): _text(row.get("name"), 120)
        for row in ((frame or {}).get("facets") or [])
        if isinstance(row, dict) and bool(row.get("exclusive"))
    }
    assignments: dict[tuple[str, str], set[str]] = defaultdict(set)
    for section in sections:
        for claim in section.get("claims") or []:
            entities = claim.get("entities") or []
            for facet_id, value in (claim.get("frame_assignments") or {}).items():
                if facet_id not in exclusive:
                    continue
                for entity in entities:
                    assignments[(_text(entity, 160).casefold(), facet_id)].add(str(value))
    out = []
    for (entity, facet_id), values in sorted(assignments.items()):
        if entity and len(values) > 1:
            out.append(
                f"实体“{entity}”在互斥维度“{exclusive[facet_id]}”中出现冲突取值："
                + "、".join(sorted(values))
            )
    return out[:16]


def fair_editor_context(
    sections: Sequence[dict], *, frame: dict | None = None,
    blueprint: dict | None = None, max_chars: int = EDITOR_CONTEXT_MAX_CHARS,
) -> str:
    """Allocate valid, bounded audit context without dropping later sections."""
    max_chars = max(0, int(max_chars))
    if not sections or max_chars == 0:
        return ""

    def bounded_conflicts() -> tuple[list[str], int]:
        conflicts = exclusive_frame_conflicts(sections, frame)
        return [_text(value, 360) for value in conflicts[:8]], len(conflicts)

    def bounded_audit(value: object) -> dict:
        if not isinstance(value, dict):
            return {}
        scalar_keys = (
            "high_risk_assertions", "supported", "unsupported", "support_rate",
            "unsupported_ratio", "threshold", "threshold_exceeded",
            "downgrade_applied", "cited", "total",
        )
        result = {
            key: raw for key in scalar_keys
            if key in value
            and isinstance((raw := value.get(key)), (bool, int, float))
        }
        samples = value.get("unsupported_samples")
        if isinstance(samples, list) and samples:
            result["unsupported_samples"] = [
                _text(sample, 200) for sample in samples[:2] if _text(sample, 200)
            ]
            result["unsupported_samples_total"] = len(samples)
        return result

    def bounded_claim(value: object) -> dict:
        if not isinstance(value, dict):
            return {}
        raw_assignments = value.get("frame_assignments")
        assignments = {}
        if isinstance(raw_assignments, dict):
            for key, assigned in list(raw_assignments.items())[:FRAME_MAX_FACETS]:
                bounded_key = _text(key, 80)
                if bounded_key:
                    assignments[bounded_key] = _text(assigned, 120)
        return {
            "claim_id": _text(value.get("claim_id") or value.get("id"), 80),
            "statement": _text(value.get("statement"), 1600),
            "type": _text(value.get("type"), 24),
            "entities": _strings(value.get("entities"), 12, 160),
            "evidence_keys": _strings(value.get("evidence_keys"), 16, 32),
            "conditions": _strings(value.get("conditions"), 8, 300),
            "same_paper_baseline": bool(value.get("same_paper_baseline", False)),
            "confidence": _confidence(value.get("confidence", 0.0)),
            "frame_assignments": assignments,
        }

    conflicts, conflict_count = bounded_conflicts()
    overhead: dict = {
        "frame": frame or {},
        "blueprint": blueprint or {},
        "conflicts": conflicts,
        "conflicts_total": conflict_count,
        "conflicts_truncated": conflict_count > len(conflicts),
    }
    head_budget = max(0, max_chars // 3)
    serialized_overhead = json.dumps(overhead, ensure_ascii=False)
    if len(serialized_overhead) > head_budget:
        blueprint_value = blueprint or {}
        frame_value = frame or {}
        overhead = {
            "frame_summary": {
                "subject_kind": _text(frame_value.get("subject_kind"), 160),
                "facet_count": len(frame_value.get("facets") or []),
                "axis_count": len(frame_value.get("axes") or []),
            },
            "blueprint_summary": {
                "status": "summarized",
                "central_answer": _text(blueprint_value.get("central_answer"), 800),
                "definition_count": len(blueprint_value.get("shared_definitions") or []),
                "claim_count": len(blueprint_value.get("claims") or []),
                "section_count": len(blueprint_value.get("sections") or []),
            },
            "conflicts": conflicts,
            "conflicts_total": conflict_count,
            "conflicts_truncated": conflict_count > len(conflicts),
        }
        serialized_overhead = json.dumps(overhead, ensure_ascii=False)
    head = "Report audit contracts:\n" + serialized_overhead

    raw_claims_by_section = [
        [bounded_claim(claim) for claim in
         list(section.get("claims") or [])[:CLAIM_LEDGER_MAX_CLAIMS]]
        for section in sections
    ]
    base_values = []
    for section, raw_claims in zip(sections, raw_claims_by_section):
        base_values.append({
            "title": _text(section.get("title"), 160),
            "claims": [],
            "claims_total": len(section.get("claims") or []),
            "claims_included": 0,
            "claims_truncated": bool(raw_claims),
            "claim_ledger_status": _text(
                section.get("claim_ledger_status", "missing"), 40
            ),
            "citation_audit": bounded_audit(section.get("citation_audit")),
        })

    delimiter = "\nProse excerpt:\n"
    separators = 2 * len(sections)
    base_serialized = [json.dumps(value, ensure_ascii=False) for value in base_values]
    minimum_sections = sum(len(value) + len(delimiter) for value in base_serialized)
    if len(head) + separators + minimum_sections > max_chars:
        # This branch is only for callers supplying a smaller-than-production
        # budget.  It still returns complete JSON rather than slicing a payload.
        compact = {
            "sections_total": len(sections),
            "titles": [_text(section.get("title"), 80) for section in sections],
            "audit_context_truncated": True,
        }
        serialized = json.dumps(compact, ensure_ascii=False)
        if len(serialized) <= max_chars:
            return serialized
        minimal = json.dumps({"sections_total": len(sections)}, ensure_ascii=False)
        return minimal if len(minimal) <= max_chars else ""

    remaining_extra = max_chars - len(head) - separators - minimum_sections
    blocks = [head]
    for index, (section, raw_claims, base_value, base_meta) in enumerate(zip(
        sections, raw_claims_by_section, base_values, base_serialized
    )):
        markdown = str(section.get("markdown") or "")
        sections_left = len(sections) - index
        extra_share = remaining_extra // sections_left
        meta_value = dict(base_value)
        meta_budget = len(base_meta) + (extra_share // 2)
        for claim in raw_claims:
            candidate = dict(meta_value)
            candidate["claims"] = [*meta_value["claims"], claim]
            candidate["claims_included"] = len(candidate["claims"])
            candidate["claims_truncated"] = candidate["claims_included"] < candidate["claims_total"]
            if len(json.dumps(candidate, ensure_ascii=False)) > meta_budget:
                break
            meta_value = candidate
        meta = json.dumps(meta_value, ensure_ascii=False)
        meta_extra = len(meta) - len(base_meta)
        prose_room = max(0, extra_share - meta_extra)
        if len(markdown) <= prose_room:
            excerpt = markdown
        elif prose_room <= 3:
            excerpt = markdown[-prose_room:] if prose_room else ""
        else:
            marker = "\n…\n"
            content_room = max(0, prose_room - len(marker))
            first = content_room // 2
            excerpt = markdown[:first] + marker + markdown[-(content_room - first):]
        blocks.append(meta + delimiter + excerpt)
        remaining_extra -= meta_extra + len(excerpt)
    result = "\n\n".join(blocks)
    assert len(result) <= max_chars
    return result
