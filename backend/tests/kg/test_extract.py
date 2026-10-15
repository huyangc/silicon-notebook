import json
import pytest
from app.services.kg.extract import (
    extract_window,
    _prompt,
    _fragment_grounds_something,
    _refine_response_grounds,
)
from app.services.kg.models import Node
from app.services.kg.parsing import SourceElementQ
from app.services.kg.run_control import KgBuildAborted, KgBuildFailure


def _se(idx: int, text: str, char_start: int) -> SourceElementQ:
    return SourceElementQ(
        id=f"SE-{idx}", type="paragraph", file="doc.md",
        line_start=idx + 1, line_end=idx + 1,
        char_start=char_start, char_end=char_start + len(text), text=text,
    )


# 4 elements with distinct text/offsets
ELEMENTS = [
    _se(0, "An analog signal is defined over a continuous range.", 0),
    _se(1, "C_j = C_j0 holds here.", 100),
    _se(2, "Engram is a memory architecture.", 200),
    _se(3, "Some unrelated filler sentence.", 300),
]


class Fake:
    def chat_json(self, messages, response_schema_hint):
        return json.dumps({"nodes": [
            # valid ev -> binds to element 0
            {"type": "Concept", "name": "analog signal", "ev": 0},
            # out-of-range ev, but name is substring of element 2 -> fallback
            {"type": "Claim", "name": "Engram", "ev": 99},
            # out-of-range ev and non-matching name -> dropped
            {"type": "Claim", "name": "no such thing here", "ev": 99}],
            "edges": [
            # valid ev -> evidence attached
            {"type": "about", "source": 1, "target": 0, "ev": 1},
            # missing ev -> edge kept, empty evidence
            {"type": "supports", "source": 0, "target": 1}]})


def test_marker_anchoring_binds_resolves_and_drops():
    nodes, edges = extract_window(Fake(), ELEMENTS, "1 > 1.1", "textbook", win_idx=0)
    # node 'c' dropped (bad ev + no name match); a and b survive
    by_name = {n.name: n for n in nodes}
    assert set(by_name) == {"analog signal", "Engram"}

    # node 'a' bound to element 0 exactly
    e0 = by_name["analog signal"].evidence[0]
    assert e0.quote == ELEMENTS[0].text
    assert e0.char_start == ELEMENTS[0].char_start
    assert e0.char_end == ELEMENTS[0].char_end
    assert e0.line_start == ELEMENTS[0].line_start
    assert e0.line_end == ELEMENTS[0].line_end

    # node 'b' fallback-bound to element 2 (name substring)
    e2 = by_name["Engram"].evidence[0]
    assert e2.quote == ELEMENTS[2].text
    assert e2.char_start == ELEMENTS[2].char_start

    # both edges kept
    assert len(edges) == 2
    edge_about = [e for e in edges if e.type == "about"][0]
    edge_supports = [e for e in edges if e.type == "supports"][0]
    # valid ev -> evidence attached to element 1
    assert len(edge_about.evidence) == 1
    assert edge_about.evidence[0].quote == ELEMENTS[1].text
    assert edge_about.evidence[0].char_start == ELEMENTS[1].char_start
    # missing ev -> empty evidence but edge kept
    assert edge_supports.evidence == []


def test_extract_window_empty_elements():
    nodes, edges = extract_window(Fake(), [], "1", "textbook")
    assert nodes == [] and edges == []


