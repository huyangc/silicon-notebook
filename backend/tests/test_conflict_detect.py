"""Tests for conflict_detect.py — pure, DB-free candidate detection (Task 2).

All fixtures are synthetic in-memory objects/relations. No DB, no LLM, no I/O.
"""
from __future__ import annotations

import pytest
from app.services.kg.conflict_detect import detect_conflict_candidates


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _obj(oid: str, name: str, obj_type: str = "Concept") -> dict:
    return {"id": oid, "object_type": obj_type, "payload": {"name": name}}


def _rel(rid: str, src: str, tgt: str, edge_type: str) -> dict:
    return {"id": rid, "source_object_id": src, "target_object_id": tgt, "edge_type": edge_type}


def _sigs(candidates: list[dict]) -> set[str]:
    """Return the set of signal tags present in a candidate list."""
    return {c["signal"] for c in candidates}


def _pairs(candidates: list[dict]) -> set[frozenset]:
    """Return unordered ref-pairs as frozensets for easy comparison."""
    return {frozenset([c["left_ref"], c["right_ref"]]) for c in candidates}


# ---------------------------------------------------------------------------
# Edge candidates — structural
# ---------------------------------------------------------------------------

class TestSharedPairDiffEdge:
    """Two edges with same (head, tail) but different edge_type → shared_pair_diff_edge."""

    def test_basic(self):
        objs = [_obj("A", "NMOS"), _obj("B", "gain")]
        rels = [
            _rel("r1", "A", "B", "supports"),
            _rel("r2", "A", "B", "contrasts_with"),
        ]
        cands = detect_conflict_candidates(objs, rels)
        assert len(cands) == 1
        c = cands[0]
        assert c["kind"] == "edge"
        assert frozenset([c["left_ref"], c["right_ref"]]) == frozenset(["r1", "r2"])
        assert c["signal"] == "shared_pair_diff_edge"

    def test_same_edge_type_no_candidate(self):
        """Same (head, tail, edge_type) → corroboration, NOT a conflict candidate."""
        objs = [_obj("A", "NMOS"), _obj("B", "gain")]
        rels = [
            _rel("r1", "A", "B", "supports"),
            _rel("r2", "A", "B", "supports"),
        ]
        cands = detect_conflict_candidates(objs, rels)
        assert cands == []

    def test_three_diff_edge_types_emits_all_pairs(self):
        """Three different edge types on same pair → 3 pairs emitted."""
        objs = [_obj("A", "NMOS"), _obj("B", "gain")]
        rels = [
            _rel("r1", "A", "B", "supports"),
            _rel("r2", "A", "B", "contrasts_with"),
            _rel("r3", "A", "B", "defines"),
        ]
        cands = detect_conflict_candidates(objs, rels)
        edge_cands = [c for c in cands if c["signal"] == "shared_pair_diff_edge"]
        assert len(edge_cands) == 3
        # Verify all pairs are present
        assert frozenset(["r1", "r2"]) in _pairs(edge_cands)
        assert frozenset(["r1", "r3"]) in _pairs(edge_cands)
        assert frozenset(["r2", "r3"]) in _pairs(edge_cands)


class TestSharedHead:
    """Same head + same edge_type but different tail → shared_head."""

    def test_basic(self):
        objs = [_obj("A", "NMOS"), _obj("B", "gain1"), _obj("C", "gain2")]
        rels = [
            _rel("r1", "A", "B", "defines"),
            _rel("r2", "A", "C", "defines"),
        ]
        cands = detect_conflict_candidates(objs, rels)
        assert len(cands) == 1
        c = cands[0]
        assert c["kind"] == "edge"
        assert c["signal"] == "shared_head"
        assert frozenset([c["left_ref"], c["right_ref"]]) == frozenset(["r1", "r2"])

    def test_different_edge_types_are_not_shared_head(self):
        """Same head, different edge_type (r1=defines, r2=about) → different pair type."""
        objs = [_obj("A", "NMOS"), _obj("B", "gain1"), _obj("C", "gain2")]
        rels = [
            _rel("r1", "A", "B", "defines"),
            _rel("r2", "A", "C", "about"),
        ]
        cands = detect_conflict_candidates(objs, rels)
        # Not shared_head (different edge_type, different target → NOT same-head-same-type-diff-tail)
        assert not any(c["signal"] == "shared_head" for c in cands)

    def test_three_way_shared_head_capped_sparsely(self):
        """Three edges sharing same head+type → should emit pairs but remain sparse."""
        objs = [_obj("A", "X"), _obj("B", "Y"), _obj("C", "Z"), _obj("D", "W")]
        rels = [
            _rel("r1", "A", "B", "defines"),
            _rel("r2", "A", "C", "defines"),
            _rel("r3", "A", "D", "defines"),
        ]
        cands = detect_conflict_candidates(objs, rels)
        sh_cands = [c for c in cands if c["signal"] == "shared_head"]
        # At least 1 candidate but bounded — current impl emits all pairs (3 here = C(3,2))
        assert len(sh_cands) >= 1


