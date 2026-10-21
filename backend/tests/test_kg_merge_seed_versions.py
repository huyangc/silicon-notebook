"""Concept seed spaces v1 / v2 and the squash key (KG findings design 2026-10-19,
§4.4 + §五 S1). Pure functions, no database.

What is pinned:
  · v1 is FROZEN — byte-for-byte today's ``seed_concept`` / ``_norm`` (golden
    values; every stored merge decision and ``K-`` id lives in this space);
  · v2 drops the ORIGINAL whitespace / hyphen / underscore BEFORE punctuation
    becomes a space, so "Q-Former" = "Qformer" but "Llama 3.1" ≠ "Llama 31";
  · the squash key is v2 without spaces and keeps ``+`` / ``/``;
  · ``_norm`` keeps its spaces, so the contrast-word guard
    ``_discriminative_conflict`` still sees two tokens to compare;
  · the claim / formula / procedure seeds are untouched.
"""
import unicodedata

import pytest

from app.domain.kg_merge_seed import (
    _ALIASES,
    _concept_seed_v1_unaliased,
    _norm,
    concept_seed_v1,
    concept_seed_v2,
    concept_squash_key,
    seed_claim,
    seed_concept,
    seed_formula,
    seed_procedure,
)
from app.services.kg_merge import _discriminative_conflict

# Golden v1 outputs, recorded from ``seed_concept`` as it stands on 2026-10-19.
_V1_GOLDEN = {
    "Q-Former": "q former",
    "Qformer": "qformer",
    "Llama 3.1": "llama 3 1",
    "Llama 31": "llama 31",
    "Llama-3-1": "llama 3 1",
    "RMS Norm": "rms norm",
    "RMSNorm": "rmsnorm",
    "op-amp": "op amp",
    "opamp": "op amp",
    "PLL": "phase locked loop",
    "Mixture-of-Experts (MoE)": "mixture of experts",
    "MoE": "moe",
    "Filter (LP)": "filter lp",
    "ＲＭＳ Ｎｏｒｍ": "rms norm",
    "注意力 机制": "注意力 机制",
    "注意力·机制": "注意力 机制",
    "snake_case name": "snake case name",
    "C++": "c++",
    "I/O": "i/o",
    "  Spaced   Out  ": "spaced out",
    "": "",
}

_CORPUS = sorted(set(_V1_GOLDEN) | {
    "Ring attention", "RingAttention", "Long-Former", "Longformer", "No PE", "NoPE",
    "L l a m a 3 . 1 - M i n i t ro n - 4 B", "Llama-3.1-Minitron-4B",
    "Llama-3.1-8B-Instruct", "Llama3.1-8B-Instruct", "Theorem QA", "TheoremQA",
    "VCO", "voltage-controlled oscillator", "MOSFET", "BJT", "LNA", "Op Amp",
    "Compressed Sparse Attention (CSA)", "Channel (n)", "Type (v3)", "Ada-KV",
    "AdaKV", "low pass filter", "high pass filter", "ＡＢＣ１２３", "（全角）括号",
    "a_b-c d", "x.y.z", "x..y", "Qwen-2 72B", "Qwen2-72B",
})


@pytest.mark.parametrize("name, expected", sorted(_V1_GOLDEN.items()))
def test_v1_is_frozen_to_todays_seed(name, expected):
    assert concept_seed_v1(name) == expected
    assert seed_concept(name) == expected
    assert seed_concept({"name": name}) == expected


def test_v1_is_norm_on_the_whole_corpus():
    for name in _CORPUS:
        assert concept_seed_v1(name) == _norm(name) == seed_concept(name), name


def test_v1_unaliased_form_is_norm_before_aliases():
    """v2 looks aliases up on the pre-alias v1 form; that helper must be exactly
    ``_norm`` minus the alias table (``_norm`` itself is deliberately untouched)."""
    for name in _CORPUS:
        unaliased = _concept_seed_v1_unaliased(name)
        assert _ALIASES.get(unaliased, unaliased) == _norm(name), name


def _reference_v2(name: str) -> str:
    """v2 written the obvious way: alias lookup on the v1 form FIRST, always."""
    import re

    unaliased = _concept_seed_v1_unaliased(name)
    if unaliased in _ALIASES:
        return _ALIASES[unaliased].replace(" ", "")
    text = _strip_paren_acronym_for_test(unicodedata.normalize("NFKC", name)).strip().lower()
    text = re.sub(r"[\s\-_]+", "", text)
    text = re.sub(r"[^\w+/ ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_paren_acronym_for_test(name: str) -> str:
    from app.domain.kg_merge_seed import _strip_paren_acronym

    return _strip_paren_acronym(name)


def test_v2_lazy_alias_check_equals_the_eager_reference():
    """v2 only builds the v1 form when the space-free v2 text could be an alias
    key (a speed-up for the millions of names the findings pass sees). It must
    give exactly what the eager definition gives."""
    import random

    alias_spellings = []
    for key in _ALIASES:
        for joiner in (" ", "-", "_", ".", "·", "", " . ", "（", "\t"):
            alias_spellings.append(key.replace(" ", joiner).upper())
            alias_spellings.append(joiner.join(key))
    rng = random.Random(7)
    alphabet = "opamvcolnbjtsf ._-·()/+（）ＯＰ\t"
    noise = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
             for _ in range(3000)]
    for name in _CORPUS + alias_spellings + noise:
        assert concept_seed_v2(name) == _reference_v2(name), repr(name)


