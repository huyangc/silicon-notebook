"""S7: LLM extraction of typed knowledge objects from one ContextPackage.

The model's output is never trusted directly: object types are constrained to
the profile vocabulary and evidence ids are filtered to atoms actually present
in the package (hallucinated ids/types are dropped).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.qiefen.llm_extract import (
    OBJECT_SCHEMA_HINT, build_object_prompt, safe_json,
)
from app.services.qiefen.models import ContextPackage, KnowledgeObjectQ
from app.services.qiefen.profiles import object_types


def extract_objects(client: Any, pkg: ContextPackage, profile: str,
                    atom_text: Optional[Dict[str, str]] = None) -> List[KnowledgeObjectQ]:
    allowed = object_types(profile)
    pkg_atom_ids = {a.get("atom_id") for a in (pkg.atoms or [])}
    prompt = build_object_prompt(pkg, profile, atom_text)
    try:
        raw = client.chat_json([{"role": "user", "content": prompt}], OBJECT_SCHEMA_HINT)
    except Exception:
        return []
    items = safe_json(raw).get("objects")
    if not isinstance(items, list):
        return []

    objects: List[KnowledgeObjectQ] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        otype = item.get("type")
        if otype not in allowed:
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        # keep only the profile-declared fields for this type, with truthy values
        payload = {k: v for k, v in payload.items() if k in allowed[otype] and v not in (None, "", [])}
        ev = [a for a in (item.get("local_evidence_atom_ids") or []) if a in pkg_atom_ids]
        if not payload and not ev:
            continue
        objects.append(KnowledgeObjectQ(
            id=f"OBJ-{pkg.id}-{i}", type=otype, section_path=pkg.section_path,
            home_package=pkg.id, payload=payload, local_evidence_atom_ids=ev,
        ))
    return objects
