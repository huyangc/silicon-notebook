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
from app.services.kg.models import Node, Evidence, KnowledgeGraph


class _PElem:
    """Minimal product-element stand-in for build_records — the SourceElement
    interface (id/text/element_type/location_label), NOT SourceElementQ."""
    def __init__(self, eid, text):
        self.id = eid
        self.text = text
        self.element_type = "paragraph"
        self.location_label = "1"


def _ev_quote(text):
    return Evidence(file="d.md", char_start=0, char_end=len(text),
                    line_start=1, line_end=1, quote=text)


def test_build_records_threads_validity_scope_into_payload():
    e1 = _PElem("EL-0", "In saturation, I_D depends on V_GS.")
    e2 = _PElem("EL-1", "Threshold voltage definition.")
    claim = Node(id="c1", type="Claim", name="I_D depends on V_GS",
                 section_path="1", evidence=[_ev_quote(e1.text)],
                 validity_scope={"region": ["saturation"]})
    plain = Node(id="c2", type="Claim", name="Threshold voltage definition.",
                 section_path="1", evidence=[_ev_quote(e2.text)])
    g = KnowledgeGraph(doc_id="d.md", doc_type="textbook",
                       nodes=[claim, plain], edges=[])
    objects, _ = build_records(g, "src1", "Doc", [e1, e2])
    by = {o["payload"]["name"]: o["payload"] for o in objects}
    assert by["I_D depends on V_GS"]["validity_scope"] == {"region": ["saturation"]}
    assert "validity_scope" not in by["Threshold voltage definition."]


def test_extract_graph_forwards_base_filter(monkeypatch):
    import app.services.kg_ingest as ingest
    from app.services.kg.parsing import SourceElementQ
    captured = {}

    def fake_extract_window(client, els, section_path, doc_type, idx,
                            refine=False, gleaning_rounds=0, base_filter=False):
        captured["base_filter"] = base_filter
        return [], []

    class _Now:
        def __init__(self, v): self._v = v
        def result(self): return self._v

    el = SourceElementQ(id="e0", type="paragraph", file="d.md", line_start=1,
                        line_end=1, char_start=0, char_end=5, text="hello world")

    class _W:
        section_path = "1"

    monkeypatch.setattr(ingest, "windows_with_elements",
                        lambda *a, **k: [(_W(), [el])])
    monkeypatch.setattr(ingest, "should_extract_window", lambda *a, **k: (True, ""))
    monkeypatch.setattr(ingest, "extract_window", fake_extract_window)
    monkeypatch.setattr(ingest, "submit_window",
                        lambda fn, *a, **k: _Now(fn(*a, **k)))
    ingest.extract_graph(object(), "text", "d.md", "textbook", base_filter=True)
    assert captured.get("base_filter") is True


from app.services.kg.extract import _parse_validity_scope


def test_parse_validity_scope_drops_non_string_items():
    assert _parse_validity_scope({"region": [None, "saturation", 3]}) == {"region": ["saturation"]}
    assert _parse_validity_scope("not a dict") == {}
    assert _parse_validity_scope({"assumptions": []}) == {}


def test_gleaning_threads_base_filter_and_parses_scope():
    class _GleanFake:
        configured = True
        def __init__(self):
            self.n = 0
        def chat_json(self, messages, hint):
            self.n += 1
            if self.n == 1:
                return json.dumps({"nodes": [
                    {"local_id": "m", "type": "Claim", "name": "main claim", "ev": 0}],
                    "edges": []})
            # gleaning round: base_filter must have reached this prompt
            assert "QUALITY FILTER" in messages[0]["content"]
            return json.dumps({"nodes": [
                {"local_id": "g", "type": "Claim",
                 "name": "In saturation gleaned claim", "ev": 0,
                 "validity_scope": {"region": ["saturation"]}}],
                "edges": []})

    nodes, _ = extract_window(_GleanFake(), _ELS, "1", "textbook", win_idx=0,
                              gleaning_rounds=1, base_filter=True)
    gleaned = [n for n in nodes if n.name == "In saturation gleaned claim"]
    assert gleaned and gleaned[0].validity_scope == {"region": ["saturation"]}
