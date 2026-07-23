"""Window -> LLM KG fragment -> grounded nodes/edges. Local ids are kept so the
caller can wire edges. Evidence is anchored by element-id markers: the LLM emits
only an integer "ev" label per node/edge, and the backend maps it back to that
source element's exact text/offsets (drop ungroundable nodes). Node types
constrained to the 4; edges to the vocab."""
from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional, Tuple
from openai import APIConnectionError, APITimeoutError
from app.core.llm import cap_kwargs
from app.services.kg.json_utils import safe_json
from app.services.kg.models import Edge, Evidence, Node, Step
from app.services.kg.parsing import SourceElementQ
from app.services.kg.run_control import KgBuildAborted
from app.services.prompts import gleaning_prompt, refine_prompt, REFINE_SCHEMA_HINT

NODE_TYPES = {"Concept", "Claim", "Formula", "Procedure"}
EDGE_TYPES = {"defines", "part_of", "composed_of", "contrasts_with", "kind_of",
              "about", "supports", "derived_from", "depends_on", "prerequisite_of",
              "used_in", "precedes"}

_KG_SCHEMA_HINT = (
    '{"nodes":[{"local_id":"","type":"Concept|Claim|Formula|Procedure","name":"",'
    '"ev":0,"validity_scope":{"region":[],"assumptions":[],"approximation":"","range":""},'
    '"steps":[{"name":"","ev":0}]}],'
    '"edges":[{"type":"about|supports|derived_from|depends_on|contrasts_with|'
    'prerequisite_of|defines|part_of|composed_of|kind_of|used_in|precedes",'
    '"source":"<local_id>","target":"<local_id>","ev":0}]}'
)


def _kg_fragment_cacheable(content: str) -> bool:
    """Cache-admission shape gate for the KG-fragment schema (_KG_SCHEMA_HINT).

    is_cacheable_llm_response already rejects the empty "{}" fallback, unparseable
    JSON and budget-truncated replies, but a syntactically valid reply can still
    be a NON-extraction that must never be frozen for the 90-day TTL. The prior
    gate was deliberately lax (list-when-present, absent-is-fine) and therefore
    admitted two poison shapes: an error envelope like ``{"error":"quota"}`` (no
    ``nodes`` key at all → treated as a "legit empty window") and a ``nodes`` list
    full of junk entries (``{"nodes":[{"garbage":1}]}``) — both ground to 0 objects
    and, once cached, make every re-parse replay the 0. Skipping a doubtful reply
    costs only one cache miss (the reply is still USED this call, just re-fetched
    next time), so tighten to "cache only when we are sure it is a real extraction"
    and mirror what extract_window ACTUALLY consumes:

    - A real extraction ALWAYS emits ``nodes`` as a LIST — even an empty window is
      ``{"nodes":[],"edges":[]}``. A reply with no ``nodes`` key, or a non-list one,
      is error/malformed, NOT a legit empty window → reject.
    - An ``error`` key (or any error-envelope shape) is a transient failure that
      would differ on retry → never cache.
    - ``edges`` when present must be a list.
    - Each PRESENT node must clear extract_window's own first-line consumption gate
      (``isinstance(it, dict) and it.get("type") in NODE_TYPES``) — the field that
      decides whether the entry becomes an object. A list where any entry fails
      that gate is (at least partly) junk; do not cache it. Groundability (ev/name
      vs the window's elements) is NOT re-checked here: it depends on ``elements``
      the validator does not have, and it is deterministic for this cache key
      anyway (the elements' text is IN the key), so a typed node is a real reply.

    Passed to chat_json as response_validator; a fault there conservatively skips
    the write (see OpenAICompatibleClient._response_validator_allows). On a cache
    HIT this same gate re-judges the stored value, so any laxer-era poison already
    in the cache is treated as a miss and re-fetched."""
    data = safe_json(content)
    if not isinstance(data, dict):
        return False
    if "error" in data:
        return False
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        return False
    edges = data.get("edges")
    if edges is not None and not isinstance(edges, list):
        return False
    for it in nodes:
        if not isinstance(it, dict) or it.get("type") not in NODE_TYPES:
            return False
    return True


