from harness import stages

# object alignment: gold_obj_id -> pred_obj_id and inverse
OBJ_G2P = {"O1": "Z1", "O2": "Z2"}
OBJ_P2G = {"Z1": "O1", "Z2": "O2"}


def rel(rid, rtype, src, tgt):
    return {"id": rid, "relation_type": rtype, "source_object_id": src, "target_object_id": tgt}


def test_relations_perfect():
    gold = [rel("R1", "method_addresses_problem", "O1", "O2")]
    pred = [rel("PR", "method_addresses_problem", "Z1", "Z2")]
    res = stages.score_relations(gold, pred, OBJ_G2P, OBJ_P2G)
    assert res["prf"]["f1"] == 1.0
    assert res["score"] == 1.0


def test_relations_type_mismatch():
    gold = [rel("R1", "method_addresses_problem", "O1", "O2")]
    pred = [rel("PR", "result_supports_claim", "Z1", "Z2")]
    res = stages.score_relations(gold, pred, OBJ_G2P, OBJ_P2G)
    assert res["prf"]["tp"] == 0
    assert res["type_mismatches"]


def test_relations_endpoint_not_aligned_is_fn():
    gold = [rel("R1", "method_addresses_problem", "O1", "O2")]
    pred = []  # nothing
    res = stages.score_relations(gold, pred, OBJ_G2P, OBJ_P2G)
    assert res["prf"]["fn"] == 1


def test_packages_object_recall():
    gold_pkgs = [{"id": "PKG1", "chunk_id": "C1", "expected_objects": ["O1", "O2"],
                  "expected_local_fields": {}}]
    pred_pkgs = [{"id": "QP", "chunk_id": "X"}]
    # both objects homed into QP by pred; chunk alignment maps C1->X
    pred_objs = [{"id": "Z1", "home_package": "QP"}, {"id": "Z2", "home_package": "QP"}]
    res = stages.score_packages(gold_pkgs, pred_pkgs, pred_objs,
                                chunk_g2p={"C1": "X"}, obj_g2p=OBJ_G2P)
    assert res["object_recall"] == 1.0
    assert res["score"] == 1.0


def test_structure_section_paths():
    gold_tree = [{"id": "S1", "path": "Abstract"}, {"id": "S2", "path": "1 Introduction"}]
    pred_tree = [{"id": "x", "path": "abstract"}]  # case-insensitive match, missing one
    res = stages.score_structure(gold_tree, pred_tree, gold_mentions=[], pred_mentions=[],
                                 atom_p2g={})
    assert res["sections"]["recall"] == 0.5


def test_do_not_extract_violation():
    gold_dne = [{"text": "https://github.com/deepseek-ai/Engram", "kind": "out_of_slice_reference"}]
    pred = {"mentions": [{"id": "m", "text": "https://github.com/deepseek-ai/Engram", "type": "Concept"}],
            "objects": [], "evidence_atoms": []}
    res = stages.score_do_not_extract(gold_dne, pred)
    assert res["violations"] == 1
    assert res["score"] < 1.0


def test_do_not_extract_clean():
    gold_dne = [{"text": "https://github.com/deepseek-ai/Engram"}]
    pred = {"mentions": [], "objects": [], "evidence_atoms": []}
    res = stages.score_do_not_extract(gold_dne, pred)
    assert res["violations"] == 0
    assert res["score"] == 1.0
