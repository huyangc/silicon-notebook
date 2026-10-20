"""Concept seed spaces, the stored-decision recompute and the squash candidate
tier — the pure half of S1 / P3 (design
docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md §4.4,
§五 S1 「实施细化」). No database: ``app.services.kg_merge`` only.

The database half (the switch inside a real rebuild, both backends) is
``tests/kg_seed_space_world.py``."""
from __future__ import annotations

import itertools
import random

import numpy as np
import pytest

from app.domain.kg_merge_seed import (
    CONCEPT_SEED_VERSION,
    OVERRIDE_SEED_PREFIX,
    _norm,
    concept_decision_seeds,
    concept_seed_in_space,
    decision_match_key,
    concept_seed_parts,
    concept_seed_v1,
    concept_seed_v2,
    concept_squash_key,
    published_concept_seed_version,
    seed_fn_for_type,
    seed_squash_key,
)
from app.services import kg_merge
from app.services.kg_merge import (
    MERGE_KIND_SIMILAR,
    MERGE_KIND_SPELLING,
    ConceptSeedSpace,
    StoredDecision,
    _discriminative_conflict,
    bridge_canonical_id,
    build_acronym_alias_map,
    build_acronym_alias_maps,
    merge_candidate_kind,
    merge_pending_tiers,
    override_keys,
    place_new_concepts,
    placement_seed_space,
    purge_bridge_canonical_ids,
    seed_or_unique,
    squash_pending_candidates,
    translate_v1_decisions,
)

# A corpus that exercises every branch of both normalizers: original
# separators, punctuation-made spaces, static aliases, paren initialisms (and
# a qualifier paren that is not one), full width, CJK, symbol-only names.
CORPUS = [
    "Q-Former", "Qformer", "Q Former", "Q_Former", "QFormer",
    "Llama 3.1", "Llama-3-1", "Llama 31", "Llama3.1", "llama 3 1",
    "RMS Norm", "RMSNorm", "rms-norm",
    "op-amp", "op amp", "opamp", "Op-Amp", "MOSFET", "mos", "MOS",
    "Metal Oxide Semiconductor (MOS)",
    "Mixture-of-Experts (MoE)", "Mixture of Experts", "MoE", "moe",
    "Compressed Sparse Attention (CSA)", "CSA", "Filter (LP)", "Filter (HP)",
    "ＲＭＳ Ｎｏｒｍ", "注意力·机制", "注意力 机制", "C++", "C", "I/O", "IO",
    "→", "☆", "", "first-order", "first order", "second order",
    "Grouped-query attention (GQA)", "GQA", "KV cache", "KV-Cache",
    "Type-N", "TypeN", "a.b(c)", "a b c",
]
VERSION_1, VERSION_2 = 1, 2


def _legacy_v1_seed(names, name, object_id):
    """Today's (pre-PR-4) rebuild seed of one concept: ``_norm`` +
    acronym redirect + ``seed_or_unique``, written out exactly as
    ``_stream_seed_reps`` composed it before the switch."""
    alias: dict = {}
    for n in names:
        m = kg_merge._PAREN_ACRONYM_RE.match(n or "")
        if not m:
            continue
        head, tok = m.group(1), m.group(2)
        if not kg_merge._ACRONYM_TOKEN_RE.match(tok) or not kg_merge._is_initialism_of(tok, head):
            continue
        a, e = _norm(tok), _norm(head)
        if not a or not e or a == e:
            continue
        if alias.get(a) is None or e < alias[a]:
            alias[a] = e
    nm = _norm(name)
    return seed_or_unique(alias.get(nm, nm), object_id)


# ------------------------------------------------------------ seed spaces

def test_code_builds_in_seed_space_2():
    assert CONCEPT_SEED_VERSION == 2


def test_space_1_is_byte_for_byte_the_legacy_rebuild_seed():
    """Space 1 must keep minting exactly today's seeds: a library that is not
    rebuilt yet places new objects in its published v1 clusters."""
    space = ConceptSeedSpace.from_names(CORPUS, version=VERSION_1)
    for index, name in enumerate(CORPUS):
        oid = f"o{index}"
        assert space.seed({"name": name}, oid) == _legacy_v1_seed(CORPUS, name, oid), name


