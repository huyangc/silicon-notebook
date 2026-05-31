"""S8: LLM extraction of ID-ized relations over a chapter's objects.

One call per chapter (not per package) so cross-package edges are possible.
Endpoints must be real object ids and relation_type must be in the profile
vocabulary; everything else is dropped in code.
"""
from __future__ import annotations

from typing import Any, List

from app.services.qiefen.llm_extract import safe_json
from app.services.qiefen.models import KnowledgeObjectQ, RelationQ
from app.services.qiefen.profiles import relation_types

_IDENTITY_FIELDS = ("name", "statement", "term", "title", "claim", "expression")

RELATION_SCHEMA_HINT = (
    '{"relations":[{"relation_type":"string","source_object_id":"string",'
    '"target_object_id":"string","evidence_atom_ids":["string"]}]}'
)


def _identity(obj: KnowledgeObjectQ) -> str:
    for f in _IDENTITY_FIELDS:
        v = obj.payload.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()[:80]
    return obj.type


def build_relation_prompt(objects: List[KnowledgeObjectQ], profile: str) -> str:
    obj_lines = "\n".join(f"- {o.id} [{o.type}] {_identity(o)}" for o in objects)
    rel_lines = ", ".join(relation_types(profile))
    return f"""You connect already-extracted knowledge objects with typed relations.

Objects (id [type] identity):
{obj_lines}

Allowed relation types (use ONLY these): {rel_lines}

Rules:
- source_object_id and target_object_id MUST be ids from the list above.
- Use only the allowed relation types. Emit a relation only when clearly supported.
- evidence_atom_ids: ids of atoms supporting the relation (may be empty).

Return JSON ONLY:
{{"relations": [{{"relation_type": "<allowed>", "source_object_id": "<id>", "target_object_id": "<id>", "evidence_atom_ids": ["<id>", ...]}}]}}
"""


def extract_relations(client: Any, objects: List[KnowledgeObjectQ], profile: str,
                      atom_text=None) -> List[RelationQ]:
    if len(objects) < 2:
        return []
    obj_ids = {o.id for o in objects}
    valid_atoms = {a for o in objects for a in o.local_evidence_atom_ids}
    allowed = set(relation_types(profile))
    prompt = build_relation_prompt(objects, profile)
    try:
        raw = client.chat_json([{"role": "user", "content": prompt}], RELATION_SCHEMA_HINT)
    except Exception:
        return []
    items = safe_json(raw).get("relations")
    if not isinstance(items, list):
        return []

    relations: List[RelationQ] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        rtype = item.get("relation_type")
        src = item.get("source_object_id")
        tgt = item.get("target_object_id")
        if rtype not in allowed or src not in obj_ids or tgt not in obj_ids or src == tgt:
            continue
        ev = [a for a in (item.get("evidence_atom_ids") or []) if a in valid_atoms]
        relations.append(RelationQ(
            id=f"R-{i}", relation_type=rtype,
            source_object_id=src, target_object_id=tgt, evidence_atom_ids=ev,
        ))
    return relations
