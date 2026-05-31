import os

from harness import run_all

REPO = "/Users/hzf/workspace/silicon_notebook"
GOLD_ROOT = os.path.join(REPO, "fangan/testcases")


def test_run_all_gold_as_candidate_scores_100(tmp_path):
    # Using gold itself as the candidate tree: every chapter must score 100.
    agg = run_all.run(gold_root=GOLD_ROOT, pred_root=GOLD_ROOT, out_dir=str(tmp_path))
    assert agg["chapters_scored"] == 14
    assert abs(agg["mean_weighted_score"] - 100.0) < 1e-9
    assert os.path.exists(os.path.join(str(tmp_path), "aggregate.json"))
    assert os.path.exists(os.path.join(str(tmp_path), "leaderboard.md"))
    lb = open(os.path.join(str(tmp_path), "leaderboard.md")).read()
    assert "ch00_abstract" in lb
