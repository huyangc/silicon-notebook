"""SA-2 A/B calibration: same windows, OLD vs NEW extraction prompt, per-metric.
Pure metric functions (below) are unit-tested; main() is a manual measurement
tool (not CI). Loads root .env in-process (no copy); scratch DB; never prod.

Run:
    cd backend && PYTHONPATH=. python -m app.eval.sa_calibration \\
        --source-md /abs/path/to/a/chapter.md --doc-type academic_paper
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Sequence
from app.services.kg.models import Node, Edge

_SPARSE = {"depends_on", "contrasts_with", "prerequisite_of"}
# ---------------------------------------------------------------------------
# Compound-claim heuristic — a deliberately PRECISION-LEANING surface proxy.
#
# HONEST LIMITS (do not over-trust):
# * SAME-SLICE OLD-vs-NEW comparison ONLY. Never read it as an absolute
#   atomicity measure, and never tune an extraction prompt against it —
#   surface cues are trivially gameable (Goodhart). If the prompt changes,
#   re-calibrate on a FRESH slice.
# * Validated on a tiny fixed set: the 21 captured calibration claims plus the
#   out-of-sample probes frozen in test_sa_calibration_metrics.py. Expect
#   drift on other registers; at SA-3 scale use an LLM-judge / human rubric
#   and keep this only as a free trend monitor.
# * Known blind spots (the price of precision): "and the <noun> <verb>"
#   clauses are NOT flagged (bare "the" caused FPs on "between the gate and
#   the source"); word-level Chinese conjunctions (并且/而且/...) are NOT
#   detected — Chinese is caught only via ；/。 boundaries.
#
# A claim is flagged "compound" (>= 2 independent propositions) iff:
#   (a) it contains >= 2 real sentences — './;' followed by space+capital, or
#       a mid-text 。 — or a mid-text ;/；; or
#   (b) a conjunction joins two CLAUSES: and/but followed by a subject pronoun
#       (it/they/we), an underscore-symbol subject + verb ("and H_m is"), or
#       directly by a verb ("and could have been", "and compute"); or a
#       clause-level while/whereas (followed by det/pronoun) or ", which".
# Coordinated objects do NOT flag: "x and y", "ω1→0 and ω1→∞", "low noise and
# high gain", "between the gate and the source", ellipses, "Eq. (2.237).".
# ---------------------------------------------------------------------------
_SENT_BOUNDARY = re.compile(r"[.;]\s+[A-Z]|。(?!\s*$)")
_VERBISH = (
    "is|are|was|were|be|can|could|should|would|will|may|might|must|"
    "has|have|had|do|does|did|"
    "yields?|gives?|produces?|appears?|becomes?|equals?|leads?|results?|"
    "causes?|implies|requires?|depends?|computes?|compute"
)
_CLAUSE_CONJ = re.compile(
    r"[;；](?!\s*$)"
    r"|,\s*which\b"
    r"|\b(?:while|whereas)\s+(?:it|they|we|the|a|an)\b"
    r"|\b(?:and|but)\s+(?:it|they|we)\b"
    rf"|\b(?:and|but)\s+[A-Za-z]\w*_\S+\s+(?:{_VERBISH})\b"
    rf"|\b(?:and|but)\s+(?:{_VERBISH})\b",
    re.IGNORECASE,
)


def _is_compound_claim(name: str) -> bool:
    return bool(_SENT_BOUNDARY.search(name) or _CLAUSE_CONJ.search(name))


def compound_claim_rate(claims: Sequence[Node]) -> float:
    names = [c.name for c in claims if c.name]
    if not names:
        return 0.0
    return sum(1 for n in names if _is_compound_claim(n)) / len(names)


def evaluate_gate(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """NEW-vs-OLD pass/fail. Atomicity uses a RELATIVE criterion (absolute
    atomicity is content-dependent and noisy at small sample): NEW must cut the
    compound rate to <= 0.8x OLD and stay < 0.5. Sparse-edge density and
    validity_scope presence keep their criteria. Returns {checks, pass}.

    USAGE DISCIPLINE: this is a ONE-SHOT sanity gate on a fixed slice, built on
    surface-proxy metrics. Do NOT iterate the extraction prompt against it
    (Goodhart); if the prompt changes, re-run on a fresh, unseen slice."""
    checks = {
        "sparse_density_up_1.5x":
            new["sparse_per_1k_tok"] >= 1.5 * max(1e-9, old["sparse_per_1k_tok"]),
        "compound_reduced_vs_old": (
            new["compound_claim_rate"] <= 0.8 * old["compound_claim_rate"]
            and new["compound_claim_rate"] < 0.5
        ),
        "validity_scope_present": new["validity_scope_fill_rate"] > 0,
    }
    return {"checks": checks, "pass": all(checks.values())}


def sparse_edge_count(edges: Sequence[Edge]) -> int:
    return sum(1 for e in edges if e.type in _SPARSE)


def validity_scope_fill_rate(nodes: Sequence[Node]) -> float:
    eligible = [n for n in nodes if n.type in ("Claim", "Formula")]
    if not eligible:
        return 0.0
    return sum(1 for n in eligible if n.validity_scope) / len(eligible)


def summarize(nodes: List[Node], edges: List[Edge], input_tokens: int) -> Dict[str, Any]:
    claims = [n for n in nodes if n.type == "Claim"]
    per_rel: Dict[str, int] = {}
    for e in edges:
        per_rel[e.type] = per_rel.get(e.type, 0) + 1
    sparse = sparse_edge_count(edges)
    return {
        "nodes": len(nodes), "edges": len(edges), "claims": len(claims),
        "compound_claim_rate": round(compound_claim_rate(claims), 3),
        "validity_scope_fill_rate": round(validity_scope_fill_rate(nodes), 3),
        "sparse_edges": sparse,
        "sparse_per_1k_tok": round(1000 * sparse / max(1, input_tokens), 3),
        "per_relation": per_rel, "input_tokens_est": input_tokens,
    }


def _load_root_env() -> None:
    import os
    path = "/Users/hzf/workspace/silicon_notebook/.env"
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _old_prompt(labeled_text: str, section_path: str, doc_type: str,
                base_filter: bool = False) -> str:
    return f"""Extract a knowledge-graph fragment from this {doc_type} passage
