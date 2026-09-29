"""PR-C C1/C2/C6: library reserve seats in the mix branch's final cut.

The mix branch's final cut is ``select_with_reserves_baseline_first`` over the
reranked pool with the rules ``retrieval.mix_reserve_rules`` builds, in
priority order: graph -> exact (per library) -> active notebook / peer-mode
per-library seats.  Pinned here:

* single-notebook runs stay byte-identical (PPR rows stamped with the active
  notebook's OWN id must not count as foreign);
* a small active notebook keeps ``min(seats, eligible)`` passages against a
  big reference library, inside the same token budget;
* no eligible active row (none / below the floor / graph-only / question-
  only) leaves the output exactly as with the rule off;
* exact seats outrank active seats, identical text spends one seat, the
  oversized-top-1 exception is not duplicated;
* the seat count is ``min(k, ceil(k * CHUNK_FEDERATION_ACTIVE_RESERVE))``,
  0 in peer mode for the active rule, and only in peer mode for the
  per-library rule;
* peer mode shares the seats per library in best-hit order;
* ``ask_chunk`` wires all of it through one ``mix_reserve_rules`` call.
"""
from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from app.models.schemas import AskRequest
from app.services import chunk_federation as cf
from app.services.retrieval import (
    RELEVANCE_FLOOR,
    RetrievalSupport,
    RetrievedChunk,
    active_reserve_eligible,
    active_reserve_rule,
    enforce_active_floor,
    est_tokens,
    exact_section_reserve_rules,
    graph_reserve_rule,
    is_active_hit,
    library_reserve_rules,
    mix_reserve_rules,
    select_with_reserves_baseline_first,
)


ACTIVE = "nb-active"
_KIND = {
    "semantic": "chunk", "lexical": "chunk", "generated_question": "chunk",
    "kg_source": "object", "ppr": "ppr", "relation": "relation",
}


def _row(chunk_id, *, tokens=10, relevance=0.5, notebook_id="",
         text=None, origin="semantic", exact=False, source_id=None):
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_id=source_id or f"src-{chunk_id}",
        source_title="t",
        section_path="",
        # Unique text per row unless given: identical text is a seat identity.
        text=text if text is not None else chunk_id.ljust(tokens * 3, "."),
        element_ids=[f"e-{chunk_id}"],
        score=relevance,
        relevance=relevance,
        notebook_id=notebook_id,
        retrieval_supports=(RetrievalSupport(
            origin, _KIND[origin], chunk_id, relevance),),
        exact_lookup=exact,
    )


def _settings(graph=0, exact=4):
    return SimpleNamespace(chunk_graph_reserve=graph, exact_section_reserve=exact)


def _cut(ranked, budget, *, active_seats=0, library_seats=0, exact_hits=(),
         settings=None, active=ACTIVE):
    return select_with_reserves_baseline_first(ranked, budget, mix_reserve_rules(
        settings or _settings(), ranked, list(exact_hits), active,
        active_seats=active_seats, library_seats_total=library_seats,
    ))


def _rule_off(ranked, budget, *, exact_hits=(), settings=None):
    """The historical mix cut: graph + exact rules only."""
    settings = settings or _settings()
    return select_with_reserves_baseline_first(ranked, budget, (
        graph_reserve_rule(max(0, settings.chunk_graph_reserve)),
        *exact_section_reserve_rules(
            max(0, settings.exact_section_reserve), list(exact_hits)),
    ))


def _tokens(rows):
    return sum(est_tokens(row.text) for row in rows)


def _ids(rows):
    return [row.chunk_id for row in rows]


def _foreign(count, *, library="ref", tokens=10, top=0.85, step=0.005):
    return [
        _row(f"{library}-{i}", tokens=tokens, relevance=top - i * step,
             notebook_id=library)
        for i in range(count)
    ]


# --------------------------------------------------------------- predicate
def test_active_hit_normalises_the_raw_own_id_stamp():
    """PPR / generated-question hydrate stamp the RAW owning id, the active
    notebook's own included; only a genuinely foreign id is foreign."""
    assert is_active_hit(_row("a", notebook_id=""), ACTIVE)
    assert is_active_hit(_row("a", notebook_id=ACTIVE), ACTIVE)
    assert not is_active_hit(_row("b", notebook_id="ref"), ACTIVE)
    # An empty active id reads only an empty stamp as active -- which is why
    # the id is a required argument (a raw own-id stamp would pose as a peer).
    assert is_active_hit(_row("a", notebook_id=""), "")
    assert not is_active_hit(_row("a", notebook_id=ACTIVE), "")


