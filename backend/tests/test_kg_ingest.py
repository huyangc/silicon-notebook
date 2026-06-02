from app.models.schemas import SourceElement
from app.services import kg_ingest
from app.services.kg.models import Node, Edge, Evidence, KnowledgeGraph


def _el(i, text):
    return SourceElement(id=i, source_id="s1", element_type="paragraph",
                         location_label=f"p{i}", text=text)


def test_build_records_binds_and_drops():
    g = KnowledgeGraph(doc_id="doc.md", doc_type="academic",
        nodes=[
            Node(id="C1", type="Concept", name="Engram",
                 evidence=[Evidence(file="doc.md", char_start=0, char_end=6,
                                    line_start=1, line_end=1, quote="Engram")]),
            Node(id="C2", type="Concept", name="Nowhere",
                 evidence=[Evidence(file="doc.md", char_start=0, char_end=3,
                                    line_start=1, line_end=1, quote="zzz")]),
        ],
        edges=[Edge(id="E1", type="about", source_id="C1", target_id="C2")])
    elements = [_el("e1", "Engram is a memory architecture.")]
    objects, relations = kg_ingest.build_records(
        g, source_id="s1", source_title="Doc", elements=elements)
    assert [o["object_type"] for o in objects] == ["concept"]   # C2 dropped (unbound)
    assert objects[0]["payload"]["name"] == "Engram"
    assert objects[0]["evidence"][0]["element_id"] == "e1"
    assert objects[0]["local_id"] == "C1"                        # carried for edge wiring
    assert relations == []                                       # edge dropped: C2 gone


def test_bind_quote_fuzzy_fallback():
    """Quote that is NOT an exact normalized substring but shares >=60% tokens."""
    # Element text: "Engram is a memory architecture"
    # Quote: "Engram memory architecture model"
    # Exact substring check: "engram memory architecture model" NOT in "engram is a memory architecture" -> fails
    # Token overlap: qt = {engram, memory, architecture, model} (4 tokens)
    #                et = {engram, is, a, memory, architecture}
    #                intersection = {engram, memory, architecture} -> 3/4 = 0.75 >= 0.6 -> binds
    elements = [_el("e1", "Engram is a memory architecture")]
    result = kg_ingest._bind_quote(
        "Engram memory architecture model", elements, "s1", "MyDoc"
    )
    assert result is not None, "fuzzy bind should succeed with 0.75 token overlap"
    assert result["element_id"] == "e1"
    assert result["source_id"] == "s1"
    assert result["source_title"] == "MyDoc"


def test_bind_quote_fuzzy_not_exact():
    """Confirm the fuzzy test quote genuinely fails exact-substring matching."""
    elements = [_el("e1", "Engram is a memory architecture")]
    q = kg_ingest._norm("Engram memory architecture model")
    text_norm = kg_ingest._norm("Engram is a memory architecture")
    assert q not in text_norm, "precondition: quote must NOT be an exact substring"


class FakeClient:
    configured = True
    def __init__(self, payload): self._p = payload
    def chat_json(self, messages: list, response_schema_hint: str) -> str: return self._p

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


def test_canonicalize_merges_across_windows():
    """Two windows both emit the same Concept 'Engram'; canonicalize must collapse
    to a single node.  The text is long enough (>200 chars in one prose section)
    to produce >=2 windows with n=200, m=20.

    We embed the evidence quote twice in the text so each window can ground its
    Engram node independently; then extract_graph's canonicalize step must merge
    them to exactly one Concept named 'Engram'.
    """
    import json

    # Evidence quote placed near position 0 (window 1: 0-200) and repeated
    # near position ~190 (window 2: 180-380) so both windows contain it.
    # We pad the text between the two occurrences to keep total length > 200.
    quote = "Engram is a memory architecture"
    # 34-char quote at start; padding to push past 200 chars; quote repeated
    padding = " " * (200 - len(quote))   # 166 spaces
    long_text = quote + padding + quote   # total ~234 chars in one paragraph

    payload = json.dumps({
        "nodes": [
            {"local_id": "a", "type": "Concept", "name": "Engram",
             "evidence": quote},
        ],
        "edges": [],
    })
    g = kg_ingest.extract_graph(
        FakeClient(payload), long_text, "doc.md", "academic", n=200, m=20
    )
    engram_nodes = [n for n in g.nodes if n.name == "Engram"]
    # Both windows ground the same Concept; canonicalize must merge to 1.
    assert len(engram_nodes) == 1, (
        f"expected 1 Engram node after canonicalization, got {len(engram_nodes)}"
    )
