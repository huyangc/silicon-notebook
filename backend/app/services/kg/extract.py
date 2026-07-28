"""Window -> LLM KG fragment -> grounded nodes/edges. Local ids are kept so the
caller can wire edges. Evidence is anchored by element-id markers: the LLM emits
only an integer "ev" label per node/edge, and the backend maps it back to that
source element's exact text/offsets (drop ungroundable nodes). Node types
constrained to the 4; edges to the vocab."""
from __future__ import annotations
import logging
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple
from openai import APIConnectionError, APITimeoutError
from app.core.llm import cap_kwargs
from app.services.kg.edge_schema import (
    VALID_EDGE_TYPES,
    edge_schema_hint,
    is_valid_edge_pair,
    render_edge_prompt_rules,
)
from app.services.kg.json_utils import safe_json
from app.services.kg.models import Edge, Evidence, Node, Step
from app.services.kg.parsing import SourceElementQ
from app.services.kg.run_control import KgBuildAborted
from app.services.prompts import gleaning_prompt, refine_prompt, REFINE_SCHEMA_HINT

NODE_TYPES = {"Concept", "Claim", "Formula", "Procedure"}
EDGE_TYPES = VALID_EDGE_TYPES
_EDGE_PROMPT_RULES = render_edge_prompt_rules()
_log = logging.getLogger(__name__)

_KG_SCHEMA_HINT = (
    '{"nodes":[{"local_id":"","type":"Concept|Claim|Formula|Procedure","name":"",'
    '"ev":0,"validity_scope":{"region":[],"assumptions":[],"approximation":"","range":""},'
    '"steps":[{"name":"","ev":0}]}],'
    f'"edges":[{{"type":"{edge_schema_hint()}",'
    '"source":"<local_id>","target":"<local_id>","ev":0}]}'
)


def _grounds_a_node(it: Any, elements: List[SourceElementQ]) -> bool:
    """Does this one LLM node dict clear the SAME gate extract_window / _glean_nodes
    apply before the entry becomes an object? Downstream keeps a node only when it
    is a typed dict (``isinstance(it, dict) and it.get("type") in NODE_TYPES``) with
    a non-empty ``name`` that ``_resolve`` can bind to some window element — by the
    integer ``ev`` label, or (fallback) because the name is a substring of an
    element's text. This calls the very same ``_resolve``, so the cache decision can
    never drift from what actually gets consumed (that drift is exactly what the
    per-shape tightening kept missing round after round).

    Degraded path — ``elements`` empty (a caller/test with no window to resolve
    against): fall back to the minimal field set the real gate needs, a non-empty
    ``name`` AND an integer ``ev``. Conservative by construction (it cannot use the
    name-substring fallback), which only ever means "re-fetch", never "cache junk"."""
    if not isinstance(it, dict) or it.get("type") not in NODE_TYPES:
        return False
    name = str(it.get("name", "")).strip()
    if not name:
        return False
    if elements:
        return _resolve(elements, it.get("ev"), name) is not None
    return isinstance(it.get("ev"), int)