def _refine_response_cacheable(content: str) -> bool:
    """Cache-admission shape gate for the refine schema (REFINE_SCHEMA_HINT,
    ``{"items":[{"index":0,"keep":true}]}``). Same tightening rationale as
    ``_kg_fragment_cacheable``: cache only a reply we are sure is a real refine
    result, since skipping a doubtful one costs just a re-fetch.

    ``refine_nodes`` consumes ``data["items"]`` as a list and acts on each entry's
    integer ``index`` + boolean ``keep`` (dropping the node when ``keep is False``);
    a reply with no ``items`` key, a wrong-typed ``items``, an ``error`` envelope,
    or entries missing those decision fields is not a usable refine result. A
    genuine "keep everything" is ``{"items":[]}`` (empty list, no drops), NOT an
    absent key — so absence is rejected, not treated as empty."""
    data = safe_json(content)
    if not isinstance(data, dict):
        return False
    if "error" in data:
        return False
    items = data.get("items")
    if not isinstance(items, list):
        return False
    for it in items:
        if (not isinstance(it, dict)
                or not isinstance(it.get("index"), int)
                or not isinstance(it.get("keep"), bool)):
            return False
    return True


def _extract_call_kwargs(
    client: Any, validator: Callable[[str], bool]
) -> Dict[str, Any]:
    """chat_json extras for the KG-extraction calls: the per-call max_tokens
    override (cap_kwargs) plus the cache-admission ``response_validator``. BOTH
    are gated on the client exposing ``settings`` — the hand-rolled test doubles
    in kg/test_extract.py etc. expose neither ``settings`` nor ``**kwargs``, so
    passing either kwarg would TypeError. Real clients (OpenAICompatibleClient
    and its run-control / concurrency wrappers) all carry ``settings`` and accept
    both, so this mirrors the existing ``**cap_kwargs`` gating exactly — no double
    that survives today's splat regresses, and every cache-backed client still
    gets the validator."""
    kwargs = cap_kwargs(client, "kg_extract_max_tokens")
    if getattr(client, "settings", None) is not None:
        kwargs["response_validator"] = validator
    return kwargs


