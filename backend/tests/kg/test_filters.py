from app.services.kg.filters import should_extract_window, is_noise_concept
from app.services.kg.parsing import SourceElementQ


def _el(text, typ="paragraph"):
    return SourceElementQ(id="SE-1", type=typ, file="b.md", line_start=1, line_end=1,
                          char_start=0, char_end=len(text), text=text)


# ---- should_extract_window ----

def test_skips_textbook_problem_sections():
    keep, reason = should_extract_window("7 > 7.5 > Problems", [_el("7.1 Calculate the gain.")], "textbook")
    assert keep is False and reason == "textbook_problem_section"


def test_skips_backmatter_index_section():
    keep, reason = should_extract_window("Index", [_el("frequency response, 495")], "textbook")
    assert keep is False and reason == "backmatter_section"


def test_skips_index_like_window():
    els = [_el("frequency response, 495"), _el("input offset voltage, 230"), _el("slew rate, 312")]
    keep, reason = should_extract_window("3 > 3.2 Body", els, "textbook")
    assert keep is False and reason == "index_like_window"


def test_keeps_formula_body_section():
    keep, reason = should_extract_window(
        "9 > 9.6 > Slew Rate",
        [_el("The slew rate is set by the compensation capacitor."), _el("SR = I/C", "formula")],
        "textbook",
    )
    assert keep is True and reason == ""


def test_problem_skip_only_for_textbook():
    keep, _ = should_extract_window("7 > Problems", [_el("Find the gain.")], "academic")
    assert keep is True


# ---- is_noise_concept ----

WL = frozenset()


def test_noise_symbols_dropped():
    for n in ["V_DD", "g_m1", "i_b68", "R_E26", "(W/L)_1", "A_v^+"]:
        assert is_noise_concept(n, WL)[0] is True, n


def test_noise_instance_labels_dropped():
    for n in ["Q12", "M10", "C20"]:
        assert is_noise_concept(n, WL)[0] is True, n


def test_noise_refs_and_sections_dropped():
    assert is_noise_concept("Fig. 5.38", WL)[0] is True
    assert is_noise_concept("Table 2.1", WL)[0] is True
    assert is_noise_concept("8.4.1 Series-Shunt Feedback", WL)[0] is True


def test_noise_too_short_dropped():
    assert is_noise_concept("Q", WL)[0] is True
    assert is_noise_concept("gm", WL)[0] is True


def test_real_concepts_kept():
    for n in ["transconductance", "current mirror", "slew rate",
              "channel length modulation", "Wilson current mirror", "741 op-amp"]:
        assert is_noise_concept(n, WL)[0] is False, n


def test_whitelist_overrides_symbol_rule():
    assert is_noise_concept("VCO", frozenset({"vco"}))[0] is False
    assert is_noise_concept("gm", frozenset({"gm"}))[0] is False


def test_backmatter_matches_segment_not_substring():
    # standalone backmatter segments -> skip
    assert should_extract_window("Index", [_el("frequency response, 495")], "textbook")[0] is False
    assert should_extract_window("8 > References", [_el("[1] Razavi, 2001.")], "textbook")[0] is False
    # content sections that merely contain 'index' as a word/substring -> keep
    assert should_extract_window("2 > Indexed Addressing", [_el("Indexed addressing adds a base and offset.")], "textbook")[0] is True
    assert should_extract_window("3 > Index Register Theory", [_el("The index register holds an address.")], "textbook")[0] is True