def _fragment_grounds_something(content: str, elements: List[SourceElementQ]) -> bool:
    """Cache-admission gate for the KG-fragment schema (_KG_SCHEMA_HINT), judged by
    running the DOWNSTREAM grounding logic itself instead of a shape approximation.

    is_cacheable_llm_response already rejects the empty "{}" fallback, unparseable
    JSON and budget-truncated replies, but a syntactically valid reply can still be
    a NON-extraction that must never be frozen for the 90-day TTL. Earlier rounds
    tightened the shape check case-by-case and never converged: every round found a
    new reply that parsed yet grounded to 0 objects downstream (an ``{"error":..}``
    envelope; a ``nodes`` list of typeless junk; and — the one this closes — a
    PRESENT-but-ungroundable node like ``{"nodes":[{"type":"Concept"}]}`` that has a
    type but no name/ev, so extract_window drops it and the window yields nothing).
    The root cause is that "is this reply usable" IS the downstream consumption
    logic; any approximation of it leaks. So cache iff the fragment produces the
    same thing the pipeline would keep:

    - dict, no ``error`` key, ``nodes`` is a list, ``edges`` (if present) a list —
      structural validity; anything else is error/malformed → reject.
    - ``nodes == []`` — a LEGIT empty window (the model genuinely found nothing to
      extract; ``{"nodes":[],"edges":[]}``). No ungroundable junk → cache it.
    - otherwise cache iff AT LEAST ONE present node grounds via ``_grounds_a_node``
      (the real ``_resolve`` gate). A list whose nodes ALL fail to ground drops to 0
      objects downstream — freezing that 0 for the TTL is the poison → reject.

    The window's ``elements`` are closed in by the caller (extract_window /
    _glean_nodes build ``lambda content: _fragment_grounds_something(content,
    elements)``) so grounding is the ACTUAL resolve, not a proxy. Passed to
    chat_json as response_validator; a fault there conservatively skips the write
    (OpenAICompatibleClient._response_validator_allows). On a cache HIT this same
    gate re-judges the stored value against this window's elements, so any
    laxer-era poison already in the cache is treated as a miss and re-fetched."""
    data = safe_json(content)
    if not isinstance(data, dict) or "error" in data:
        return False
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        return False
    edges = data.get("edges")
    if edges is not None and not isinstance(edges, list):
        return False
    if not nodes:
        return True  # legit empty window — a real "found nothing" extraction
    return any(_grounds_a_node(it, elements) for it in nodes)


def _kg_fragment_cacheable(content: str) -> bool:
    """Degraded (window-less) entry to _fragment_grounds_something for callers/tests
    that have no ``elements`` to resolve against — approximates grounding with the
    minimal field set (see _grounds_a_node). Real extraction callers pass an
    elements-closed validator instead, so grounding is checked for real."""
    return _fragment_grounds_something(content, [])


def _refine_response_grounds(content: str, nodes: List[Node]) -> bool:
    """Cache-admission gate for the refine schema (REFINE_SCHEMA_HINT,
    ``{"items":[{"index":0,"keep":true}]}``), closed over the CURRENT node list the
    same way ``_fragment_grounds_something`` closes over the window elements. Same
    tightening rationale as ``_kg_fragment_cacheable``: cache only a reply we are sure
    is a real refine result, since skipping a doubtful one costs just a re-fetch.

    ``refine_nodes`` consumes ``data["items"]`` and, per entry, indexes
    ``nodes[index]`` and drops that node when ``keep is False``. So a usable reply
    requires, for EVERY item:

    - ``type(index) is int`` — ``type() is``, NOT ``isinstance``: ``bool`` is a
      subclass of ``int`` in Python, so ``isinstance(True, int)`` is True and an
      ``index: true`` would sail through and be consumed as index 1. ``type() is int``
      excludes ``True``/``False``.
    - ``0 <= index < len(nodes)`` WHEN the node list is known (the real refine call
      closes it in): an out-of-range index is silently ignored downstream, so a reply
      full of them refines nothing — freezing that no-op for the 90-day TTL is the
      poison. With NO nodes (the window-less degraded entry / test doubles) the bound
      is unknowable, so it degrades to the type check only (conservative: it can only
      ever mean "re-fetch", never "cache junk").
    - ``keep`` is a real ``bool``.

    A genuine "keep everything" is ``{"items":[]}`` (empty list, no drops), NOT an
    absent key — so absence / a wrong-typed ``items`` / an ``error`` envelope is
    rejected, not treated as empty. Passed to chat_json as response_validator; a fault
    there conservatively skips the write (OpenAICompatibleClient.
    _response_validator_allows). On a cache HIT this same gate re-judges the stored
    value against the current nodes, so any laxer-era poison is treated as a miss."""
    data = safe_json(content)
    if not isinstance(data, dict) or "error" in data:
        return False
    items = data.get("items")
    if not isinstance(items, list):
        return False
    for it in items:
        if not isinstance(it, dict):
            return False
        index = it.get("index")
        if type(index) is not int:  # bool is an int subclass — exclude True/False
            return False
        if not isinstance(it.get("keep"), bool):
            return False
        if nodes and not (0 <= index < len(nodes)):
            return False
    return True


