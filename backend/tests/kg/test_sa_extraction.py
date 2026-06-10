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


from app.services.kg.extract import extract_window
from app.services.kg.parsing import SourceElementQ


def _el(idx, text, cs):
    return SourceElementQ(id=f"SE-{idx}", type="paragraph", file="d.md",
                          line_start=idx + 1, line_end=idx + 1,
                          char_start=cs, char_end=cs + len(text), text=text)


_ELS = [_el(0, "In saturation, I_D depends on V_GS.", 0),
        _el(1, "Threshold voltage definition.", 100)]


class _VSFake:
    def chat_json(self, messages, hint):
        return json.dumps({"nodes": [
            {"local_id": "c1", "type": "Claim",
             "name": "I_D depends on V_GS", "ev": 0,
             "validity_scope": {"region": ["saturation"], "assumptions": [],
                                "approximation": "", "range": ""}},
            {"local_id": "k1", "type": "Concept", "name": "threshold voltage",
             "ev": 1, "validity_scope": {"region": ["bogus"]}}],
            "edges": []})


def test_extract_window_parses_validity_scope_claim_only():
    nodes, _ = extract_window(_VSFake(), _ELS, "1", "textbook", win_idx=0)
    by = {n.name: n for n in nodes}
    # claim keeps normalized scope (empty subfields dropped)
    assert by["I_D depends on V_GS"].validity_scope == {"region": ["saturation"]}
    # concept never carries validity_scope (schema: claim/formula only)
    assert by["threshold voltage"].validity_scope == {}


def test_extract_window_backward_compat_no_scope():
    # node JSON without validity_scope still parses -> {}
    class _Old:
        def chat_json(self, m, h):
            return json.dumps({"nodes": [
                {"local_id": "c", "type": "Claim", "name": "I_D depends on V_GS",
                 "ev": 0}], "edges": []})
    nodes, _ = extract_window(_Old(), _ELS, "1", "textbook", win_idx=0)
    assert nodes[0].validity_scope == {}


def test_extract_window_accepts_base_filter():
    nodes, _ = extract_window(_VSFake(), _ELS, "1", "textbook", win_idx=0,
                              base_filter=True)
    assert any(n.type == "Claim" for n in nodes)


from app.services.kg_ingest import build_records
from app.services.kg.models import Node, Edge, Evidence, KnowledgeGraph


def _ev_for(el):
    return Evidence(file=el.file, char_start=el.char_start, char_end=el.char_end,
                    line_start=el.line_start, line_end=el.line_end, quote=el.text)


def test_build_records_threads_validity_scope_into_payload():
    el = _ELS[0]
    claim = Node(id="c1", type="Claim", name="I_D depends on V_GS",
                 section_path="1", evidence=[_ev_for(el)],
                 validity_scope={"region": ["saturation"]})
    plain = Node(id="c2", type="Claim", name="Threshold voltage definition.",
                 section_path="1", evidence=[_ev_for(_ELS[1])])
    g = KnowledgeGraph(doc_id="d.md", doc_type="textbook",
                       nodes=[claim, plain], edges=[])
    objects, _ = build_records(g, "src1", "Doc", _ELS)
    by = {o["payload"]["name"]: o["payload"] for o in objects}
    assert by["I_D depends on V_GS"]["validity_scope"] == {"region": ["saturation"]}
    assert "validity_scope" not in by["Threshold voltage definition."]
