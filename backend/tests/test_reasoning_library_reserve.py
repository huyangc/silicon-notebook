"""Reasoning passage order: the active notebook's (or, in a peer run, every
library's) reserved prefix behind the exact prefix -- PR-C, C3.

`retrieval.reasoning_passage_order` step 4 (`promote_library_prefix` over
`library_floor_rules`, the same rules the mix cut uses): after the exact
prefix, stably move up to `seats - (active passages the exact prefix already
holds)` eligible active passages, in their current order, directly behind it.
A reordering only.  Inert when no FOREIGN passage exists, which is why the
real active id must be passed: reasoning's PPR passages carry the active
notebook's own id.  Peer runs share the seats per library, exactly like mix.
"""
from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from app.domain.retrieval import RetrievalSupport, RetrievedChunk
from app.services import chunk_federation as cf
from app.services.chunk_federation import reasoning_order_for
from app.services.retrieval import (
    RELEVANCE_FLOOR,
    order_reasoning_passages,
    reasoning_passage_order,
)
from tests.test_exact_lookup import repo  # noqa: F401  (fixture)

ACTIVE = "nb-active"


def _p(chunk_id, relevance, *, notebook_id="", origin="semantic", exact=False,
       text=None):
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=f"s-{chunk_id}", source_title="t",
        section_path="", text=text if text is not None else f"{chunk_id} body",
        relevance=relevance, score=relevance, notebook_id=notebook_id,
        retrieval_supports=(RetrievalSupport(
            origin, "ppr" if origin == "ppr" else "chunk", chunk_id, relevance),),
        exact_lookup=exact,
    )


def _ids(rows):
    return [row.chunk_id for row in rows]


def _order(chunks, *, exact=4, active=0, library=0, active_id=ACTIVE, held=()):
    return reasoning_passage_order(
        chunks, exact_reserve=exact, active_reserve=active,
        library_reserve=library, active_notebook_id=active_id, held=held)


def _pool():
    """A strong reference library, a weak active notebook, one exact section."""
    foreign = [_p(f"ref-{i}", 0.9 - i * 0.01, notebook_id="ref") for i in range(8)]
    mine = [_p(f"mine-{i}", 0.3 - i * 0.01) for i in range(4)]
    exact = [_p("exact-ref", 1.0, notebook_id="ref", exact=True),
             _p("exact-mine", 1.0, exact=True)]
    return foreign + mine + exact


# ------------------------------------------------------------- byte identity
def test_single_notebook_order_is_byte_identical_with_own_id_ppr_stamps():
    """PPR passages carry the active notebook's OWN id; with the real id the
    floor sees no foreign passage and step 4 moves nothing."""
    chunks = (
        [_p(f"ppr-{i}", 1.0 - i * 0.1, notebook_id=ACTIVE, origin="ppr")
         for i in range(6)]
        + [_p(f"seed-{i}", 0.4 - i * 0.02) for i in range(6)]
        + [_p("exact", 1.0, exact=True)]
    )
    before = order_reasoning_passages(chunks, exact_reserve=4)
    got = _order(chunks, active=4)
    assert _ids(got.passages) == _ids(before)
    assert got.prefix == 1
    # The harness is live: an empty id reads the own-id stamp as foreign.
    assert _ids(_order(chunks, active=4, active_id="").passages) != _ids(before)


def test_seatless_order_is_the_three_step_order():
    chunks = _pool()
    assert _ids(_order(chunks, active=0).passages) == _ids(
        order_reasoning_passages(chunks, exact_reserve=4))


# ---------------------------------------------------------------- the prefix
def test_active_prefix_sits_directly_behind_the_exact_prefix():
    got = _order(_pool(), exact=4, active=3)
    ids = _ids(got.passages)
    assert ids[:2] == ["exact-ref", "exact-mine"]
    # 3 seats, one already held by the active exact passage -> 2 promoted.
    assert ids[2:4] == ["mine-0", "mine-1"]
    assert got.prefix == 4
    assert sorted(ids) == sorted(_ids(_pool())), "a reordering drops nothing"


def test_exact_prefix_actives_reduce_the_seats():
    chunks = _pool()
    held_by_exact = _order(chunks, exact=4, active=2)
    assert _ids(held_by_exact.passages)[2] == "mine-0"
    assert held_by_exact.prefix == 3        # 2 exact + 1 active
    no_exact = [chunk for chunk in chunks if not chunk.exact_lookup]
    assert _order(no_exact, exact=4, active=2).prefix == 2


