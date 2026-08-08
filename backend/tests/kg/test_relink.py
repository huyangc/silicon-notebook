"""Tests for relink core — complete_isolated_edges.

确定性重连孤立节点(degree 0)的纯函数行为契约:
- Rule 1 共享证据元素(shared element_id)→ about/used_in 按类型定向
- Rule 2 概念名出现在 claim/formula 文本中(词边界)→ about
- INTRA-SOURCE only / 不自环 / 去重 / max_per_node 上限
"""
import app.services.kg.relink as relink_mod
from app.services.kg.relink import complete_isolated_edges, _pair_key


def _node(nid, otype, name, source_id="S1", element_ids=()):
    return {
        "id": nid,
        "object_type": otype,
        "name": name,
        "source_id": source_id,
        "element_ids": set(element_ids),
    }


# ---- Rule 1: shared evidence element ----

def test_isolated_claim_concept_shared_element_emits_about():
    nodes = [
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1"}),
        _node("k1", "concept", "open-loop gain", element_ids={"E1"}),
    ]
    out = complete_isolated_edges(nodes, edges=[])
    assert out == [{
        "source_object_id": "c1",
        "target_object_id": "k1",
        "edge_type": "about",
        "basis": "relink:shared-element",
    }]


def test_isolated_concept_with_claim_emits_about_claim_as_source():
    # N=concept, M=claim → edge must originate from the claim (M → N).
    nodes = [
        _node("k1", "concept", "open-loop gain", element_ids={"E1"}),
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1"}),
    ]
    out = complete_isolated_edges(nodes, edges=[])
    assert out == [{
        "source_object_id": "c1",
        "target_object_id": "k1",
        "edge_type": "about",
        "basis": "relink:shared-element",
    }]


def test_formula_concept_shared_element_emits_about():
    nodes = [
        _node("f1", "formula", "A = gm * Rout", element_ids={"E2"}),
        _node("k1", "concept", "transconductance", element_ids={"E2"}),
    ]
    out = complete_isolated_edges(nodes, edges=[])
    assert out == [{
        "source_object_id": "f1",
        "target_object_id": "k1",
        "edge_type": "about",
        "basis": "relink:shared-element",
    }]


def test_procedure_concept_shared_element_emits_used_in_concept_to_procedure():
    # concept used_in procedure → (concept → procedure).
    nodes = [
        _node("p1", "procedure", "Bias the amplifier", element_ids={"E3"}),
        _node("k1", "concept", "quiescent current", element_ids={"E3"}),
    ]
    out = complete_isolated_edges(nodes, edges=[])
    assert out == [{
        "source_object_id": "k1",
        "target_object_id": "p1",
        "edge_type": "used_in",
        "basis": "relink:shared-element",
    }]


def test_isolated_concept_with_procedure_emits_used_in():
    # N=concept, M=procedure → (N → M) used_in.
    nodes = [
        _node("k1", "concept", "quiescent current", element_ids={"E3"}),
        _node("p1", "procedure", "Bias the amplifier", element_ids={"E3"}),
    ]
    out = complete_isolated_edges(nodes, edges=[])
    assert out == [{
        "source_object_id": "k1",
        "target_object_id": "p1",
        "edge_type": "used_in",
        "basis": "relink:shared-element",
    }]


def test_no_concept_involved_is_skipped():
    # claim↔claim sharing an element has no safe edge type → skip.
    nodes = [
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1"}),
        _node("c2", "claim", "Bandwidth shrinks", element_ids={"E1"}),
    ]
    assert complete_isolated_edges(nodes, edges=[]) == []


def test_claim_formula_shared_element_skipped_no_concept():
    nodes = [
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1"}),
        _node("f1", "formula", "A = gm Rout", element_ids={"E1"}),
    ]
    assert complete_isolated_edges(nodes, edges=[]) == []


# ---- INTRA-SOURCE guard ----

def test_different_source_never_linked():
    nodes = [
        _node("c1", "claim", "Gain rises with bias", source_id="S1", element_ids={"E1"}),
        _node("k1", "concept", "open-loop gain", source_id="S2", element_ids={"E1"}),
    ]
    assert complete_isolated_edges(nodes, edges=[]) == []


# ---- already-connected nodes ----

