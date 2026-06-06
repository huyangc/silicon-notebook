from app.eval.probes import (classify_concept, enumerated_groups,
                             near_duplicate_groups)


def test_symbol_variables():
    assert "symbol" in classify_concept("Vb1")
    assert "symbol" in classify_concept("R_0")
    assert "symbol" in classify_concept("Z_out,0")
    assert "symbol" in classify_concept("place_opt_design")  # innovus 小写命令(无空格含_)


def test_reference_like():
    assert "reference" in classify_concept("Circuit of Fig. 12.3(a)")
    assert "reference" in classify_concept("CMFB using error amplifier (Fig. 9.51)")
    assert "reference" in classify_concept("Table 2-1")


def test_quantity_like():
    assert "quantity" in classify_concept("7nm")
    assert "quantity" in classify_concept("0.18 um process")
    assert "quantity" in classify_concept("3.5 GHz")


def test_code_identifier_not_misfiring_on_terms():
    assert "code" in classify_concept("SET_DB")
    assert "code" in classify_concept("getValue()")
    assert "code" not in classify_concept("FinFET")   # 驼峰术语不算代码
    assert "code" not in classify_concept("MOSFET")


def test_clean_concepts_have_no_tags():
    assert classify_concept("cascode connection") == set()
    assert classify_concept("current mirror") == set()
    assert classify_concept("bandgap reference") == set()


def test_enumerated_groups_catches_level_models():
    names = ["Level 1 Model", "Level 2 Model", "Level 3 Model", "current mirror"]
    groups = enumerated_groups(names)
    assert "level # model" in groups
    assert set(groups["level # model"]) == {"Level 1 Model", "Level 2 Model", "Level 3 Model"}


def test_near_duplicate_groups():
    names = ["S_A-B = (rate A) / (rate B)", "S_A-B = rate A / rate B", "noise"]
    groups = near_duplicate_groups(names)
    assert any(len(v) >= 2 for v in groups.values())
