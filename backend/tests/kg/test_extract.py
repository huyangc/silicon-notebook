import json
from app.services.kg.extract import extract_window

SRC = "An analog signal is defined over a continuous range. C_j = C_j0 here."

class Fake:
    def chat_json(self, messages, response_schema_hint):
        return json.dumps({"nodes": [
            {"local_id": "a", "type": "Concept", "name": "analog signal",
             "evidence": "analog signal"},
            {"local_id": "b", "type": "Formula", "name": "C_j = C_j0",
             "evidence": "C_j = C_j0"},
            {"local_id": "c", "type": "Claim", "name": "x",
             "evidence": "NOT IN SOURCE"}],          # ungroundable -> dropped
            "edges": [
            {"type": "about", "source": "b", "target": "a", "evidence": "C_j = C_j0"},
            {"type": "about", "source": "b", "target": "zzz", "evidence": ""}]})  # bad endpoint

def test_extract_grounds_evidence_and_drops_ungroundable():
    nodes, edges = extract_window(Fake(), SRC, 0, len(SRC), "1 > 1.1", "textbook")
    assert len(nodes) == 2                       # claim 'c' dropped (ungroundable)
    for n in nodes:
        e = n.evidence[0]
        assert SRC[e.char_start:e.char_end] == e.quote   # hard invariant
    assert len(edges) == 1                        # bad-endpoint edge dropped
    assert edges[0].type == "about"

from app.services.kg.extract import _prompt
def test_prompt_requests_sentence_evidence_and_precedes():
    p = _prompt("some passage", "1.1", "academic")
    low = p.lower()
    assert "sentence" in low                      # evidence should be the full sentence
    assert "precedes" in low and "step" in low    # connect ordered procedure steps