(section: {section_path}). Use EXACTLY these node types: Concept, Claim, Formula,
Procedure (see definitions: Concept=a NAMED, reusable technical entity — a method,
mechanism, component, named model/structure/distribution; Claim=truth-evaluable
assertion about concepts; Formula=equation; Procedure=ordered process). Edges
(source->target): defines(Claim->Concept), about(Claim|Formula->Concept),
supports(Claim|Formula->Claim), part_of/composed_of/contrasts_with/kind_of(Concept->
Concept), derived_from(Formula->Formula), depends_on/prerequisite_of,
used_in(Formula->Procedure), precedes.

Be SELECTIVE with Concepts: emit a Concept only for a distinctive named entity.
Do NOT emit Concepts for generic/common terms (e.g. training, inference, buffer,
latency, forward pass, backward pass, hidden state, input sequence, host memory) or
for trivial sub-parts of another concept. Also do NOT emit Concepts for: bare
symbols/variables (e.g. V_DD, g_m1, i_b68, R_E26, (W/L)_1, A_v^+); instance labels
of a specific device/node/pole (e.g. Q1, M5, Pole p8); figure/table/equation/section
references (e.g. Fig. 5.38, Table 2.1, Eq. 9.4); or section headings/numbers (e.g.
"8.4.1 Series-Shunt Feedback"); or numeric levels / enumerated values that are
settings of a parent concept (e.g. "Level 1/2/3 Model", "Type I/II PLL", "Case I/II",
op-amp part-number series like "702/709/741 op amp") — treat these as attributes or
instances of the parent concept, NOT separate Concepts. In contrast, capture EVERY
Formula (equation) and EVERY Procedure (process/phase) present — do not skip those.

