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
    if profile == "article_research":
        keep = ("core claims/contributions, method/architecture descriptions, "
                "mechanism explanations, quantitative experimental results and "
                "metric values, formulas, scaling laws, ablation findings, and "
                "stated limitations")
    else:
        keep = ("concept/term definitions, formulas and equations, quantitative "
                "results/values, design principles and rules, physical effects, "
                "named process/fabrication steps, table header/rows, and "
                "example/problem statements")
    return f"""You curate the HIGH-VALUE knowledge atoms from one section of a
technical document (a {profile.replace('_', ' ')}). A reader citing this work
would keep: {keep}.

EXCLUDE sentences that are narrative, motivation, history, transitions,
restatements, figure/citation cross-references, or filler. In ordinary expository
prose MOST sentences are NOT high-value — be selective; a definition or result
stated once is enough.

Atoms (id | type | text):
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