def _prompt(labeled_text: str, section_path: str, doc_type: str,
            base_filter: bool = False) -> str:
    base_rule = (
        "\nBASE-TIER QUALITY FILTER: drop non-knowledge meta-text — pedagogical "
        "asides (\"In a semester system…\"), exercise/homework hints, tool/UI "
        "trivia, document navigation. Keep only durable technical knowledge.\n"
        if base_filter else ""
    )
    return f"""Extract a knowledge-graph fragment from this {doc_type} passage
(section: {section_path}). Use EXACTLY these node types: Concept, Claim, Formula,
Procedure (Concept=a NAMED reusable technical entity — method, mechanism,
component, named model/structure/distribution; Claim=truth-evaluable assertion;
Formula=equation; Procedure=ordered process).

Be SELECTIVE with Concepts: emit a Concept only for a distinctive NAMED entity.
Do NOT emit Concepts for generic/common terms or trivial sub-parts; nor for bare
symbols/variables (V_DD, g_m1, (W/L)_1); instance labels (Q1, M5, Pole p8);
figure/table/equation/section references (Fig. 5.38, Eq. 9.4); section headings;
or enumerated settings (Level 1/2/3 Model, Type I/II). Capture EVERY Formula and
EVERY Procedure present.

CLAIMS — ATOMIC (one proposition per node). SPLIT compound statements:
- "A and B" / "A; B"  ->  two separate Claims.
- "B because/therefore A", "A, which causes B"  ->  TWO atomic Claims (A, B) PLUS
  a reasoning edge between them (supports / derived_from / depends_on) by local_id.
  Connecting the atoms with an edge is HOW reasoning edges get built — do it.
GUARDRAIL: split ONLY when each part stands alone as truth-evaluable; never
fragment a single proposition just because it is long. Do NOT emit Claims for
section headings, narrative/meta sentences about the document ("This chapter
covers…"), or navigation.

VALIDITY SCOPE: when a Claim or Formula holds only under a stated condition, put
that condition in a structured `validity_scope` object ON that node — NOT as prose,
NOT as a separate dangling Claim (never emit "This holds for DC…" as its own Claim).
Fields (ALL optional; include only what the text explicitly states):
  region: [..]        e.g. ["saturation"] | ["weak-inversion"]
  assumptions: [..]   e.g. ["perfect matching", "R_in << R_C"]
  approximation: ".." e.g. "small-signal" | "neglecting body effect"
  range: ".."         e.g. "low-frequency" | "f << f_T"
NEVER invent a scope the text does not state; OMIT validity_scope when none.

EDGES (source->target by local_id). REASONING-BEARING edges are the PRIORITY and
connect Claims/Formulas/Concepts (not only Concept->Concept):
- supports (claim/formula/concept -> claim): evidence/argument backing a claim.
- derived_from (claim/formula -> claim/formula): result follows from another.
- depends_on (claim/formula/concept -> ...): validity/value depends on target.
- contrasts_with (claim/formula/concept <-> ...): trade-off / disagreement /
  contradiction.
- prerequisite_of (concept/claim -> concept/claim): must hold/be understood first.
EXPLICITLY HUNT depends_on, contrasts_with, prerequisite_of — rare and high-value;
look for "requires", "assuming", "unlike", "trade-off", "valid when", "before".
Structural edges (secondary): about(claim/formula->concept), defines(claim->
concept), part_of/composed_of/kind_of(concept->concept), used_in(formula/concept->
procedure), precedes.

CONNECTIVITY (REQUIRED): every Claim MUST appear in at least one edge — at minimum
an `about` edge to the Concept it concerns; every Concept SHOULD appear in at least
one edge. Prefer reasoning edges (supports/derived_from/depends_on); fall back to
`about`.
{base_rule}
The passage is numbered elements, one per line, prefixed like [3]. Every node and
edge MUST include "ev": the INTEGER label of the element that best contains it.
Give each node a "local_id" reused in edges. "name" carries the node's text
(Concept/Procedure name, Claim proposition, Formula expression). For an ordered
multi-step Procedure emit ONE Procedure node with an ordered `steps` array, each
{{"name":..,"ev":..}}. Skip narrative/filler.

Preserve entity/concept names, formula expressions and canonical labels EXACTLY
as they appear in the source text, in their ORIGINAL LANGUAGE — do NOT translate
or transliterate them (a Chinese term stays Chinese, an English term stays
English). Write any Claim proposition in the language of the source passage.

Passage:
\"\"\"{labeled_text}\"\"\"

Return JSON ONLY:
{_KG_SCHEMA_HINT}
"""