def test_space_2_seed_is_the_v2_seed_with_the_v2_acronym_redirect():
    space = ConceptSeedSpace.from_names(CORPUS, version=VERSION_2)
    alias = build_acronym_alias_map(CORPUS, seed_version=2)
    for index, name in enumerate(CORPUS):
        oid = f"o{index}"
        v2 = concept_seed_v2(name)
        assert space.seed({"name": name}, oid) == seed_or_unique(alias.get(v2, v2), oid), name


def test_concept_seed_parts_is_the_v2_seed_and_the_v1_squash():
    for name in CORPUS:
        seed, squashed = concept_seed_parts(name)
        assert seed == concept_seed_v2(name)
        # The squashed pre-alias text equals the v1 form without its spaces
        # whenever no static alias rewrote the v1 form.
        if _norm(name) not in kg_merge._ALIASES.values():
            assert squashed == _norm(name).replace(" ", ""), name


def test_space_2_merges_original_separators_but_keeps_punctuation_spaces():
    space = ConceptSeedSpace.from_names(CORPUS, version=VERSION_2)
    seed = lambda n: space.seed({"name": n}, "x")  # noqa: E731
    assert seed("Q-Former") == seed("Qformer") == seed("Q Former") == seed("Q_Former") == "qformer"
    assert seed("Llama 3.1") == "llama3 1"
    assert seed("Llama 31") == seed("Llama-3-1") == "llama31"
    assert seed("Llama 3.1") != seed("Llama 31")       # left to a human (F4)
    assert seed("MoE") == seed("Mixture-of-Experts (MoE)") == "mixtureofexperts"
    assert seed("MOSFET") == seed("mos") == seed("MOS")
    # ... which the corpus' "Metal Oxide Semiconductor (MOS)" redirects onto
    # its expansion, the acronym alias matched on words and squashed like v2.
    assert seed("MOS") == "metaloxidesemiconductor"
    assert seed("C++") != seed("C") and seed("I/O") != seed("IO")


def test_alias_maps_in_one_pass_equal_one_map_per_version():
    both = build_acronym_alias_maps(iter(CORPUS), (2, 1))
    assert both[1] == build_acronym_alias_map(CORPUS, seed_version=1)
    assert both[2] == build_acronym_alias_map(CORPUS, seed_version=2)
    assert both[2]["moe"] == "mixtureofexperts"
    assert both[1]["moe"] == "mixture of experts"


def test_seed_versions_are_never_defaulted():
    with pytest.raises(ValueError):
        ConceptSeedSpace.from_names(CORPUS, version=3)
    with pytest.raises(ValueError):
        bridge_canonical_id("x", "o", seed_version=0)
    with pytest.raises(TypeError):
        bridge_canonical_id("x", "o")   # the version is keyword-only and required
    with pytest.raises(TypeError):
        build_acronym_alias_map(CORPUS)


def test_override_keeps_the_v1_seed_and_tracking_reports_it():
    space = ConceptSeedSpace.from_names(
        CORPUS, version=VERSION_2, override={"llama 3 1"}, track={"llama 3 1", "qformer"})
    assert space.seed_and_key({"name": "Llama 3.1"}, "a") == ("~v1:llama 3 1", "llama 3 1")
    assert space.seed_and_key({"name": "Llama-3-1"}, "b") == ("~v1:llama 3 1", "llama 3 1")
    assert space.seed_and_key({"name": "Qformer"}, "c") == ("qformer", "qformer")
    # "Q Former" has v1 seed "q former": not tracked, not overridden.
    assert space.seed_and_key({"name": "Q Former"}, "d") == ("qformer", None)
    assert space.seed_and_key({"name": "Llama 31"}, "e") == ("llama31", None)


def test_override_and_track_need_the_v1_alias_map():
    with pytest.raises(ValueError):
        ConceptSeedSpace(2, {}, override={"x"})


