import json
from app.services.kg.extract import extract_window, _prompt
from app.services.kg.parsing import SourceElementQ


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
            {"local_id": "b", "type": "Concept", "name": "Engram", "ev": 99},
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