def test_q_former_and_qformer_share_one_v2_seed():
    assert concept_seed_v1("Q-Former") != concept_seed_v1("Qformer")
    assert concept_seed_v2("Q-Former") == concept_seed_v2("Qformer") == "qformer"
    assert concept_seed_v2("Q Former") == "qformer"


def test_v2_drops_original_separators_before_punctuation():
    """The order is the whole point: the space a dot leaves stays a boundary."""
    assert concept_seed_v2("Llama 3.1") == "llama3 1"
    assert concept_seed_v2("Llama 31") == "llama31"
    assert concept_seed_v2("Llama 3.1") != concept_seed_v2("Llama 31")
    # …but they do share the squash key: that is F4's wide half, for a human.
    assert concept_squash_key("Llama 3.1") == concept_squash_key("Llama 31") == "llama31"


def test_v2_merges_original_hyphens_into_the_unspaced_spelling():
    """"Llama-3-1" and "Llama 3.1" share a v1 seed, but v2 splits them: the
    hyphen is original text (removed), the dot is punctuation (kept as space)."""
    assert concept_seed_v1("Llama-3-1") == concept_seed_v1("Llama 3.1")
    assert concept_seed_v2("Llama-3-1") == concept_seed_v2("Llama 31") == "llama31"
    assert concept_seed_v2("Llama-3-1") != concept_seed_v2("Llama 3.1")


def test_op_amp_aliases_are_looked_up_on_the_v1_form_and_squashed():
    for name in ("op-amp", "opamp", "Op Amp", "OP AMP", "op_amp"):
        assert concept_seed_v2(name) == "opamp", name
    # Alias VALUES lose their spaces too, so the full spelling meets the acronym.
    assert concept_seed_v2("PLL") == concept_seed_v2("Phase-Locked Loop") == "phaselockedloop"
    assert concept_seed_v2("MOSFET") == concept_seed_v2("MOS transistor") == "mostransistor"


def test_moe_initialism_is_still_stripped_before_v2():
    assert concept_seed_v2("Mixture-of-Experts (MoE)") == "mixtureofexperts"
    assert concept_seed_v2("Mixture of Experts") == "mixtureofexperts"
    assert concept_seed_v2("MoE") == "moe"
    # A qualifier paren is not an initialism: it stays, as its own token.
    assert concept_seed_v2("Filter (LP)") == "filter lp"
    assert concept_seed_v2("Filter (LP)") != concept_seed_v2("Filter (HP)")


def test_full_width_names_fold_before_v2():
    assert concept_seed_v2("ＲＭＳ Ｎｏｒｍ") == concept_seed_v2("RMSNorm") == "rmsnorm"
    assert concept_seed_v2("ＡＢＣ１２３") == "abc123"
    assert concept_seed_v2("ＲＭＳ　Ｎｏｒｍ") == "rmsnorm"     # full-width space


def test_cjk_names_keep_their_characters():
    assert concept_seed_v2("注意力 机制") == concept_seed_v2("注意力机制") == "注意力机制"
    # A middle dot is punctuation: v2 keeps the boundary, the squash key does not.
    assert concept_seed_v2("注意力·机制") == "注意力 机制"
    assert concept_squash_key("注意力·机制") == "注意力机制"
    assert concept_seed_v2("（全角）括号") == unicodedata.normalize("NFKC", "全角 括号")


def test_squash_key_keeps_plus_and_slash():
    assert concept_squash_key("C++") == "c++" != concept_squash_key("C")
    assert concept_squash_key("I/O") == "i/o" != concept_squash_key("IO")
    assert concept_squash_key("x.y.z") == "xyz"


def test_norm_still_keeps_spaces_for_the_contrast_word_guard():
    """S1 lives in v2 only. Squashing spaces inside ``_norm`` would make every
    multi-word name one token, and "only one contrast word differs" would never
    fire again."""
    assert _norm("low pass filter") == "low pass filter"
    assert _discriminative_conflict("low pass filter", "high pass filter")
    assert _discriminative_conflict("first stage amplifier", "second stage amplifier")
    assert not _discriminative_conflict("low pass filter", "low pass filter design")


def test_claim_formula_procedure_seeds_are_untouched():
    assert seed_claim("The KV-cache, it grows!") == "the kv cache it grows"
    assert seed_claim({"name": "  A  b. "}) == "a b"
    assert seed_formula("E = m c ^ 2") == "e=mc^2"
    assert seed_formula({"name": "X + Y"}) == "x+y"
    assert seed_procedure("Train-the Model") == "train the model"
    assert seed_procedure({
        "name": "Fine tune",
        "payload": {"steps": [{"name": "Load data."}, {"name": "Train!"}, {"x": 1}]},
    }) == "fine tune#load data|train"