def test_v1_probe_never_misses_a_key():
    """The cheap pre-test (squashed pre-alias text in the probe) is a superset
    test: brute force over every (name, key) of the corpus — keys taken from
    the corpus' own v1 seeds, acronym and static alias targets included."""
    keys_universe = sorted({_legacy_v1_seed(CORPUS, n, f"o{i}") for i, n in enumerate(CORPUS)})
    rng = random.Random(7)
    for _ in range(60):
        keys = frozenset(rng.sample(keys_universe, k=rng.randint(1, 6)))
        space = ConceptSeedSpace.from_names(CORPUS, version=VERSION_2, track=keys)
        for index, name in enumerate(CORPUS):
            oid = f"o{index}"
            v1 = _legacy_v1_seed(CORPUS, name, oid)
            _seed, key = space.seed_and_key({"name": name}, oid)
            assert key == (v1 if v1 in keys else None), (name, keys)


def test_space_without_keys_never_computes_a_v1_seed(monkeypatch):
    calls = []
    real = kg_merge._norm

    def spy(name):
        calls.append(name)
        return real(name)

    space = ConceptSeedSpace.from_names(CORPUS, version=VERSION_2)
    monkeypatch.setattr(kg_merge, "_norm", spy)
    for index, name in enumerate(CORPUS):
        space.seed({"name": name}, f"o{index}")
    assert calls == []


def test_discriminative_conflict_guard_still_reads_words():
    """``_norm`` is untouched: the contrast-word guard keeps seeing tokens."""
    assert _discriminative_conflict("first order filter", "second order filter")
    assert _discriminative_conflict("Low-pass", "High-pass")
    assert _norm("first order filter") == "first order filter"


def test_other_types_have_no_seed_version():
    for object_type in ("claim", "formula", "procedure"):
        assert seed_fn_for_type(object_type, concept_seed_version=1) is \
            seed_fn_for_type(object_type, concept_seed_version=2)
    assert seed_fn_for_type("concept", concept_seed_version=1)({"name": "Q-Former"}) == "q former"
    assert seed_fn_for_type("concept", concept_seed_version=2)({"name": "Q-Former"}) == "qformer"


def test_published_version_of_a_missing_state_row_is_1():
    assert published_concept_seed_version(None) == 1
    assert published_concept_seed_version({"concept_seed_version": 2}) == 2
    with pytest.raises(ValueError):
        published_concept_seed_version({"concept_seed_version": 7})


# ----------------------------------------------------- placement / bridges

def test_placement_follows_the_published_space():
    new = [{"object_id": "n1", "name": "Q-Former"}, {"object_id": "n2", "name": "→"}]
    canon = {"K-q former": "Q Former", "K-qformer": "Qformer"}
    v1 = place_new_concepts(new, canon, id_prefix="K-", seed_space=placement_seed_space(
        "concept", new, canon, concept_seed_version=1))
    v2 = place_new_concepts(new, canon, id_prefix="K-", seed_space=placement_seed_space(
        "concept", new, canon, concept_seed_version=2))
    assert [r["canonical_id"] for r in v1] == ["K-q former", "K-~n2"]
    assert [r["canonical_id"] for r in v2] == ["K-qformer", "K-~n2"]
    assert v1[0]["canonical_name"] == "Q Former" and v2[0]["canonical_name"] == "Qformer"


def test_placement_applies_the_override():
    new = [{"object_id": "n1", "name": "Llama 3.1"}]
    rows = place_new_concepts(new, {}, id_prefix="K-", seed_space=placement_seed_space(
        "concept", new, {}, concept_seed_version=2, override={"llama 3 1"}))
    assert rows[0]["canonical_id"] == "K-~v1:llama 3 1"


def test_bridge_ids_per_space_and_override():
    assert bridge_canonical_id("Q-Former", "o", seed_version=1) == "K-q former"
    assert bridge_canonical_id("Q-Former", "o", seed_version=2) == "K-qformer"
    assert bridge_canonical_id("Q-Former", "o", seed_version=2,
                               override=frozenset({"q former"})) == "K-~v1:q former"
    assert bridge_canonical_id("→", "o", seed_version=2) == "K-~o"
    # Bridge ids never follow the acronym redirect (unchanged contract).
    assert bridge_canonical_id("MoE", "o", seed_version=2) == "K-moe"


