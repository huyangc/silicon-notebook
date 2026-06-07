import json
from app.services.kg.extract import refine_nodes, extract_window
from app.services.kg.models import Node, Evidence
from app.services.kg.parsing import SourceElementQ


def _se(idx, text, cs):
    return SourceElementQ(id=f"SE-{idx}", type="paragraph", file="d.md",
                          line_start=idx + 1, line_end=idx + 1,
                          char_start=cs, char_end=cs + len(text), text=text)


def _node(name):
    return Node(id=f"n-{name}", type="Concept", name=name,
                evidence=[Evidence(file="d", char_start=0, char_end=1,
                                   line_start=1, line_end=1, quote="z")])


class _RefineLLM:
    """Returns a refine verdict: drop index 1, keep the rest."""
    configured = True

    def chat_json(self, messages, response_schema_hint):
        return json.dumps({"items": [
            {"index": 0, "keep": True}, {"index": 1, "keep": False},
        ]})


def test_refine_nodes_drops_rejected():
    nodes = [_node("Engram"), _node("vague thing")]
    elements = [_se(0, "Engram is a memory module.", 0)]
    out = refine_nodes(_RefineLLM(), elements, nodes)
    assert [n.name for n in out] == ["Engram"]


def test_refine_nodes_noop_when_client_unconfigured():
    class _Off:
        configured = False
        def chat_json(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("should not call LLM when unconfigured")
    nodes = [_node("A"), _node("B")]
    assert refine_nodes(_Off(), [], nodes) == nodes


class _ExtractThenRefineLLM:
    """First call (extraction schema) returns nodes; refine call drops 'filler'."""
    configured = True

    def chat_json(self, messages, response_schema_hint):
        if '"items"' in response_schema_hint:
            return json.dumps({"items": [{"index": 0, "keep": True},
                                         {"index": 1, "keep": False}]})
        return json.dumps({"nodes": [
            {"local_id": "a", "type": "Concept", "name": "analog signal", "ev": 0},
            {"local_id": "b", "type": "Concept", "name": "filler", "ev": 1}],
            "edges": []})


def test_extract_window_applies_refine_when_enabled():
    elements = [_se(0, "Analog signal is continuous.", 0), _se(1, "filler", 40)]
    nodes, _edges = extract_window(_ExtractThenRefineLLM(), elements, "1", "textbook",
                                   win_idx=0, refine=True)
    assert [n.name for n in nodes] == ["analog signal"]   # 'filler' refined away