def test_already_connected_node_gets_no_new_edge():
    # c1 already appears in an existing edge → it is not isolated.
    nodes = [
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1"}),
        _node("k1", "concept", "open-loop gain", element_ids={"E1"}),
        _node("kx", "concept", "feedback factor", element_ids={"E9"}),
    ]
    edges = [("c1", "kx")]
    # k1 is isolated, c1 is not. k1 shares E1 with c1 (a claim) → c1→k1 about.
    out = complete_isolated_edges(nodes, edges=edges)
    assert out == [{
        "source_object_id": "c1",
        "target_object_id": "k1",
        "edge_type": "about",
        "basis": "relink:shared-element",
    }]


def test_both_connected_emits_nothing():
    nodes = [
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1"}),
        _node("k1", "concept", "open-loop gain", element_ids={"E1"}),
    ]
    edges = [("c1", "k1")]
    assert complete_isolated_edges(nodes, edges=edges) == []


# ---- Rule 2: name-in-text ----

def test_rule2_name_match_claim_to_concept():
    nodes = [
        _node("c1", "claim", "DeepSeek-V4 uses hybrid CSA for attention",
              element_ids={"E1"}),
        # concept lives in same source but shares NO element id → only rule 2 can link.
        _node("k1", "concept", "hybrid CSA", element_ids={"E2"}),
    ]
    out = complete_isolated_edges(nodes, edges=[])
    assert out == [{
        "source_object_id": "c1",
        "target_object_id": "k1",
        "edge_type": "about",
        "basis": "relink:name-match",
    }]


def test_rule2_rejects_generic_concept_name():
    nodes = [
        _node("c1", "claim", "The model converges quickly", element_ids={"E1"}),
        _node("k1", "concept", "model", element_ids={"E2"}),
    ]
    assert complete_isolated_edges(nodes, edges=[]) == []


def test_rule2_stoplist_rejects_even_when_long_enough_single_token():
    # "system" is 6 chars — it clears the >=6 single-token length guard on
    # its own, so this isolates the STOPLIST rejection specifically. (The
    # test above uses "model", which is ALSO caught by the length guard —
    # emptying _GENERIC_STOPLIST would leave it green. This one would not.)
    nodes = [
        _node("c1", "claim", "The system converges quickly", element_ids={"E1"}),
        _node("k1", "concept", "system", element_ids={"E2"}),
    ]
    assert complete_isolated_edges(nodes, edges=[]) == []


def test_rule2_requires_word_boundary():
    # Concept "regularizer" (11 chars, single token) appears only as a substring
    # of "regularizers" — that DOES word-match (\b allows the plural? no: \b sits
    # between 'r' and 's'? 's' is a word char so NO boundary). Use a true
    # substring-only case: concept embedded mid-word must NOT match.
    nodes = [
        _node("c1", "claim", "The preconditioner stabilises training",
              element_ids={"E1"}),
        _node("k1", "concept", "condition", element_ids={"E2"}),
    ]
    # "condition" is a substring of "preconditioner" but not a word-boundary
    # match → no link.
    assert complete_isolated_edges(nodes, edges=[]) == []


def test_rule2_single_token_short_concept_rejected():
    # single-token concept length 5 (<6) → rejected even on a clean word match.
    nodes = [
        _node("c1", "claim", "The relu activation saturates", element_ids={"E1"}),
        _node("k1", "concept", "relu", element_ids={"E2"}),
    ]
    # "relu" is 4 chars single-token → below the 6-char single-token guard.
    assert complete_isolated_edges(nodes, edges=[]) == []


def test_rule2_disabled_by_flag():
    nodes = [
        _node("c1", "claim", "DeepSeek-V4 uses hybrid CSA for attention",
              element_ids={"E1"}),
        _node("k1", "concept", "hybrid CSA", element_ids={"E2"}),
    ]
    assert complete_isolated_edges(nodes, edges=[], enable_name_match=False) == []


def test_rule2_only_for_claim_or_formula():
    # An isolated concept does not get rule-2 treatment (it is not claim/formula).
    nodes = [
        _node("k1", "concept", "hybrid CSA mechanism", element_ids={"E1"}),
        _node("k2", "concept", "hybrid CSA", element_ids={"E2"}),
    ]
    # No shared element; rule 2 does not apply to concept N → nothing.
    assert complete_isolated_edges(nodes, edges=[]) == []


