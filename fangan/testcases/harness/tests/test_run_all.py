from pathlib import Path

from harness import run_all


def test_run_all_gold_as_candidate_scores_100(
    tmp_path: Path,
    testcases_root: Path,
) -> None:
    aggregate = run_all.run(
        gold_root=str(testcases_root),
        pred_root=str(testcases_root),
        out_dir=str(tmp_path),
    )
    assert aggregate["chapters_scored"] == 14
    assert abs(aggregate["mean_weighted_score"] - 100.0) < 1e-9
    assert (tmp_path / "aggregate.json").exists()
    leaderboard = tmp_path / "leaderboard.md"
    assert leaderboard.exists()
    assert "ch00_abstract" in leaderboard.read_text(encoding="utf-8")
