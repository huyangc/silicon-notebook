from harness import align, stages


def atom(aid, c0, c1, atype="claim_sentence", line=11):
    return {"id": aid, "atom_type": atype,
            "source_span": {"file": "s.md", "line_start": line, "line_end": line,
                            "char_start": c0, "char_end": c1}}


GOLD = [atom("A1", 0, 100), atom("A2", 100, 200, "result_sentence")]


def test_match_atoms_identical():
    al = align.match_atoms(GOLD, GOLD, thresh=0.5)
    assert al["g2p"] == {"A1": "A1", "A2": "A2"}


def test_match_atoms_by_span_not_id():
    pred = [atom("P9", 2, 99), atom("P8", 101, 199, "result_sentence")]
    al = align.match_atoms(GOLD, pred, thresh=0.5)
    assert al["g2p"] == {"A1": "P9", "A2": "P8"}


def test_score_atoms_perfect():
    res = stages.score_atoms(GOLD, GOLD)
    assert res["prf"]["f1"] == 1.0
    assert res["type_accuracy"] == 1.0
    assert res["score"] == 1.0


def test_score_atoms_type_mismatch_hits_f1():
    pred = [atom("P1", 0, 100, "WRONG"), atom("P2", 100, 200, "result_sentence")]
    res = stages.score_atoms(GOLD, pred)
    assert res["prf"]["tp"] == 1
    assert res["type_accuracy"] == 0.5
    assert res["score"] < 1.0
    assert res["type_mismatches"] == [{"gold_id": "A1", "pred_id": "P1",
                                       "gold_type": "claim_sentence", "pred_type": "WRONG"}]


def test_score_atoms_missing_atom_lowers_recall():
    res = stages.score_atoms(GOLD, [GOLD[0]])
    assert res["prf"]["fn"] == 1
    assert res["prf"]["recall"] == 0.5
