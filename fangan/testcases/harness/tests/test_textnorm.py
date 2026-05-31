from harness import textnorm


def test_norm_collapses_ws_and_lowercases():
    assert textnorm.norm_text("  The   U-Shaped  Law ") == "the u-shaped law"


def test_norm_strips_quotes():
    assert textnorm.norm_text("'Engram'") == "engram"


def test_text_equiv_exact_after_norm():
    assert textnorm.text_equiv("O(1)  lookup", "o(1) lookup") is True


def test_text_equiv_containment():
    # short gold value contained in longer pred value counts as equivalent
    assert textnorm.text_equiv("MMLU +3.4", "knowledge: MMLU +3.4 and CMMLU +4.0") is True


def test_text_equiv_negative():
    assert textnorm.text_equiv("deterministic addressing", "random eviction") is False


def test_payload_values_flatten_nested():
    payload = {"a": "x", "b": {"c": "y", "d": ["m", "n"]}}
    vals = textnorm.payload_values(payload)
    assert set(vals) == {"x", "y", "m", "n"}