def test_ineligible_active_passages_are_skipped_and_one_text_spends_one_seat():
    foreign = [_p(f"ref-{i}", 0.9, notebook_id="ref") for i in range(4)]
    chunks = foreign + [
        _p("graph-only", 0.8, origin="ppr", notebook_id=ACTIVE),
        _p("weak", RELEVANCE_FLOOR / 2),
        _p("gq", 0.7, origin="generated_question"),
        _p("twin-a", 0.5, text="same passage"),
        _p("twin-b", 0.45, text="same passage"),
        _p("ok", 0.2),
    ]
    got = _order(chunks, exact=0, active=3)
    assert _ids(got.passages)[:2] == ["twin-a", "ok"]
    assert got.prefix == 2


def test_an_active_copy_of_a_passage_already_ahead_takes_no_seat():
    """Seat identity is the text across libraries: the exact prefix already
    carries the passage through the reference library's copy."""
    shared = "the same passage in two libraries"
    chunks = [_p(f"ref-{i}", 0.9, notebook_id="ref") for i in range(4)] + [
        _p("exact-ref", 1.0, notebook_id="ref", exact=True, text=shared),
        _p("mine-copy", 0.5, text=shared),
        _p("mine-own", 0.3),
    ]
    got = _order(chunks, exact=4, active=2)
    ids = _ids(got.passages)
    assert ids[:2] == ["exact-ref", "mine-own"]
    assert got.prefix == 2


def test_no_eligible_active_passage_moves_nothing():
    chunks = [_p(f"ref-{i}", 0.9, notebook_id="ref") for i in range(4)] + [
        _p("weak", 0.05)]
    got = _order(chunks, exact=0, active=4)
    assert _ids(got.passages) == _ids(order_reasoning_passages(chunks, exact_reserve=0))
    assert got.prefix == 0


def test_held_passages_spend_seats_without_moving():
    """Deep report: outline-bound passages are rendered ahead and counted."""
    chunks = [chunk for chunk in _pool() if not chunk.exact_lookup]
    bound = [_p("bound-mine", 0.1), _p("bound-mine-2", 0.1)]
    got = _order(chunks, exact=0, active=3, held=bound)
    assert _ids(got.passages)[:2] == ["mine-0", "ref-0"]
    assert got.prefix == 1
    assert _order(chunks, exact=0, active=3).prefix == 3


# ------------------------------------------------------------------- peer mode
def _peer_pool():
    big = [_p(f"nb-z-{i}", 0.9 - i * 0.01, notebook_id="nb-z") for i in range(10)]
    return big + [
        _p("nb-b-0", 0.35, notebook_id="nb-b"),
        _p("nb-b-1", 0.34, notebook_id="nb-b"),
        _p("nb-c-0", 0.3, notebook_id="nb-c"),
    ]


def test_peer_run_shares_the_prefix_per_library():
    got = _order(_peer_pool(), exact=0, library=3, active_id="nb-z")
    # best-hit order z, b, c; one seat each -- z already leads, so b and c move.
    assert _ids(got.passages)[:3] == ["nb-z-0", "nb-b-0", "nb-c-0"]
    assert got.prefix == 3


def test_peer_run_with_one_library_is_inert():
    pool = [chunk for chunk in _peer_pool() if chunk.notebook_id == "nb-z"]
    got = _order(pool, exact=0, library=3, active_id="nb-z")
    assert _ids(got.passages) == _ids(order_reasoning_passages(pool, exact_reserve=0))
    assert got.prefix == 0


def test_reasoning_order_for_reads_the_seats_from_settings(monkeypatch):
    settings = SimpleNamespace(
        reasoning_exact_reserve=4, chunk_mmr_k=8,
        chunk_federation_active_reserve=0.25)
    got = reasoning_order_for(settings, _pool(), ACTIVE)
    assert got == _order(_pool(), exact=4, active=2)
    monkeypatch.setattr(cf, "federated_ask_active", lambda: True)
    peer = reasoning_order_for(settings, _peer_pool(), "nb-z")
    assert peer == _order(_peer_pool(), exact=4, library=2, active_id="nb-z")


# ----------------------------------------------------------------- determinism
@pytest.mark.parametrize("peer", [False, True])
def test_equal_relevance_ties_are_deterministic_across_shuffles(peer):
    rng = random.Random(20260929)
    if peer:
        base = [_p(f"nb-z-{i}", 0.9, notebook_id="nb-z") for i in range(6)]
        tail = [_p(f"{lib}-{i}", 0.3, notebook_id=lib)
                for lib in ("nb-a", "nb-b", "nb-c") for i in range(2)]
        kwargs = dict(exact=0, library=3, active_id="nb-z")
    else:
        base = [_p(f"ref-{i}", 0.9, notebook_id="ref") for i in range(6)]
        tail = [_p(f"mine-{i}", 0.3) for i in range(6)]
        kwargs = dict(exact=0, active=3)
    for _ in range(50):
        shuffled = list(tail)
        rng.shuffle(shuffled)
        chunks = base + shuffled
        first = _order(chunks, **kwargs)
        assert first == _order(list(chunks), **kwargs)
        promoted = _ids(first.passages[:first.prefix])
        if peer:
            libs = []
            for chunk in shuffled:
                if chunk.notebook_id not in libs:
                    libs.append(chunk.notebook_id)
            # nb-z leads and holds one seat; the other two go to the first two
            # tied libraries in input order, their first-ranked copy each.
            expected = ["nb-z-0"] + [
                next(c.chunk_id for c in shuffled if c.notebook_id == lib)
                for lib in libs[:2]]
            assert sorted(promoted) == sorted(expected)
        else:
            assert promoted == _ids(shuffled[:3])


