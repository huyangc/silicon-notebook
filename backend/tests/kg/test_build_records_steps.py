from types import SimpleNamespace
from app.services.kg_ingest import build_records
from app.services.kg.models import KnowledgeGraph, Node, Step, Evidence


def _el(eid, text):
    return SimpleNamespace(id=eid, element_type="paragraph", location_label="1", text=text)


def _kev(text):
    return Evidence(file="d.md", char_start=0, char_end=len(text), line_start=1, line_end=1, quote=text)


def test_build_records_binds_procedure_steps():
    el0 = _el("E0", "import the design netlist")
    el1 = _el("E1", "run floorplanning now")
    node = Node(id="p1", type="Procedure", name="Foundation Flow", section_path="1 > Flow",
                evidence=[_kev("import the design netlist")],
                steps=[Step(name="import", evidence=[_kev("import the design netlist")]),
                       Step(name="floorplan", evidence=[_kev("run floorplanning now")])])
    g = KnowledgeGraph(doc_id="d", doc_type="manual", nodes=[node], edges=[])
    objects, _ = build_records(g, "src-1", "Doc", [el0, el1])
    proc = [o for o in objects if o["object_type"] == "procedure"][0]
    steps = proc["payload"]["steps"]
    assert [s["name"] for s in steps] == ["import", "floorplan"]
    assert steps[0]["element_id"] == "E0" and steps[1]["element_id"] == "E1"
    assert steps[0]["quote"]


def test_build_records_procedure_without_steps_unchanged():
    el0 = _el("E0", "a single action happens here")
    node = Node(id="p1", type="Procedure", name="lone action", section_path="1 > X",
                evidence=[_kev("a single action happens here")])
    g = KnowledgeGraph(doc_id="d", doc_type="manual", nodes=[node], edges=[])
    objects, _ = build_records(g, "src-1", "Doc", [el0])
    proc = [o for o in objects if o["object_type"] == "procedure"][0]
    assert "steps" not in proc["payload"]
