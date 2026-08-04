"""Pure historical source-fact projection shared by both repository adapters."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


SOURCE_FACT_PROJECTION_VERSION = 1


@dataclass(frozen=True)
class HistoricalSourceFact:
    fact_id: str
    local_object_id: str
    global_object_id: str
    object_type: str
    payload_json: str
    evidence_json: str
    element_ids: tuple[str, ...]


def _json_value(value: object, expected: type) -> object | None:
    if isinstance(value, expected):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, expected) else None


def project_historical_source_fact(
    row: Mapping[str, Any], source_id: str, source_generation: str
) -> tuple[HistoricalSourceFact | None, str]:
    """Return an exact single-source projection or a stable incomplete code.

    A merged payload is safe only when both its owner and every evidence record
    still point at the same source.  Missing/ambiguous provenance is never
    guessed from names or question text.
    """
    payload = _json_value(row.get("payload"), dict)
    evidence = _json_value(row.get("evidence"), list)
    if payload is None or evidence is None:
        return None, "invalid_json"
    if str(row.get("source_id") or "") != source_id:
        return None, "ambiguous_owner"
    if not evidence:
        return None, "missing_evidence"

    element_ids: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            return None, "invalid_evidence"
        if str(item.get("source_id") or "") != source_id:
            return None, "mixed_source_evidence"
        element_id = str(item.get("element_id") or "")
        if not element_id:
            return None, "missing_element"
        element_ids.append(element_id)
    element_ids = list(dict.fromkeys(element_ids))

    global_object_id = str(row.get("id") or "")
    if not global_object_id:
        return None, "missing_object_id"
    local_object_id = f"legacy:{global_object_id}"
    fact_id = "ksf-" + hashlib.sha256(
        f"historical\0{source_id}\0{source_generation}\0{local_object_id}".encode(
            "utf-8"
        )
    ).hexdigest()[:32]
    return HistoricalSourceFact(
        fact_id=fact_id,
        local_object_id=local_object_id,
        global_object_id=global_object_id,
        object_type=str(row.get("object_type") or ""),
        payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        evidence_json=json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        element_ids=tuple(element_ids),
    ), ""
