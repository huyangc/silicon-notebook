import pathlib, yaml, pytest
from app.services.kg.models import KnowledgeGraph
from app.services.kg_eval.score import score_kg

DRAFT = pathlib.Path(__file__).resolve().parents[3] / "fangan" / "testcases_kg" / "engram" / "ch00_abstract" / "gold_kg.yaml"

def test_draft_vs_itself_is_perfect():
    if not DRAFT.exists():
        pytest.skip("draft gold not generated yet")
    g = KnowledgeGraph(**yaml.safe_load(DRAFT.read_text()))
    r = score_kg(g, g)
    assert r["nodes"]["f1"] == 1.0 and r["edges"]["f1"] == 1.0