@pytest.mark.parametrize("row, eligible", [
    (_row("ok", relevance=0.3), True),
    (_row("floor", relevance=RELEVANCE_FLOOR), True),
    (_row("weak", relevance=RELEVANCE_FLOOR - 0.01), False),
    (_row("weak-exact", relevance=0.05, origin="lexical", exact=True), True),
    (_row("graph", relevance=0.9, origin="ppr"), False),
    (_row("kg", relevance=0.3, origin="kg_source"), False),
    (_row("question", relevance=0.9, origin="generated_question"), False),
    (_row("foreign", relevance=0.9, notebook_id="ref"), False),
])
def test_active_reserve_eligible(row, eligible):
    assert active_reserve_eligible(row, ACTIVE) is eligible


# ------------------------------------------------------------ seat numbers
def _seat_settings(ratio, k=16):
    return SimpleNamespace(chunk_mmr_k=k, chunk_federation_active_reserve=ratio)


@pytest.mark.parametrize("ratio, seats", [(0.0, 0), (0.25, 4), (0.3, 5), (1.0, 16)])
def test_active_reserve_seats_is_ceil_of_k_times_ratio(ratio, seats):
    assert cf.active_reserve_seats(_seat_settings(ratio)) == seats
    assert cf.peer_library_reserve_seats(_seat_settings(ratio)) == 0


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_out_of_range_ratio_is_refused_at_startup(value):
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, chunk_federation_active_reserve=value)


def test_peer_mode_moves_the_seats_from_the_active_rule_to_the_library_rule(
    monkeypatch,
):
    monkeypatch.setattr(cf, "federated_ask_active", lambda: True)
    assert cf.active_reserve_seats(_seat_settings(0.25)) == 0
    assert cf.peer_library_reserve_seats(_seat_settings(0.25)) == 4
    assert cf.peer_library_reserve_seats(_seat_settings(0.0)) == 0


@pytest.mark.parametrize("ratio, seats", [(0.25, 4), (0.3, 5), (1.0, 16)])
def test_peer_library_seats_are_ceil_of_k_times_ratio(monkeypatch, ratio, seats):
    """0.3 x 16 = 4.8: ceil gives 5, a floor would give 4."""
    monkeypatch.setattr(cf, "federated_ask_active", lambda: True)
    assert cf.peer_library_reserve_seats(_seat_settings(ratio)) == seats


# --------------------------------------------------------- single notebook
def test_single_notebook_mix_is_byte_identical_with_own_id_ppr_stamps():
    """No foreign row -> the active rule is inert, even though the PPR lane
    stamped the active notebook's own id and weak own rows sit past the cut."""
    # The PPR rows lead the ranking; the unstamped own rows sit past the cut.
    # Read raw, the stamps would look foreign and the tail would be rescued.
    ranked = [
        _row(f"ppr-{i}", relevance=0.9, notebook_id=ACTIVE, origin="ppr")
        for i in range(8)
    ] + [
        _row(f"sem-{i}", relevance=0.8 - i * 0.01) for i in range(2)
    ] + [
        _row(f"tail-{i}", relevance=0.3) for i in range(4)
    ]
    budget = _tokens(ranked[:6])
    for settings in (_settings(), _settings(graph=1)):
        historical = _rule_off(ranked, budget, settings=settings)
        out = _cut(ranked, budget, active_seats=4, settings=settings)
        assert _ids(out) == _ids(historical)
        assert all(a is b for a, b in zip(out, historical))
    assert active_reserve_rule(4, ranked, ACTIVE).reserve == 0


# ------------------------------------------------ small active, big library
def test_small_active_notebook_keeps_its_seats_against_a_big_library():
    foreign = _foreign(40, tokens=20, top=0.85)
    active = [
        _row("mine-1", relevance=0.40, tokens=20),
        _row("mine-2", relevance=0.30, tokens=20),
    ]
    ranked = foreign + active          # rerank put the user's own notes last
    budget = _tokens(foreign[:12])

    assert not {"mine-1", "mine-2"} & set(_ids(_rule_off(ranked, budget)))
    out = _cut(ranked, budget, active_seats=4)

    assert {"mine-1", "mine-2"} <= set(_ids(out)), _ids(out)
    assert _tokens(out) <= budget
    # Selection keeps ranked order; the seats came from the tail of the foreign run.
    assert _ids(out) == [c.chunk_id for c in ranked if c in out]


