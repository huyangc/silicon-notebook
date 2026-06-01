from app.services.kg.models import Node, Edge, Evidence
from app.services.kg.canonicalize import canonicalize

def _c(nid, name):
    return Node(id=nid, type="Concept", name=name,
                evidence=[Evidence(file="x", char_start=0, char_end=1, line_start=1, line_end=1, quote="z")])

def test_merges_concepts_by_normalized_name_and_rewires_edges():
    nodes = [_c("A", "Depletion Region"), _c("B", "depletion  region"),
             Node(id="F", type="Formula", name="x=1",
                  evidence=[Evidence(file="x", char_start=2, char_end=3, line_start=1, line_end=1, quote="x")])]
    edges = [Edge(id="e1", type="about", source_id="F", target_id="B")]
    g_nodes, g_edges = canonicalize(nodes, edges, doc_id="d")
    concepts = [n for n in g_nodes if n.type == "Concept"]
    assert len(concepts) == 1                      # A & B merged
    assert len(concepts[0].mentions) == 2          # both spans recorded
    assert g_edges[0].target_id == concepts[0].id  # edge rewired to canonical id
