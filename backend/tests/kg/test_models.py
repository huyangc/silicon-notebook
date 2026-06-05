from app.services.kg.models import Evidence, Node, Edge, KnowledgeGraph
from app.services.kg.emit import to_yaml
import yaml

def test_node_carries_ordered_steps():
    from app.services.kg.models import Node, Step, Evidence
    ev = Evidence(file="d.md", char_start=0, char_end=5, line_start=1, line_end=1, quote="hello")
    n = Node(id="p1", type="Procedure", name="Foundation Flow",
             steps=[Step(name="import", evidence=[ev]), Step(name="floorplan", evidence=[ev])])
    assert [s.name for s in n.steps] == ["import", "floorplan"]
    assert n.steps[0].evidence[0].quote == "hello"

def test_kg_roundtrips_and_emits_ordered():
    n1 = Node(id="C1", type="Concept", name="depletion region",
              evidence=[Evidence(file="x.md", char_start=0, char_end=16,
                                 line_start=1, line_end=1, quote="depletion region")])
    n2 = Node(id="F1", type="Formula", name="x_d = x_n - x_p",
              evidence=[Evidence(file="x.md", char_start=20, char_end=35,
                                 line_start=2, line_end=2, quote="x_d = x_n - x_p")])
    g = KnowledgeGraph(doc_id="cmos", doc_type="textbook", nodes=[n1, n2],
                       edges=[Edge(id="E1", type="about", source_id="F1", target_id="C1")])
    d = yaml.safe_load(to_yaml(g))
    assert list(d.keys()) == ["doc_id", "doc_type", "nodes", "edges"]
    assert d["nodes"][0]["type"] == "Concept"
    assert d["nodes"][0]["evidence"][0]["quote"] == "depletion region"
    assert d["edges"][0]["source_id"] == "F1"
