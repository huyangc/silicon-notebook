import json
from app.services.kg.extract import extract_window
from app.services.kg.parsing import SourceElementQ


def _se(idx, text, cs):
    return SourceElementQ(id=f"SE-{idx}", type="paragraph", file="d.md",
                          line_start=idx + 1, line_end=idx + 1,
                          char_start=cs, char_end=cs + len(text), text=text)


class _GleanLLM:
    """First call (len(messages)==1) returns first-pass nodes; gleaning calls
    (len>=3) return `extra` nodes."""
    configured = True

    def __init__(self, extra):
        self.extra = extra
        self.calls = 0

    def chat_json(self, messages, schema_hint):
        self.calls += 1
        if len(messages) == 1:
            return json.dumps({"nodes": [
                {"local_id": "a", "type": "Concept", "name": "analog signal", "ev": 0}],
                "edges": []})
        return json.dumps({"nodes": self.extra, "edges": []})


def test_gleaning_adds_missed_node():
    els = [_se(0, "Analog signal is continuous.", 0),
           _se(1, "Engram is a memory module.", 40)]
    llm = _GleanLLM([{"local_id": "b", "type": "Concept", "name": "Engram", "ev": 1}])
    nodes, _e = extract_window(llm, els, "1", "textbook", win_idx=0, gleaning_rounds=1)
    assert {n.name for n in nodes} == {"analog signal", "Engram"}


def test_gleaning_dedups_existing():
    els = [_se(0, "Analog signal is continuous.", 0)]
    llm = _GleanLLM([{"local_id": "a2", "type": "Concept", "name": "analog  signal", "ev": 0}])
    nodes, _e = extract_window(llm, els, "1", "textbook", win_idx=0, gleaning_rounds=1)
    assert [n.name for n in nodes] == ["analog signal"]   # not duplicated (norm-equal)


def test_gleaning_early_stop_on_empty():
    els = [_se(0, "x", 0)]
    llm = _GleanLLM([])   # gleaning returns no nodes
    nodes, _e = extract_window(llm, els, "1", "textbook", win_idx=0, gleaning_rounds=3)
    assert len(nodes) == 1
    assert llm.calls == 2   # first pass + ONE gleaning (empty -> stop), not 1+3


def test_gleaning_off_by_default():
    els = [_se(0, "x", 0)]
    llm = _GleanLLM([{"local_id": "b", "type": "Concept", "name": "Engram", "ev": 0}])
    nodes, _e = extract_window(llm, els, "1", "textbook", win_idx=0)   # gleaning_rounds defaults 0
    assert len(nodes) == 1
    assert llm.calls == 1   # no gleaning call