def test_purge_computes_every_form_per_space():
    forms = purge_bridge_canonical_ids([
        {"object_id": "o1", "object_type": "concept", "name": "Q-Former"},
        {"object_id": "o2", "object_type": "claim", "name": "Q-Former is a module"},
        {"object_id": "o3", "object_type": "concept", "name": ""},
    ])
    assert forms == {1: ["K-q former", "K-~o3"],
                     2: ["K-qformer", "K-~o3", "K-~v1:q former", "K-~v1:~o3"]}


def test_decision_seeds_cover_both_spaces_and_the_override():
    assert concept_decision_seeds("Q-Former") == {"q former", "qformer"}
    assert concept_decision_seeds("") == frozenset()
    # An override seed names the members of its v1 seed.
    assert decision_match_key("~v1:q former") == "q former"
    assert decision_match_key("qformer") == "qformer"


def test_the_read_side_also_asks_for_override_keys():
    """The findings page reads the stored decisions of an F4 group by its
    member seeds; a decision the recompute kept on the old seed is keyed
    ``~v1:<seed>``, so the bound list carries that form too."""
    from app.services.kg_findings_view import decision_lookup_seeds

    assert decision_lookup_seeds({"K-a": {"llama 3 1", "llama3 1"}, "K-b": {"llama31"}}) == [
        "llama 3 1", "llama3 1", "llama31",
        "~v1:llama 3 1", "~v1:llama3 1", "~v1:llama31"]
    assert decision_lookup_seeds({}) == []


def test_object_level_dedup_seed_follows_the_space_and_the_override():
    """Promotion dedup into a public library compares seeds in the base's
    published space, with its override keys (no acronym redirect)."""
    v1 = seed_fn_for_type("concept", concept_seed_version=1)
    v2 = seed_fn_for_type("concept", concept_seed_version=2)
    kept = seed_fn_for_type("concept", concept_seed_version=2, override={"q former"})
    assert (v1({"name": "Q-Former"}), v2({"name": "Q-Former"})) == ("q former", "qformer")
    assert kept({"name": "Q Former"}) == "~v1:q former"
    assert kept({"name": "Qformer"}) == "qformer"
    assert concept_seed_in_space("Q-Former", 1, {"q former"}) == "q former"  # space 1: no override


def test_override_keys_are_read_off_v2_decisions():
    decided = {frozenset(("~v1:q former", "~v1:qformer")): "rejected",
               frozenset(("a", "b")): "confirmed"}
    assert override_keys(decided) == {"q former", "qformer"}


# ---------------------------------------------------- decision recompute

def _rows(*rows):
    return [StoredDecision(f"r{i}", a, b, st) for i, (a, b, st) in enumerate(rows)]


def test_a_split_confirmed_group_is_joined_back_by_every_pair():
    """"Llama 3.1" and "Llama-3-1" shared v1 seed ``llama 3 1``; S1 splits it.
    A confirmed decision with "Meta Llama" becomes BOTH pairs, so the two new
    seeds stay in one component (only the first pair would split them)."""
    plan = translate_v1_decisions(
        _rows(("llama 3 1", "meta llama", "confirmed")),
        {"llama 3 1": {"llama3 1", "llama31"}, "meta llama": {"metallama"}})
    assert plan.pairs == {"r0": [("llama3 1", "metallama"), ("llama31", "metallama")]}
    assert plan.override == frozenset() and plan.suspended == []
    assert plan.decided_pairs() == {
        frozenset(("llama3 1", "metallama")): "confirmed",
        frozenset(("llama31", "metallama")): "confirmed",
    }


def test_new_seeds_come_from_the_names_not_from_the_v1_string():
    """Deriving the v2 seed from the v1 key string (drop its spaces) would map
    ``llama 3 1`` to ``llama31`` only and lose "Llama 3.1" (``llama3 1``)."""
    names = ["Llama 3.1", "Llama-3-1"]
    space = ConceptSeedSpace.from_names(names, version=2, track={"llama 3 1"})
    seeds_of_key: dict = {}
    for i, n in enumerate(names):
        seed, key = space.seed_and_key({"name": n}, f"o{i}")
        seeds_of_key.setdefault(key, set()).add(seed)
    assert seeds_of_key == {"llama 3 1": {"llama3 1", "llama31"}}
    assert {"llama 3 1".replace(" ", "")} != seeds_of_key["llama 3 1"]


