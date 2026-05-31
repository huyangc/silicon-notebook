"""S6 (deterministic): mentions + canonicalization derived from extracted
objects — no extra LLM call. A mention is an object's short identity surface
(name/term/title) anchored to one of its evidence atoms; canonicalization
groups mentions sharing a normalized key.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.services.qiefen.models import KnowledgeObjectQ, Mention

# Identity fields that read as a short entity name (NOT prose like "statement").
_NAME_FIELDS = ("name", "term", "title", "symbol")


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


def derive_mentions(objects: List[KnowledgeObjectQ]) -> List[Mention]:
    mentions: List[Mention] = []
    n = 0
    for obj in objects:
        if not obj.local_evidence_atom_ids:
            continue
        name = next((obj.payload.get(f) for f in _NAME_FIELDS
                     if isinstance(obj.payload.get(f), str) and obj.payload.get(f).strip()), None)
        if not name:
            continue
        n += 1
        mentions.append(Mention(
            id=f"M-{n}", text=name.strip()[:80], type=obj.type,
            atom_id=obj.local_evidence_atom_ids[0], canonical_key=_slug(name),
        ))
    return mentions


def build_canonicalization(mentions: List[Mention]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[str]] = {}
    for m in mentions:
        groups.setdefault(m.canonical_key, [])
        if m.text not in groups[m.canonical_key]:
            groups[m.canonical_key].append(m.text)
    return [{"canonical": key, "aliases": aliases}
            for key, aliases in groups.items() if key]
