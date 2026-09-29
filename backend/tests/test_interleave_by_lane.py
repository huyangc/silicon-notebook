"""Reasoning synthesis assembly: two-lane interleave of the passage partition.

A graph-bearing reasoning run fills its passage partition from two producers
whose `relevance` values live on different scales: concept-walk (PPR) passages
carry a per-run min-max normalized score (the top one is always 1.0), seeded /
searched passages an absolute fused score. `_answer_reasoning` therefore sorts by
relevance and then alternates the two lanes (`interleave_by_lane`), so neither
lane can evict the other wholesale under the character budget. Exact-lookup
passages are anchored at their relevance position (they do not alternate), and
the exact prefix seat is applied AFTER the interleave and stays in front. The
whole order lives in `retrieval.order_reasoning_passages`, shared with deep-report
section drafting.
"""
from __future__ import annotations

import pytest

from app.domain.retrieval import RetrievalSupport, RetrievedChunk
from app.services.retrieval import (
    interleave_by_lane,
    is_exact_lookup_chunk,
    prefer_stronger_chunk_candidate,
    promote_bounded_prefix_by_library,
    relevance_on_ppr_scale,
)
from tests.test_exact_lookup import repo  # noqa: F401  (repo fixture)
from tests.test_reasoning_exact_reserve import _assemble, _object_ids


# ------------------------------------------------------------ the pure function
def _lane(chunk_id):
    return chunk_id.startswith("g")


def _ids(chunks):
    return [chunk.chunk_id for chunk in chunks]


class _Stub:
    def __init__(self, chunk_id):
        self.chunk_id = chunk_id


def _stubs(*ids):
    return [_Stub(chunk_id) for chunk_id in ids]


def _interleave(*ids):
    return _ids(interleave_by_lane(_stubs(*ids), lambda c: _lane(c.chunk_id)))


def test_the_two_lanes_alternate_starting_with_the_lane_of_the_first_chunk():
    assert _interleave("g1", "g2", "g3", "r1", "r2", "r3") == [
        "g1", "r1", "g2", "r2", "g3", "r3"]
    assert _interleave("r1", "g1", "g2", "r2") == ["r1", "g1", "r2", "g2"]


def test_the_rest_of_the_longer_lane_follows_as_one_block():
    assert _interleave("g1", "g2", "g3", "g4", "r1") == [
        "g1", "r1", "g2", "g3", "g4"]
    assert _interleave("r1", "r2", "r3", "g1") == ["r1", "g1", "r2", "r3"]


@pytest.mark.parametrize("ids", [("r1", "r2", "r3"), ("g1", "g2"), ()])
def test_a_single_lane_is_a_same_order_copy(ids):
    items = _stubs(*ids)
    result = interleave_by_lane(items, lambda c: _lane(c.chunk_id))
    assert result == items
    assert result is not items


def test_each_lane_keeps_its_input_order_and_nothing_is_dropped():
    items = _stubs("g3", "r9", "g1", "r2", "r5", "g2", "r1")
    result = interleave_by_lane(items, lambda c: _lane(c.chunk_id))
    assert sorted(map(id, result)) == sorted(map(id, items))
    assert [c.chunk_id for c in result if _lane(c.chunk_id)] == ["g3", "g1", "g2"]
    assert [c.chunk_id for c in result if not _lane(c.chunk_id)] == [
        "r9", "r2", "r5", "r1"]


# ------------------------------------------------------------- the lane predicate
def _chunk(chunk_id, relevance, *supports):
    return RetrievedChunk(
        chunk_id=chunk_id, source_id="s1", source_title="t", section_path="1",
        text=f"{chunk_id} 正文", relevance=relevance, score=relevance,
        retrieval_supports=tuple(supports))


def _ppr(score):
    return RetrievalSupport("ppr", "ppr", "", score)


def _semantic(chunk_id, score):
    return RetrievalSupport("semantic", "chunk", chunk_id, score)


def test_a_ppr_passage_is_in_the_graph_lane_and_a_seeded_one_is_not():
    assert relevance_on_ppr_scale(_chunk("g", 0.8, _ppr(0.8))) is True
    assert relevance_on_ppr_scale(_chunk("r", 0.5, _semantic("r", 0.7))) is False
    assert relevance_on_ppr_scale(_chunk("r", 0.5)) is False
    # Duck-typed doubles without the provenance attributes stay in the other lane.
    assert relevance_on_ppr_scale(_Stub("x")) is False


