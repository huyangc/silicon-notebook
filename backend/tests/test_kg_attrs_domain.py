"""Pure-function contract of app.domain.kg_attrs (spec 2026-10-28 §7.2–§7.3).

Every rule case below names the mutation it exists to catch (plan G0-T1).
"""
from __future__ import annotations

import copy
import inspect
import re
from pathlib import Path

import pytest
import yaml

from app.domain import kg_attrs
from app.domain import kg_attrs_contracts as contracts
from app.domain.kg_concept_norm import norm_concept_name
from app.services.kg import canonicalize
from app.services.knowhow import ids as knowhow_ids

_ALL = contracts.ALIAS_RULES
_ZH = Path(__file__).resolve().parent / "fixtures" / "kg_attrs_zh_synthetic.yaml"


def _item(text: str, element_id: str = "el-1", read: bool = True, **extra) -> dict:
    return {"element_id": element_id, "element_text": text, "element_read": read, **extra}


def _trace(name, items, *, exclude=(), rules=_ALL):
    return [(h["rule"], h["text"]) for h in kg_attrs.trace_concept_aliases(
        name, items, exclude_names=exclude, rules=rules)]


# --- normalizer move --------------------------------------------------------

def test_canonicalize_uses_the_domain_normalizer():
    assert canonicalize._norm is norm_concept_name


def test_knowhow_object_ids_use_the_shared_prefix():
    assert knowhow_ids._cell_ko_id("t", "c", "v").startswith(contracts.KNOWHOW_OBJECT_ID_PREFIX)


# --- alias rules ------------------------------------------------------------

def test_r1_name_carries_its_own_initialism():
    hits = kg_attrs.trace_concept_aliases(
        "Voltage Controlled Oscillator (VCO)",
        [_item("x", element_id=""), _item("x", element_id="el-2", read=False), _item("x", "el-3")],
        exclude_names=(), rules=_ALL,
    )
    # element_id is the first readable item, skipping "" and unread ones.
    assert hits == [{"rule": "R1", "text": "VCO", "element_id": "el-3"}]


def test_r1_without_a_readable_item_has_no_element_id():
    hits = kg_attrs.trace_concept_aliases(
        "Phase Locked Loop (PLL)", [_item("x", element_id="")], exclude_names=(), rules=("R1",))
    assert hits == [{"rule": "R1", "text": "PLL"}]


def test_r2_name_followed_by_initialism_halfwidth_and_fullwidth():
    items = [_item("A Phase Locked Loop (PLL) locks."), _item("Phase Locked Loop（PLL）again", "el-2")]
    assert _trace("Phase Locked Loop", items) == [("R2", "PLL")]


def test_r3_acronym_name_finds_its_expansion():
    items = [_item("we use a phase-locked loop (PLL) here")]
    assert _trace("PLL", items) == [("R3", "phase-locked loop")]


def test_r4_cjk_name_followed_by_acronym():
    assert _trace("压控振荡器", [_item("压控振荡器（VCO）的频率")]) == [("R4", "VCO")]


def test_r4_rejects_instance_labels():
    # mutation: drop the instance-label filter -> M1/Q5 become aliases.
    assert _trace("晶体管", [_item("晶体管（M1）与晶体管（Q5）")]) == []


@pytest.mark.parametrize("name, text", [
    ("Fig. 3 (a)", "Fig. 3 (a)"),            # R1 shape, not an initialism
    ("Filter (LP)", "Filter (LP)"),          # qualifier paren
    ("Filter", "the Filter (LP) stage"),     # R2: LP is not an initialism of Filter
    ("LP", "a low pass filter (LP)"),        # R3: three words, two letters
])
def test_non_initialism_parens_are_not_aliases(name, text):
    # mutation: drop _is_initialism_of -> every paren token becomes an alias.
    assert _trace(name, [_item(text)]) == []


def test_alias_equal_to_name_or_excluded_name_is_dropped():
    items = [_item("Phase Locked Loop (PLL)")]
    assert _trace("Phase Locked Loop", items, exclude=("pll",)) == []
    # the name itself is always excluded, even when not passed explicitly
    assert _trace("PLL", [_item("the PLL (PLL) block")]) == []