def test_edge_endpoints_read_like_ev_and_reject_bad_indexes(caplog):
    """Endpoints are integer positions in the reply's nodes array, read with
    the ``ev`` rule: an integer or a digit string binds; a boolean, float,
    negative, out-of-range or self-referencing endpoint loses the edge and
    is counted under missing_or_self_endpoint."""
    class Indexed:
        def chat_json(self, messages, response_schema_hint):
            return json.dumps({"nodes": [
                {"type": "Claim", "name": "Alpha", "ev": 0},
                {"type": "Claim", "name": "Beta", "ev": 0},
                {"type": "Claim", "name": "Gamma", "ev": 0},
            ], "edges": [
                {"type": "supports", "source": 0, "target": 2},
                {"type": "supports", "source": "1", "target": "2"},
                {"type": "supports", "source": True, "target": 2},
                {"type": "supports", "source": 0.0, "target": 2},
                {"type": "supports", "source": -1, "target": 2},
                {"type": "supports", "source": 0, "target": 3},
                {"type": "supports", "source": 2, "target": 2},
                {"type": "supports", "source": "x", "target": 0},
                {"type": "supports", "target": 0},
            ]})

    with caplog.at_level("WARNING", logger="app.services.kg.extract"):
        nodes, edges = extract_window(Indexed(), ELEMENTS, "1", "textbook")
    assert [node.name for node in nodes] == ["Alpha", "Beta", "Gamma"]
    names = {node.id: node.name for node in nodes}
    assert [(names[edge.source_id], names[edge.target_id]) for edge in edges] == [
        ("Alpha", "Gamma"), ("Beta", "Gamma"),
    ]
    assert "('supports', 'missing_or_self_endpoint', 7)" in caplog.text


@pytest.mark.parametrize("beta_ev", [0, 99])
def test_edges_index_the_original_reply_array_and_drop_with_a_dropped_node(beta_ev):
    """A node the gate drops (ev=99 and no name hit) leaves a hole: an edge on
    it is rejected, and later positions are NOT shifted onto its neighbour."""
    class DroppedNode:
        def chat_json(self, messages, response_schema_hint):
            return json.dumps({"nodes": [
                {"type": "Claim", "name": "Alpha", "ev": 0},
                {"type": "Claim", "name": "Beta", "ev": beta_ev},
                {"type": "Claim", "name": "Gamma", "ev": 0},
                {"type": "Claim", "name": "Delta", "ev": 0},
            ], "edges": [
                {"type": "supports", "source": 0, "target": 1},
                {"type": "supports", "source": 2, "target": 3},
            ]})

    nodes, edges = extract_window(DroppedNode(), ELEMENTS, "1", "textbook")
    assert [node.name for node in nodes] == (
        ["Alpha", "Beta", "Gamma", "Delta"] if beta_ev == 0
        else ["Alpha", "Gamma", "Delta"]
    )
    names = {node.id: node.name for node in nodes}
    pairs = [(names[edge.source_id], names[edge.target_id]) for edge in edges]
    assert pairs == (
        [("Alpha", "Beta"), ("Gamma", "Delta")] if beta_ev == 0
        else [("Gamma", "Delta")]
    )


def test_a_null_node_survives_shared_normalisation_as_a_hole():
    """JSON mode: the scheduler's shape walk runs before extract_window, and a
    null node it removed would shift every later position, re-pointing the
    edge 1 -> 2 (Alpha -> Beta) at Beta -> Gamma."""
    from app.core.model_json import validate_model_json_shape
    from app.services.kg.extract import _KG_SCHEMA_HINT

    raw = json.dumps({"nodes": [
        None,
        {"type": "Claim", "name": "Alpha", "ev": 0},
        {"type": "Claim", "name": "Beta", "ev": 0},
        {"type": "Claim", "name": "Gamma", "ev": 0},
    ], "edges": [{"type": "supports", "source": 1, "target": 2}]})
    delivered = validate_model_json_shape(raw, _KG_SCHEMA_HINT).content

    class Normalised:
        def chat_json(self, messages, response_schema_hint):
            return delivered

    nodes, edges = extract_window(Normalised(), ELEMENTS, "1", "textbook")
    names = {node.id: node.name for node in nodes}
    assert [(names[e.source_id], names[e.target_id]) for e in edges] == [("Alpha", "Beta")]


