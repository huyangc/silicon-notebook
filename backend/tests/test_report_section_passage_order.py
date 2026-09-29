"""Deep-report section drafting orders its passage partition like Ask synthesis.

`ReportEngine._draft_section` used to hand `result.chunks` to the renderer in
insertion order (outline-bound passages first). With the first-round passage seed
decoupled from the knowledge graph, insertion order puts the whole seeded batch
ahead of the passages the model fetched on purpose during reflection, and the
character budget cut the latter. Unbound passages now go through the same
`retrieval.order_reasoning_passages` Ask uses; bound passages still come first.
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
    monkeypatch.setattr(
        "app.services.retrieval.order_reasoning_passages",
        lambda chunks, *, exact_reserve: list(chunks))
    captured = _draft(repo, monkeypatch)

    assert captured["order"][0] == "bound"
    assert "action" not in captured["rendered"]