def test_alias_comparison_does_not_fold_through_merge_seed_aliases():
    # mutation: compare with kg_merge_seed._norm -> "VCO" folds to the full
    # name, equals the excluded member name, and the real alias is lost.
    items = [_item("The Voltage Controlled Oscillator (VCO) is tuned.")]
    assert _trace("Voltage Controlled Oscillator", items,
                  exclude=("Voltage Controlled Oscillator",)) == [("R2", "VCO")]


def test_rules_read_the_full_element_text_not_the_quoted_span():
    # mutation: read quoted_span -> an acronym past character 400 is missed.
    text = "x " * 300 + "Low Noise Amplifier (LNA)"
    item = _item(text, quoted_span=text[:400])
    assert _trace("Low Noise Amplifier", [item]) == [("R2", "LNA")]


@pytest.mark.parametrize("item", [
    _item("Low Noise Amplifier (LNA)", element_id=""),
    _item("Low Noise Amplifier (LNA)", read=False),
    {"element_id": "el-1", "element_text": "Low Noise Amplifier (LNA)"},
])
def test_text_rules_skip_unlocated_or_unread_items(item):
    # mutation: run R2-R4 on these -> the stored 400-char quote of a dangling
    # or foreign pointer would yield aliases.
    assert _trace("Low Noise Amplifier", [item]) == []


def test_names_with_regex_metacharacters_are_escaped():
    # mutation: drop re.escape -> re.error or wildcard matches.
    assert _trace("C++ Compiler", [_item("the C++ Compiler (CC) runs")]) == [("R2", "CC")]
    assert _trace("a.b Layer", [_item("an axb Layer (AL) only")]) == []
    assert _trace("a.b Layer", [_item("the a.b Layer (AL) here")]) == [("R2", "AL")]
    assert _trace("x*y Rule", [_item("the xxxy Rule (XR) only")]) == []
    assert _trace("(W/L) Ratio", [_item("the (W/L) Ratio (WR) here")]) == []
    assert _trace("C++编译器", [_item("C++编译器（CPPC）")]) == [("R4", "CPPC")]


def test_trace_dedupes_within_a_rule_not_across_rules():
    items = [_item("Phase Locked Loop (PLL), Phase Locked Loop (PLL)")]
    assert _trace("Phase Locked Loop (PLL)", items) == [("R1", "PLL")]
    # R1 and R2 both hit "PLL": trace keeps both, derive keeps one.
    assert _trace("Phase Locked Loop (PLL)", [_item("Phase Locked Loop (PLL) (PLL)")]) \
        == [("R1", "PLL")]


def test_derive_dedupes_across_rules_and_truncates_to_the_cap():
    tokens = [f"A{chr(ord('A') + i)}" for i in range(contracts.ALIASES_MAX + 1)]
    items = [_item("".join(f"放大器（{t}）" for t in tokens))]
    out = kg_attrs.derive_concept_aliases("放大器", items, exclude_names=(), rules=_ALL)
    # mutation: silently drop everything past the cap -> fewer than the cap.
    assert [a["text"] for a in out] == tokens[: contracts.ALIASES_MAX]
    assert all(set(a) == {"text", "element_id"} for a in out)


def test_derive_uses_the_enabled_rules_by_default():
    default = inspect.signature(kg_attrs.derive_concept_aliases).parameters["rules"].default
    assert default is contracts.ENABLED_ALIAS_RULES
    two = kg_attrs.derive_concept_aliases(
        "Phase Locked Loop (PLL)", [_item("Phase Locked Loop (PLL) (PLL)")],
        exclude_names=(), rules=("R1", "R2"))
    assert two == [{"text": "PLL", "element_id": "el-1"}]


def test_long_alias_is_not_emitted():
    long_words = " ".join(["Word"] * 12)
    acronym = "W" * 12
    assert _trace(acronym, [_item(f"{long_words} ({acronym})")]) == []  # shape cap: 8 chars
    name = "Abc " * 30
    text = f"{name.strip()} (AAAA)"
    assert all(len(h[1]) <= contracts.ALIAS_CHARS_MAX for h in _trace(name.strip(), [_item(text)]))


def test_alias_functions_do_not_mutate_their_input():
    items = [_item("Phase Locked Loop (PLL)", quoted_span="Phase")]
    before = copy.deepcopy(items)
    kg_attrs.derive_concept_aliases("Phase Locked Loop", items, exclude_names=(), rules=_ALL)
    assert items == before


