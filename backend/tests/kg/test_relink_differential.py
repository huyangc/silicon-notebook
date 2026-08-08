"""Differential regression for relink's hot-path memoization (PR-A / 审计批2).

`complete_isolated_edges` moved per-pair `re.compile` calls (rule-2 concept
pattern) and per-pair `_element_ids` recomputation (rule-1) to a single
precompute pass, and switched the existing-edge undirected dedupe key from
`frozenset` to a sorted tuple. None of that is meant to change what edges
come out for a given input — this file freezes a verbatim copy of the
PRE-OPTIMIZATION algorithm (as it stood at the start of this change; the
`_ref_` prefix marks it as a frozen oracle, not a second production
implementation) and asserts the live `complete_isolated_edges` produces the
IDENTICAL edge list (content AND order) across a battery of boundary and
randomized inputs. Divergence here means the memoization changed behavior,
not just cost.

Retirement condition: if a future change intentionally changes relink's
PRODUCTION algorithm (not just its cost — e.g. a new rule, a different
tie-break, a changed guard threshold), this file's whole purpose goes away
along with it. Delete it wholesale and replace it with a fresh equivalence
argument for the new behavior — do not "fix" `_ref_*` to track the new
algorithm; a frozen oracle that gets synchronized with the thing it's
supposed to independently check stops checking anything.
"""
from __future__ import annotations

import random
import re
from typing import Dict, Iterable, List, Tuple

from app.services.kg.relink import complete_isolated_edges

# --------------------------------------------------------------------------
# Frozen oracle: pre-optimization implementation, copied verbatim (renamed
# `_ref_*`) from app/services/kg/relink.py as it stood before this change.
# Do NOT "fix" this to match the new implementation — its entire purpose is
# to stay naive (compiles/recomputes per pair) so it stays an independent
# check on output, not on cost.
# --------------------------------------------------------------------------

_REF_ABOUT = "about"
_REF_USED_IN = "used_in"
_REF_BASIS_SHARED = "relink:shared-element"
_REF_BASIS_NAME = "relink:name-match"

_REF_GENERIC_STOPLIST = frozenset({
    "model", "system", "method", "value", "data", "result", "function",
    "process", "approach", "network", "layer", "input", "output",
})

_REF_WS_RE = re.compile(r"[\s\-_]+")


def _ref_norm(name: str) -> str:
    return _REF_WS_RE.sub(" ", (name or "").strip().lower())


def _ref_element_ids(node: Dict) -> set:
    raw = node.get("element_ids") or ()
    return set(raw)


def _ref_shared_edge(n: Dict, m: Dict):
    tn, tm = n["object_type"], m["object_type"]
    if tn in ("claim", "formula") and tm == "concept":
        return (n["id"], m["id"], _REF_ABOUT)
    if tn == "concept" and tm in ("claim", "formula"):
        return (m["id"], n["id"], _REF_ABOUT)
    if tn == "procedure" and tm == "concept":
        return (m["id"], n["id"], _REF_USED_IN)
    if tn == "concept" and tm == "procedure":
        return (n["id"], m["id"], _REF_USED_IN)
    return None


def _ref_name_matches(concept_name: str, text: str) -> bool:
    norm = _ref_norm(concept_name)
    if len(norm) < 4:
        return False
    if norm in _REF_GENERIC_STOPLIST:
        return False
    if " " not in norm and len(norm) < 6:
        return False
    tokens = [re.escape(t) for t in norm.split(" ") if t]
    if not tokens:
        return False
    pattern = r"\b" + r"\W+".join(tokens) + r"\b"
    return re.search(pattern, text or "", re.IGNORECASE) is not None