def test_rule2_picks_longest_matching_concept_when_capped():
    # The claim text matches BOTH concepts by word boundary — "phase margin"
    # contains the standalone word "margin" too ("...phase margin behavior...").
    # With max_per_node=1 only ONE edge can land on c1, and it must go to the
    # longer/more-specific name per the concept_cands sort key
    # (-len(name), id). k_short is listed BEFORE k_long here (opposite of
    # the expected winning order) on purpose: a mutation that just deletes
    # the `sorted(...)` call (falling back to insertion/sibling-scan order)
    # would then bind "margin" too, same as a mutation that reverses the
    # sort key — both must turn this red.
    nodes = [
        _node("c1", "claim", "The phase margin behavior degrades under load",
              element_ids={"E1"}),
        _node("k_short", "concept", "margin", element_ids={"E3"}),
        _node("k_long", "concept", "phase margin", element_ids={"E2"}),
    ]
    out = complete_isolated_edges(nodes, edges=[], max_per_node=1)
    assert out == [{
        "source_object_id": "c1",
        "target_object_id": "k_long",
        "edge_type": "about",
        "basis": "relink:name-match",
    }]


# ---- max_per_node cap ----

def test_max_per_node_cap_respected():
    # max_per_node caps the relink degree of EVERY endpoint. Here c1 (a claim)
    # could share E1 with four concepts; with cap=2 it gets exactly 2 edges, and
    # no single node id ever exceeds the cap regardless of iteration order.
    nodes = [
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1"}),
        _node("k1", "concept", "open-loop gain", element_ids={"E1"}),
        _node("k2", "concept", "phase margin", element_ids={"E1"}),
        _node("k3", "concept", "feedback factor", element_ids={"E1"}),
        _node("k4", "concept", "slew rate", element_ids={"E1"}),
    ]
    out = complete_isolated_edges(nodes, edges=[], max_per_node=2)
    src_c1 = [e for e in out if e["source_object_id"] == "c1"]
    assert len(src_c1) == 2
    # No node id participates in more than `max_per_node` new edges.
    from collections import Counter
    deg = Counter()
    for e in out:
        deg[e["source_object_id"]] += 1
        deg[e["target_object_id"]] += 1
    assert all(v <= 2 for v in deg.values())


def test_ranking_prefers_more_shared_elements():
    # c1 shares 2 elements with k2, 1 with k1 → k2 ranked first.
    nodes = [
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1", "E2"}),
        _node("k1", "concept", "open-loop gain", element_ids={"E1"}),
        _node("k2", "concept", "phase margin", element_ids={"E1", "E2"}),
    ]
    out = complete_isolated_edges(nodes, edges=[], max_per_node=1)
    assert [e["target_object_id"] for e in out] == ["k2"]


# ---- dedupe ----

def test_existing_edge_either_direction_blocks_relink():
    # Connected-node short-circuit: the existing k1→c1 edge makes BOTH c1 and k1
    # non-isolated, so neither is ever processed — relink returns early at the
    # `connected` check. (The either-direction dedupe guard inside _try_emit is
    # NOT what fires here; that path is covered by the test below where c1 stays
    # isolated.)
    nodes = [
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1"}),
        _node("k1", "concept", "open-loop gain", element_ids={"E1"}),
    ]
    edges = [("k1", "c1")]   # reverse direction of what relink would propose
    assert complete_isolated_edges(nodes, edges=edges) == []


def test_try_emit_blocks_reverse_existing_edge_to_connected_sibling():
    # An existing reverse edge k1→c1 must never be duplicated as c1→k1 by relink.
    # NOTE: because any edge that mentions c1 also marks c1 connected, c1 is not
    # isolated here and is short-circuited before _try_emit — so the EXISTING-pair
    # dedupe guard is structurally subsumed by the `connected` check for degree-0
    # inputs. This test pins the observable contract (no reverse duplicate emitted);
    # the guard itself is kept as cheap defensive insurance for callers that may
    # compute isolation differently than strict degree-0. See report concern.
    nodes = [
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1"}),
        _node("k1", "concept", "open-loop gain", element_ids={"E1"}),
        _node("kx", "concept", "feedback factor", element_ids={"E9"}),
    ]
    edges = [("k1", "kx"), ("k1", "c1")]
    assert complete_isolated_edges(nodes, edges=edges) == []


