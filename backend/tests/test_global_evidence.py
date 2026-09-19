"""Cross-library selection, model-budget fairness and safe answer recovery."""
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

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


def test_context_reserves_other_library_before_admitting_near_budget_chunk():
    run = synthesis(RecordingModels(), budget=1000)
    large = chunk("large", text="x" * 700, notebook_id="a")
    a = chunk("small-a", text="a support", notebook_id="a")
    b = chunk("small-b", text="b support", notebook_id="b")
    context, identities = run._context([large, b, a], {})
    assert {row["notebook_id"] for row in identities.values()} == {"a", "b"}
    assert "a support" in context and "b support" in context
    assert len(context) <= 1000


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


def test_total_retrieval_budget_discloses_unsearched_remaining_notebooks(setup, monkeypatch):
    import time
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
    assert "总检索时限" in result.skipped_notebooks[1].reason


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
        progress = service.get_job(request.job_id, user_id="u")
        assert progress.status == "running"
        assert [row.notebook_id for row in progress.skipped_notebooks] == ["a"]
        stopped = service.cancel(request.job_id, user_id="u")
        assert stopped.skipped_notebooks == progress.skipped_notebooks
    finally:
        release.set()
    assert finished(service, request).status == "cancelled"
