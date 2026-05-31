from harness import align, stages

P2G = {"pa": "A1", "pb": "A2", "pc": "A3"}


def obj(oid, otype, local, payload, home="PKG1"):
    return {"id": oid, "type": otype, "home_package": home,
            "local_evidence_atom_ids": local, "payload": payload,
            "supporting_context_atom_ids": []}


GOLD = [
    obj("O1", "ArticleClaim", ["A1", "A2"], {"statement": "conditional memory is a new sparsity axis"}),
    obj("O2", "ArticleMethod", ["A3"], {"name": "Engram", "mechanism": "O(1) lookup"}),
]


def test_objects_perfect_self():
    res = stages.score_objects(GOLD, GOLD, {a: a for a in ["A1", "A2", "A3"]})
    assert res["prf"]["f1"] == 1.0
    assert res["payload"]["f1"] == 1.0
    assert res["evidence"]["mean_jaccard"] == 1.0
    assert res["score"] == 1.0


def test_objects_match_by_content_not_id():
    pred = [
        obj("Z1", "ArticleClaim", ["pa", "pb"], {"claim": "conditional memory is a new sparsity axis"}),
        obj("Z2", "ArticleMethod", ["pc"], {"name": "Engram", "how": "O(1) lookup"}),
    ]
    res = stages.score_objects(GOLD, pred, P2G)
    assert res["alignment"]["g2p"] == {"O1": "Z1", "O2": "Z2"}
    # payload values captured despite different keys
    assert res["payload"]["f1"] == 1.0


def test_objects_type_mismatch_hits_f1():
    pred = [
        obj("Z1", "WRONGTYPE", ["pa", "pb"], {"statement": "conditional memory is a new sparsity axis"}),
        obj("Z2", "ArticleMethod", ["pc"], {"name": "Engram", "mechanism": "O(1) lookup"}),
    ]
    res = stages.score_objects(GOLD, pred, P2G)
    assert res["prf"]["tp"] == 1
    assert any(tm["gold_id"] == "O1" for tm in res["type_mismatches"])


def test_objects_missing_payload_field_lowers_payload_recall():
    pred = [
        obj("Z1", "ArticleClaim", ["pa", "pb"], {"statement": "conditional memory is a new sparsity axis"}),
        obj("Z2", "ArticleMethod", ["pc"], {"name": "Engram"}),  # dropped mechanism
    ]
    res = stages.score_objects(GOLD, pred, P2G)
    assert res["payload"]["recall"] < 1.0