def test_dedupe_among_new_edges():
    # Two isolated claims both point at the same concept via shared element — each
    # claim emits its own edge (different source), but a single claim must not emit
    # the same edge twice even if two element ids are shared.
    nodes = [
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1", "E2"}),
        _node("k1", "concept", "open-loop gain", element_ids={"E1", "E2"}),
    ]
    out = complete_isolated_edges(nodes, edges=[])
    assert out == [{
        "source_object_id": "c1",
        "target_object_id": "k1",
        "edge_type": "about",
        "basis": "relink:shared-element",
    }]


def test_no_self_loop():
    # A degenerate node sharing an element with itself only → never a self-loop.
    nodes = [_node("k1", "concept", "open-loop gain", element_ids={"E1"})]
    assert complete_isolated_edges(nodes, edges=[]) == []


# ---- combined rule-1 then rule-2 honour the same cap ----

def test_rule2_counts_against_rule1_cap():
    # c1 gets 1 rule-1 edge (to k1 via shared E1) then rule-2 would add k2 by name,
    # but max_per_node=1 means rule-2 is blocked.
    nodes = [
        _node("c1", "claim", "DeepSeek-V4 uses hybrid CSA for attention",
              element_ids={"E1"}),
        _node("k1", "concept", "open-loop gain", element_ids={"E1"}),
        _node("k2", "concept", "hybrid CSA", element_ids={"E2"}),
    ]
    out = complete_isolated_edges(nodes, edges=[], max_per_node=1)
    assert out == [{
        "source_object_id": "c1",
        "target_object_id": "k1",
        "edge_type": "about",
        "basis": "relink:shared-element",
    }]


# ---- rule-2 pattern compiled once per concept, not once per (claim, concept)
#      pair (perf-audit hot path: re.compile dominated 89% of a full rebuild
#      before this cache) ----

def test_name_pattern_compiled_once_per_concept_not_per_pair(monkeypatch):
    calls = []
    real_compile = relink_mod.re.compile
    str_pattern_search_calls = []
    real_search = relink_mod.re.search

    def counting_compile(pattern, *args, **kwargs):
        calls.append(pattern)
        return real_compile(pattern, *args, **kwargs)

    def counting_search(pattern, *args, **kwargs):
        # The match call site must go through the COMPILED Pattern object's
        # own .search() (a bound method on the object _name_pattern
        # returned), never the module-level re.search(str, ...) function —
        # a regression to a per-pair `re.search(pattern_string, text)` call
        # would defeat the compile-once cache above (re's own internal
        # compile cache would silently absorb the recompiles) while still
        # passing the `calls`/one-per-concept assertion below, since that
        # assertion only watches relink_mod.re.compile, which a
        # re.search(str, ...) call site never touches. Calls with an
        # already-compiled Pattern object as the first arg are fine (not
        # what this guards against); only str-typed patterns are recorded.
        if isinstance(pattern, str):
            str_pattern_search_calls.append(pattern)
        return real_search(pattern, *args, **kwargs)

    monkeypatch.setattr(relink_mod.re, "compile", counting_compile)
    monkeypatch.setattr(relink_mod.re, "search", counting_search)

    # 3 isolated claims, all rule-2-testable against the same 2 concepts (no
    # shared elements between any claim and any concept, so only rule 2 can
    # fire). Each concept's pattern must be compiled exactly once regardless
    # of how many claims end up testing it.
    nodes = [
        _node(f"c{i}", "claim",
              "Note the open-loop gain and phase margin behavior here",
              element_ids={f"E{i}"})
        for i in range(3)
    ] + [
        _node("k1", "concept", "open-loop gain", element_ids={"EK1"}),
        _node("k2", "concept", "phase margin", element_ids={"EK2"}),
    ]
    out = complete_isolated_edges(nodes, edges=[], max_per_node=10)
    assert len(out) == 6  # 3 claims × 2 concepts, all within cap
    assert len(calls) == 2  # one compile per concept, not per (claim, concept) pair
    assert str_pattern_search_calls == []


# ---- element_ids computed once per node, not once per (isolated-node,
#      sibling) pair (perf-audit hot path, same shape as the re.compile
#      guard above) ----

def test_element_ids_returns_set_input_by_reference_without_copy():
    """已是 set 的 element_ids 必须按引用返回(消费方只读):整库 relink 路径
    本就物化了 set,再复制一份=受影响 source 的证据索引第二份全量拷贝
    (codex R1 P2)。"""
    owned = {"e1", "e2"}
    assert relink_mod._element_ids({"element_ids": owned}) is owned
    materialized = relink_mod._element_ids({"element_ids": ["e1", "e1", "e2"]})
    assert materialized == {"e1", "e2"}