def test_refine_dropping_a_node_drops_its_edges_without_shifting_the_rest():
    class Refining:
        configured = True

        def chat_json(self, messages, response_schema_hint, **kwargs):
            if "self-refinement" in messages[0]["content"]:
                return json.dumps({"drop": [1]})
            return json.dumps({"nodes": [
                {"type": "Claim", "name": "Alpha", "ev": 0},
                {"type": "Claim", "name": "Beta", "ev": 0},
                {"type": "Claim", "name": "Gamma", "ev": 0},
            ], "edges": [
                {"type": "supports", "source": 0, "target": 1},
                {"type": "supports", "source": 0, "target": 2},
            ]})

    nodes, edges = extract_window(Refining(), ELEMENTS, "1", "textbook", refine=True)
    assert [node.name for node in nodes] == ["Alpha", "Gamma"]
    names = {node.id: node.name for node in nodes}
    assert [(names[edge.source_id], names[edge.target_id]) for edge in edges] == [
        ("Alpha", "Gamma"),
    ]


def test_extract_window_propagates_task_abort():
    class AbortedClient:
        def chat_json(self, messages, response_schema_hint):
            raise KgBuildAborted(
                KgBuildFailure("model_unavailable", "model unavailable")
            )

    with pytest.raises(KgBuildAborted):
        extract_window(
            AbortedClient(), ELEMENTS, "1", "textbook", refine=True,
            gleaning_rounds=1,
        )


def test_prompt_template_valid():
    p = _prompt("[0] foo", "1", "academic")
    assert isinstance(p, str)
    assert "[0] foo" in p
    assert "ev" in p


def test_prompt_and_schema_mention_steps():
    from app.services.kg.extract import _prompt, _KG_SCHEMA_HINT
    assert '"steps"' in _KG_SCHEMA_HINT
    p = _prompt("[0] x", "1 > Flow", "manual")
    assert "steps" in p


def test_extract_window_parses_procedure_steps():
    import json
    class FakeProc:
        def chat_json(self, messages, hint):
            return json.dumps({"nodes": [
                {"type": "Procedure", "name": "Foundation Flow", "ev": 0,
                 "steps": [{"name": "import design", "ev": 0},
                           {"name": "floorplan", "ev": 1},
                           {"name": "unbindable step", "ev": 99}]}],
                "edges": []})
    nodes, _ = extract_window(FakeProc(), ELEMENTS, "1 > Flow", "manual", win_idx=0)
    proc = [n for n in nodes if n.type == "Procedure"][0]
    assert [s.name for s in proc.steps] == ["import design", "floorplan"]
    assert proc.steps[0].evidence[0].quote == ELEMENTS[0].text
    assert proc.steps[1].evidence[0].quote == ELEMENTS[1].text


def test_prompt_forbids_symbol_and_label_concepts():
    from app.services.kg.extract import _prompt
    p = _prompt("[0] sample", "1 > intro", "textbook")
    assert "Do NOT emit Concepts" in p
    assert "symbol" in p.lower()
    assert "figure" in p.lower()


# --- Cache-admission gate runs the REAL grounding logic (Codex round-8 P2-4) ----
# The gate extract_window closes over its own `elements` and caches a fragment only
# when that fragment grounds >=1 object THE SAME WAY the window consumes it. This
# is the structural fix for the round-4..8 whack-a-mole: earlier shape checks kept
# admitting replies that parsed but grounded to 0 (so re-parse replayed the 0).

def test_cache_gate_grounds_with_real_elements_when_a_node_resolves():
    # ev=0 binds to element 0; ev=99 falls back to a name-substring hit on element 2
    # — the exact resolves extract_window performs. A real extraction -> cacheable.
    assert _fragment_grounds_something(
        json.dumps({"nodes": [
            {"type": "Concept", "name": "analog signal", "ev": 0}], "edges": []}),
        ELEMENTS,
    ) is True
    assert _fragment_grounds_something(
        json.dumps({"nodes": [
            {"type": "Concept", "name": "Engram", "ev": 99}]}),  # name-substring resolve
        ELEMENTS,
    ) is True


