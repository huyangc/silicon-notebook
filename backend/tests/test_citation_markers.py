"""Model punctuation variants identify the same issued evidence keys."""
import pytest

from app.services.citation_markers import LOOSE_MARKER_RE, MARKER_RE, marker_keys


@pytest.mark.parametrize("opening,closing", [("[", "]"), ("【", "】"), ("(", ")"), ("（", "）")])
@pytest.mark.parametrize("separator", [",", "，", "、"])
def test_marker_variants_preserve_key_order(opening, closing, separator):
    marker = opening + separator.join(["k5", "k9", "k7"]) + closing
    assert MARKER_RE.findall("依据" + marker + "。") == [marker]
    assert marker_keys(marker) == ["k5", "k9", "k7"]
    padded = opening + " k5" + separator + "\n k9 " + closing
    assert LOOSE_MARKER_RE.fullmatch(padded)
    assert marker_keys(padded) == ["k5", "k9"]


@pytest.mark.parametrize("text", ["(2026)", "（1、2）", "(k5、k9】", "（k5、k9)", "(k8s)", "(k5、说明)"])
def test_ordinary_or_mismatched_parentheses_are_not_evidence(text):
    assert not MARKER_RE.search(text)
    assert not LOOSE_MARKER_RE.search(text)
