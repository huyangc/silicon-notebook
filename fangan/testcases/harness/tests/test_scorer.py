from pathlib import Path

import yaml

from harness import scorer


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_gold_files_found(gold_paths: tuple[Path, ...]) -> None:
    assert len(gold_paths) == 14


def test_gold_vs_gold_is_perfect(gold_paths: tuple[Path, ...]) -> None:
    for gold_path in gold_paths:
        gold = _read_yaml(gold_path)
        result = scorer.score_fixture(gold, gold)
        assert result["weighted_score"] == 100.0, (
            f"{gold_path} -> {result['weighted_score']}"
        )
        for bucket, score in result["stage_scores"].items():
            assert abs(score - 1.0) < 1e-9, (
                f"{gold_path} bucket {bucket} = {score}"
            )


def test_dropping_an_object_lowers_score(
    gold_paths: tuple[Path, ...],
) -> None:
    gold = _read_yaml(gold_paths[0])
    pred = _read_yaml(gold_paths[0])
    if pred.get("objects"):
        pred["objects"] = pred["objects"][:-1]
    result = scorer.score_fixture(gold, pred)
    assert result["weighted_score"] < 100.0
