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


from app.services.kg.extract import _prompt, _KG_SCHEMA_HINT


def test_schema_hint_includes_validity_scope():
    assert "validity_scope" in _KG_SCHEMA_HINT


def test_prompt_has_sa_directives():
    p = _prompt("[0] foo", "1 > 1.1", "textbook")
    assert "[0] foo" in p and "ev" in p          # 既有契约不破
    assert "validity_scope" in p
    assert "ATOMIC" in p or "atomic" in p
    for e in ("depends_on", "contrasts_with", "prerequisite_of"):
        assert e in p


def test_prompt_base_filter_toggles_meta_rule():
    on = _prompt("[0] x", "1", "textbook", base_filter=True)
    off = _prompt("[0] x", "1", "textbook", base_filter=False)
    assert "QUALITY FILTER" in on
    assert "QUALITY FILTER" not in off
