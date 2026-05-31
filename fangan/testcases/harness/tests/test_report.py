import json

from harness import report

RESULT = {
    "weighted_score": 87.5,
    "schema_version": "0.3.3",
    "profile": "article_research",
    "stage_scores": {"evidence_atoms": 0.9, "objects": 0.8},
    "stages": {
        "evidence_atoms": {"score": 0.9, "prf": {"precision": 0.9, "recall": 0.9, "f1": 0.9, "tp": 9, "fp": 1, "fn": 1},
                           "type_accuracy": 1.0, "mean_iou": 0.95,
                           "missed": ["A-X"], "spurious": ["P-Y"], "type_mismatches": []},
        "objects": {"score": 0.8, "prf": {"precision": 0.8, "recall": 0.8, "f1": 0.8, "tp": 8, "fp": 2, "fn": 2},
                    "type_accuracy": 0.9, "payload": {"f1": 0.7, "precision": 0.7, "recall": 0.7, "gaps": []},
                    "evidence": {"mean_jaccard": 0.85},
                    "missed": ["O-Z"], "spurious": [], "type_mismatches": [
                        {"gold_id": "O1", "pred_id": "Z1", "gold_type": "ArticleClaim", "pred_type": "ArticleMethod"}]},
    },
}


def test_to_json_roundtrips():
    s = report.to_json(RESULT)
    assert json.loads(s)["weighted_score"] == 87.5


def test_to_markdown_has_headline_and_sections():
    md = report.to_markdown(RESULT, title="ch00_abstract")
    assert "87.5" in md
    assert "ch00_abstract" in md
    assert "Missed" in md and "A-X" in md          # FN listed
    assert "Type mismatch" in md and "ArticleMethod" in md