@pytest.mark.parametrize("variant", [
    "none", "below_floor", "graph_only", "question_only",
])
def test_no_eligible_active_row_is_identical_to_rule_off(variant):
    foreign = _foreign(30)
    tail = {
        "none": [],
        "below_floor": [_row("mine", relevance=0.05)],
        "graph_only": [_row("mine", relevance=0.9, origin="ppr")],
        "question_only": [_row("mine", relevance=0.9, origin="generated_question")],
    }[variant]
    ranked = foreign + tail
    budget = _tokens(foreign[:10])

    out = _cut(ranked, budget, active_seats=4)

    assert _ids(out) == _ids(_rule_off(ranked, budget))


# ---------------------------------------------------- priority and bounds
def test_exact_seats_come_first_and_active_seats_take_what_is_left():
    foreign = _foreign(6)
    exact = [
        _row(f"exact-{i}", relevance=0.2, notebook_id="ref", origin="lexical",
             exact=True)
        for i in range(2)
    ]
    active = [_row(f"mine-{i}", relevance=0.35) for i in range(3)]
    ranked = foreign + exact + active
    budget = _tokens(ranked[:4])

    out = _cut(ranked, budget, active_seats=4, exact_hits=exact,
               settings=_settings(exact=2))

    ids = _ids(out)
    assert {"exact-0", "exact-1"} <= set(ids), ids
    assert ids == ["ref-0", "exact-0", "exact-1", "mine-0"], ids
    assert _tokens(out) <= budget


def test_active_seats_never_create_a_second_oversize_exception():
    oversized = _row("huge", tokens=100, relevance=0.95, notebook_id="ref")
    ranked = [oversized, *_foreign(3), _row("mine", relevance=0.4)]

    assert _ids(_cut(ranked, 20, active_seats=4)) == ["huge"]
    # A seat candidate larger than the whole budget is skipped, not admitted.
    big_mine = _row("big-mine", tokens=100, relevance=0.4)
    ranked = [*_foreign(10), big_mine]
    out = _cut(ranked, _tokens(ranked[:4]), active_seats=4)
    assert "big-mine" not in _ids(out)
    assert _tokens(out) <= _tokens(ranked[:4])


def test_identical_text_in_two_active_sources_spends_one_seat():
    same = "the same passage".ljust(30, ".")
    active = [
        _row("mine-1", relevance=0.40, text=same, source_id="s1"),
        _row("mine-1-copy", relevance=0.39, text=same, source_id="s2"),
        _row("mine-2", relevance=0.30),
    ]
    ranked = _foreign(20) + active
    out = _cut(ranked, _tokens(ranked[:8]), active_seats=2)

    ids = set(_ids(out))
    assert {"mine-1", "mine-2"} <= ids, ids
    assert "mine-1-copy" not in ids


def test_an_active_copy_of_a_selected_foreign_passage_takes_no_seat():
    """Seat identity is the text over the WHOLE selection: ``mine`` repeats
    ``ref-3``'s text from another source, so pulling it in would evict a
    different passage for nothing -- the MMR / quota floor skips it too."""
    same = "shared passage".ljust(30, ".")
    foreign = _foreign(6)
    foreign[3] = _row("ref-3", relevance=foreign[3].relevance, notebook_id="ref",
                      text=same, source_id="s-ref")
    ranked = foreign + [
        _row("mine", relevance=0.4, text=same, source_id="s-mine"),
        _row("mine-2", relevance=0.3),
    ]
    budget = _tokens(ranked[:6])

    out = _cut(ranked, budget, active_seats=2)

    ids = _ids(out)
    assert "mine" not in ids and "mine-2" in ids, ids
    assert len({row.text for row in out}) == len(out), "one text, one seat"
    floor = enforce_active_floor(ranked[:6], ranked, 2, active_notebook_id=ACTIVE)
    assert "mine" not in _ids(floor) and "mine-2" in _ids(floor)


def test_peer_copy_of_a_selected_passage_takes_no_library_seat():
    same = "shared passage".ljust(30, ".")
    ranked = _foreign(8, library="nb-z") + [
        _row("nb-b-copy", relevance=0.35, notebook_id="nb-b", text=same),
        _row("nb-b-own", relevance=0.3, notebook_id="nb-b"),
    ]
    ranked[2] = _row("nb-z-2", relevance=ranked[2].relevance,
                     notebook_id="nb-z", text=same)
    out = _cut(ranked, _tokens(ranked[:5]), library_seats=2, active="nb-z")

    ids = _ids(out)
    assert "nb-b-copy" not in ids and "nb-b-own" in ids, ids
    assert len({row.text for row in out}) == len(out)


