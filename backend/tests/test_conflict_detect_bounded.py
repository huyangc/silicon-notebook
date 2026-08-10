"""Bounded conflict detection — Strategy 3 equivalence + Strategy 4 dispatch.

Strategy 3's inverted-index rewrite claims to be a *lossless* replacement for the
old O(N²) double loop: same candidate set, same order, same cap semantics.  That
claim is only worth as much as an oracle test, so the pre-rewrite loop is kept
verbatim in this file and both are run over randomised contrast-group names.

Strategy 4's ANN branch is a deliberate approximation, so it is tested for what
it must guarantee instead: high-similarity pairs surface, low-similarity ones do
not, and the sparsity cap still holds.
"""
from __future__ import annotations

import random

from app.services.kg import conflict_detect
from app.services.kg.conflict_detect import detect_conflict_candidates
from app.services.kg_merge import _CONTRAST_GROUPS, _discriminative_conflict


# ---------------------------------------------------------------------------
# Oracle: the pre-rewrite Strategy 3 loop, copied verbatim
# ---------------------------------------------------------------------------

def _legacy_discriminative_pairs(node_objects, max_pairs=200):
    """The original double loop, emitting (left_ref, right_ref) in discovery order.

    Mirrors the old code exactly, including its dedup-by-unordered-pair and the
    fact that ``disc_count`` advances on every accepted pair (not on every
    *emitted* candidate).
    """
    out = []
    seen = set()
    disc_count = 0
    for i in range(len(node_objects)):
        if disc_count >= max_pairs:
            break
        oi = node_objects[i]
        pi = oi.get("payload") or {}
        name_i = pi.get("name", "") if isinstance(pi, dict) else ""
        for j in range(i + 1, len(node_objects)):
            if disc_count >= max_pairs:
                break
            oj = node_objects[j]
            pj = oj.get("payload") or {}
            name_j = pj.get("name", "") if isinstance(pj, dict) else ""
            if not _discriminative_conflict(name_i, name_j):
                continue
            a, b = oi["id"], oj["id"]
            key = frozenset([a, b])
            if key not in seen and a != b:
                seen.add(key)
                out.append((a, b) if a < b else (b, a))
            disc_count += 1
    return out


def _emitted_discriminative(objects):
    """Run the production detector and project its discriminative candidates."""
    candidates = detect_conflict_candidates(objects, [])
    return [
        (c["left_ref"], c["right_ref"])
        for c in candidates
        if c["kind"] == "node" and c["signal"] == "discriminative"
    ]


# ---------------------------------------------------------------------------
# Random corpus generator
# ---------------------------------------------------------------------------

_STEMS = [
    "transistor", "amplifier", "bias network", "clock tree", "current mirror",
    "ring oscillator", "reference buffer", "sense amp", "level shifter",
]


def _random_objects(count, seed, *, contrast_ratio=0.7):
    rng = random.Random(seed)
    objects = []
    for index in range(count):
        stem = rng.choice(_STEMS)
        if rng.random() < contrast_ratio:
            group = rng.choice(_CONTRAST_GROUPS)
            token = rng.choice(sorted(group))
            # Vary token position so the remainder multiset — not word order —
            # is what decides bucket membership.
            name = f"{token} {stem}" if rng.random() < 0.5 else f"{stem} {token}"
        else:
            name = f"{stem} {rng.randint(0, 3)}"
        objects.append({
            "id": f"o{index:04d}",
            "object_type": "concept" if index % 2 else "claim",
            "payload": {"name": name},
        })
    return objects


# ---------------------------------------------------------------------------
# Strategy 3 — equivalence with the pre-rewrite loop
# ---------------------------------------------------------------------------

class TestDiscriminativeEquivalence:
    def test_matches_legacy_loop_on_a_dense_random_corpus(self):
        objects = _random_objects(240, seed=20260810)
        legacy = _legacy_discriminative_pairs(objects)
        assert legacy, "fixture must actually produce discriminative pairs"
        assert _emitted_discriminative(objects) == legacy

    def test_matches_legacy_loop_across_many_seeds(self):
        for seed in range(12):
            objects = _random_objects(120, seed=seed)
            assert _emitted_discriminative(objects) == (
                _legacy_discriminative_pairs(objects)
            ), f"divergence at seed {seed}"

    def test_matches_legacy_loop_when_the_cap_bites(self):
        # One shared remainder ⇒ every nmos×pmos pair qualifies (30×30 = 900),
        # far past the 200 sparsity cap, so the emission ORDER — not just the
        # set — is what this assertion pins.
        objects = [
            {"id": f"o{index:03d}", "object_type": "claim",
             "payload": {"name": f"{'nmos' if index % 2 else 'pmos'} widget"}}
            for index in range(60)
        ]
        uncapped = _legacy_discriminative_pairs(objects, max_pairs=10 ** 9)
        assert len(uncapped) == 900, "fixture must exceed the sparsity cap"
        legacy = _legacy_discriminative_pairs(objects)
        assert len(legacy) == 200, "the cap must bite"
        assert _emitted_discriminative(objects) == legacy

    def test_matches_legacy_loop_with_no_contrast_tokens(self):
        objects = _random_objects(200, seed=3, contrast_ratio=0.0)
        assert _legacy_discriminative_pairs(objects) == []
        assert _emitted_discriminative(objects) == []

    def test_identical_names_are_not_conflicts(self):
        objects = [
            {"id": f"o{i}", "object_type": "concept",
             "payload": {"name": "nmos transistor"}}
            for i in range(50)
        ]
        assert _emitted_discriminative(objects) == []

    def test_multiword_remainders_and_repeated_tokens_agree(self):
        names = [
            "input input buffer", "output input buffer", "input output buffer",
            "input buffer input", "nmos pmos device", "pmos pmos device",
            "upper lower rail", "lower lower rail",
        ]
        objects = [
            {"id": f"o{i}", "object_type": "claim", "payload": {"name": name}}
            for i, name in enumerate(names)
        ]
        assert _emitted_discriminative(objects) == (
            _legacy_discriminative_pairs(objects)
        )

    def test_large_zero_hit_corpus_does_no_quadratic_predicate_work(self, monkeypatch):
        """The whole point: a big non-matching corpus must not be N² compared."""
        calls = {"n": 0}
        real = conflict_detect._discriminative_conflict

        def _counting(name_a, name_b):
            calls["n"] += 1
            return real(name_a, name_b)

        monkeypatch.setattr(conflict_detect, "_discriminative_conflict", _counting)
        objects = _random_objects(1200, seed=11, contrast_ratio=0.0)
        assert _emitted_discriminative(objects) == []
        # The old loop would have run C(1200,2) ≈ 719,400 predicate calls.
        assert calls["n"] == 0, calls["n"]


