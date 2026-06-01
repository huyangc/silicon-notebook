from app.services import kg_ingest

class FakeClient:
    configured = True
    def __init__(self, payload): self._p = payload
    def chat_json(self, prompt: str, retries: int = 4) -> str: return self._p

ABS = "We propose Engram, a memory architecture. Engram improves perplexity."

def test_extract_graph_grounds_nodes():
    import json
    payload = json.dumps({
        "nodes": [
            {"local_id": "a", "type": "Concept", "name": "Engram",
             "evidence": "Engram, a memory architecture"},
            {"local_id": "b", "type": "Claim", "name": "Engram improves perplexity",
             "evidence": "Engram improves perplexity"},
            {"local_id": "z", "type": "Concept", "name": "Ghost",
             "evidence": "text that does not appear"},
        ],
        "edges": [{"type": "about", "source": "b", "target": "a",
                   "evidence": "Engram improves perplexity"}],
    })
    g = kg_ingest.extract_graph(FakeClient(payload), ABS, "doc.md", "academic")
    names = {n.name for n in g.nodes}
    assert "Engram" in names and "Engram improves perplexity" in names
    assert "Ghost" not in names           # ungroundable node dropped (evidence not in text)
    assert len(g.edges) == 1              # edge endpoints survived
