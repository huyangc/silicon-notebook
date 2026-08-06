from __future__ import annotations

import random

import pytest

from app.services.graph_retrieval import _normalized_score_ranking


def _legacy_ranking(scores):
    raw = [(chunk_id, float(score)) for chunk_id, score in scores]
    values = [score for _, score in raw]
    lo, hi = min(values), max(values)
    span = hi - lo
    normalized = [
        (chunk_id, (score - lo) / span if span > 0 else 0.0)
        for chunk_id, score in raw
    ]
    normalized.sort(key=lambda item: item[1], reverse=True)
    return normalized


@pytest.mark.parametrize("limit", [1, 3, 20, 200])
def test_bounded_ppr_ranking_is_byte_identical_to_legacy_prefix(limit):
    rng = random.Random(20260806)
    # Repeated rounded values deliberately exercise the stable tie contract.
    scores = [
        (f"chunk-{index:04d}", round(rng.random(), 2))
        for index in range(500)
    ]

    got, considered = _normalized_score_ranking(iter(scores), limit=limit)

    assert considered == len(scores)
    assert got == _legacy_ranking(scores)[:limit]


def test_unbounded_ppr_ranking_preserves_public_diagnostic_result():
    scores = [("late", 0.5), ("first-tie", 1.0), ("second-tie", 1.0), ("low", 0.0)]

    got, considered = _normalized_score_ranking(iter(scores))

    assert considered == len(scores)
    assert got == _legacy_ranking(scores)


def test_bounded_ppr_ranking_preserves_all_zero_input_order():
    scores = [(f"chunk-{index}", 0.0) for index in range(10)]

    got, considered = _normalized_score_ranking(iter(scores), limit=4)

    assert considered == 10
    assert got == [(f"chunk-{index}", 0.0) for index in range(4)]


@pytest.mark.parametrize("bad_score", [float("nan"), float("inf"), float("-inf")])
def test_bounded_ppr_ranking_rejects_non_finite_scores(bad_score):
    with pytest.raises(ValueError, match="non_finite_ppr_score"):
        _normalized_score_ranking(
            iter([("before", 1.0), ("bad", bad_score), ("after", 2.0)]),
            limit=2,
        )