def _parse_validity_scope(raw: Any) -> Dict[str, Any]:
    """Normalize an LLM validity_scope object -> {} unless real content.
    Keeps only known keys; drops empty lists/strings. claim/formula only."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in ("region", "assumptions"):
        v = raw.get(key)
        if isinstance(v, list):
            items = [x.strip() for x in v if isinstance(x, str) and x.strip()]
            if items:
                out[key] = items
    for key in ("approximation", "range"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
    return out


def _resolve(elements: List[SourceElementQ], ev: Any,
             name: str) -> Optional[SourceElementQ]:
    try:
        i = int(ev)
    except Exception:
        i = -1
    if 0 <= i < len(elements):
        return elements[i]
    # fallback: element whose text contains the node name (normalized substring)
    nn = " ".join((name or "").split()).lower()
    if nn:
        for e in elements:
            if nn in " ".join(e.text.split()).lower():
                return e
    return None


def _ev(el: SourceElementQ) -> Evidence:
    return Evidence(file=el.file, char_start=el.char_start, char_end=el.char_end,
                    line_start=el.line_start, line_end=el.line_end, quote=el.text)


def _parse_steps(elements: List[SourceElementQ], raw_steps: Any) -> List[Step]:
    """Resolve an LLM `steps` array into grounded Step objects (drop unbindable)."""
    steps: List[Step] = []
    if not isinstance(raw_steps, list):
        return steps
    for st in raw_steps:
        if not isinstance(st, dict):
            continue
        nm = str(st.get("name", "")).strip()
        if not nm:
            continue
        el = _resolve(elements, st.get("ev"), nm)
        if el is None:
            continue
        steps.append(Step(name=nm, evidence=[_ev(el)]))
    return steps


def refine_nodes(client: Any, elements: List[SourceElementQ], nodes: List[Node],
                 section_path: str = "") -> List[Node]:
    """Self-refinement pass: ask the LLM to drop nodes not supported by the source
    elements. No-op when there are no nodes or the client is unconfigured (so the
    deterministic / test path never calls the network). On any parse/transport
    soft-failure, returns nodes unchanged (only hard transport errors propagate)."""
    if not nodes or not getattr(client, "configured", False):
        return nodes
    records_block = "\n".join(f"[{i}] {n.type}: {n.name}" for i, n in enumerate(nodes))
    elements_block = "\n".join(f"[{i}] {e.text}" for i, e in enumerate(elements))
    try:
        raw = client.chat_json(
            [{"role": "user",
              "content": refine_prompt(section_path, records_block, elements_block)}],
            REFINE_SCHEMA_HINT,
            **_extract_call_kwargs(client, _refine_response_cacheable),
        )
        data = safe_json(raw)
    except KgBuildAborted:
        raise
    except (APIConnectionError, APITimeoutError):
        raise
    except Exception:
        return nodes
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return nodes
    drop = set()
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("index"), int) \
                and it.get("keep") is False:
            drop.add(it["index"])
    if not drop:
        return nodes
    return [n for i, n in enumerate(nodes) if i not in drop]


def _glean_nodes(client: Any, elements: List[SourceElementQ], section_path: str,
                 doc_type: str, first_raw: str, nodes: List[Node], win_idx: int,
                 max_rounds: int, base_filter: bool = False) -> None:
    """Gleaning: ask the LLM for MISSED nodes and append new (deduped, grounded)
    ones to `nodes` in place. v1 = nodes only (no edges). Best-effort: no-op when
    unconfigured; on any failure returns with whatever was gathered (never raises —
    the first pass already succeeded). Early-stops when a round adds nothing."""
    if not getattr(client, "configured", False) or max_rounds < 1:
        return

    def _nm(s: str) -> str:
        return " ".join((s or "").split()).lower()

    seen = {(n.type, _nm(n.name)) for n in nodes}
    labeled = "\n".join(f"[{i}] {e.text}" for i, e in enumerate(elements))
    messages = [
        {"role": "user", "content": _prompt(labeled, section_path, doc_type, base_filter=base_filter)},
        {"role": "assistant", "content": first_raw},
        {"role": "user", "content": gleaning_prompt(section_path, doc_type)},
    ]
    for _ in range(max_rounds):
        try:
            raw = client.chat_json(messages, _KG_SCHEMA_HINT,
                                   **_extract_call_kwargs(client, _kg_fragment_cacheable))
            data = safe_json(raw)
        except KgBuildAborted:
            raise
        except Exception:
            return
        added = 0
        for it in (data.get("nodes") or []):
            if not isinstance(it, dict) or it.get("type") not in NODE_TYPES:
                continue
            nm = _nm(str(it.get("name", "")))
            key = (it["type"], nm)
            if not nm or key in seen:
                continue
            el = _resolve(elements, it.get("ev"), str(it.get("name", "")))
            if el is None:
                continue
            node = Node(id=f"W{win_idx}-{len(nodes)}", type=it["type"],
                        name=str(it.get("name", "")), section_path=section_path,
                        evidence=[_ev(el)])
            if it["type"] == "Procedure":
                node.steps = _parse_steps(elements, it.get("steps"))
            if it["type"] in ("Claim", "Formula"):
                node.validity_scope = _parse_validity_scope(it.get("validity_scope"))
            nodes.append(node)
            seen.add(key)
            added += 1
        if added == 0:
            return   # early stop: nothing new this round
        messages += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "Any more missed nodes? Same rules; empty list if none."},
        ]


def extract_window(client: Any, elements: List[SourceElementQ], section_path: str,
                   doc_type: str, win_idx: int = 0, refine: bool = False,
                   gleaning_rounds: int = 0, base_filter: bool = False,
                   refine_client: Any | None = None,
                   glean_client: Any | None = None,
                   ) -> Tuple[List[Node], List[Edge]]:
    if not elements:
        return [], []
    labeled = "\n".join(f"[{i}] {e.text}" for i, e in enumerate(elements))
    try:
        # OpenAICompatibleClient.chat_json takes (messages, response_schema_hint).
        raw = client.chat_json(
            [{"role": "user",
              "content": _prompt(labeled, section_path, doc_type, base_filter=base_filter)}],
            _KG_SCHEMA_HINT,
            **_extract_call_kwargs(client, _kg_fragment_cacheable),
        )
        data = safe_json(raw)
    except KgBuildAborted:
        raise
    except (APIConnectionError, APITimeoutError):
        raise            # hard failure: window never processed — caller counts it
    except Exception:
        return [], []    # soft: unparseable/empty — legitimately 0 nodes
    nodes: List[Node] = []
    by_local = {}
    for it in (data.get("nodes") or []):
        if not isinstance(it, dict) or it.get("type") not in NODE_TYPES:
            continue
        el = _resolve(elements, it.get("ev"), str(it.get("name", "")))
        if el is None:
            continue
        nid = f"W{win_idx}-{len(nodes)}"
        node = Node(id=nid, type=it["type"], name=str(it.get("name", "")),
                    section_path=section_path, evidence=[_ev(el)])
        if it["type"] == "Procedure":
            node.steps = _parse_steps(elements, it.get("steps"))
        if it["type"] in ("Claim", "Formula"):
            node.validity_scope = _parse_validity_scope(it.get("validity_scope"))
        nodes.append(node)
        if it.get("local_id"):
            by_local[str(it["local_id"])] = nid
    if gleaning_rounds and nodes:
        _glean_nodes(glean_client or client, elements, section_path, doc_type, raw, nodes, win_idx,
                     gleaning_rounds, base_filter=base_filter)
    if refine and nodes:
        # refine is best-effort: a failure (incl. hard transport errors that
        # refine_nodes re-raises) must NOT discard a successfully extracted
        # window — degrade to unfiltered nodes instead.
        try:
            kept = refine_nodes(refine_client or client, elements, nodes, section_path)
        except KgBuildAborted:
            raise
        except Exception:
            kept = nodes
        kept_ids = {n.id for n in kept}
        nodes = kept
        by_local = {lid: nid for lid, nid in by_local.items() if nid in kept_ids}
    edges: List[Edge] = []
    for it in (data.get("edges") or []):
        if not isinstance(it, dict) or it.get("type") not in EDGE_TYPES:
            continue
        s = by_local.get(str(it.get("source"))); t = by_local.get(str(it.get("target")))
        if not s or not t or s == t:
            continue
        el = _resolve(elements, it.get("ev"), "")
        ev = [_ev(el)] if el is not None else []
        edges.append(Edge(id=f"E{win_idx}-{len(edges)}", type=it["type"],
                          source_id=s, target_id=t, evidence=ev))
    return nodes, edges
