"""Tests for isolated-node rank penalty in score_knowledge.

Critical invariant: w_isolated_penalty multiplies into `score` ONLY.
`relevance` must remain untouched ([0,1]/tau gate elsewhere).
"""

import pytest
from app.services.retrieval import score_knowledge


def _obj(oid, name):
    return {"id": oid, "payload": {"name": name, "section_path": "1"}, "evidence": []}


# ---------------------------------------------------------------------------
# Helper: two objects with identical keyword relevance inputs so any ordering
# difference comes purely from the isolated penalty.
# ---------------------------------------------------------------------------

QUERY = "cascode output resistance"
OBJ_CONNECTED = _obj("connected", "cascode output resistance")
OBJ_ISOLATED = _obj("isolated", "cascode output resistance")
OBJS = [OBJ_CONNECTED, OBJ_ISOLATED]


def test_isolated_gets_lower_score_and_sorts_after_connected():
    """孤立节点 score 被压半,排在有边节点之后;relevance 两者相同(不变)。"""
    hits = score_knowledge(
        QUERY, OBJS, "claim",
        isolated_ids={"isolated"},
        w_isolated_penalty=0.5,
    )
    assert len(hits) == 2
    connected_hit = next(h for h in hits if h.object_id == "connected")
    isolated_hit = next(h for h in hits if h.object_id == "isolated")

    # relevance MUST be identical — penalty must not touch it (invariant)
    assert connected_hit.relevance == pytest.approx(isolated_hit.relevance, abs=1e-9), (
        "relevance diverged — penalty leaked into relevance, violating [0,1]/tau invariant"
    )

    # score of isolated should be ~0.5× connected
    assert isolated_hit.score == pytest.approx(connected_hit.score * 0.5, abs=1e-9)

    # isolated sorts AFTER connected
    ids_in_order = [h.object_id for h in hits]
    assert ids_in_order.index("connected") < ids_in_order.index("isolated"), (
        "isolated node should sort after connected node"
    )


def test_penalty_1_0_is_exact_noop():
    """w_isolated_penalty=1.0 → scores/order identical to no penalty."""
    baseline = score_knowledge(QUERY, OBJS, "claim")
    penalized = score_knowledge(
        QUERY, OBJS, "claim",
        isolated_ids={"isolated"},
        w_isolated_penalty=1.0,
    )
    baseline_scores = {h.object_id: h.score for h in baseline}
    penalized_scores = {h.object_id: h.score for h in penalized}
    assert baseline_scores == pytest.approx(penalized_scores, abs=1e-12)


def test_isolated_ids_none_is_noop():
    """isolated_ids=None (default) → exact no-op, no behaviour change."""
    baseline = score_knowledge(QUERY, OBJS, "claim")
    with_none = score_knowledge(
        QUERY, OBJS, "claim",
        isolated_ids=None,
        w_isolated_penalty=0.5,
    )
    baseline_scores = {h.object_id: h.score for h in baseline}
    with_none_scores = {h.object_id: h.score for h in with_none}
    assert baseline_scores == pytest.approx(with_none_scores, abs=1e-12)


def test_no_penalty_params_at_all_is_noop():
    """No isolated_ids/penalty params → default args, exact no-op (existing callers safe)."""
    baseline = score_knowledge(QUERY, OBJS, "claim")
    explicit_default = score_knowledge(QUERY, OBJS, "claim")
    baseline_ids = [h.object_id for h in baseline]
    explicit_ids = [h.object_id for h in explicit_default]
    assert baseline_ids == explicit_ids


def test_relevance_stays_in_0_1_under_penalty():
    """After penalty, all relevance values must remain in [0, 1]."""
    hits = score_knowledge(
        QUERY, OBJS, "claim",
        isolated_ids={"isolated", "connected"},
        w_isolated_penalty=0.1,
    )
    for h in hits:
        assert 0.0 <= h.relevance <= 1.0, (
            f"relevance={h.relevance} out of [0,1] for {h.object_id}"
        )


def test_empty_isolated_ids_set_is_noop():
    """Empty isolated_ids set → no node gets penalised."""
    baseline = score_knowledge(QUERY, OBJS, "claim")
    with_empty = score_knowledge(
        QUERY, OBJS, "claim",
        isolated_ids=set(),
        w_isolated_penalty=0.5,
    )
    baseline_scores = {h.object_id: h.score for h in baseline}
    with_empty_scores = {h.object_id: h.score for h in with_empty}
    assert baseline_scores == pytest.approx(with_empty_scores, abs=1e-12)
