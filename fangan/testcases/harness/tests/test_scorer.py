import glob
import os

import yaml

from harness import scorer

REPO = "/Users/hzf/workspace/silicon_notebook"
GOLDS = sorted(glob.glob(os.path.join(REPO, "fangan/testcases/*/ch*/gold.yaml")))


def test_gold_files_found():
    assert len(GOLDS) == 14


def test_gold_vs_gold_is_perfect():
    # The core sanity invariant: scoring gold against itself yields 100 on every chapter.
    for gp in GOLDS:
        gold = yaml.safe_load(open(gp, encoding="utf-8"))
        result = scorer.score_fixture(gold, gold)
        assert result["weighted_score"] == 100.0, f"{gp} -> {result['weighted_score']}"
        for bucket, s in result["stage_scores"].items():
            assert abs(s - 1.0) < 1e-9, f"{gp} bucket {bucket} = {s}"


def test_dropping_an_object_lowers_score():
    gold = yaml.safe_load(open(GOLDS[0], encoding="utf-8"))
    pred = yaml.safe_load(open(GOLDS[0], encoding="utf-8"))
    if pred.get("objects"):
        pred["objects"] = pred["objects"][:-1]
    result = scorer.score_fixture(gold, pred)
    assert result["weighted_score"] < 100.0