def test_a_rejection_the_new_seeds_would_collapse_keeps_the_old_seeds():
    plan = translate_v1_decisions(
        _rows(("q former", "qformer", "rejected"), ("x", "y", "deferred")),
        {"q former": {"qformer"}, "qformer": {"qformer"}, "x": {"x2"}, "y": {"y2"}})
    assert plan.override == {"q former", "qformer"}
    assert plan.pairs["r0"] == [("~v1:q former", "~v1:qformer")]
    assert plan.pairs["r1"] == [("x2", "y2")]
    assert plan.unresolved == 0


def test_a_collision_through_a_confirmed_chain_is_resolved_too():
    """Rejected (a, b); confirmed (a, c); c's new seed equals b's: a joins b
    through c. Overriding the rejected pair's keys separates them."""
    plan = translate_v1_decisions(
        _rows(("a", "b", "rejected"), ("a", "c", "confirmed")),
        {"a": {"a2"}, "b": {"bc"}, "c": {"bc"}})
    assert {"a", "b"} <= plan.override
    uf = kg_merge._UF({s for ps in plan.pairs.values() for p in ps for s in p})
    for a, b in plan.pairs["r1"]:
        uf.union(a, b)
    a_side, b_side = plan.pairs["r0"][0]
    assert uf.find(a_side) != uf.find(b_side)


def test_when_both_keys_are_overridden_the_component_keys_follow():
    """A rejection still collapsed after its own keys were overridden: an
    existing v2 confirmed pair joins ~v1:a to the seed key c lands on, and c
    to ~v1:b — the keys in that component join the override."""
    plan = translate_v1_decisions(
        _rows(("a", "b", "rejected"), ("c", "d", "rejected")),
        {"a": {"s"}, "b": {"s"}, "c": {"k"}, "d": {"m"}},
        override={"a", "b"},
        v2_confirmed=[frozenset(("~v1:a", "k")), frozenset(("k", "~v1:b"))])
    assert "c" in plan.override
    # c is overridden now, but the stored v2 confirmed pairs still chain
    # ~v1:a to ~v1:b: confirmed stays authoritative — reported, not looped.
    assert plan.unresolved == 1


def test_a_rejection_already_contradicted_in_space_1_is_left_alone():
    plan = translate_v1_decisions(
        _rows(("a", "b", "confirmed"), ("a", "b", "rejected")),
        {"a": {"s"}, "b": {"s"}})
    assert plan.override == frozenset() and plan.unresolved == 0


def test_rows_whose_objects_are_gone_are_suspended():
    plan = translate_v1_decisions(
        _rows(("gone", "b", "rejected"), ("a", "b", "confirmed")),
        {"a": {"a2"}, "b": {"b2"}})
    assert plan.suspended == ["r0"]
    assert set(plan.pairs) == {"r1"} and set(plan.rows) == {"r1"}


def test_a_confirmed_pair_that_merged_into_one_seed_keeps_one_self_pair():
    plan = translate_v1_decisions(
        _rows(("q former", "qformer", "confirmed")),
        {"q former": {"qformer"}, "qformer": {"qformer"}})
    assert plan.pairs == {"r0": [("qformer", "qformer")]}
    assert plan.decided_pairs() == {frozenset(("qformer",)): "confirmed"}


def test_pending_rows_are_not_translated():
    plan = translate_v1_decisions(_rows(("a", "b", "pending")), {"a": {"a"}, "b": {"b"}})
    assert plan.pairs == {} and plan.suspended == []


def test_translation_terminates_with_every_key_overridden():
    """Worst case: every rejection collides; the override grows until every
    key keeps its v1 seed, which is exactly the v1 world."""
    keys = [f"k{i}" for i in range(8)]
    rows = [StoredDecision(f"r{i}", a, b, "rejected")
            for i, (a, b) in enumerate(itertools.combinations(keys, 2))]
    plan = translate_v1_decisions(rows, {k: {"same"} for k in keys})
    assert plan.override == set(keys) and plan.unresolved == 0


