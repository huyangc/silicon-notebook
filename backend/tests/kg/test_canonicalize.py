from app.services.kg.models import Node, Edge, Evidence
from app.services.kg.canonicalize import canonicalize

def _c(nid, name):
    return Node(id=nid, type="Concept", name=name,
                evidence=[Evidence(file="x", char_start=0, char_end=1, line_start=1, line_end=1, quote="z")])

def test_canonicalize_merges_procedure_steps_across_windows():
    from app.services.kg.canonicalize import canonicalize
    from app.services.kg.models import Node, Step, Evidence
    def ev(cs):
        return Evidence(file="d", char_start=cs, char_end=cs + 5, line_start=1, line_end=1, quote="q")
    n1 = Node(id="W0-0", type="Procedure", name="Foundation Flow", section_path="1 > Flow",
              steps=[Step(name="b", evidence=[ev(100)])])
    n2 = Node(id="W1-0", type="Procedure", name="foundation  flow", section_path="1 > Flow",
              steps=[Step(name="a", evidence=[ev(10)])])
    other = Node(id="W2-0", type="Procedure", name="Foundation Flow", section_path="2 > Other",
                 steps=[Step(name="z", evidence=[ev(5)])])
    out, _ = canonicalize([n1, n2, other], [], "doc")
    procs = [n for n in out if n.type == "Procedure"]
    merged = [n for n in procs if n.section_path == "1 > Flow"]
    assert len(merged) == 1
    assert [s.name for s in merged[0].steps] == ["a", "b"]
    assert any(n.section_path == "2 > Other" for n in procs)


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


def test_cjk_concepts_merge_within_doc():
    out, _ = canonicalize([_c("W0-0", "铸币平价"), _c("W1-0", "铸币平价")], [], doc_id="d")
    concepts = [n for n in out if n.type == "Concept"]
    assert len(concepts) == 1
    assert len(concepts[0].mentions) == 2


def test_cjk_distinct_concepts_not_merged():
    out, _ = canonicalize([_c("W0-0", "双重汇率制"), _c("W1-0", "组织策略")], [], doc_id="d")
    assert len([n for n in out if n.type == "Concept"]) == 2


def test_symbol_only_concepts_never_merge():
    # 空归一名走 else 分支原样保留(既有守卫),修复后必须维持
    out, _ = canonicalize([_c("W0-0", "→"), _c("W1-0", "→")], [], doc_id="d")
    assert len([n for n in out if n.type == "Concept"]) == 2
