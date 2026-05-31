from harness import stages

# atom alignment map: pred atom id -> gold atom id
P2G = {"pa": "A1", "pb": "A2", "pc": "A3"}


def chunk(cid, atoms, ctype="article_core_claim_block"):
    return {"id": cid, "chunk_type": ctype, "atom_ids": atoms}


GOLD = [chunk("C1", ["A1", "A2"]), chunk("C2", ["A3"])]


def test_chunks_perfect():
    pred = [chunk("X", ["pa", "pb"]), chunk("Y", ["pc"])]
    res = stages.score_chunks(GOLD, pred, P2G)
    assert res["prf"]["f1"] == 1.0
    assert res["score"] == 1.0
    assert res["type_accuracy"] == 1.0


def test_chunks_oversplit_detected():
    # pred splits gold C1's atoms into two chunks => C1 matches at most one, other is spurious
    pred = [chunk("X", ["pa"]), chunk("Y", ["pb"]), chunk("Z", ["pc"])]
    res = stages.score_chunks(GOLD, pred, P2G)
    assert res["over_split"] >= 1
    assert res["prf"]["fp"] >= 1


def test_chunks_type_mismatch():
    pred = [chunk("X", ["pa", "pb"], "WRONG"), chunk("Y", ["pc"])]
    res = stages.score_chunks(GOLD, pred, P2G)
    assert res["type_accuracy"] == 0.5