def test_the_lane_follows_the_scale_of_the_representative_dedup_keeps():
    """`take_distinct_chunk_hits` keeps the higher-relevance duplicate and unions
    the supports. PPR 0.9 then seed 0.45 keeps the PPR object: graph lane. PPR 0.3
    then seed 0.45 keeps the seed object, which now carries a ppr support too but
    an absolute relevance: retrieval lane."""
    kept_ppr = prefer_stronger_chunk_candidate(
        _chunk("c", 0.9, _ppr(0.9)), _chunk("c", 0.45, _semantic("c", 0.6)))
    assert kept_ppr.relevance == 0.9
    assert relevance_on_ppr_scale(kept_ppr) is True

    kept_seed = prefer_stronger_chunk_candidate(
        _chunk("c", 0.3, _ppr(0.3)), _chunk("c", 0.45, _semantic("c", 0.6)))
    assert kept_seed.relevance == 0.45
    assert any(s.origin == "ppr" for s in kept_seed.retrieval_supports)
    assert relevance_on_ppr_scale(kept_seed) is False


# ----------------------------------------------------------- the assembly itself
# Every chunk text is 20 chars, so each line costs "kN: " (4) + 20 = 24 chars;
# four lines plus three separators = 99, a fifth does not fit.
_FOUR_LINES = 99


def _passage(chunk_id, relevance, *, ppr, exact=False):
    text = f"{chunk_id}{'x' * (20 - len(chunk_id))}"
    support = _ppr(relevance) if ppr else _semantic(chunk_id, relevance)
    return RetrievedChunk(
        chunk_id=chunk_id, source_id="s1", source_title="t", section_path="1",
        text=text, relevance=relevance, score=relevance,
        retrieval_supports=(support,), exact_lookup=exact)


def _ppr_and_seeded():
    return [
        _passage("ppr1", 1.0, ppr=True), _passage("ppr2", 0.9, ppr=True),
        _passage("ppr3", 0.8, ppr=True), _passage("seed1", 0.5, ppr=False),
        _passage("seed2", 0.4, ppr=False), _passage("seed3", 0.3, ppr=False),
    ]


def test_the_seeded_lane_keeps_half_the_seats_against_normalized_ppr_scores(
    repo, monkeypatch
):
    """Budget fits four lines. One relevance key would admit ppr1..ppr3 + seed1;
    the interleave admits two of each, each lane in its own relevance order."""
    captured, baseline = _assemble(
        repo, monkeypatch, chunks=_ppr_and_seeded(),
        chunk_context_chars=_FOUR_LINES)

    assert captured["chunks"][:4] == ["ppr1", "seed1", "ppr2", "seed2"]
    assert _object_ids(baseline["id_map"]) == {"ppr1", "ppr2", "seed1", "seed2"}


def test_exact_passages_still_take_the_front_seats_after_the_interleave(
    repo, monkeypatch
):
    """The exact prefix seat is applied last, so the two exact passages (lowest
    relevance, retrieval lane) still come first; the interleave follows."""
    chunks = _ppr_and_seeded() + [
        _passage("exact1", 0.2, ppr=False, exact=True),
        _passage("exact2", 0.1, ppr=False, exact=True),
    ]
    captured, baseline = _assemble(
        repo, monkeypatch, chunks=chunks, chunk_context_chars=_FOUR_LINES)

    assert captured["chunks"][:4] == ["exact1", "exact2", "ppr1", "seed1"]
    assert _object_ids(baseline["id_map"]) == {"exact1", "exact2", "ppr1", "seed1"}


def _pre_interleave_order(chunks, reserve):
    """The assembly order before this change, rebuilt locally as the control:
    stable relevance sort, then the exact prefix seat. Nothing else."""
    ordered = sorted(chunks, key=lambda chunk: -chunk.relevance)
    return _ids(promote_bounded_prefix_by_library(
        ordered, is_exact_lookup_chunk, reserve))