# ---------------------------------------------------------------------------
# Strategy 4 — brute force below the threshold, ANN above it
# ---------------------------------------------------------------------------

def _semantic_pairs(candidates):
    return {
        frozenset([c["left_ref"], c["right_ref"]])
        for c in candidates
        if c["kind"] == "node" and c["signal"] == "semantic"
    }


def _semantic_corpus():
    """Three tight clusters plus orthogonal filler, in a 4-d embedding space."""
    objects, embeddings = [], {}
    vectors = {
        "a1": [1.0, 0.0, 0.0, 0.0],
        "a2": [0.99, 0.01, 0.0, 0.0],   # ~1.0 cosine with a1
        "b1": [0.0, 1.0, 0.0, 0.0],
        "b2": [0.0, 0.98, 0.02, 0.0],   # ~1.0 cosine with b1
        "c1": [0.0, 0.0, 1.0, 0.0],     # orthogonal to everything else
        "c2": [0.0, 0.0, 0.0, 1.0],
    }
    for index, (name, vector) in enumerate(vectors.items()):
        objects.append({
            "id": name, "object_type": "concept",
            "payload": {"name": f"entity {index}"},
        })
        embeddings[name] = vector
    return objects, embeddings


class TestSemanticDispatch:
    def test_below_threshold_uses_the_exact_brute_force_path(self):
        objects, embeddings = _semantic_corpus()
        exact = detect_conflict_candidates(
            objects, [], embeddings=embeddings, sim_threshold=0.9,
            semantic_bruteforce_max=100,
        )
        assert _semantic_pairs(exact) == {
            frozenset(["a1", "a2"]), frozenset(["b1", "b2"]),
        }

    def test_ann_branch_finds_the_similar_pairs_and_skips_the_rest(self):
        objects, embeddings = _semantic_corpus()
        # bruteforce_max=2 with a 6-node group forces the ANN branch without
        # needing a 2000-node fixture.
        ann = detect_conflict_candidates(
            objects, [], embeddings=embeddings, sim_threshold=0.9,
            semantic_bruteforce_max=2, semantic_ann_k=5,
        )
        assert _semantic_pairs(ann) == {
            frozenset(["a1", "a2"]), frozenset(["b1", "b2"]),
        }

    def test_ann_branch_honours_the_sparsity_cap(self):
        count = 40
        objects, embeddings = [], {}
        for index in range(count):
            oid = f"n{index:03d}"
            objects.append({
                "id": oid, "object_type": "concept",
                "payload": {"name": f"node {index}"},
            })
            # Near-identical vectors ⇒ every pair clears the threshold, so the
            # 500-pair cap is the only thing bounding the output.
            embeddings[oid] = [1.0, index * 1e-6, 0.0, 0.0]
        candidates = detect_conflict_candidates(
            objects, [], embeddings=embeddings, sim_threshold=0.5,
            semantic_bruteforce_max=2, semantic_ann_k=count,
        )
        pairs = _semantic_pairs(candidates)
        assert 0 < len(pairs) <= 500

    def test_ann_helper_is_fail_open(self):
        """A broken ANN pass returns None (⇒ group skipped), it never raises."""
        objects, _ = _semantic_corpus()
        broken = {node["id"]: "not-a-vector" for node in objects}
        assert conflict_detect._semantic_ann_pairs(objects, broken, 0.9, 5) is None

    def test_ann_unavailable_skips_the_group_instead_of_raising(self, monkeypatch):
        objects, embeddings = _semantic_corpus()
        monkeypatch.setattr(
            conflict_detect, "_semantic_ann_pairs",
            lambda *args, **kwargs: None,
        )
        candidates = detect_conflict_candidates(
            objects, [], embeddings=embeddings, sim_threshold=0.9,
            semantic_bruteforce_max=2,
        )
        assert _semantic_pairs(candidates) == set()
