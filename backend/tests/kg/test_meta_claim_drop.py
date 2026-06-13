from app.services.kg.models import Node, Edge
from app.services.kg_ingest import drop_meta_claims


def _n(nid, typ, name):
    return Node(id=nid, type=typ, name=name)   # evidence 等字段均有默认值


def test_drop_meta_claims_removes_meta_and_dangling_edges():
    nodes = [
        _n("c1", "Claim", "This book deals with the analysis of RF circuits"),
        _n("c2", "Claim", "Thermal noise increases with temperature"),
        _n("k1", "Concept", "thermal noise"),
    ]
    edges = [
        Edge(id="e1", type="about", source_id="c1", target_id="k1"),
        Edge(id="e2", type="about", source_id="c2", target_id="k1"),
    ]
    kept_nodes, kept_edges, dropped = drop_meta_claims(nodes, edges)
    assert dropped == 1
    assert {n.id for n in kept_nodes} == {"c2", "k1"}
    assert len(kept_edges) == 1 and kept_edges[0].source_id == "c2"


def test_drop_meta_claims_only_touches_claims():
    nodes = [_n("k1", "Concept", "this chapter")]  # Concept 不受 claim 过滤影响
    kept_nodes, _, dropped = drop_meta_claims(nodes, [])
    assert dropped == 0 and len(kept_nodes) == 1
