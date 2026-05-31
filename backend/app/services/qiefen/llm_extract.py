"""Shared helpers for the LLM extraction stages (S6-S8): prompt construction,
robust JSON parsing, and high-value atom selection to bound prompt size.

The package's atoms are rendered WITH their ids so the model can return
evidence as `local_evidence_atom_ids` chosen from exactly those ids. Code (not
the model) is the source of truth for which ids/types are valid — callers
filter the model output.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from app.services.qiefen.models import ContextPackage
from app.services.qiefen.profiles import object_types

# Cap atoms shown per prompt so a large section package stays cheap and focused.
MAX_PROMPT_ATOMS = 30

# Atom types that most often carry an extractable knowledge object — preferred
# when a package has more atoms than the cap.
_HIGH_VALUE = {
    "formula_atom", "table_header_atom", "table_row_atom", "table_caption_atom",
    "scaling_law_result_atom", "result_sentence", "method_sentence",
    "mechanism_sentence", "definition_atom", "concept_definition_atom",
    "design_principle_atom", "example_problem_atom", "problem_statement_atom",
    "process_step_atom", "claim_sentence", "limitation_sentence",
}


def strip_fences(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def safe_json(raw: str) -> Dict[str, Any]:
    """Parse a model JSON reply; return {} on any failure (never raise)."""
    if not raw:
        return {}
    try:
        out = json.loads(strip_fences(raw))
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}


def select_prompt_atoms(pkg: ContextPackage) -> List[Dict[str, str]]:
    """Up to MAX_PROMPT_ATOMS package atoms, high-value types first, original
    order preserved within each tier."""
    atoms = pkg.atoms or []
    if len(atoms) <= MAX_PROMPT_ATOMS:
        return list(atoms)
    high = [a for a in atoms if a.get("atom_type") in _HIGH_VALUE]
    low = [a for a in atoms if a.get("atom_type") not in _HIGH_VALUE]
    return (high + low)[:MAX_PROMPT_ATOMS]


def _atoms_block(atoms: List[Dict[str, str]], atom_text: Optional[Dict[str, str]]) -> str:
    lines = []
    for a in atoms:
        aid = a.get("atom_id", "")
        atype = a.get("atom_type", "")
        text = (atom_text or {}).get(aid, "")
        text = (text[:240] + "...") if len(text) > 240 else text
        lines.append(f"[{aid}|{atype}] {text}".rstrip())
    return "\n".join(lines)


def _types_block(profile: str) -> str:
    return "\n".join(
        f"- {t}: payload fields {fields}"
        for t, fields in object_types(profile).items()
    )


def build_object_prompt(pkg: ContextPackage, profile: str,
                        atom_text: Optional[Dict[str, str]] = None) -> str:
    atoms = select_prompt_atoms(pkg)
    return f"""You extract structured knowledge objects from one section of a document.

Document: {pkg.document_title}
Section: {pkg.section_path}
Profile: {profile}

Allowed object types (use ONLY these `type` values; fill ONLY the listed payload fields):
{_types_block(profile)}

Evidence atoms (id | atom_type | text). Choose local_evidence_atom_ids ONLY from these ids:
{_atoms_block(atoms, atom_text)}

Rules:
- Emit an object only when the atoms clearly support it. Do NOT invent facts.
- Every payload field value must be supported by the cited atoms.
- Keep values CONCISE and ATOMIC, not copied sentences:
  * names/terms: the bare name only (e.g. "Engram", not the whole sentence).
  * numbers/metrics: put each value in its own field as a bare token
    (e.g. before: "84.2", after: "97.0", knowledge: "MMLU +3.4; CMMLU +4.0").
  * a "statement"/"finding"/"definition" should be one short clause stating the
    fact, NOT the verbatim source sentence.
- local_evidence_atom_ids: the atom ids (from the list above) that support this object.
- Skip citations, figure/table cross-references, and bare URLs (not objects).

Return JSON ONLY:
{{"objects": [{{"type": "<one allowed type>", "payload": {{...}}, "local_evidence_atom_ids": ["<id>", ...]}}]}}
"""


OBJECT_SCHEMA_HINT = (
    '{"objects":[{"type":"string","payload":{},"local_evidence_atom_ids":["string"]}]}'
)