def test_active_seats_never_evict_a_graph_seat_holder():
    graph = _row("graph", relevance=0.5, notebook_id="ref", origin="ppr")
    ranked = [*_foreign(2), graph, *_foreign(4, library="ref2"),
              *[_row(f"mine-{i}", relevance=0.3) for i in range(4)]]
    budget = _tokens(ranked[:3])

    out = _cut(ranked, budget, active_seats=4, settings=_settings(graph=1))

    ids = _ids(out)
    assert "graph" in ids, ids
    assert any(i.startswith("mine-") for i in ids), ids
    assert _tokens(out) <= budget


def test_ratio_one_is_capped_by_eligible_rows_and_budget():
    active = [_row("mine-1", relevance=0.4), _row("mine-2", relevance=0.3)]
    ranked = _foreign(30) + active
    budget = _tokens(ranked[:10])

    out = _cut(ranked, budget, active_seats=cf.active_reserve_seats(
        _seat_settings(1.0)))

    assert len(out) == 10
    assert sum(1 for row in out if not row.notebook_id) == 2
    assert _tokens(out) <= budget


def test_ratio_zero_is_inert():
    ranked = _foreign(30) + [_row("mine", relevance=0.4)]
    budget = _tokens(ranked[:10])
    out = _cut(ranked, budget, active_seats=cf.active_reserve_seats(
        _seat_settings(0.0)))
    assert _ids(out) == _ids(_rule_off(ranked, budget))


# ------------------------------------------------------------- peer mode
def _peer_pool():
    big = _foreign(30, library="nb-a", top=0.9)
    mid = [
        _row(f"nb-b-{i}", relevance=0.35 - i * 0.01, notebook_id="nb-b")
        for i in range(3)
    ]
    small = [
        _row(f"nb-c-{i}", relevance=0.30 - i * 0.01, notebook_id="nb-c")
        for i in range(2)
    ]
    return big, mid, small


def test_peer_mode_keeps_every_library_with_eligible_rows_its_seats():
    big, mid, small = _peer_pool()
    ranked = big + mid + small
    budget = _tokens(ranked[:10])
    # nominal active = nb-a; the active rule gets no seats in peer mode.
    out = _cut(ranked, budget, library_seats=4, active="nb-a")

    by_library = {}
    for row in out:
        by_library[row.notebook_id] = by_library.get(row.notebook_id, 0) + 1
    # 4 seats dealt in best-hit order: nb-a, nb-b, nb-c, nb-a.
    assert by_library.get("nb-b", 0) >= 1 and by_library.get("nb-c", 0) >= 1, by_library
    assert _tokens(out) <= budget
    assert not {"nb-b", "nb-c"} & {row.notebook_id for row in _rule_off(ranked, budget)}


def test_peer_rule_is_inert_with_one_participating_library():
    ranked = _foreign(20, library="nb-a")
    assert library_reserve_rules(4, ranked, "nb-a") == ()
    budget = _tokens(ranked[:8])
    assert _ids(_cut(ranked, budget, library_seats=4, active="nb-a")) == _ids(
        _rule_off(ranked, budget))


def test_peer_rule_leaves_the_exact_per_library_split_intact():
    big, mid, small = _peer_pool()
    exact = [
        _row("exact-b", relevance=0.2, notebook_id="nb-b", origin="lexical", exact=True),
        _row("exact-c", relevance=0.2, notebook_id="nb-c", origin="lexical", exact=True),
    ]
    ranked = big + mid + small + exact
    budget = _tokens(ranked[:8])
    rules = mix_reserve_rules(_settings(exact=2), ranked, exact, "nb-a",
                              active_seats=0, library_seats_total=4)
    # graph, two exact rules (one per library), an inert active rule, then the
    # per-library rules -- exact before library.
    assert [rule.reserve for rule in rules[:3]] == [0, 1, 1]
    assert rules[3].reserve == 0

    out = select_with_reserves_baseline_first(ranked, budget, rules)
    assert {"exact-b", "exact-c"} <= set(_ids(out)), _ids(out)
    assert _tokens(out) <= budget


