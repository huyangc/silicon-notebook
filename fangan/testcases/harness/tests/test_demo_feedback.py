import copy
import os

import yaml

from harness import scorer, report

REPO = "/Users/hzf/workspace/silicon_notebook"
GOLD = os.path.join(REPO, "fangan/testcases/engram/ch00_abstract/gold.yaml")


def test_imperfect_candidate_produces_actionable_markdown():
    gold = yaml.safe_load(open(GOLD, encoding="utf-8"))
    pred = copy.deepcopy(gold)
    # degrade: drop one object, drop one atom, flip one relation type
    pred["objects"] = pred["objects"][:-1]
    pred["evidence_atoms"] = pred["evidence_atoms"][:-1]
    if pred.get("relations"):
        pred["relations"][0]["relation_type"] = "WRONG_REL_TYPE"
    result = scorer.score_fixture(gold, pred)
    md = report.to_markdown(result, title="degraded-demo")

    assert result["weighted_score"] < 100.0
    # the report must name at least one concrete missed/spurious/type-mismatch item
    assert ("false negatives" in md) or ("Type mismatches" in md)
    # machine report must be JSON-serializable
    assert report.to_json(result).startswith("{")