For Claims: emit a Claim ONLY for a complete, truth-evaluable technical assertion
(a subject plus a predicate stating a fact or relationship). Do NOT emit Claims for:
section headings or titles (e.g. "Effect of Negative Feedback on Distortion");
narrative/meta sentences about the document itself (e.g. "This chapter covers…",
"This book deals with…", "I wanted to teach…"); or chapter/section navigation.

The passage is given as numbered elements, one per line, each prefixed with its
integer label like [3]. Every node and edge MUST include "ev": the INTEGER label
of the element that best contains it (NOT a quote, NOT text). Give each node a
"local_id" you reuse in edges. "name" carries the node's text (Concept/Procedure
name, Claim statement, Formula expression). For a Procedure that is an ordered multi-step process/flow, emit it as ONE Procedure node (named after the flow — use the section heading if it names the flow) and list its ordered steps in a `steps` array, each {{"name":..,"ev":..}} where ev is the element label containing that step; prefer this over many separate Procedure nodes. `steps` is the source of truth for order (you may still add `precedes` edges). Skip narrative/filler.

Passage:
\"\"\"{labeled_text}\"\"\"

Return JSON ONLY:
{{"nodes":[{{"local_id":"..","type":"..","name":"..","ev":0,"steps":[{{"name":"..","ev":0}}]}}],
 "edges":[{{"type":"..","source":"<local_id>","target":"<local_id>","ev":0}}]}}
"""


def _run_arm(prompt_fn, windows, doc_type: str) -> Dict[str, Any]:
    from app.core.config import Settings
    from app.services.kg import extract as E
    from app.services.sqlite_repository import SQLiteRepository
    repo = SQLiteRepository(Settings())
    client = repo.chat("kg_extract")
    all_nodes: List[Node] = []
    all_edges: List[Edge] = []
    tok = 0
    orig = E._prompt
    E._prompt = prompt_fn
    try:
        for els, section_path in windows:
            tok += sum(len(e.text) for e in els) // 4
            ns, es = E.extract_window(client, els, section_path, doc_type,
                                      win_idx=0, base_filter=True)
            all_nodes += ns
            all_edges += es
    finally:
        E._prompt = orig
    return summarize(all_nodes, all_edges, tok)


def main() -> None:
    import argparse, json, tempfile, os
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-md", default="")
    ap.add_argument("--doc-type", default="academic_paper")
    ap.add_argument("--max-windows", type=int, default=12)
    args = ap.parse_args()

    _load_root_env()
    d = tempfile.mkdtemp(prefix="sacal_")
    os.environ["DATABASE_URL"] = f"sqlite:///{d}/cal.db"
    os.environ["SILICON_NOTEBOOK_STORAGE_DIR"] = f"{d}/storage"
    os.environ["LLM_LOG_ENABLED"] = "false"
    os.environ["EVENT_LOG_ENABLED"] = "false"

    from app.services.kg.windowing import windows_with_elements
    from app.services.kg.extract import _prompt as new_prompt
    if not args.source_md:
        raise SystemExit("请用 --source-md 指定一章 markdown(只读;勿用 prod 库)")
    raw = open(args.source_md, encoding="utf-8").read()
    pairs = [(els, w.section_path)
             for w, els in windows_with_elements(raw, "cal.md", None, 9000, 450)
             if els][: args.max_windows]
    print(f"[cal] {len(pairs)} windows")
    old = _run_arm(_old_prompt, pairs, args.doc_type)
    new = _run_arm(new_prompt, pairs, args.doc_type)
    print(json.dumps({"OLD": old, "NEW": new}, ensure_ascii=False, indent=2))
    result = evaluate_gate(old, new)
    print(json.dumps({"GATE": result["checks"], "PASS": result["pass"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