# ---------------------------------------------------------- determinism
def test_equal_relevance_ties_are_deterministic_across_shuffles():
    rng = random.Random(20260929)
    foreign = _foreign(12, step=0.0)       # every foreign row ties at 0.85
    active = [_row(f"mine-{i}", relevance=0.3) for i in range(5)]
    budget = _tokens(foreign[:6])
    for _ in range(50):
        f, a = list(foreign), list(active)
        rng.shuffle(f)
        rng.shuffle(a)
        ranked = f + a
        first = _cut(ranked, budget, active_seats=4)
        again = _cut(list(ranked), budget, active_seats=4)
        assert _ids(first) == _ids(again)
        assert sum(1 for row in first if not row.notebook_id) == 4
        assert _ids(first) == [c.chunk_id for c in ranked if c in first]
        # The seats go to the first-ranked eligible own rows.
        assert [row.chunk_id for row in first if not row.notebook_id] == [
            row.chunk_id for row in a[:4]]


def test_peer_library_order_is_deterministic_across_shuffles():
    rng = random.Random(7)
    libraries = [
        [_row(f"{lib}-{i}", relevance=0.3, notebook_id=lib) for i in range(2)]
        for lib in ("nb-a", "nb-b", "nb-c", "nb-d")
    ]
    dominant = _foreign(20, library="nb-z", top=0.9)
    for _ in range(50):
        tail = [row for rows in libraries for row in rows]
        rng.shuffle(tail)
        ranked = dominant + tail
        budget = _tokens(ranked[:8])
        first = _cut(ranked, budget, library_seats=3, active="nb-z")
        assert _ids(first) == _ids(_cut(list(ranked), budget, library_seats=3,
                                        active="nb-z"))
        # Tied best hits: libraries in order of their first row in ``ranked``.
        order = []
        for row in tail:
            if row.notebook_id not in order:
                order.append(row.notebook_id)
        got = {row.notebook_id for row in first} - {"nb-z"}
        assert got == set(order[:2]), (got, order)


# ----------------------------------------------------- MMR / quota floor
def test_enforce_active_floor_reads_a_raw_own_id_stamp_as_active_when_told():
    peers = [_row(f"b{i}", relevance=0.9, notebook_id="ref") for i in range(4)]
    own_ppr = _row("own", relevance=0.9, notebook_id=ACTIVE)
    selected = [own_ppr, *peers]
    spare = [_row("mine", relevance=0.4)]

    # With the real id, the raw own-id row already holds the one seat.
    assert enforce_active_floor(selected, selected + spare, 1,
                                active_notebook_id=ACTIVE) is selected
    # An empty id: the historical reading treats that stamp as a peer.
    out = enforce_active_floor(selected, selected + spare, 1,
                               active_notebook_id="")
    assert "mine" in _ids(out)


@pytest.mark.parametrize("spare", [
    _row("mine", relevance=RELEVANCE_FLOOR / 2),
    _row("mine", relevance=0.5, origin="ppr"),
], ids=["below_floor", "graph_only"])
def test_mmr_and_quota_floors_never_pull_in_an_ineligible_active_row(spare):
    """``enforce_active_floor``'s spare candidates pass the same predicate as
    the mix seats: below ``RELEVANCE_FLOOR`` or graph-only is never pulled in,
    on either the MMR branch (``apply_active_reserve``) or the quota branch
    (``quota_fuse_baseline_first`` over a ``FederatedCollected``)."""
    from app.services.retrieval import quota_fuse_baseline_first

    foreign = _foreign(6)      # all rank above the spare: the fusion skips it
    pool = foreign + [spare]
    settings = _seat_settings(0.5, k=6)
    mmr = cf.apply_active_reserve(settings, list(foreign), pool, 6,
                                  active_notebook_id=ACTIVE)
    assert _ids(mmr) == _ids(foreign)

    collected = cf.with_active_reserve({row.chunk_id: row for row in pool}, 3)
    quota, _counts = quota_fuse_baseline_first(
        collected, [{row.chunk_id: row for row in pool}], 6,
        active_notebook_id=ACTIVE)
    assert _ids(quota) == _ids(foreign)
    # Control: an eligible spare at the same rank IS pulled in on both branches.
    ok = _row("mine", relevance=0.5)
    assert "mine" in _ids(cf.apply_active_reserve(
        settings, list(foreign), foreign + [ok], 6, active_notebook_id=ACTIVE))
    both = cf.with_active_reserve(
        {row.chunk_id: row for row in foreign + [ok]}, 3)
    assert "mine" in _ids(quota_fuse_baseline_first(
        both, [dict(both)], 6, active_notebook_id=ACTIVE)[0])