def test_cache_gate_rejects_present_but_all_ungroundable_nodes():
    """THE poison this closes: nodes are PRESENT (so not a legit empty window) but
    NONE grounds against the real window elements -> extract_window drops them all
    -> 0 objects. Must NOT be cached (else re-parse replays the 0 for the TTL).

    This is the case every prior per-shape tightening missed — it is only visible
    by running the actual _resolve gate, which needs the window's elements."""
    # (a) typed node with no name/ev — parses, passes every shape check, drops.
    assert _fragment_grounds_something(
        json.dumps({"nodes": [{"type": "Concept"}]}), ELEMENTS
    ) is False
    # (b) name + out-of-range ev that ALSO name-substring-misses every element.
    assert _fragment_grounds_something(
        json.dumps({"nodes": [
            {"type": "Concept", "name": "nonexistent-token-zzz", "ev": 999}]}),
        ELEMENTS,
    ) is False
    # (c) empty window is NOT this case — nodes present-but-ungroundable != nodes:[].
    assert _fragment_grounds_something(
        json.dumps({"nodes": [], "edges": []}), ELEMENTS
    ) is True


def test_cache_gate_rejects_error_envelopes_even_with_elements():
    assert _fragment_grounds_something('{"error": "quota"}', ELEMENTS) is False
    assert _fragment_grounds_something('{"nodes": "invalid"}', ELEMENTS) is False
    # A groundable node alongside a junk entry still grounds (junk is skipped, as
    # downstream skips it) -> cacheable, mirroring what the window actually keeps.
    assert _fragment_grounds_something(
        json.dumps({"nodes": [
            {"type": "Concept", "name": "analog signal", "ev": 0}, 5]}),
        ELEMENTS,
    ) is True


def test_cache_gate_rejects_edges_that_all_fail_to_resolve():
    """Edges are present but none names two distinct grounded node positions
    (out of range, self-referencing, or on a dropped node): the window wires
    nothing, so freezing it is the edge-half poison -> a miss, not a hit."""
    grounded = [
        {"type": "Claim", "name": "Alpha", "ev": 0},
        {"type": "Claim", "name": "nonexistent-token-zzz", "ev": 999},  # dropped
        {"type": "Claim", "name": "Gamma", "ev": 0},
    ]

    def gate(edges, nodes=grounded, **kwargs):
        content = json.dumps({"nodes": nodes, "edges": edges})
        return _fragment_grounds_something(content, ELEMENTS, **kwargs)

    bad = [
        {"type": "supports", "source": 0, "target": 7},     # out of range
        {"type": "supports", "source": 2, "target": 2},     # self
        {"type": "supports", "source": 0, "target": 1},     # dropped node
        {"type": "supports", "source": True, "target": 2},  # not an index
    ]
    assert gate(bad) is False
    assert gate(bad + [{"type": "supports", "source": 0, "target": 2}]) is True
    assert gate(bad + [{"type": "supports", "source": "0", "target": "2"}]) is True
    assert gate([]) is True
    # No nodes at all, yet edges: nothing to wire them to.
    assert gate(bad[:1], nodes=[]) is False
    # Gleaning reads nodes only; its (ignored) edges never decide caching.
    assert gate(bad, edges_consumed=False) is True


# ------------------------------- refine cache gate: index type + range vs nodes

# refine 决策针对**当前 node 列表**:refine_nodes 用 index 索引 nodes[index]。缓存门
# 必须闭包在这份列表上(与 _fragment_grounds_something 闭包 elements 同构)。
_REFINE_NODES = [
    Node(id="n0", type="Concept", name="alpha"),
    Node(id="n1", type="Claim", name="beta"),
]  # len 2 → 合法 index ∈ {0, 1}


def test_refine_gate_caches_a_legal_in_range_decision():
    assert _refine_response_grounds(json.dumps({"drop": [0]}), _REFINE_NODES) is True
    assert _refine_response_grounds(json.dumps({"drop": [0, 1]}), _REFINE_NODES) is True
    # 「全保留」= 空 drop 列表,不是缺 drop 键。
    assert _refine_response_grounds('{"drop": []}', _REFINE_NODES) is True