def ref_complete_isolated_edges(
    nodes: Iterable[Dict],
    edges: Iterable[Tuple[str, str]],
    *,
    max_per_node: int = 3,
    enable_name_match: bool = True,
) -> List[Dict]:
    nodes = list(nodes)
    by_id = {n["id"]: n for n in nodes}

    connected: set = set()
    existing_pairs: set = set()
    for src, tgt in edges:
        connected.add(src)
        connected.add(tgt)
        existing_pairs.add(frozenset((src, tgt)))

    by_source: Dict[str, List[Dict]] = {}
    for n in nodes:
        by_source.setdefault(n.get("source_id"), []).append(n)

    new_edges: List[Dict] = []
    emitted_keys: set = set()
    degree_added: Dict[str, int] = {}

    def _at_cap(node_id: str) -> bool:
        return degree_added.get(node_id, 0) >= max_per_node

    def _try_emit(src: str, tgt: str, edge_type: str, basis: str) -> bool:
        if src == tgt:
            return False
        if _at_cap(src) or _at_cap(tgt):
            return False
        key = (src, tgt, edge_type)
        if key in emitted_keys:
            return False
        if frozenset((src, tgt)) in existing_pairs:
            return False
        emitted_keys.add(key)
        degree_added[src] = degree_added.get(src, 0) + 1
        degree_added[tgt] = degree_added.get(tgt, 0) + 1
        new_edges.append({
            "source_object_id": src,
            "target_object_id": tgt,
            "edge_type": edge_type,
            "basis": basis,
        })
        return True

    for n in nodes:
        if n["id"] in connected:
            continue

        emitted_for_n = 0
        n_elems = _ref_element_ids(n)
        siblings = by_source.get(n.get("source_id"), ())

        if n_elems:
            candidates: List[Tuple[int, int, int, str]] = []
            for m in siblings:
                if m["id"] == n["id"]:
                    continue
                overlap = len(n_elems & _ref_element_ids(m))
                if overlap <= 0:
                    continue
                if _ref_shared_edge(n, m) is None:
                    continue
                candidates.append((
                    overlap,
                    1 if m["object_type"] == "concept" else 0,
                    len(m.get("name") or ""),
                    m["id"],
                ))
            candidates.sort(key=lambda c: (-c[0], -c[1], -c[2], c[3]))
            for _shared, _isc, _nlen, mid in candidates:
                if _at_cap(n["id"]):
                    break
                edge = _ref_shared_edge(n, by_id[mid])
                if edge is None:
                    continue
                if _try_emit(edge[0], edge[1], edge[2], _REF_BASIS_SHARED):
                    emitted_for_n += 1

        if (
            emitted_for_n == 0
            and enable_name_match
            and n["object_type"] in ("claim", "formula")
        ):
            text = n.get("name") or ""
            concept_cands = sorted(
                (m for m in siblings
                 if m["id"] != n["id"] and m["object_type"] == "concept"),
                key=lambda m: (-len(m.get("name") or ""), m["id"]),
            )
            for m in concept_cands:
                if _at_cap(n["id"]):
                    break
                if _ref_name_matches(m.get("name") or "", text):
                    if _try_emit(n["id"], m["id"], _REF_ABOUT, _REF_BASIS_NAME):
                        emitted_for_n += 1

    return new_edges


# --------------------------------------------------------------------------
# Fixtures + differential assertions
# --------------------------------------------------------------------------

def _node(nid, otype, name, source_id="S1", element_ids=()):
    return {
        "id": nid, "object_type": otype, "name": name,
        "source_id": source_id, "element_ids": set(element_ids),
    }


def _assert_matches_oracle(nodes, edges=(), **kw):
    live = complete_isolated_edges(list(nodes), list(edges), **kw)
    ref = ref_complete_isolated_edges(list(nodes), list(edges), **kw)
    assert live == ref
    return live


def test_len_boundary_4_accept_3_reject_matches_oracle():
    nodes = [
        _node("c1", "claim", "The a bc value rises here", element_ids={"E1"}),
        _node("c2", "claim", "The a b value also appears", element_ids={"E2"}),
        _node("k4", "concept", "a bc", element_ids={"EK4"}),
        _node("k3", "concept", "a b", element_ids={"EK3"}),
    ]
    out = _assert_matches_oracle(nodes)
    assert out == [{
        "source_object_id": "c1", "target_object_id": "k4",
        "edge_type": "about", "basis": "relink:name-match",
    }]


def test_single_token_boundary_6_accept_5_reject_matches_oracle():
    nodes = [
        _node("c1", "claim", "The tanhkx activation saturates", element_ids={"E1"}),
        _node("c2", "claim", "The tanhk activation saturates too", element_ids={"E2"}),
        _node("k6", "concept", "tanhkx", element_ids={"EK6"}),
        _node("k5", "concept", "tanhk", element_ids={"EK5"}),
    ]
    out = _assert_matches_oracle(nodes)
    assert out == [{
        "source_object_id": "c1", "target_object_id": "k6",
        "edge_type": "about", "basis": "relink:name-match",
    }]


