from app.eval.probes import (classify_concept, enumerated_groups,
                             near_duplicate_groups)
from app.eval.probes import (claim_degraded, formula_degraded,
                             procedure_degraded, aggregate_quality)


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


def test_claim_degraded():
    assert not claim_degraded("The cascode connection increases output resistance.")
    assert claim_degraded("cascode")                  # 太短/无动词
    assert claim_degraded("output resistance of the")  # 截断结尾


def test_formula_degraded():
    assert not formula_degraded("f_write = (M/(2N)) * f_ref")
    assert not formula_degraded("$C/g_m$")
    assert formula_degraded("the gain")               # 无运算符/等号/数学符号


def test_procedure_degraded():
    assert procedure_degraded({"name": "Analysis process"})         # 无 steps
    assert procedure_degraded({"name": "x", "steps": []})           # 空 steps
    assert not procedure_degraded({"name": "x", "steps": [{"name": "a"}, {"name": "b"}]})


def test_aggregate_quality_counts_and_rate():
    concepts = [
        {"id": "1", "name": "cascode connection", "evidence_count": 3},
        {"id": "2", "name": "Vb1", "evidence_count": 1},
        {"id": "3", "name": "Circuit of Fig. 9.1", "evidence_count": 1},
        {"id": "4", "name": "Level 1 Model", "evidence_count": 1},
        {"id": "5", "name": "Level 2 Model", "evidence_count": 1},
    ]
    degree = {"1": 2}  # 只有 cascode 有关系;其余度=0
    m = aggregate_quality(concepts, degree)
    assert m["total"] == 5
    assert m["probe_counts"]["symbol"] >= 1
    assert m["probe_counts"]["reference"] >= 1
    assert m["enumerated_groups"] >= 1          # Level 1/2 Model
    assert m["orphans"] >= 1                     # 度=0 且 evidence=1
    assert 0.0 < m["suspect_non_atomic_rate"] <= 1.0
    assert len(m["samples"]["symbol"]) >= 1     # 样例清单