def test_element_ids_computed_once_per_node_not_per_pair(monkeypatch):
    calls = []
    real_element_ids = relink_mod._element_ids

    def counting_element_ids(node):
        calls.append(node["id"])
        return real_element_ids(node)

    monkeypatch.setattr(relink_mod, "_element_ids", counting_element_ids)

    # 3 isolated claims all share element E1 with both concepts → each claim's
    # Rule-1 scan visits both concepts (and the other 2 claims) as siblings.
    # A per-pair recompute would call _element_ids many times per node (once
    # per sibling scan); the precompute must call it exactly once per node.
    nodes = [
        _node(f"c{i}", "claim", "shared claim text", element_ids={"E1", f"Ex{i}"})
        for i in range(3)
    ] + [
        _node("k1", "concept", "open-loop gain", element_ids={"E1"}),
        _node("k2", "concept", "phase margin", element_ids={"E1"}),
    ]
    out = complete_isolated_edges(nodes, edges=[], max_per_node=10)
    assert len(out) > 0  # sanity: this shape actually produces relink edges
    assert sorted(calls) == sorted(n["id"] for n in nodes)  # exactly once each


# ---- zero-isolated input skips ALL precompute (perf-audit P1: relink runs
#      after every successful extraction, and "LLM already connected every
#      node" is the steady-state common case, not an edge case) ----

def test_zero_isolated_input_skips_all_precompute(monkeypatch):
    elem_calls = []
    compile_calls = []
    real_element_ids = relink_mod._element_ids
    real_compile = relink_mod.re.compile

    def counting_element_ids(node):
        elem_calls.append(node["id"])
        return real_element_ids(node)

    def counting_compile(pattern, *args, **kwargs):
        compile_calls.append(pattern)
        return real_compile(pattern, *args, **kwargs)

    monkeypatch.setattr(relink_mod, "_element_ids", counting_element_ids)
    monkeypatch.setattr(relink_mod.re, "compile", counting_compile)

    nodes = [
        _node("c1", "claim", "Gain rises with bias", element_ids={"E1"}),
        _node("k1", "concept", "open-loop gain", element_ids={"E1"}),
    ]
    edges = [("c1", "k1")]  # every node already connected — nothing isolated
    assert complete_isolated_edges(nodes, edges=edges) == []
    assert elem_calls == []
    assert compile_calls == []


def test_precompute_scoped_to_sources_with_an_isolated_node(monkeypatch):
    """Item A ②: even when SOME nodes are isolated, precompute must skip
    every node belonging to a fully-connected source — the notebook-wide
    zero-isolated case (test above) is the degenerate empty-affected-set
    instance of this same scoping, not a separately-guarded special case.
    S1 has an isolated claim/concept pair; S2 is fully connected. Only S1's
    2 nodes may ever reach _element_ids/re.compile."""
    elem_calls = []
    real_element_ids = relink_mod._element_ids

    def counting_element_ids(node):
        elem_calls.append(node["id"])
        return real_element_ids(node)

    monkeypatch.setattr(relink_mod, "_element_ids", counting_element_ids)

    nodes = [
        _node("c1", "claim", "Gain rises with bias", source_id="S1", element_ids={"E1"}),
        _node("k1", "concept", "open-loop gain", source_id="S1", element_ids={"E1"}),
        _node("c2", "claim", "Unrelated other-source claim", source_id="S2", element_ids={"E9"}),
        _node("k2", "concept", "phase margin", source_id="S2", element_ids={"E9"}),
    ]
    edges = [("c2", "k2")]  # only S2's pair is connected; S1's pair is isolated
    out = complete_isolated_edges(nodes, edges=edges)
    assert out == [{
        "source_object_id": "c1",
        "target_object_id": "k1",
        "edge_type": "about",
        "basis": "relink:shared-element",
    }]
    assert sorted(elem_calls) == ["c1", "k1"]  # S2's nodes never touched


# ---- undirected existing-edge dedupe key ----

def test_pair_key_is_undirected_and_order_independent():
    assert _pair_key("a", "b") == _pair_key("b", "a")
    assert _pair_key("x1", "x1") == ("x1", "x1")
    # distinct pairs never collide via the min/max normalization.
    assert _pair_key("a", "b") != _pair_key("a", "c")
