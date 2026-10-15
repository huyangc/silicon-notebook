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
        return json.dumps({"drop": [1]})


def test_refine_nodes_drops_rejected():
    nodes = [_node("Engram"), _node("vague thing")]
    elements = [_se(0, "Engram is a memory module.", 0)]
    out = refine_nodes(_RefineLLM(), elements, nodes)
    assert [n.name for n in out] == ["Engram"]


def test_refine_empty_drop_list_keeps_every_node():
    class _KeepAll:
        configured = True

        def chat_json(self, messages, response_schema_hint):
            return json.dumps({"drop": []})

    nodes = [_node("Engram"), _node("vague thing")]
    assert refine_nodes(_KeepAll(), [_se(0, "Engram", 0)], nodes) == nodes


def test_refine_hint_projects_to_an_integer_drop_list():
    from app.core.model_schema import response_format_for_hint, validate_structured_response
    from app.services.prompts import REFINE_SCHEMA_HINT, refine_prompt

    response_format = response_format_for_hint(REFINE_SCHEMA_HINT)
    assert response_format["json_schema"]["schema"]["properties"]["drop"] == {
        "type": "array", "items": {"type": "integer"},
    }
    assert validate_structured_response('{"drop":[]}', response_format)
    assert validate_structured_response('{"drop":[0,2]}', response_format)
    assert not validate_structured_response('{"drop":["0"]}', response_format)
    prompt = refine_prompt("1", "[0] Concept: x", "[0] x")
    assert '"drop" lists the bracketed numbers of the items to drop' in prompt
    assert '"drop": [] to keep every item' in prompt
    assert "keep=" not in prompt


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
        if '"drop"' in response_schema_hint:
            return json.dumps({"drop": [1]})
        return json.dumps({"nodes": [
            {"type": "Concept", "name": "analog signal", "ev": 0},
            {"type": "Concept", "name": "filler", "ev": 1}],
            "edges": []})


def test_extract_window_applies_refine_when_enabled():
    elements = [_se(0, "Analog signal is continuous.", 0), _se(1, "filler", 40)]
    nodes, _edges = extract_window(_ExtractThenRefineLLM(), elements, "1", "textbook",
                                   win_idx=0, refine=True)
    assert [n.name for n in nodes] == ["analog signal"]   # 'filler' refined away