def _withheld(lane, *, min_relevance=0.0, relative=0.0):
    candidates = SimpleNamespace(settings=_seat_settings(0.25, k=8))
    return cf._withheld_active(
        candidates, [lane], 100, min_relevance=min_relevance,
        relative_relevance=relative, active_notebook_id=ACTIVE)


def test_withheld_floor_reads_the_peak_off_the_whole_active_lane():
    """A graph-only own row can hold the lane's peak without being eligible;
    the relative floor is still measured from it, as ``peer_evidence`` would
    measure the lane."""
    lane = [_row("peak", relevance=0.9, origin="ppr"),
            _row("mid", relevance=0.5), _row("low", relevance=0.3)]
    kept, _unqualified = _withheld(lane, relative=0.6)
    assert kept == []                     # floor 0.54 rejects mid and low
    kept, _unqualified = _withheld(lane[1:], relative=0.6)
    assert _ids(kept) == ["mid", "low"]   # control: peak 0.5 -> floor 0.30


def test_withheld_reports_a_sub_floor_lane_row_it_never_withholds():
    lane = [_row("strong", relevance=0.8), _row("weak", relevance=0.05)]
    kept, unqualified = _withheld(lane, min_relevance=0.1)
    assert _ids(kept) == ["strong"]
    assert "weak" in unqualified


# --------------------------------------------------------- ask_chunk wiring
def _ask_with_pool(monkeypatch, pool, *, peer=False, reserve=0.25):
    from app.core.config import Settings
    from tests.test_ask_service_boundary import (
        _MinimalCandidates, _MinimalEvidence, _minimal_ask_service,
    )

    captured = {}

    class _Candidates(_MinimalCandidates):
        def mixed_chunk_candidates(self, notebook_id, query, high_level, queries):
            return list(pool), "", {}, [], 0

    class _Evidence(_MinimalEvidence):
        def chunk_citations(self, chunks, *, notebook_id, anchors=None):
            captured["selected"] = [chunk.chunk_id for chunk in chunks]
            return []

    if peer:
        monkeypatch.setattr(cf, "federated_ask_active", lambda: True)
    service = _minimal_ask_service()
    service.candidates = _Candidates()
    service.evidence_context = _Evidence()
    service.settings = Settings(
        query_rewrite_enabled=False, graph_ppr_enabled=False,
        chunk_federation_active_reserve=reserve,
    )
    service.ask_chunk(ACTIVE, AskRequest(question="q"), user_id="user")
    return captured["selected"]


def _big_library(library="ref"):
    # 40 x ~1000 tokens against the default 30000 - 2000 mix budget.
    return _foreign(40, library=library, tokens=1167)


def test_ask_chunk_mix_holds_the_active_seats(monkeypatch):
    pool = _big_library() + [
        _row("mine-1", relevance=0.4, tokens=100),
        _row("mine-2", relevance=0.3, tokens=100),
    ]
    with_seats = _ask_with_pool(monkeypatch, pool)
    without = _ask_with_pool(monkeypatch, pool, reserve=0.0)

    assert {"mine-1", "mine-2"} <= set(with_seats)
    assert not {"mine-1", "mine-2"} & set(without)


def test_ask_chunk_single_notebook_mix_is_unchanged(monkeypatch):
    pool = [
        _row(f"ppr-{i}", relevance=0.9, tokens=1167, notebook_id=ACTIVE,
             origin="ppr")
        for i in range(40)
    ] + [_row("mine", relevance=0.3, tokens=100)]
    selected = _ask_with_pool(monkeypatch, pool)
    assert selected == _ask_with_pool(monkeypatch, pool, reserve=0.0)
    assert "mine" not in selected


def test_ask_chunk_peer_mix_shares_seats_per_library(monkeypatch):
    pool = _big_library("nb-z") + [
        _row("nb-b-1", relevance=0.3, tokens=100, notebook_id="nb-b"),
        _row("nb-c-1", relevance=0.3, tokens=100, notebook_id="nb-c"),
    ]
    selected = _ask_with_pool(monkeypatch, pool, peer=True)

    assert {"nb-b-1", "nb-c-1"} <= set(selected)