def test_refine_gate_rejects_out_of_range_index():
    """越界 index(999 ≫ len(nodes)=2)下游被 refine_nodes 静默忽略,这条回复其实什么都
    没精修——把这个 no-op 固化整个 TTL 就是毒。闭包在 nodes 上才挡得住。
    变异验证:去掉 `0 <= index < len(nodes)` 范围门后本测试转红(degraded 入口无 nodes、
    挡不住这个变异)。"""
    assert _refine_response_grounds(json.dumps({"drop": [999]}), _REFINE_NODES) is False
    assert _refine_response_grounds(json.dumps({"drop": [-1]}), _REFINE_NODES) is False
    assert _refine_response_grounds(
        json.dumps({"drop": [2]}), _REFINE_NODES  # == len,越界
    ) is False
    # 一条合法 + 一条越界:整份回复仍不可信(有坏决策)→ 不缓存。
    assert _refine_response_grounds(json.dumps({"drop": [0, 999]}), _REFINE_NODES) is False


def test_refine_gate_rejects_bool_index_even_when_in_range():
    """bool 是 int 子类:`true` 会被下游当成 index 1(恰好在界内!)消费。
    `type(index) is int` 排除 True/False,与范围无关。
    变异验证:把 `type(index) is not int` 换成 `not isinstance(index, int)` 后本测试转红。"""
    assert _refine_response_grounds(
        json.dumps({"drop": [True]}), _REFINE_NODES  # True==1,界内
    ) is False
    assert _refine_response_grounds(
        json.dumps({"drop": [False]}), _REFINE_NODES  # False==0,界内
    ) is False


def test_refine_gate_structural_rejections_hold_with_nodes():
    assert _refine_response_grounds('{"error": "quota"}', _REFINE_NODES) is False
    assert _refine_response_grounds('{"drop": "nope"}', _REFINE_NODES) is False
    assert _refine_response_grounds('{}', _REFINE_NODES) is False  # 缺 drop ≠ 全保留
    # 旧契约的 items 回复不再被读取,也不能当成「全保留」缓存。
    assert _refine_response_grounds(
        json.dumps({"items": [{"index": 0, "keep": False}]}), _REFINE_NODES
    ) is False
    assert _refine_response_grounds(
        json.dumps({"drop": [{"index": 0}]}), _REFINE_NODES  # 元素非整数
    ) is False


class _OffTypeFake:
    """A reply the shape boundary now DELIVERS (it reports ``invalid_type``
    instead of rejecting): ``type`` as a list on one node and one edge."""

    def chat_json(self, messages, response_schema_hint):
        return json.dumps({"nodes": [
            {"type": ["Concept"], "name": "analog signal", "ev": 0},
            {"type": "Claim", "name": "Engram", "ev": 2},
            {"type": {"kind": "Claim"}, "name": "C_j", "ev": 1}],
            "edges": [
            {"type": ["about"], "source": 1, "target": 0, "ev": 1},
            {"type": "about", "source": 1, "target": 1, "ev": 1}]})


def test_off_type_type_fields_are_dropped_per_item_not_per_window():
    # Membership against a set used to raise TypeError (unhashable list/dict)
    # and fail the whole window; only the off-type items are dropped now.
    nodes, edges = extract_window(_OffTypeFake(), ELEMENTS, "1 > 1.1", "textbook", win_idx=0)

    assert [n.name for n in nodes] == ["Engram"]
    assert edges == []


def test_gleaning_faults_never_discard_the_first_pass(monkeypatch):
    from app.services.kg import extract as extract_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("gleaning parser bug")

    monkeypatch.setattr(extract_mod, "_glean_nodes", boom)

    nodes, _edges = extract_window(
        Fake(), ELEMENTS, "1 > 1.1", "textbook", win_idx=0, gleaning_rounds=1,
    )

    assert {n.name for n in nodes} == {"analog signal", "Engram"}


class _ListNameFake:
    def chat_json(self, messages, response_schema_hint):
        return json.dumps({"nodes": [
            {"type": "Concept", "name": [], "ev": 0},
            {"type": "Claim", "name": {"zh": "Engram"}, "ev": 2},
            {"type": "Procedure", "name": "Engram", "ev": 2,
             "steps": [{"name": ["step"], "ev": 1}, {"name": "C_j", "ev": 1}]}],
            "edges": []})


