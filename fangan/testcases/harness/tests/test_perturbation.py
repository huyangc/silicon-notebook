import copy
from pathlib import Path

import pytest
import yaml

from harness import scorer


@pytest.fixture
def architecture_gold_path(testcases_root: Path) -> Path:
    return testcases_root / "engram" / "ch02_architecture" / "gold.yaml"


def load(gold_path: Path) -> tuple[dict, dict]:
    gold = yaml.safe_load(gold_path.read_text(encoding="utf-8"))
    return gold, copy.deepcopy(gold)


def test_shifting_a_span_lowers_atom_iou_and_recall(
    architecture_gold_path: Path,
) -> None:
    gold, pred = load(architecture_gold_path)
    atom = pred["evidence_atoms"][0]["source_span"]
    atom["char_start"] += 100000
    atom["char_end"] += 100000
    result = scorer.score_fixture(gold, pred)
    assert result["stage_scores"]["evidence_atoms"] < 1.0


def test_flipping_an_atom_type_lowers_type_accuracy(
    architecture_gold_path: Path,
) -> None:
    gold, pred = load(architecture_gold_path)
    pred["evidence_atoms"][0]["atom_type"] = "DEFINITELY_WRONG"
    result = scorer.score_fixture(gold, pred)
    assert result["stages"]["evidence_atoms"]["type_accuracy"] < 1.0


def test_injecting_spurious_object_lowers_object_precision(
    architecture_gold_path: Path,
) -> None:
    gold, pred = load(architecture_gold_path)
    pred["objects"].append(
        {
            "id": "JUNK",
            "type": "ArticleClaim",
            "home_package": "PKG-NONE",
            "local_evidence_atom_ids": [],
            "supporting_context_atom_ids": [],
            "payload": {"statement": "totally unrelated fabricated claim xyz"},
        }
    )
    result = scorer.score_fixture(gold, pred)
    assert result["stages"]["objects"]["prf"]["precision"] < 1.0


def test_dropping_a_relation_lowers_relation_recall(
    architecture_gold_path: Path,
) -> None:
    gold, pred = load(architecture_gold_path)
    pred["relations"] = pred["relations"][:-1]
    result = scorer.score_fixture(gold, pred)
    assert result["stages"]["relations"]["prf"]["recall"] < 1.0


def test_extracting_forbidden_text_triggers_violation(
    architecture_gold_path: Path,
) -> None:
    gold, pred = load(architecture_gold_path)
    forbidden = None
    for entry in gold.get("do_not_extract") or []:
        forbidden = entry.get("text") or (entry.get("examples") or [None])[0]
        if forbidden:
            break
    if forbidden:
        pred.setdefault("mentions", []).append(
            {
                "id": "BAD",
                "text": forbidden,
                "type": "Concept",
                "atom_id": pred["evidence_atoms"][0]["id"],
            }
        )
        result = scorer.score_fixture(gold, pred)
        assert result["stages"]["do_not_extract"]["violations"] >= 1
