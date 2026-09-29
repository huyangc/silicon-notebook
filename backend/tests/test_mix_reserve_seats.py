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
    # Default active id "": the historical "empty stamp == active" reading.
    assert is_active_hit(_row("a", notebook_id=""))
    assert not is_active_hit(_row("a", notebook_id=ACTIVE))


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
    # Default "": the historical reading treats that stamp as a peer.
    out = enforce_active_floor(selected, selected + spare, 1)
    assert "mine" in _ids(out)


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
