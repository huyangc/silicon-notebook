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


def test_skips_frontmatter_sections():
    for path in ["Preface", "PREFACE", "To the Instructor", "Foreword",
                 "Acknowledgments", "前言", "目录"]:
        keep, reason = should_extract_window(
            path, [_el("This book deals with the analysis of RF circuits.")], "textbook")
        assert keep is False and reason == "frontmatter_section", path


def test_frontmatter_segment_exact_match_no_false_positive():
    # 段内含 "preface" 词但不是 frontmatter 段名 → 不拦
    keep, _ = should_extract_window(
        "3 > 3.2 Preface to the Noise Model",
        [_el("Thermal noise arises from random carrier motion.")], "textbook")
    assert keep is True


def test_contents_segment_blocked_as_frontmatter():
    keep, reason = should_extract_window("Contents", [_el("x")], "textbook")
    assert keep is False and reason == "frontmatter_section"


def test_skips_toc_like_window():
    els = [_el("1.1 General Considerations 7"),
           _el("1.2 Costs of Integration 9"),
           _el("2.1 General Considerations 15")]
    keep, reason = should_extract_window("1 Overview", els, "textbook")
    assert keep is False and reason == "toc_like_window"


def test_toc_like_not_triggered_by_body_prose():
    els = [_el("The gain of the amplifier is set by the ratio of resistors."),
           _el("2.1 The small-signal model applies at low frequencies.")]
    keep, _ = should_extract_window("2 > 2.1 Body", els, "textbook")
    assert keep is True


def test_spec_like_lines_below_ratio_threshold_kept():
    # "1.2 V supply 3" 单行确实命中 _TOC_LINE_RE(已知限制),
    # 但正文窗口中此类行占比 < 0.6 → 不跳。固化该阈值保护行为。
    els = [_el("1.2 V supply 3"),
           _el("The amplifier operates from a single supply rail."),
           _el("Gain accuracy depends on resistor matching.")]
    keep, _ = should_extract_window("4 > 4.2 Body", els, "textbook")
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


def test_multiword_name_with_symbol_kept():
    # bare single-token symbols are dropped
    for n in ["V_DD", "g_m1", "(W/L)_1", "A_v^+"]:
        assert is_noise_concept(n, WL)[0] is True, n
    # P0-2: narrower symbol rule -- an underscore token is only "symbol" noise
    # when some segment is a bare single-char/empty stem. "phi_MS" has two
    # multi-char segments (phi, MS) so it no longer matches; this is the same
    # segment-length test that lets snake_case command names like set_db pass
    # (see test_snake_case_command_names_kept below).
    assert is_noise_concept("phi_MS", WL)[0] is False
    # descriptive multi-word concepts that merely CONTAIN a symbol notation are
    # kept — the caret example pins the "^" branch inside the `" " not in raw`
    # guard (hoisting it above the space check would wrongly drop this name).
    for n in ["Oxide Capacitance (C_ox)", "Threshold Voltage (V_T0)",
              "gate-to-source capacitance (C_GS)", "depletion-layer capacitance C_j",
              "gain A_v^+ at low frequency"]:
        assert is_noise_concept(n, WL)[0] is False, n


def test_snake_case_command_names_kept():
    # P0-2: EDA/CLI command-style snake_case identifiers where every
    # underscore segment has length > 1 must NOT be classified as bare
    # symbol noise -- the old blanket "_ in raw" rule filtered these out
    # systematically, keeping command entities out of the KG.
    for n in ["set_db", "report_timing", "place_opt_design", "create_clock"]:
        keep, reason = is_noise_concept(n, WL)
        assert keep is False, (n, reason)


def test_symbol_rule_still_drops_short_segment_symbols():
    # symbols with a bare single-char/empty underscore segment, or a caret
    # (mathematical superscript), remain classified as symbol noise.
    # "_foo" pins the empty-segment half of the predicate (len(seg) == 0):
    # rewriting it as min(...) == 1 or pre-filtering empty segments would
    # silently start keeping leading/trailing-underscore names.
    for n in ["V_DD", "g_m1", "x_0", "a^2", "_foo"]:
        keep, reason = is_noise_concept(n, WL)
        assert keep is True and reason == "symbol", n


def test_section_heading_vs_measurement_or_standard():
    # real numbered section headings (number + Title-Case) -> drop
    for n in ["1.1 Introduction", "1.2.1 Depletion-Region Capacitance", "8.4.1 Series-Shunt Feedback"]:
        assert is_noise_concept(n, WL)[0] is True, n
    # decimals that are measurements / standards / ratios -> keep
    for n in ["0.8 μm CMOS process", "802.11g band", "1.25 divider"]:
        assert is_noise_concept(n, WL)[0] is False, n


# ---- is_meta_claim ----

from app.services.kg.filters import is_meta_claim


def test_meta_claims_dropped():
    for s in [
        "This book deals with the analysis and design of RF integrated circuits",
        "CMOS technology is the subject of this text.",
        "Chapter 9 presents the topic of switched capacitor circuits.",
        "This chapter forms the foundation for synthesizers.",
        "Section 1.1 gave a definition of signals in analog circuits",
        "I wanted to teach design in addition to analysis",
        "In this chapter, we will see the noise model of the MOSFET",
    ]:
        hit, reason = is_meta_claim(s)
        assert hit is True and reason == "meta_narrative", s


def test_technical_claims_kept():
    for s in [
        # 真断言不得误杀——含 chapter/section 词但指涉技术对象或他文引用
        "The input section of the op amp dominates the noise budget",
        "MOS transistors continue to become faster, but at the cost of their 'analog' properties.",
        "The slew rate is set by the compensation capacitor.",
        "Thermal noise increases with temperature",
    ]:
        assert is_meta_claim(s)[0] is False, s


def test_this_section_circuit_sense_false_positive_documented():
    # 已知误杀基线: "this section" 指电路区段时也会命中(见 filters.py 注释
    # 的权衡说明)。若此测试开始失败, 说明口径变了, 需重新评估拦截/误杀面。
    assert is_meta_claim("This section of the filter has a low-pass characteristic")[0] is True
