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
            {"local_id": "a", "type": "Concept", "name": "analog signal", "ev": 0},
            # out-of-range ev, but name is substring of element 2 -> fallback
            {"local_id": "b", "type": "Claim", "name": "Engram", "ev": 99},
            # out-of-range ev and non-matching name -> dropped
            {"local_id": "c", "type": "Claim", "name": "no such thing here", "ev": 99}],
            "edges": [
            # valid ev -> evidence attached
            {"type": "about", "source": "b", "target": "a", "ev": 1},
            # missing ev -> edge kept, empty evidence
            {"type": "supports", "source": "a", "target": "b"}]})


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
                {"local_id": "p", "type": "Procedure", "name": "Foundation Flow", "ev": 0,
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


# ------------------------------- refine cache gate: index type + range vs nodes

# refine 决策针对**当前 node 列表**:refine_nodes 用 index 索引 nodes[index]。缓存门
# 必须闭包在这份列表上(与 _fragment_grounds_something 闭包 elements 同构)。
_REFINE_NODES = [
    Node(id="n0", type="Concept", name="alpha"),
    Node(id="n1", type="Claim", name="beta"),
]  # len 2 → 合法 index ∈ {0, 1}


def test_refine_gate_caches_a_legal_in_range_decision():
    assert _refine_response_grounds(
        json.dumps({"items": [{"index": 0, "keep": False}]}), _REFINE_NODES
    ) is True
    assert _refine_response_grounds(
        json.dumps({"items": [{"index": 1, "keep": True}]}), _REFINE_NODES
    ) is True
    # 「全保留」= 空决策列表,不是缺 items 键。
    assert _refine_response_grounds('{"items": []}', _REFINE_NODES) is True


def test_refine_gate_rejects_out_of_range_index():
    """越界 index(999 ≫ len(nodes)=2)下游被 refine_nodes 静默忽略,这条回复其实什么都
    没精修——把这个 no-op 固化整个 TTL 就是毒。闭包在 nodes 上才挡得住。
    变异验证:去掉 `0 <= index < len(nodes)` 范围门后本测试转红(degraded 入口无 nodes、
    挡不住这个变异)。"""
    assert _refine_response_grounds(
        json.dumps({"items": [{"index": 999, "keep": False}]}), _REFINE_NODES
    ) is False
    assert _refine_response_grounds(
        json.dumps({"items": [{"index": -1, "keep": False}]}), _REFINE_NODES
    ) is False
    assert _refine_response_grounds(
        json.dumps({"items": [{"index": 2, "keep": False}]}), _REFINE_NODES  # == len,越界
    ) is False
    # 一条合法 + 一条越界:整份回复仍不可信(有坏决策)→ 不缓存。
    assert _refine_response_grounds(
        json.dumps({"items": [{"index": 0, "keep": True}, {"index": 999, "keep": False}]}),
        _REFINE_NODES,
    ) is False


def test_refine_gate_rejects_bool_index_even_when_in_range():
    """bool 是 int 子类:`index: true` 会被下游当成 index 1(恰好在界内!)消费。
    `type(index) is int` 排除 True/False,与范围无关。
    变异验证:把 `type(index) is not int` 换成 `not isinstance(index, int)` 后本测试转红。"""
    assert _refine_response_grounds(
        json.dumps({"items": [{"index": True, "keep": False}]}), _REFINE_NODES  # True==1,界内
    ) is False
    assert _refine_response_grounds(
        json.dumps({"items": [{"index": False, "keep": False}]}), _REFINE_NODES  # False==0,界内
    ) is False


def test_refine_gate_structural_rejections_hold_with_nodes():
    assert _refine_response_grounds('{"error": "quota"}', _REFINE_NODES) is False
    assert _refine_response_grounds('{"items": "nope"}', _REFINE_NODES) is False
    assert _refine_response_grounds('{}', _REFINE_NODES) is False  # 缺 items ≠ 全保留
    assert _refine_response_grounds(
        json.dumps({"items": [{"index": 0, "keep": 1}]}), _REFINE_NODES  # keep 非 bool
    ) is False