# ------------------------------------- chunk mode render cut (MMR / quota path)
def _render_rows(count, *, library="ref", length=300, top=0.9):
    return [_row(f"{library}-{i}", relevance=top - i * 0.01, notebook_id=library,
                 text=f"{library}-{i} ".ljust(length, "x"))
            for i in range(count)]


def _floored_selection():
    """What ``enforce_active_floor`` hands the answer: the reserved own rows
    swapped into the TAIL positions of the finished selection."""
    foreign = _render_rows(6)
    mine = [_row(f"mine-{i}", relevance=0.3, text=f"mine-{i} ".ljust(300, "m"))
            for i in range(2)]
    return foreign + mine


def test_render_cut_spares_the_reserved_tail_rows():
    from app.services.retrieval import spare_reserved_rows

    rows = _floored_selection()
    budget = 5 * 305                       # five whole rows fit, not eight
    rule = active_reserve_rule(2, rows, ACTIVE)
    kept = spare_reserved_rows(rows, budget, rule, protected_chars=budget)
    ids = _ids(kept)
    assert ids[-2:] == ["mine-0", "mine-1"], ids
    assert ids == [row.chunk_id for row in rows if row in kept], "order kept"
    assert "ref-5" not in ids and "ref-4" not in ids   # lowest-ranked dropped


def test_render_cut_is_inert_when_everything_fits_or_nothing_is_foreign():
    from app.services.retrieval import spare_reserved_rows

    rows = _floored_selection()
    rule = active_reserve_rule(2, rows, ACTIVE)
    assert spare_reserved_rows(rows, 10**6, rule, protected_chars=10**6) is rows
    own = [_row(f"own-{i}", relevance=0.9, notebook_id=ACTIVE, origin="ppr",
                text=f"own-{i} ".ljust(300, "o")) for i in range(6)] + rows[-2:]
    inert = active_reserve_rule(2, own, ACTIVE)
    assert inert.reserve == 0
    assert spare_reserved_rows(own, 5 * 305, inert, protected_chars=5 * 305) is own
    assert spare_reserved_rows(rows, 5 * 305, active_reserve_rule(0, rows, ACTIVE),
                               protected_chars=5 * 305) is rows


