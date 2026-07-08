from app.services.kg.mention_scan import build_alias_table, boundary_hit, is_latin


def test_alias_table_full_head_acronym():
    at = build_alias_table([("K-gqa", "Grouped-query attention (GQA)")])
    assert {"grouped-query attention (gqa)", "grouped-query attention", "gqa"} <= at["K-gqa"]


def test_alias_length_gates():
    at = build_alias_table([("K-a", "RoPE"), ("K-b", "V2"), ("K-c", "铸币平价"), ("K-d", "汇率")])
    assert "rope" in at.get("K-a", set())          # Latin 全名 len==4 通过
    assert at.get("K-b", set()) == set()            # 全名 len<4 且非括号缩写 → 不入表
    assert "铸币平价" in at.get("K-c", set())        # CJK len>=3
    assert at.get("K-d", set()) == set()            # CJK len==2 放弃


def test_acronym_bypasses_latin_min():
    # 括号缩写(3-8位,来自显式 "(ACR)" 模式,precision 高)绕过 latin_min=4 的长度门:
    # GQA/MQA/SFT 这类 3 位缩写正是共提桥最有价值的别名。
    at = build_alias_table([("K-gqa", "Grouped-query attention (GQA)")])
    assert "gqa" in at["K-gqa"]


def test_boundary_hit_latin_word_boundary():
    assert boundary_hit("rope", "we use rope embeddings")
    assert not boundary_hit("rope", "in europe the model")   # 子串不算
    assert boundary_hit("gqa", "gqa reduces kv cache")


def test_boundary_hit_cjk_substring():
    assert boundary_hit("铸币平价", "在金本位下铸币平价决定汇率")
    assert is_latin("rope") and not is_latin("铸币平价")
