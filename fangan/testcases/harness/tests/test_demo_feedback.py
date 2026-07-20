import copy
from pathlib import Path

import yaml

from harness import report, scorer


def test_imperfect_candidate_produces_actionable_markdown(
    testcases_root: Path,
) -> None:
    gold_path = testcases_root / "engram" / "ch00_abstract" / "gold.yaml"
    gold = yaml.safe_load(gold_path.read_text(encoding="utf-8"))
    pred = copy.deepcopy(gold)
    pred["objects"] = pred["objects"][:-1]
    pred["evidence_atoms"] = pred["evidence_atoms"][:-1]
    if pred.get("relations"):
        pred["relations"][0]["relation_type"] = "WRONG_REL_TYPE"
    result = scorer.score_fixture(gold, pred)
    md = report.to_markdown(result, title="degraded-demo")

    assert result["weighted_score"] < 100.0
    assert ("false negatives" in md) or ("Type mismatches" in md)
    assert report.to_json(result).startswith("{")