class TestSharedTail:
    """Same tail + same edge_type but different head → shared_tail."""

    def test_basic(self):
        objs = [_obj("A", "gainA"), _obj("B", "gainB"), _obj("C", "result")]
        rels = [
            _rel("r1", "A", "C", "supports"),
            _rel("r2", "B", "C", "supports"),
        ]
        cands = detect_conflict_candidates(objs, rels)
        st_cands = [c for c in cands if c["signal"] == "shared_tail"]
        assert len(st_cands) == 1
        assert frozenset([st_cands[0]["left_ref"], st_cands[0]["right_ref"]]) == frozenset(["r1", "r2"])


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    """Unordered pairs must not be emitted twice."""

    def test_no_duplicate_pairs(self):
        objs = [_obj("A", "NMOS"), _obj("B", "gain")]
        rels = [
            _rel("r1", "A", "B", "supports"),
            _rel("r2", "A", "B", "contrasts_with"),
        ]
        cands = detect_conflict_candidates(objs, rels)
        pairs = [(c["left_ref"], c["right_ref"]) for c in cands]
        # Unordered dedup: frozenset pairs should all be unique
        fp = [frozenset(p) for p in pairs]
        assert len(fp) == len(set(fp)), "Duplicate unordered pairs emitted"


# ---------------------------------------------------------------------------
# Node candidates — discriminative
# ---------------------------------------------------------------------------

class TestDiscriminative:
    """Two Claims/Concepts that differ by exactly one contrast-token → discriminative."""

    def test_nmos_pmos_claim(self):
        objs = [
            _obj("c1", "NMOS increases gain", "Claim"),
            _obj("c2", "PMOS increases gain", "Claim"),
        ]
        cands = detect_conflict_candidates(objs, [])
        disc = [c for c in cands if c["signal"] == "discriminative"]
        assert len(disc) == 1
        c = disc[0]
        assert c["kind"] == "node"
        assert frozenset([c["left_ref"], c["right_ref"]]) == frozenset(["c1", "c2"])

    def test_positive_negative_concept(self):
        objs = [
            _obj("o1", "positive feedback", "Concept"),
            _obj("o2", "negative feedback", "Concept"),
        ]
        cands = detect_conflict_candidates(objs, [])
        disc = [c for c in cands if c["signal"] == "discriminative"]
        assert len(disc) >= 1

    def test_unrelated_names_no_discriminative(self):
        objs = [
            _obj("o1", "transistor gain", "Concept"),
            _obj("o2", "phase margin stability", "Concept"),
        ]
        cands = detect_conflict_candidates(objs, [])
        assert not any(c["signal"] == "discriminative" for c in cands)

    def test_identical_names_no_candidate(self):
        """Exact duplicates are merge candidates, not conflict candidates."""
        objs = [
            _obj("o1", "NMOS transistor", "Concept"),
            _obj("o2", "NMOS transistor", "Concept"),
        ]
        cands = detect_conflict_candidates(objs, [])
        assert not any(c["signal"] == "discriminative" for c in cands)

    def test_formula_type_skipped(self):
        """Formula/Procedure objects are not flagged for discriminative conflict."""
        objs = [
            _obj("f1", "positive gain", "Formula"),
            _obj("f2", "negative gain", "Formula"),
        ]
        cands = detect_conflict_candidates(objs, [])
        assert not any(c["signal"] == "discriminative" for c in cands)


# ---------------------------------------------------------------------------
# Node candidates — semantic (embeddings)
# ---------------------------------------------------------------------------

class TestSemantic:
    """When embeddings provided, near-duplicate-but-opposite node pairs get semantic signal."""

    def _unit(self, n: int, i: int) -> list[float]:
        """A unit vector of dimension n with value 1.0 at index i, rest 0.0."""
        v = [0.0] * n
        v[i] = 1.0
        return v

    def _cosine(self, a: list[float], b: list[float]) -> float:
        import math
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    def test_semantic_signal_with_high_cosine_discriminative(self):
        """Objects that are near-identical vectors AND discriminative → semantic candidate."""
        # Make two nearly-identical 4D vectors (cosine ≈ 0.999)
        v1 = [1.0, 0.1, 0.0, 0.0]
        v2 = [1.0, 0.1, 0.0, 0.0]  # identical direction
        objs = [
            _obj("n1", "NMOS gain", "Claim"),
            _obj("n2", "PMOS gain", "Claim"),
        ]
        embeddings = {"n1": v1, "n2": v2}
        cands = detect_conflict_candidates(objs, [], embeddings=embeddings, sim_threshold=0.8)
        sem = [c for c in cands if c["signal"] == "semantic"]
        assert len(sem) == 1
        assert frozenset([sem[0]["left_ref"], sem[0]["right_ref"]]) == frozenset(["n1", "n2"])

    def test_semantic_skipped_when_no_embeddings(self):
        """Without embeddings, semantic strategy is skipped entirely."""
        objs = [
            _obj("n1", "NMOS gain", "Claim"),
            _obj("n2", "PMOS gain", "Claim"),
        ]
        cands = detect_conflict_candidates(objs, [], embeddings=None)
        assert not any(c["signal"] == "semantic" for c in cands)

    def test_semantic_no_candidate_when_cosine_below_threshold(self):
        """Low-cosine pairs even if discriminative → no semantic signal."""
        # Orthogonal vectors: cosine = 0.0
        v1 = [1.0, 0.0]
        v2 = [0.0, 1.0]
        objs = [
            _obj("n1", "NMOS gain", "Claim"),
            _obj("n2", "PMOS gain", "Claim"),
        ]
        embeddings = {"n1": v1, "n2": v2}
        cands = detect_conflict_candidates(objs, [], embeddings=embeddings, sim_threshold=0.8)
        assert not any(c["signal"] == "semantic" for c in cands)

    def test_semantic_no_candidate_when_not_discriminative(self):
        """High cosine but NOT discriminative → no semantic signal."""
        v1 = [1.0, 0.1]
        v2 = [1.0, 0.1]
        objs = [
            _obj("n1", "voltage amplifier", "Concept"),
            _obj("n2", "voltage regulator", "Concept"),
        ]
        embeddings = {"n1": v1, "n2": v2}
        cands = detect_conflict_candidates(objs, [], embeddings=embeddings, sim_threshold=0.8)
        assert not any(c["signal"] == "semantic" for c in cands)