def _refine_response_cacheable(content: str) -> bool:
    """Degraded (node-less) entry to ``_refine_response_grounds`` for callers/tests
    with no ``nodes`` to bound-check against — keeps the structural + ``type(index)
    is int`` + ``keep`` bool checks and drops only the range check (see there). Real
    refine callers pass a nodes-closed validator, so the bound is enforced there."""
    return _refine_response_grounds(content, [])


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

EDGES (source->target by local_id). Use ONLY the endpoint pairs below. Reasoning-
bearing edges are the priority and may connect Claims/Formulas/Concepts, not only
Concept->Concept:
{_EDGE_PROMPT_RULES}
EXPLICITLY HUNT depends_on, contrasts_with, prerequisite_of — rare and high-value;
look for "requires", "assuming", "unlike", "trade-off", "valid when", "before".

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
            **_extract_call_kwargs(
                client, lambda content: _refine_response_grounds(content, nodes)
            ),
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
            raw = client.chat_json(
                messages, _KG_SCHEMA_HINT,
                **_extract_call_kwargs(
                    client,
                    lambda content: _fragment_grounds_something(content, elements),
                ),
            )
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
    # OpenAICompatibleClient.chat_json takes (messages, response_schema_hint).
    # Let every invocation exception escape: extract_graph owns per-window
    # isolation and increments failed_windows when a Future raises.  Returning an
    # empty fragment here used to turn adapter/programming failures (notably an
    # unsupported response_validator kwarg) into false successes, producing
    # completed runs with windows_failed=0/N and objects=0 without ever calling
    # the model.  Malformed *content* remains a soft empty fragment through
    # safe_json below; only a failed invocation is counted as a failed window.
    raw = client.chat_json(
        [{"role": "user",
          "content": _prompt(labeled, section_path, doc_type, base_filter=base_filter)}],
        _KG_SCHEMA_HINT,
        **_extract_call_kwargs(
            client,
            lambda content: _fragment_grounds_something(content, elements),
        ),
    )
    data = safe_json(raw)
    nodes: List[Node] = []
    by_local = {}
    type_by_node_id: Dict[str, str] = {}
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
        type_by_node_id[nid] = node.type
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
        type_by_node_id = {
            node_id: node_type for node_id, node_type in type_by_node_id.items()
            if node_id in kept_ids
        }
    edges: List[Edge] = []
    rejected_edges: Counter[tuple[str, str]] = Counter()
    for it in (data.get("edges") or []):
        if not isinstance(it, dict):
            continue
        if it.get("type") not in EDGE_TYPES:
            rejected_edges[(str(it.get("type") or "<missing>"), "unknown_edge_type")] += 1
            continue
        s = by_local.get(str(it.get("source"))); t = by_local.get(str(it.get("target")))
        if not s or not t or s == t:
            rejected_edges[(it["type"], "missing_or_self_endpoint")] += 1
            continue
        if not is_valid_edge_pair(
            it["type"], type_by_node_id.get(s), type_by_node_id.get(t)
        ):
            rejected_edges[(it["type"], "invalid_endpoint_pair")] += 1
            continue
        el = _resolve(elements, it.get("ev"), "")
        ev = [_ev(el)] if el is not None else []
        edges.append(Edge(id=f"E{win_idx}-{len(edges)}", type=it["type"],
                          source_id=s, target_id=t, evidence=ev))
    if rejected_edges:
        _log.warning(
            "kg_edge_contract_rejections window=%s counts=%s",
            win_idx,
            sorted(
                (edge_type, reason, count)
                for (edge_type, reason), count in rejected_edges.items()
            ),
        )
    return nodes, edges
