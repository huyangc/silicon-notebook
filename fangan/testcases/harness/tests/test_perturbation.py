import copy
import os

import yaml

from harness import scorer

REPO = "/Users/hzf/workspace/silicon_notebook"
# pick a chapter with atoms, chunks, objects, relations all present
GOLD = os.path.join(REPO, "fangan/testcases/engram/ch02_architecture/gold.yaml")


def load():
    g = yaml.safe_load(open(GOLD, encoding="utf-8"))
    return g, copy.deepcopy(g)


def test_shifting_a_span_lowers_atom_iou_and_recall():
    gold, pred = load()
    a = pred["evidence_atoms"][0]["source_span"]
    a["char_start"] = a["char_start"] + 100000  # push it far enough to break overlap
    a["char_end"] = a["char_end"] + 100000
    res = scorer.score_fixture(gold, pred)
    assert res["stage_scores"]["evidence_atoms"] < 1.0


def test_flipping_an_atom_type_lowers_type_accuracy():
    gold, pred = load()
    pred["evidence_atoms"][0]["atom_type"] = "DEFINITELY_WRONG"
    res = scorer.score_fixture(gold, pred)
    assert res["stages"]["evidence_atoms"]["type_accuracy"] < 1.0


def test_injecting_spurious_object_lowers_object_precision():
    gold, pred = load()
    pred["objects"].append({"id": "JUNK", "type": "ArticleClaim", "home_package": "PKG-NONE",
                            "local_evidence_atom_ids": [], "supporting_context_atom_ids": [],
                            "payload": {"statement": "totally unrelated fabricated claim xyz"}})
    res = scorer.score_fixture(gold, pred)
    assert res["stages"]["objects"]["prf"]["precision"] < 1.0


def test_dropping_a_relation_lowers_relation_recall():
    gold, pred = load()
    pred["relations"] = pred["relations"][:-1]
    res = scorer.score_fixture(gold, pred)
    assert res["stages"]["relations"]["prf"]["recall"] < 1.0


def test_extracting_forbidden_text_triggers_violation():
    gold, pred = load()
    dne = (gold.get("do_not_extract") or [])
    forbidden = None
    for e in dne:
        forbidden = e.get("text") or (e.get("examples") or [None])[0]
        if forbidden:
            break
    if forbidden:
        pred.setdefault("mentions", []).append(
            {"id": "BAD", "text": forbidden, "type": "Concept", "atom_id": pred["evidence_atoms"][0]["id"]})
        res = scorer.score_fixture(gold, pred)
        assert res["stages"]["do_not_extract"]["violations"] >= 1