@pytest.mark.parametrize("object_id, object_type, eligible", [
    ("ko-1", "concept", True),
    ("ko-1", "formula", True),
    ("ko-1", "claim", False),
    ("ko-kh-abc", "concept", False),
    ("ko-kh-abc", "formula", False),
])
def test_attrs_eligible(object_id, object_type, eligible):
    assert kg_attrs.attrs_eligible(object_id, object_type) is eligible


# --- LaTeX normalization ----------------------------------------------------

def test_normalize_unwraps_commands_before_dropping_backslashes():
    assert kg_attrs.normalize_latex(r"\mathrm{g}_{m}") == "g_m"
    assert kg_attrs.normalize_latex(r"\text{\mathit{V}}_{DD}") == "v_dd"
    assert kg_attrs.normalize_latex(r"\operatorname{tr}(A)") == "tr(a)"
    assert kg_attrs.normalize_latex(r"\boldsymbol{x}") == "x"


def test_normalize_greek_both_ways_and_delimiters():
    assert kg_attrs.normalize_latex(r"$\alpha$") == kg_attrs.normalize_latex("α") == "α"
    assert kg_attrs.normalize_latex(r"\(\Omega\)") == kg_attrs.normalize_latex("Ω")
    assert kg_attrs.normalize_latex(r"\mu_{n}") == kg_attrs.normalize_latex("μ_n") == "μ_n"
    assert kg_attrs.normalize_latex(r"\pitchfork x") == "x"  # a command, not \pi + "tchfork"


def test_normalize_drops_command_names_and_reads_fractions():
    # mutation: keep command names -> "sqrt" would let symbol "t" through.
    assert kg_attrs.normalize_latex(r"\sqrt{2 I_D}") == "2 i_d"
    assert "t" not in kg_attrs.normalize_latex(r"g_m = \sqrt{2 I_D \mu C_{ox}}")
    assert kg_attrs.normalize_latex(r"a \cdot b\,c") == "a bc"
    # mutation: no fraction rule -> "fracwl", and "W/L" is not in the formula.
    assert kg_attrs.normalize_latex(r"\frac{W}{L}") == kg_attrs.normalize_latex("W/L") == "w/l"
    assert kg_attrs.normalize_latex(r"\frac{1}{2\pi R C}") == "1/2πr c"
    assert kg_attrs.normalize_latex(r"\dfrac{a}{\frac{b}{c}}") == "a/b/c"
    # mutation: flat-brace wrapper regex -> nested braces break the unwrap.
    assert kg_attrs.normalize_latex(r"\mathrm{V_{DD}}") == kg_attrs.normalize_latex("V_{DD}") == "v_dd"
    assert kg_attrs.normalize_latex(r"\frac{W") == "w"                      # unbalanced: no crash


def test_formula_symbols_match_through_the_new_normalization():
    result = _verify(r"I_D = \frac{1}{2}\mu C \frac{W}{L} \sqrt{2 I_D}",
                     [r"I_D = \frac{1}{2}\mu C \frac{W}{L}", "where W/L is the aspect ratio, t is time"],
                     [("W/L", "the aspect ratio"), ("t", "time")])
    assert [k["symbol"] for k in result.kept] == ["W/L"]
    assert _reasons(result) == {"t": "symbol_not_in_formula"}


def test_rivals_are_only_symbols_that_pass_checks_one_and_two():
    # "b" is not in the formula, so it cannot out-compete "a" for "the bias".
    result = _verify("y = a", ["y = a", "where a is the bias b"], [("a", "the bias"), ("b", "x")])
    assert [k["symbol"] for k in result.kept] == ["a"]


def test_signature_covers_every_cross_module_import():
    import ast
    tree = ast.parse(Path(kg_attrs.__file__).read_text(encoding="utf-8"))
    covered = dict(kg_attrs.SIGNATURE_DEPENDENCIES)
    imported = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.domain."):
            imported[node.module] = {a.name for a in node.names}
        if isinstance(node, ast.ImportFrom) and node.module == "app.domain":
            imported.update({f"app.domain.{a.name}": {"*"} for a in node.names})
    # kg_attrs_contracts is dumped whole; every other dependency is listed by name.
    for module, names in imported.items():
        if module == "app.domain.kg_attrs_contracts":
            continue
        short = module.rsplit(".", 1)[-1]
        assert short in covered and names <= set(covered[short]), (module, names)


