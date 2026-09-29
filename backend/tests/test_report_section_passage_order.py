"""Deep-report section drafting orders its passage partition like Ask synthesis.

`ReportEngine._draft_section` used to hand `result.chunks` to the renderer in
insertion order (outline-bound passages first). With the first-round passage seed
decoupled from the knowledge graph, insertion order puts the whole seeded batch
ahead of the passages the model fetched on purpose during reflection, and the
character budget cut the latter. Unbound passages now go through the same
`retrieval.reasoning_order_for` Ask uses (exact prefix, then the active /
per-library reserved prefix); bound passages still come first.
"""
from __future__ import annotations

import json

from app.domain.retrieval import RetrievalSupport, RetrievedChunk
from app.services.reasoning_retrieval import OutlineSection, ReasoningResult
from tests.test_report_engine import _mk_engine, _mk_nb, repo  # noqa: F401


class _SectionLLM:
    configured = True
    model = "m"

    def chat_json(self, *args, **kwargs):
        return json.dumps({"markdown": "本节正文。"})


def _passage(chunk_id, relevance, *, ppr=False, exact=False, length=1500):
    support = (RetrievalSupport("ppr", "ppr", "", relevance) if ppr
               else RetrievalSupport("semantic", "chunk", chunk_id, relevance))
    return RetrievedChunk(
        chunk_id=chunk_id, source_id="s1", source_title="t", section_path="1",
        text=(chunk_id + " ") * (length // (len(chunk_id) + 1)),
        relevance=relevance, score=relevance,
        retrieval_supports=(support,), exact_lookup=exact)


def _section_result():
    """Insertion order as a graph run produces it: PPR seed, exact seed, the
    first-round passage seed (ten long passages), then the passage one
    reflection action fetched. One passage is bound by the section's outline."""
    ppr = [_passage(f"ppr{i}", round(1.0 - i * 0.1, 2), ppr=True) for i in range(6)]
    exact = [_passage("exact", 1.0, exact=True, length=300)]
    seeds = [_passage(f"seed{i}", round(0.45 - i * 0.015, 3)) for i in range(10)]
    action = [_passage("action", 0.6, length=300)]
    bound = [_passage("bound", 0.2, length=300)]
    return ReasoningResult(
        chunks=ppr + exact + seeds + action + bound,
        outline=[OutlineSection(id="o1", title="节", evidence_keys=["bound"])],
    )


def _draft(repo, monkeypatch):
    eng = _mk_engine(repo, _SectionLLM())
    nb = _mk_nb(repo)
    context = eng.dependencies.evidence_context
    real = context.chunk_context
    captured: dict = {}

    def _spy(chunks, **kwargs):
        block, id_map = real(chunks, **kwargs)
        captured.setdefault("order", [chunk.chunk_id for chunk in chunks])
        captured.setdefault(
            "rendered", {entry["object_id"] for entry in id_map.values()})
        return block, id_map

    monkeypatch.setattr(context, "chunk_context", _spy)
    eng._draft_section(nb.id, {"title": "A", "scope": "a"}, "问题",
                       _section_result(), depth=1)
    return captured


def test_bound_first_then_exact_then_the_two_lanes_alternate(repo, monkeypatch):
    captured = _draft(repo, monkeypatch)

    assert captured["order"][:7] == [
        "bound", "exact", "ppr0", "action", "ppr1", "seed0", "ppr2"]
    # The passage reflection fetched on purpose is inside the overview budget.
    assert {"bound", "exact", "action"} <= captured["rendered"]
    assert len(captured["rendered"]) < len(captured["order"]), (
        "the budget must actually truncate, or this proves nothing")


def test_control_insertion_order_pushes_the_action_passage_out(repo, monkeypatch):
    """The same section with the old insertion order: the seeded batch spends
    the budget and the action passage never reaches the prompt."""
    from app.services.retrieval import ReasoningPassageOrder

    monkeypatch.setattr(
        "app.services.chunk_federation.reasoning_order_for",
        lambda settings, chunks, notebook_id, *, held=(): ReasoningPassageOrder(
            list(chunks), 0))
    captured = _draft(repo, monkeypatch)

    assert captured["order"][0] == "bound"
    assert "action" not in captured["rendered"]


# ------------------------------------------------ library reserve (PR-C, C3)
def _stamped(chunk_id, relevance, notebook_id, **kwargs):
    from dataclasses import replace

    return replace(_passage(chunk_id, relevance, **kwargs), notebook_id=notebook_id)


def test_single_notebook_report_order_is_unchanged_with_own_id_ppr(repo, monkeypatch):
    """Concept-walk passages carry the notebook's own id: no foreign passage,
    so the active prefix is inert and the order is the three-step order."""
    from app.services.retrieval import reasoning_passage_order

    eng = _mk_engine(repo, _SectionLLM())
    nb = _mk_nb(repo)
    result = _section_result()
    chunks = [
        _stamped(c.chunk_id, c.relevance, nb.id, ppr=True)
        if c.chunk_id.startswith("ppr") else c
        for c in result.chunks
    ]
    got = eng._section_passage_order(chunks, {"bound"}, nb.id)
    unbound = [c for c in chunks if c.chunk_id != "bound"]
    expected = ["bound"] + [c.chunk_id for c in reasoning_passage_order(
        unbound, exact_reserve=eng.settings.reasoning_exact_reserve,
        active_reserve=0, library_reserve=0, active_notebook_id=nb.id).passages]
    assert [c.chunk_id for c in got] == expected


def test_report_order_is_bound_then_exact_then_active_prefix(repo):
    eng = _mk_engine(repo, _SectionLLM())
    nb = _mk_nb(repo)
    foreign = [_stamped(f"ref{i}", round(0.9 - i * 0.01, 2), "ref") for i in range(8)]
    mine = [_passage(f"mine{i}", round(0.3 - i * 0.01, 2)) for i in range(5)]
    exact = [_stamped("exact", 1.0, "ref", exact=True)]
    bound = [_stamped("bound", 0.2, "ref")]
    got = [c.chunk_id for c in eng._section_passage_order(
        foreign + mine + exact + bound, {"bound"}, nb.id)]
    seats = 4   # ceil(chunk_mmr_k 16 x 0.25)
    assert got[:2] == ["bound", "exact"]
    assert got[2:2 + seats] == [f"mine{i}" for i in range(seats)]
    assert sorted(got) == sorted(
        c.chunk_id for c in foreign + mine + exact + bound)


def test_bound_active_passages_spend_report_seats(repo):
    eng = _mk_engine(repo, _SectionLLM())
    nb = _mk_nb(repo)
    foreign = [_stamped(f"ref{i}", round(0.9 - i * 0.01, 2), "ref") for i in range(8)]
    mine = [_passage(f"mine{i}", round(0.3 - i * 0.01, 2)) for i in range(5)]
    bound = [_passage(f"bound{i}", 0.1) for i in range(3)]
    got = [c.chunk_id for c in eng._section_passage_order(
        foreign + mine + bound, {f"bound{i}" for i in range(3)}, nb.id)]
    assert got[:4] == ["bound0", "bound1", "bound2", "mine0"]
    assert got[4] == "ref0"


def test_peer_report_order_shares_the_prefix_per_library(repo, monkeypatch):
    from app.services import chunk_federation as cf

    monkeypatch.setattr(cf, "federated_ask_active", lambda: True)
    eng = _mk_engine(repo, _SectionLLM())
    nb = _mk_nb(repo)
    big = [_stamped(f"z{i}", round(0.9 - i * 0.01, 2), "nb-z") for i in range(10)]
    small = [_stamped("b0", 0.3, "nb-b"), _stamped("c0", 0.25, "nb-c")]
    got = [c.chunk_id for c in eng._section_passage_order(big + small, set(), nb.id)]
    # 4 seats round-robin in best-hit order z, b, c, z; picks keep their order.
    assert got[:4] == ["z0", "z1", "b0", "c0"]
    # One participant: inert.
    alone = [c.chunk_id for c in eng._section_passage_order(big, set(), nb.id)]
    assert alone == [c.chunk_id for c in big]
