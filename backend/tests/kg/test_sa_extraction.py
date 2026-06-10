import json
from app.services.kg.models import Node


def test_node_validity_scope_defaults_empty():
    n = Node(id="n1", type="Claim", name="x")
    assert n.validity_scope == {}


def test_node_validity_scope_roundtrips():
    vs = {"region": ["saturation"], "approximation": "small-signal"}
    n = Node(id="n2", type="Formula", name="g_m = ...", validity_scope=vs)
    assert n.validity_scope == vs
    assert n.model_dump()["validity_scope"] == vs