# ------------------------------------------------------- sectioned synthesis
def test_sections_carry_no_unbound_active_passage():
    """J5: only exact hits are injected into every section; an unbound active
    passage enters none, whatever the seats."""
    from app.services.outline_synthesis import plan_outline_sections
    from app.services.reasoning_retrieval import OutlineSection

    chunk_by_id = {
        "ref-a": _p("ref-a", 0.9, notebook_id="ref"),
        "ref-b": _p("ref-b", 0.8, notebook_id="ref"),
        "mine-unbound": _p("mine-unbound", 0.3),
        "exact-unbound": _p("exact-unbound", 1.0, notebook_id="ref", exact=True),
    }
    slices, skipped = plan_outline_sections(
        [OutlineSection(id="a", title="A", evidence_keys=["ref-a"]),
         OutlineSection(id="b", title="B", evidence_keys=["ref-b"])],
        kg_by_id={}, element_by_id={}, chunk_by_id=chunk_by_id,
        exact_reserve=4)
    assert skipped == []
    for item in slices:
        carried = {c.chunk_id for c in [*item.chunks, *item.exact_chunks]}
        assert "mine-unbound" not in carried
        assert {c.chunk_id for c in item.exact_chunks} == {"exact-unbound"}


def test_a_section_promotes_its_own_bound_active_passages(repo, monkeypatch):  # noqa: F811
    """Inside one section the prefix works over the bound passages only; the
    injected exact copy stays in its own trailing segment."""
    import json

    from app.models.schemas import NotebookCreate
    from app.services.ask_service import AskService
    from tests.model_testkit import bind_chat_client

    class _Echo:
        configured = True
        model = "m"

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({"answer": "结论。", "grounded": False})

    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    bind_chat_client(repo, "ask_answer", _Echo())
    bound = [_p(f"ref-{i}", 0.9 - i * 0.01, notebook_id="ref") for i in range(6)]
    bound += [_p("mine-bound", 0.2)]
    trailing = [_p("exact-copy", 1.0, notebook_id="ref")]
    captured: list = []
    real = AskService._chunk_answer_context

    def _spy(self, chunks, budget_chars=None, notebook_id="", id_offset=0):
        captured.append([chunk.chunk_id for chunk in chunks])
        return real(self, chunks, budget_chars=budget_chars,
                    notebook_id=notebook_id, id_offset=id_offset)

    monkeypatch.setattr(AskService, "_chunk_answer_context", _spy)
    repo._runtime.ask_service()._answer_reasoning(
        notebook.id, "q", [], [], chunks=bound, sectioned=True,
        section_title="A", section_index=1, section_total=2,
        trailing_chunks=trailing, chunk_context_chars=10_000)
    assert captured[0][0] == "mine-bound", captured
    assert "exact-copy" not in captured[0]
    assert captured[1] == ["exact-copy"]


def test_reasoning_synthesis_passes_the_real_id(repo):  # noqa: F811
    """End to end through ``_answer_reasoning``: own-id-stamped PPR passages
    in a single-notebook run keep the three-step order."""
    import json

    from app.models.schemas import NotebookCreate
    from app.services.ask_service import AskService
    from tests.model_testkit import bind_chat_client

    class _Echo:
        configured = True
        model = "m"

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({"answer": "结论。", "grounded": False})

    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    bind_chat_client(repo, "ask_answer", _Echo())
    chunks = [_p(f"ppr-{i}", 1.0 - i * 0.1, notebook_id=notebook.id, origin="ppr")
              for i in range(6)] + [_p(f"seed-{i}", 0.4 - i * 0.02) for i in range(6)]
    captured: list = []
    real = AskService._chunk_answer_context

    def _spy(self, chunks, budget_chars=None, notebook_id="", id_offset=0):
        captured.append([chunk.chunk_id for chunk in chunks])
        return real(self, chunks, budget_chars=budget_chars,
                    notebook_id=notebook_id, id_offset=id_offset)

    AskService._chunk_answer_context = _spy
    try:
        repo._runtime.ask_service()._answer_reasoning(
            notebook.id, "q", [], [], chunks=chunks, chunk_context_chars=10_000)
    finally:
        AskService._chunk_answer_context = real
    assert captured[0] == _ids(order_reasoning_passages(
        chunks, exact_reserve=repo.settings.reasoning_exact_reserve))
