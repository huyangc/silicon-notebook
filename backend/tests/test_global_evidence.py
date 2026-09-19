"""Cross-library selection, model-budget fairness and safe answer recovery."""
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
import time

import pytest

from app.models.ask import Citation
from app.models.global_ask import GlobalAskRequest
from app.services.ask_service import AskService
from app.services.global_evidence import peer_evidence
from tests.test_global_ask import setup, finished
from tests.test_global_ask_synthesis import chunk, synthesis, RecordingModels


def test_twenty_four_libraries_get_first_round_before_dominant_scores_repeat():
    pools = [[replace(chunk(f"{nb}-{i}", text=f"evidence {nb} {i}", notebook_id=f"nb-{nb}"),
                      relevance=1000 if nb == 0 else 0.01) for i in range(30)] for nb in range(24)]
    selected = peer_evidence(pools, 48)
    assert len(selected) == 48
    assert all(sum(hit.notebook_id == f"nb-{nb}" for hit in selected) == 2 for nb in range(24))


def test_duplicate_text_does_not_consume_other_library_unique_slot():
    selected = peer_evidence([
        [chunk("a", text="same", notebook_id="a")],
        [chunk("b", text="same", notebook_id="b"), chunk("b2", text="different", notebook_id="b")],
    ], 2)
    assert [hit.text for hit in selected] == ["same", "different"]


def test_focused_question_does_not_reserve_slots_for_noise_libraries():
    strong = [replace(chunk(f"a-{i}", text=f"relevant {i}", notebook_id="a"), relevance=0.9 - i * 0.01)
              for i in range(12)]
    noise = [[replace(chunk(f"n-{i}", text=f"weak {i}", notebook_id=f"noise-{i}"), relevance=0.13)]
             for i in range(31)]
    selected = peer_evidence([strong, *noise], 12, min_relevance=0.25, relative_relevance=0.6)
    assert selected == strong


def test_library_reservation_keeps_relevant_minority_but_not_its_weak_tail():
    strong = [replace(chunk(f"a-{i}", text=f"strong {i}", notebook_id="a"), relevance=0.9)
              for i in range(10)]
    minority = [replace(chunk("b", text="minority", notebook_id="b"), relevance=0.5),
                replace(chunk("weak", text="weak tail", notebook_id="b"), relevance=0.26)]
    selected = peer_evidence([strong, minority], 6, min_relevance=0.25, relative_relevance=0.6)
    assert selected[:2] == [strong[0], minority[0]]
    assert sum(hit.notebook_id == "a" for hit in selected) == 5
    assert minority[1] not in selected


def test_remaining_capacity_prefers_local_confidence_over_equal_turns():
    sustained = [replace(chunk(f"a-{i}", text=f"sustained {i}", notebook_id="a"), relevance=0.9)
                 for i in range(3)]
    falling = [replace(chunk("b0", text="b best", notebook_id="b"), relevance=0.9),
               replace(chunk("b1", text="b tail", notebook_id="b"), relevance=0.4)]
    selected = peer_evidence([sustained, falling], 4, min_relevance=0.25, relative_relevance=0.4)
    assert selected == [sustained[0], falling[0], sustained[1], sustained[2]]


def test_context_reserves_other_library_before_admitting_near_budget_chunk():
    run = synthesis(RecordingModels(), budget=1000)
    large = chunk("large", text="x" * 700, notebook_id="a")
    a = chunk("small-a", text="a support", notebook_id="a")
    b = chunk("small-b", text="b support", notebook_id="b")
    context, identities = run._context([large, b, a], {})
    assert {row["notebook_id"] for row in identities.values()} == {"a", "b"}
    assert "a support" in context and "b support" in context
    assert len(context) <= 1000


def test_context_does_not_replace_a_fitting_best_passage_with_a_weaker_tail():
    run = synthesis(RecordingModels(), budget=1000)
    best = chunk("a-best", text="a" * 500, notebook_id="a")
    tail = chunk("a-tail", text="t" * 300, notebook_id="a")
    other = chunk("b", text="b" * 100, notebook_id="b")
    _, refs = run._context([best, other, tail], {})
    assert {row["object_id"] for row in refs.values()} == {best.chunk_id, other.chunk_id}


def test_exact_dedup_preserves_semantically_different_code_indentation():
    a = chunk("a", text="if enabled:\n    run()\nfinish()", notebook_id="a")
    b = chunk("b", text="if enabled:\n    run()\n    finish()", notebook_id="b")
    assert peer_evidence([[a], [b]], 2) == [a, b]


def test_shared_answer_retry_recovers_empty_model_response():
    models = RecordingModels("")
    def after_call():
        if len(models.calls) == 2:
            models.answer = "recovered [k1]"
    models.after_call = after_call
    run = synthesis(models)
    owner = SimpleNamespace(model_errors=SimpleNamespace(note_model_error=lambda *a, **kw: None))
    run.answer_with_retry = lambda generate, label: AskService._answer_with_retry(owner, generate, label)
    answer, grounded, _, refs = run("q", [chunk()], {}, "", Event())
    assert answer == "recovered [k1]" and grounded and refs
    assert len(models.calls) == 2