@pytest.mark.parametrize("ppr", [False, True])
def test_a_single_lane_partition_assembles_exactly_as_before_the_interleave(
    repo, monkeypatch, ppr
):
    """No-graph runs (no PPR passage) and PPR-only partitions: the order handed
    to the renderer is item-for-item the pre-interleave order."""
    chunks = [_passage(f"c{i}", 1.0 - i / 10, ppr=ppr) for i in range(6)]
    captured, _baseline = _assemble(
        repo, monkeypatch, chunks=list(chunks), chunk_context_chars=_FOUR_LINES)

    assert captured["chunks"] == _pre_interleave_order(
        chunks, repo.settings.reasoning_exact_reserve)


# --------------------------------------- exact passages beyond the prefix seat
def _named_section_with_ppr(seeded):
    """A graph run that named an identifier: a 10-chunk exact section (all 1.0,
    fetched after the PPR seed), 20 PPR passages, and ``seeded`` retrieved ones."""
    ppr = [_passage(f"p{i:02d}", round(1.0 - i * 0.03, 2), ppr=True)
           for i in range(20)]
    exact = [_passage(f"e{i:02d}", 1.0, ppr=False, exact=True) for i in range(10)]
    seeds = [_passage(f"s{i:02d}", round(0.5 - i * 0.05, 2), ppr=False)
             for i in range(seeded)]
    return ppr[:1] + exact + ppr[1:] + seeds


def test_a_whole_exact_section_beyond_the_seat_stays_in_front(repo, monkeypatch):
    """Reserve 4 < section of 10: the six unseated exact passages are anchored at
    relevance 1.0, so all ten sit in the first eleven positions (the top PPR
    passage ties at 1.0 and was fetched first). Seeded passages still alternate
    with the PPR lane after them."""
    captured, _baseline = _assemble(
        repo, monkeypatch, chunks=_named_section_with_ppr(3),
        chunk_context_chars=_FOUR_LINES)

    assert repo.settings.reasoning_exact_reserve == 4
    order = captured["chunks"]
    assert {f"e{i:02d}" for i in range(10)} <= set(order[:11])
    assert order[:5] == ["e00", "e01", "e02", "e03", "p00"]
    assert order[11:17] == ["s00", "p01", "s01", "p02", "s02", "p03"]


def test_switch_off_graph_run_keeps_the_pre_interleave_order(repo, monkeypatch):
    """Passage-search switch off on a graph run: no retrieved passage, exact
    section larger than the seat, PPR present. The order is item-for-item the
    pre-interleave order (sort + exact seat) -- the switch still restores the
    old assembly."""
    chunks = _named_section_with_ppr(0)
    captured, _baseline = _assemble(
        repo, monkeypatch, chunks=list(chunks), chunk_context_chars=_FOUR_LINES)

    assert captured["chunks"] == _pre_interleave_order(
        chunks, repo.settings.reasoning_exact_reserve)


def test_an_anchored_chunk_goes_back_before_the_first_strictly_lower_one():
    """A tie keeps the interleaved chunk first (strictly lower, not lower-or-
    equal): the 1.0 PPR passage fetched earlier stays ahead of the 1.0 exact
    passage, as the stable sort had it. A lower-relevance anchored chunk lands
    in front of the first interleaved chunk below it, even mid-lane."""
    ordered = [
        _passage("p1", 1.0, ppr=True), _passage("e1", 1.0, ppr=False, exact=True),
        _passage("p2", 0.9, ppr=True), _passage("s1", 0.5, ppr=False),
        _passage("e2", 0.45, ppr=False, exact=True), _passage("s2", 0.4, ppr=False),
        _passage("p3", 0.3, ppr=True),
    ]
    result = interleave_by_lane(
        ordered, relevance_on_ppr_scale, anchored=is_exact_lookup_chunk)
    # interleaved: p1 s1 p2 s2 p3; e1 goes before s1 (first < 1.0), e2 before s2.
    assert _ids(result) == ["p1", "e1", "s1", "p2", "e2", "s2", "p3"]


def test_anchored_chunks_do_not_count_as_a_lane():
    """Only exact passages besides the PPR lane: the retrieval lane is empty, so
    the result is a same-order copy -- exact passages keep their sorted spot."""
    ordered = [_passage("p1", 1.0, ppr=True), _passage("e1", 1.0, ppr=False, exact=True),
               _passage("p2", 0.9, ppr=True)]
    result = interleave_by_lane(
        ordered, relevance_on_ppr_scale, anchored=is_exact_lookup_chunk)
    assert result == ordered and result is not ordered