def test_normalize_fullwidth_and_whitespace():
    assert kg_attrs.normalize_latex("（Ｗ／Ｌ）") == "(w/l)"
    assert kg_attrs.normalize_latex("g _ m") == "g_m"
    # one space survives only between two ASCII letters/digits
    assert kg_attrs.normalize_latex("where  a\nis") == "where a is"


# --- formula context and prompt --------------------------------------------

def test_build_formula_context_truncates_in_order():
    out = kg_attrs.build_formula_context(("e1", "abcdef"), [("e2", "ghij"), ("e3", "zz")], max_chars=8)
    assert out == [("e1", "abcdef"), ("e2", "gh")]
    assert kg_attrs.build_formula_context(("e1", "  "), [("e2", "x")], max_chars=8) == [("e2", "x")]


def test_messages_carry_formula_and_context():
    messages = kg_attrs.formula_variables_messages("E=mc^2", [("e1", "where m is mass")])
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "E=mc^2" in messages[1]["content"] and "where m is mass" in messages[1]["content"]


@pytest.mark.parametrize("raw, ok", [
    ('{"variables": []}', True),
    ('{"variables": [{"symbol": "m", "meaning": "mass"}]}', True),
    ('{"variables": [{"symbol": "m", "meaning": "mass", "extra": 1}], "note": ""}', True),
    ('{}', False),
    ('{"vars": []}', False),
    ('{"variables": {}}', False),
    ('{"variables": [{"symbol": "m"}]}', False),
    ('{"variables": [{"symbol": null, "meaning": "mass"}]}', False),
    ('[]', False),
    ('not json', False),
])
def test_reply_validator_checks_shape_only(raw, ok):
    # mutation: reject the empty list -> "stated nothing" never gets cached.
    assert kg_attrs.formula_variables_reply_ok(raw) is ok


# --- verification -----------------------------------------------------------

def _verify(formula, texts, pairs, window=120):
    segments = [(f"el-{i}", t) for i, t in enumerate(texts)]
    reply = {"variables": [{"symbol": s, "meaning": m} for s, m in pairs]}
    return kg_attrs.verify_formula_variables(formula, segments, reply, pair_window=window)


def _reasons(result):
    return {r["symbol"]: r["reason"] for r in result.rejected}


def test_correct_pairs_kept_with_the_meaning_element():
    result = _verify("F = a + b", ["F = a + b", "where a is the acceleration, b is the bias"],
                     [("a", "the acceleration"), ("b", "the bias")])
    assert result.status == "ok"
    assert result.kept == (
        {"symbol": "a", "meaning": "the acceleration", "element_id": "el-1"},
        {"symbol": "b", "meaning": "the bias", "element_id": "el-1"},
    )


def test_swapped_pairs_are_rejected():
    # mutation: drop the nearest-symbol rule -> both swapped pairs kept.
    result = _verify("F = a + b", ["F = a + b", "where a is the acceleration, b is the bias"],
                     [("a", "the bias"), ("b", "the acceleration")])
    assert result.status == "none_grounded"
    assert _reasons(result) == {"a": "nearer_other_symbol", "b": "nearer_other_symbol"}


def test_symbol_inside_a_word_is_not_an_occurrence():
    result = _verify("y = a", ["the bias term"], [("a", "the bias term")])
    assert _reasons(result) == {"a": "symbol_not_in_context"}


@pytest.mark.parametrize("gap, kept", [(120, True), (121, False)])
def test_too_far_boundary(gap, kept):
    # mutation: off-by-one on the window.
    result = _verify("z", ["z" + "-" * gap + "foo"], [("z", "foo")], window=120)
    assert (result.status == "ok") is kept
    if not kept:
        assert _reasons(result) == {"z": "too_far"}


