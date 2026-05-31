"""Optional LLM atom selector (runs BEFORE chunking).

Unlike post-hoc curation (which perturbs object alignment), selecting the
high-value atoms first means chunks/packages/objects are all built on the
curated set — consistent and no alignment mismatch. The model is asked, in
batches, which atoms are gold-grade knowledge atoms; everything else is dropped.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Set

from app.services.qiefen.llm_extract import safe_json
from app.services.qiefen.models import EvidenceAtom

BATCH = 40
_SCHEMA_HINT = '{"core_atom_ids":["string"]}'


def _prompt(batch: List[EvidenceAtom], profile: str) -> str:
    lines = []
    for a in batch:
        t = a.raw_text[:200] + ("..." if len(a.raw_text) > 200 else "")
        lines.append(f"[{a.id}|{a.atom_type}] {t}")
    body = "\n".join(lines)
    return f"""From the atoms below (id | type | text), select the HIGH-VALUE knowledge
atoms worth keeping as standalone evidence in a {profile}: definitions, formulas,
key quantitative results/values, design principles/rules, physical effects, table
header/rows, and example/problem statements.

EXCLUDE narrative, motivation, history, transitions, figure/citation references,
and filler. Be selective — in a long passage MOST sentences are NOT high-value.

Atoms:
{body}

Return JSON ONLY: {{"core_atom_ids": ["<id>", ...]}}
"""


def select_core_atoms(client: Any, atoms: List[EvidenceAtom], profile: str,
                      workers: int = 8) -> Set[str]:
    """Return the set of atom ids the model marks high-value. On any failure for
    a batch, that batch's atoms are kept (fail-open) so we never lose everything."""
    batches = [atoms[i:i + BATCH] for i in range(0, len(atoms), BATCH)]
    if not batches:
        return set()

    def _one(batch):
        try:
            raw = client.chat_json([{"role": "user", "content": _prompt(batch, profile)}],
                                   _SCHEMA_HINT)
            core = safe_json(raw).get("core_atom_ids")
            ids = {a.id for a in batch}
            sel = {c for c in (core or []) if c in ids}
            return sel if sel else ids  # fail-open if the model returned nothing usable
        except Exception:
            return {a.id for a in batch}

    keep: Set[str] = set()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as pool:
        for sel in pool.map(_one, batches):
            keep.update(sel)
    return keep
