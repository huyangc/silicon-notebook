from app.services.kg.models import Node, Edge
from app.eval.sa_calibration import (
    compound_claim_rate, sparse_edge_count, validity_scope_fill_rate,
)


def _claim(name, vs=None):
    return Node(id=name, type="Claim", name=name, validity_scope=vs or {})


def test_compound_claim_rate():
    claims = [_claim("A and B holds"), _claim("single fact"),
              _claim("x; y"), _claim("plain")]
    assert compound_claim_rate(claims) == 0.5


def test_sparse_edge_count():
    edges = [Edge(id="1", type="depends_on", source_id="a", target_id="b"),
             Edge(id="2", type="contrasts_with", source_id="a", target_id="b"),
             Edge(id="3", type="about", source_id="a", target_id="b")]
    assert sparse_edge_count(edges) == 2


def test_validity_scope_fill_rate():
    nodes = [_claim("a", {"region": ["sat"]}), _claim("b"),
             Node(id="f", type="Formula", name="f", validity_scope={"range": "DC"}),
             Node(id="c", type="Concept", name="c")]
    assert round(validity_scope_fill_rate(nodes), 3) == round(2 / 3, 3)