# ------------------------------------------------------------ squash tier

def _vec(*values):
    return np.asarray(values, dtype=np.float32)


def test_squash_tier_stars_from_the_biggest_canonical():
    seed_to_canonical = {
        "llama3 1": "K-llama3 1", "llama31": "K-llama31", "llama 3 1x": "K-other",
        "llama3 1 ": "K-llama3 1",   # never mind; a second seed of the anchor
        "qformer": "K-qformer", "~v1:llama 31": "K-~v1:llama 31", "~o9": "K-~o9",
        "solo": "K-solo",
    }
    members = {"llama3 1": 5, "llama3 1 ": 1, "llama31": 3, "qformer": 4,
               "~v1:llama 31": 9}
    reps = {"llama3 1": _vec(1, 0), "llama31": _vec(1, 1)}
    rows, total = squash_pending_candidates(seed_to_canonical, members, reps, limit=10)
    assert total == 1
    assert rows == [("llama3 1", "llama31", "K-llama3 1", "K-llama31",
                     pytest.approx(1 / np.sqrt(2)))]


def test_squash_tier_scores_zero_without_vectors_and_reports_the_cap():
    seed_to_canonical = {"a 1": "K-a", "a1": "K-b",
                         "ab 2": "K-c", "ab2": "K-d", "a b2": "K-e"}
    members = {"a 1": 2, "a1": 1, "ab 2": 1, "ab2": 3, "a b2": 1}
    rows, total = squash_pending_candidates(seed_to_canonical, members, {}, limit=2)
    assert total == 3
    # heaviest group first (ab2: 5 members), anchor = most members (K-d),
    # the other two by members, then by id
    assert rows == [("ab2", "ab 2", "K-d", "K-c", 0.0), ("ab2", "a b2", "K-d", "K-e", 0.0)]


def test_squash_tier_skips_canonicals_already_merged():
    rows, total = squash_pending_candidates(
        {"a 1": "K-a", "a1": "K-a"}, {"a 1": 1, "a1": 1}, {}, limit=10)
    assert (rows, total) == ([], 0)


def test_tiers_merge_one_row_per_canonical_pair():
    ann = [("x", "y", "K-x", "K-y", 0.9), ("p", "q", "K-p", "K-q", 0.85)]
    squash = [("q 1", "q1", "K-q", "K-p", 0.4), ("m 1", "m1", "K-m", "K-n", 0.0)]
    merged = merge_pending_tiers(ann, squash)
    assert merged == [("x", "y", "K-x", "K-y", 0.9),
                      ("q 1", "q1", "K-p", "K-q", 0.85),
                      ("m 1", "m1", "K-m", "K-n", 0.0)]


def test_kind_reads_the_decision_keys():
    assert merge_candidate_kind({"seed_a": "llama3 1", "seed_b": "llama31",
                                 "canonical_a": "K-x", "canonical_b": "K-y"}) == MERGE_KIND_SPELLING
    # v1 seeds squash to the same key as the v2 seeds of the same names
    assert merge_candidate_kind({"seed_a": "", "seed_b": "",
                                 "canonical_a": "K-llama 3 1", "canonical_b": "K-llama 31"}) \
        == MERGE_KIND_SPELLING
    assert merge_candidate_kind({"seed_a": "transformer", "seed_b": "attention",
                                 "canonical_a": "K-t", "canonical_b": "K-a"}) == MERGE_KIND_SIMILAR
    assert merge_candidate_kind({"seed_a": "~v1:a b", "seed_b": "ab",
                                 "canonical_a": "K-x", "canonical_b": "K-y"}) == MERGE_KIND_SIMILAR


def test_squash_key_of_a_seed_equals_the_squash_key_of_its_names():
    for name in CORPUS:
        assert seed_squash_key(concept_seed_v2(name)) == concept_squash_key(name)
    assert seed_squash_key(OVERRIDE_SEED_PREFIX + "a b") == ""
    assert seed_squash_key("~o1") == ""


def test_v1_seed_function_is_unchanged():
    assert concept_seed_v1("Q-Former") == "q former"