# ---------------------------------------------------------------------------
# Non-conflicting baseline — no false positives on clean data
# ---------------------------------------------------------------------------

class TestNonConflicting:
    """A clearly non-conflicting KG should produce no candidates."""

    def test_completely_distinct_edges(self):
        objs = [_obj("A", "concept A"), _obj("B", "concept B"),
                _obj("C", "concept C"), _obj("D", "concept D")]
        rels = [
            _rel("r1", "A", "B", "part_of"),
            _rel("r2", "C", "D", "kind_of"),
        ]
        cands = detect_conflict_candidates(objs, rels)
        assert cands == []

    def test_empty_graph(self):
        cands = detect_conflict_candidates([], [])
        assert cands == []

    def test_single_edge(self):
        objs = [_obj("A", "foo"), _obj("B", "bar")]
        rels = [_rel("r1", "A", "B", "supports")]
        cands = detect_conflict_candidates(objs, rels)
        assert cands == []

    def test_unrelated_concept_objects(self):
        """Three concepts with unrelated names → no discriminative candidates."""
        objs = [
            _obj("o1", "amplifier", "Concept"),
            _obj("o2", "oscillator", "Concept"),
            _obj("o3", "filter", "Concept"),
        ]
        cands = detect_conflict_candidates(objs, [])
        assert cands == []


# ---------------------------------------------------------------------------
# Mixed graph — edge + node signals coexist
# ---------------------------------------------------------------------------

class TestMixedSignals:
    """Edge and node signals can coexist without interfering."""

    def test_edge_and_node_candidates_in_one_call(self):
        objs = [
            _obj("A", "NMOS", "Concept"),
            _obj("B", "gain", "Concept"),
            _obj("c1", "NMOS increases gain", "Claim"),
            _obj("c2", "PMOS increases gain", "Claim"),
        ]
        rels = [
            _rel("r1", "A", "B", "supports"),
            _rel("r2", "A", "B", "contrasts_with"),
        ]
        cands = detect_conflict_candidates(objs, rels)
        sigs = _sigs(cands)
        assert "shared_pair_diff_edge" in sigs
        assert "discriminative" in sigs


# ---------------------------------------------------------------------------
# Sparsity guard — large groups are capped
# ---------------------------------------------------------------------------

class TestSparsity:
    """Large groups must not explode the candidate list quadratically."""

    def test_large_shared_head_group_is_capped(self):
        """50 edges sharing the same head+type should yield a bounded candidate set."""
        N = 50
        objs = [_obj("HEAD", "head")] + [_obj(f"T{i}", f"tail{i}") for i in range(N)]
        rels = [_rel(f"r{i}", "HEAD", f"T{i}", "defines") for i in range(N)]
        cands = detect_conflict_candidates(objs, rels)
        sh = [c for c in cands if c["signal"] == "shared_head"]
        # Must not be O(N^2) = 1225; should be capped well below that
        assert len(sh) < 200, f"Too many candidates: {len(sh)}"

    def test_large_discriminative_group_is_bounded(self):
        """Many pairwise-discriminative objects should not produce O(N^2) candidates."""
        # Alternate nmos/pmos names to create many discriminative pairs
        N = 20
        objs = [
            _obj(f"n{i}", f"nmos stage {i}", "Claim") for i in range(N)
        ] + [
            _obj(f"p{i}", f"pmos stage {i}", "Claim") for i in range(N)
        ]
        cands = detect_conflict_candidates(objs, [])
        disc = [c for c in cands if c["signal"] == "discriminative"]
        # Should be bounded, not N^2 = 400
        assert len(disc) < 100, f"Too many discriminative candidates: {len(disc)}"
