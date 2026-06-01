from app.services.kg.models import KnowledgeGraph, Node, Edge, Evidence
from app.services.kg_eval.score import score_kg

def _c(nid, name, cs):
    return Node(id=nid, type="Concept", name=name,
                evidence=[Evidence(file="x", char_start=cs, char_end=cs+3, line_start=1, line_end=1, quote="abc")])

def _g():
    return KnowledgeGraph(doc_id="d", doc_type="textbook",
        nodes=[_c("C1", "analog signal", 0), _c("C2", "digital signal", 10)],
        edges=[Edge(id="e1", type="contrasts_with", source_id="C1", target_id="C2")])

def test_gold_vs_gold_is_perfect():
    r = score_kg(_g(), _g())
    assert r["nodes"]["f1"] == 1.0
    assert r["edges"]["f1"] == 1.0

def test_partial_node_and_missing_edge():
    pred = KnowledgeGraph(doc_id="d", doc_type="textbook",
        nodes=[_c("P1", "analog signal", 0)], edges=[])   # 1/2 nodes, 0 edges
    r = score_kg(_g(), pred)
    assert r["nodes"]["recall"] == 0.5
    assert r["edges"]["recall"] == 0.0