def test_non_string_names_are_never_persisted_as_container_text():
    # A node whose ``name`` is a list/dict must be dropped before grounding
    # (codex #720 R4): ``str([])`` would otherwise become the knowledge
    # object "[]". Steps follow the same rule.
    nodes, _edges = extract_window(_ListNameFake(), ELEMENTS, "1 > 1.1", "textbook", win_idx=0)

    assert [n.name for n in nodes] == ["Engram"]
    assert [s.name for s in nodes[0].steps] == ["C_j"]


def test_evidence_index_accepts_only_integer_labels():
    # ``true`` used to read as element 1 and ``0.5`` as element 0 through a
    # bare int(); both now hand over to the name fallback (codex #720 R5).
    from app.services.kg.extract import _evidence_index, _resolve

    assert _evidence_index(2) == 2
    assert _evidence_index("2") == 2
    for bogus in (True, False, 0.5, 1.0, "1.0", "x", None, [1], {}):
        assert _evidence_index(bogus) == -1, bogus
    # name fallback still grounds a node whose label is unusable
    assert _resolve(ELEMENTS, True, "Engram") is ELEMENTS[2]
    assert _resolve(ELEMENTS, 0.5, "no such text") is None


def test_refine_ignores_a_boolean_index_instead_of_deleting_node_one():
    # codex #720 R6 P1: ``bool`` is an ``int`` subclass; a delivered
    # ``{"index": true, "keep": false}`` must not remove nodes[1].
    from app.services.kg.extract import refine_nodes

    class _BoolIndex:
        configured = True

        def chat_json(self, messages, response_schema_hint, **kwargs):
            return json.dumps({"drop": [True]})

    nodes = [
        Node(id="W0-0", type="Concept", name="analog signal", section_path="1", evidence=[]),
        Node(id="W0-1", type="Claim", name="Engram", section_path="1", evidence=[]),
    ]
    kept = refine_nodes(_BoolIndex(), ELEMENTS, list(nodes), "1")
    assert [n.name for n in kept] == ["analog signal", "Engram"]


class _ScalarEdgesFake:
    def chat_json(self, messages, response_schema_hint):
        # Bypasses the boundary (raw client), so the consumer must guard too.
        return json.dumps({"nodes": [
            {"type": "Concept", "name": "analog signal", "ev": 0}],
            "edges": True})


def test_scalar_collections_never_discard_the_extracted_nodes():
    nodes, edges = extract_window(_ScalarEdgesFake(), ELEMENTS, "1 > 1.1", "textbook", win_idx=0)

    assert [n.name for n in nodes] == ["analog signal"]
    assert edges == []


def test_prompt_states_scalar_types_for_ids_labels_and_names():
    """Prompt/parser alignment (audit A2): the parser binds edge endpoints by
    integer position in the reply's nodes array and reads ev as an integer
    label; the prompt must say so instead of leaving the shape to the model."""
    from app.services.kg.extract import _KG_SCHEMA_HINT

    prompt = _prompt("[0] text", "1 > 1.1", "textbook")
    assert 'bare INTEGER label' in prompt
    assert 'write 3, never "3", 3.0 or true' in prompt
    assert "local_id" not in prompt and "local_id" not in _KG_SCHEMA_HINT
    assert 'bare INTEGER position of the endpoint node in THIS reply\'s "nodes"' in prompt
    assert "Source and target must be two different nodes" in prompt
    assert "less than the number of nodes you return" in prompt
    edge = json.loads(_KG_SCHEMA_HINT)["edges"][0]
    assert (edge["source"], edge["target"]) == (0, 1)
    # Optional parts are spelled null, matching the strict projection.
    assert "OMIT" not in prompt
    assert '"validity_scope": null' in prompt
    assert 'every other node writes "steps": null' in prompt
    assert '"type" is exactly one\nof the four node-type words above, as a string' in prompt
    assert "carries the node's text as\na single string" in prompt