def test_segments_keep_a_boundary_between_them():
    # mutation: plain join -> "gainb" hides b; or offsets ignore the separator.
    result = _verify("a+b", ["where a is gain", "b is bias"], [("a", "gain"), ("b", "bias")])
    assert result.status == "ok"
    assert result.kept == ({"symbol": "a", "meaning": "gain", "element_id": "el-0"},
                           {"symbol": "b", "meaning": "bias", "element_id": "el-1"})
    # a meaning ending its segment is credited to that segment, not the next
    # (mutation: offsets that skip the separator drift one character per segment).
    tail = _verify("a+b", ["a+b", "where a is g", "b is h"], [("a", "g")])
    assert tail.kept == ({"symbol": "a", "meaning": "g", "element_id": "el-1"},)


def test_reject_reasons_in_check_order():
    result = _verify("x = y", ["x = y", "where y is height"],
                     [("q", "height"), ("x", "width"), ("y", "height")])
    assert _reasons(result) == {"q": "symbol_not_in_formula", "x": "meaning_not_in_context"}
    result = _verify("x = w", ["here height"], [("w", "height")])
    assert _reasons(result) == {"w": "symbol_not_in_context"}


def test_duplicate_symbol_first_wins():
    result = _verify("x = y", ["x = y", "where y is height"],
                     [("y", "height"), ("y", "is height")])
    assert [k["meaning"] for k in result.kept] == ["height"]
    assert _reasons(result) == {"y": "duplicate"}


def test_over_cap_truncates_in_order():
    n = contracts.VARIABLES_MAX + 1
    formula = "+".join(f"x_{{{i}}}" for i in range(n))
    context = ", ".join(f"x_{i} is term{i}q" for i in range(n))
    result = _verify(formula, [context], [(f"x_{i}", f"term{i}q") for i in range(n)])
    # mutation: drop everything past the cap -> the first 24 would vanish too.
    assert [k["symbol"] for k in result.kept] == [f"x_{i}" for i in range(contracts.VARIABLES_MAX)]
    assert _reasons(result) == {f"x_{n - 1}": "over_cap"}


def test_over_long_meaning_is_over_cap():
    meaning = "m" * (contracts.MEANING_CHARS_MAX + 1)
    result = _verify("y", [f"where y is {meaning}"], [("y", meaning)])
    assert _reasons(result) == {"y": "over_cap"}


def test_four_statuses():
    assert _verify("y", ["where y is height"], [("y", "height")]).status == "ok"
    assert _verify("y", ["where y is height"], []).status == "none_stated"
    assert _verify("y", ["where y is height"], [("y", "width")]).status == "none_grounded"
    failed = kg_attrs.verify_formula_variables("y", [("e", "t")], '{"vars": []}', pair_window=120)
    assert (failed.status, failed.kept, failed.rejected) == ("failed", (), ())
    assert set(contracts.FORMULA_STATUSES) == {"ok", "none_stated", "none_grounded", "failed"}


def test_verify_accepts_the_raw_reply_string():
    raw = '{"variables": [{"symbol": "y", "meaning": "height"}]}'
    result = kg_attrs.verify_formula_variables("y", [("e", "where y is height")], raw, pair_window=120)
    assert result.status == "ok"


# --- Chinese synthetic fixture ---------------------------------------------

_ZH_CASES = yaml.safe_load(_ZH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _ZH_CASES["aliases"], ids=lambda c: c["id"])
def test_zh_synthetic_aliases(case):
    items = [_item(t, f"el-{i}") for i, t in enumerate(case["texts"])]
    assert _trace(case["name"], items) == [(e["rule"], e["text"]) for e in case["expect"]]


@pytest.mark.parametrize("case", _ZH_CASES["formulas"], ids=lambda c: c["id"])
def test_zh_synthetic_formulas(case):
    segments = [(f"el-{i}", t) for i, t in enumerate(case["segments"])]
    result = kg_attrs.verify_formula_variables(
        case["formula"], segments, {"variables": case["reply"]}, pair_window=120)
    assert result.status == case["status"]
    assert [k["symbol"] for k in result.kept] == case["kept"]
    assert _reasons(result) == case["rejected"]


# --- hint and signature -----------------------------------------------------

def test_hint_is_the_literal_snapshotted_one():
    assert kg_attrs.FORMULA_VARIABLES_HINT == '{"variables":[{"symbol":"","meaning":""}]}'


def test_rules_signature_is_a_stable_sha256():
    first = kg_attrs.rules_signature()
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert kg_attrs.rules_signature() == first