def test_changed_later_element_rebuilds_only_from_surviving_notebook(setup):
    service, _, _, _ = setup
    original = service.retrieve
    def retrieve(nb, query):
        hits, ids, matrix = original(nb, query)
        if nb == "a":
            hits = [replace(hits[0], element_ids=["e-a", "e-a2"])]
        return hits, ids, matrix
    service.retrieve = retrieve
    fingerprints = {"e-a": ("s-a", "one"), "e-a2": ("s-a", "two"), "e-b": ("s-b", "three")}
    service.sources.evidence_fingerprints = lambda ids: {key: fingerprints[key] for key in ids if key in fingerprints}
    calls = []
    def synthesize(question, chunks, *args):
        calls.append([row.notebook_id for row in chunks])
        if len(calls) == 1:
            fingerprints["e-a2"] = ("s-a", "changed")
        hit = chunks[0]
        return f"claim {hit.notebook_id}", True, [], [Citation(
            label="source", source_id=hit.source_id, element_id=hit.element_ids[0],
            location_label="", quoted_span=hit.text, notebook_id=hit.notebook_id,
        )]
    service.synthesize = synthesize
    result = finished(service, service.start(GlobalAskRequest(question="q"), user_id="u"))
    assert result.status == "done" and result.response.answer == "claim b"
    assert calls == [["a", "b"], ["b"]]
    assert result.cited_notebook_ids == ["b"]


def test_revocation_during_validation_prevents_second_model_call(setup):
    service, readable, _, _ = setup
    reads, calls = [], []
    def fingerprints(ids):
        reads.append(ids)
        if not calls:
            return {"e-a": ("s-a", "old")}
        readable.clear()
        return {}
    service.sources.evidence_fingerprints = fingerprints
    def synthesize(*args):
        calls.append(True)
        return "claim", True, [], [Citation(label="s", source_id="s-a", element_id="e-a",
            location_label="", quoted_span="", notebook_id="a")]
    service.synthesize = synthesize
    result = finished(service, service.start(GlobalAskRequest(question="q"), user_id="u"))
    assert result.status == "failed" and result.response is None
    assert calls == [True]


def test_retrieval_snapshot_cannot_be_replaced_by_a_newer_validation_baseline(setup):
    from app.services.global_retrieval import GlobalRetrievalResult
    service, _, _, _ = setup
    original = service.retrieve
    def retrieve(nb, query):
        hits, ids, matrix = original(nb, query)
        return GlobalRetrievalResult(hits, ids, matrix, False, {f"e-{nb}": (f"s-{nb}", "old")})
    service.retrieve = retrieve
    service.sources.evidence_fingerprints = lambda ids: {key: ("s-" + key[2:], "new") for key in ids}
    def synthesize(question, chunks, *args):
        hit = chunks[0]
        return "stale claim", True, [], [Citation(label="s", source_id=hit.source_id,
            element_id=hit.element_ids[0], location_label="", quoted_span=hit.text, notebook_id=hit.notebook_id)]
    service.synthesize = synthesize
    result = finished(service, service.start(GlobalAskRequest(question="q"), user_id="u"))
    assert result.status == "done" and not result.response.grounded
    assert "stale claim" not in result.response.answer
    assert not result.response.citations


@pytest.mark.parametrize("setup", [{"global_ask_retrieval_concurrency": 1}], indirect=True)
def test_total_retrieval_budget_discloses_unsearched_remaining_notebooks(setup, monkeypatch):
    # One retrieval slot is what makes "the second library starts after the
    # first has already spent the whole phase budget" reachable at all; with a
    # wider pool both libraries start before the clock moves.
    service, _, retrieved, _ = setup
    clock = [time.monotonic()]
    monkeypatch.setattr("app.services.global_ask.time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = service.retrieve
    def retrieve(nb, query):
        result = original(nb, query)
        clock[0] += service.settings.global_ask_retrieval_timeout_seconds + 1
        return result
    service.retrieve = retrieve
    result = finished(service, service.start(GlobalAskRequest(question="q"), user_id="u"))
    assert result.status == "done"
    assert retrieved == ["a"]
    assert result.searched_notebook_ids == []
    assert [row.notebook_id for row in result.skipped_notebooks] == ["a", "b"]
    # "b" never issued a query: it was still queued when the phase budget ran
    # out, so the receipt must not blame the selected scope.
    assert result.skipped_notebooks[1].reason == "检索未开始，请稍后重试。"


def test_polling_and_cancellation_retain_completed_notebook_progress(setup):
    from app.services.global_retrieval import GlobalRetrievalSkipped
    service, _, _, _ = setup
    entered, release = Event(), Event()
    def retrieve(nb, query):
        if nb == "a":
            raise GlobalRetrievalSkipped("timeout")
        entered.set()
        assert release.wait(5)
        return [], [], None
    service.retrieve = retrieve
    request = service.start(GlobalAskRequest(question="progress"), user_id="u")
    try:
        assert entered.wait(5)
        # Libraries now retrieve concurrently, so "b is in flight" no longer
        # implies "a's receipt has already been persisted". Hand-shake on the
        # observable progress row instead of on a wall-clock assumption.
        progress = service.get_job(request.job_id, user_id="u")
        deadline = time.monotonic() + 5
        while not progress.skipped_notebooks and time.monotonic() < deadline:
            Event().wait(0.01)
            progress = service.get_job(request.job_id, user_id="u")
        assert progress.status == "running"
        assert [row.notebook_id for row in progress.skipped_notebooks] == ["a"]
        stopped = service.cancel(request.job_id, user_id="u")
        assert stopped.skipped_notebooks == progress.skipped_notebooks
    finally:
        release.set()
    assert finished(service, request).status == "cancelled"