def test_case_insensitive_hyphen_joined_text_matches_oracle():
    nodes = [
        _node("c1", "claim", "The Open-Loop-GAIN increases with feedback",
              element_ids={"E1"}),
        _node("k1", "concept", "open loop gain", element_ids={"EK1"}),
    ]
    out = _assert_matches_oracle(nodes)
    assert out == [{
        "source_object_id": "c1", "target_object_id": "k1",
        "edge_type": "about", "basis": "relink:name-match",
    }]


def test_underscore_joined_text_does_not_match_either_implementation():
    # `_` is a \w char, so it does NOT introduce a \b boundary — pre-existing
    # regex semantics, pinned so a future change to the join character can't
    # silently start/stop matching underscore-glued mentions.
    nodes = [
        _node("c1", "claim", "The Open_Loop_GAIN increases with feedback",
              element_ids={"E1"}),
        _node("k1", "concept", "open loop gain", element_ids={"EK1"}),
    ]
    out = _assert_matches_oracle(nodes)
    assert out == []


def test_stoplist_whole_string_matches_oracle():
    nodes = [
        _node("c1", "claim", "The model converges quickly", element_ids={"E1"}),
        _node("k1", "concept", "model", element_ids={"EK1"}),
    ]
    assert _assert_matches_oracle(nodes) == []


def test_randomized_multi_source_mixed_rules_matches_oracle():
    """Many sources, mixed rule-1/rule-2 candidates, existing edges (including
    ones referencing ids outside `nodes`), several max_per_node caps and
    enable_name_match on/off — the combination that exercises both
    memoization paths (concept-pattern cache + element-id cache + rule-2
    per-source candidate cache) together."""
    rng = random.Random(20260808)
    names_concept = [
        "open-loop gain", "phase margin", "feedback factor", "slew rate",
        "quiescent current", "transconductance", "bandwidth", "noise figure",
        "common mode rejection", "power supply rejection",
        "input offset voltage", "gm", "vgs", "temperature coefficient",
        "a bc", "tanhkx",
    ]
    names_claim = [
        "Gain rises with bias and open-loop gain increases",
        "Phase margin shrinks under heavy load with feedback factor",
        "Slew rate limits large signal response near quiescent current",
        "The transconductance and bandwidth trade off against noise figure",
        "Common mode rejection depends on power supply rejection ratio",
        "Input offset voltage tracks temperature coefficient drift",
        "GM VGS relationship in saturation region for tanhkx activation",
    ]
    names_formula = ["A = gm * Rout", "BW = 1/(2*pi*R*C)", "PSRR = 20*log10(Av)"]

    nodes: List[Dict] = []
    nid = 0
    for src_i in range(6):
        sid = f"BIGSRC{src_i}"
        elem_pool = [f"E{src_i}-{j}" for j in range(8)]
        for _ in range(rng.randint(2, 6)):
            nid += 1
            elems = rng.sample(elem_pool, k=rng.randint(0, 3))
            nodes.append(_node(f"n{nid}", "concept", rng.choice(names_concept), sid, elems))
        for _ in range(rng.randint(2, 5)):
            nid += 1
            elems = rng.sample(elem_pool, k=rng.randint(0, 3))
            nodes.append(_node(f"n{nid}", "claim", rng.choice(names_claim), sid, elems))
        for _ in range(rng.randint(0, 2)):
            nid += 1
            elems = rng.sample(elem_pool, k=rng.randint(0, 3))
            nodes.append(_node(f"n{nid}", "formula", rng.choice(names_formula), sid, elems))
        for _ in range(rng.randint(0, 2)):
            nid += 1
            elems = rng.sample(elem_pool, k=rng.randint(0, 3))
            nodes.append(_node(f"n{nid}", "procedure", "Bias the amplifier stage", sid, elems))

    ids = [n["id"] for n in nodes]
    existing_edges = [tuple(rng.sample(ids, 2)) for _ in range(5)]

    for cap in (1, 2, 3, 5):
        out = _assert_matches_oracle(nodes, existing_edges, max_per_node=cap)
        assert out  # sanity: the random corpus actually produces relink edges
    _assert_matches_oracle(nodes, existing_edges, enable_name_match=False)