def test_reserved_rows_longer_than_the_seat_share_do_not_displace_strong_rows():
    """Four 900-char reserved rows against a 3000-char budget: their seat
    share (3000 x 4 / 16 = 750) holds none of them, so ranking order decides
    and the strong reference rows keep their place (no flip to all-reserved)."""
    from app.services.retrieval import spare_reserved_rows

    rows = _render_rows(12, length=250) + [
        _row(f"mine-{i}", relevance=0.3, text=f"mine-{i} ".ljust(900, "m"))
        for i in range(4)]
    rule = active_reserve_rule(4, rows, ACTIVE)
    assert spare_reserved_rows(rows, 3000, rule, protected_chars=3000 * 4 // 16) is rows
    # Normal case: a share that holds them still spares them.
    kept = spare_reserved_rows(rows, 3000, rule, protected_chars=2000)
    assert "mine-0" in _ids(kept) and "mine-1" in _ids(kept) and "ref-0" in _ids(kept)


def test_render_cut_estimates_after_the_renderers_de_duplication():
    """A same-source same-text copy is dropped by ``chunk_context`` before it
    renders, so it must not make a row that fits look like it does not."""
    from app.services.retrieval import spare_reserved_rows

    rows = _floored_selection()
    dup = _row("ref-0-dup", relevance=0.5, notebook_id="ref", text=rows[0].text,
               source_id=rows[0].source_id)
    rows = rows[:6] + [dup] + rows[6:]
    budget = 8 * 305                      # the eight distinct rows fit exactly
    rule = active_reserve_rule(2, rows, ACTIVE)
    assert spare_reserved_rows(rows, budget, rule, protected_chars=budget) is rows


def test_answer_chunks_renders_the_reserved_rows_under_a_tight_budget(monkeypatch):
    import json

    from tests.test_ask_service_boundary import _minimal_ask_service
    from app.core.config import Settings

    captured = {}

    class _Echo:
        configured = True
        model = "m"

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({"answer": "结论。", "grounded": False})

    service = _minimal_ask_service()
    # 2 seats of k=4: the seats' share (762 chars) holds both reserved rows.
    service.settings = Settings(chunk_answer_budget_chars=5 * 305, chunk_mmr_k=4,
                                chunk_federation_active_reserve=0.5)

    def _context(chunks, notebook_id="", budget_chars=None, id_offset=0):
        captured["ids"] = _ids(chunks)
        return "", {}

    monkeypatch.setattr(service, "_chunk_answer_context", _context)
    rows = _floored_selection()
    service._answer_chunks("q", rows, notebook_id=ACTIVE, llm_client=_Echo())
    # The render really was cut, and the cut took the lowest-ranked
    # unreserved rows -- not the reserved tail.
    assert captured["ids"] == ["ref-0", "ref-1", "ref-2", "mine-0", "mine-1"]
    service.settings = Settings(chunk_answer_budget_chars=10**6, chunk_mmr_k=4,
                                chunk_federation_active_reserve=0.5)
    service._answer_chunks("q", rows, notebook_id=ACTIVE, llm_client=_Echo())
    assert captured["ids"] == _ids(rows)


def test_peer_mix_hands_on_a_seat_whose_only_passage_is_a_copy():
    shared = "one passage held by two libraries".ljust(30, ".")
    ranked = [
        _row("z-0", relevance=0.9, notebook_id="nb-z", text=shared),
        *[_row(f"nb-z-{i}", relevance=0.89 - i * 0.01, notebook_id="nb-z",
               origin="ppr") for i in range(8)],   # Z: capacity exactly one
        _row("b-0", relevance=0.35, notebook_id="nb-b", text=shared),
        _row("c-0", relevance=0.3, notebook_id="nb-c"),
        _row("c-1", relevance=0.29, notebook_id="nb-c"),
    ]
    out = _cut(ranked, _tokens(ranked[:4]), library_seats=3, active="nb-z")
    ids = _ids(out)
    assert {"z-0", "c-0", "c-1"} <= set(ids), ids
    assert "b-0" not in ids


def test_graph_and_exact_rules_keep_the_historical_text_identity():
    """Only the library seats dedupe by text; the graph and exact rules still
    pull in a row whose text another selected row carries."""
    from app.services.retrieval import ReserveRule, exact_section_reserve_rule

    assert ReserveRule(reserve=1, holds=bool, admits=bool).distinct_text is False
    assert graph_reserve_rule(1).distinct_text is False
    assert exact_section_reserve_rule(1, {"x"}).distinct_text is False
    same = "same text".ljust(30, ".")
    exact = _row("exact", relevance=0.1, notebook_id="ref", origin="lexical",
                 exact=True, text=same)
    ranked = [_row("top", relevance=0.9, notebook_id="ref", text=same),
              *_foreign(6, library="ref2"), exact]
    out = _cut(ranked, _tokens(ranked[:3]), exact_hits=[exact],
               settings=_settings(exact=1))
    assert "exact" in _ids(out)


def test_mix_seat_identity_is_the_raw_text():
    foreign = _foreign(6)
    foreign[2] = _row("ref-2", relevance=foreign[2].relevance, notebook_id="ref",
                      text="x".ljust(30, "."))
    ranked = foreign + [_row("mine", relevance=0.4, text=" x".ljust(31, "."))]
    out = _cut(ranked, _tokens(ranked[:5]), active_seats=1)
    assert "mine" in _ids(out)
    floor = enforce_active_floor(ranked[:5], ranked, 1, active_notebook_id=ACTIVE)
    assert "mine" in _ids(floor)


def test_peer_seat_skipped_after_an_exact_rule_copy_is_handed_on():
    """B's only passage is the first copy of its text, but the exact-seat rule
    (earlier in priority) pulled in A's lower-ranked copy of the same text;
    B's candidate is then skipped as a duplicate.  Its seat goes to the next
    library with candidates (C) instead of staying idle."""
    def row(cid, lib, rel, text=None, exact=False):
        return _row(cid, relevance=rel, notebook_id=lib, text=(text or cid).ljust(30, "x"),
                    origin="lexical" if exact else "semantic", exact=exact)

    ranked = [row(f"A{i}", "A", 0.9 - i * 0.01) for i in range(6)] + [
        row("B1", "B", 0.5, text="T")] + [
        row(f"C{i}", "C", 0.45 - i * 0.01) for i in range(3)] + [
        row("A9", "A", 0.2, text="T", exact=True)]
    out = _cut(ranked, _tokens(ranked[:5]), library_seats=3,
               exact_hits=[ranked[-1]], settings=_settings(exact=1), active="A")
    ids = _ids(out)
    assert "A9" in ids and "B1" not in ids
    assert {"C0", "C1"} <= set(ids), ids
