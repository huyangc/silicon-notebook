"""A chunk recall during which any lane failed open is not kept for reuse.

One case per failure point the recall memo listens to
(``note_recall_failure``, directly or through ``_note_model_error`` or a
noting hook): each injects exactly that failure, on the lane that reaches it,
and requires the run to keep nothing -- next to a healthy control on the
same lane that does keep the recall, so no case is vacuous.  The structural
half (every handler on the tree notes, re-raises or is registered) is
``test_chunk_recall_failure_guard``.
"""
from __future__ import annotations

import pytest

from app.domain.retrieval import RetrievedChunk
from app.repositories.ports import ChunkLexicalSearchTimeout
from app.services.retrieval_run import retrieval_run
from tests import test_chunk_recall_memo as _memo_tests

# The same world as the memo's own cases: its fixtures, re-bound here.
repo = _memo_tests.repo
library = _memo_tests.library
TEXT = _memo_tests.TEXT
_add_source = _memo_tests._add_source

QUERY = "bandgap reference"


def _stores(call, *, actor_id: str = "", run_kind: str = "ask_reasoning") -> int:
    with retrieval_run(run_kind=run_kind, actor_id=actor_id) as run:
        call()
    return run.chunk_recall_stores


def _raiser(exc):
    def raise_it(*_args, **_kwargs):
        raise exc
    return raise_it


# ---------------------------------------------------------------------------
# The FTS-degraded lane (a large library without a scale index)
# ---------------------------------------------------------------------------

def test_the_degraded_lane_keeps_a_healthy_recall(library):
    repo, _owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates
    assert _stores(lambda: candidates._retrieve_chunks(nb, QUERY)) == 1


@pytest.mark.parametrize("failure", [
    ChunkLexicalSearchTimeout("slow"), RuntimeError("down"),
], ids=["timeout", "failure"])
def test_a_degraded_lane_whose_fts_failed_keeps_nothing(library, monkeypatch, failure):
    repo, _owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(candidates.knowledge, "chunk_fts_search", _raiser(failure))
    assert _stores(lambda: candidates._retrieve_chunks(nb, QUERY)) == 0


def test_a_failed_language_probe_keeps_nothing(library, monkeypatch):
    repo, _owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(candidates.settings, "lexical_language_gate_enabled", True)
    monkeypatch.setattr(candidates, "_notebook_langs", _raiser(RuntimeError("x")))
    assert _stores(lambda: candidates._retrieve_chunks(nb, QUERY)) == 0


def test_a_failed_drift_probe_keeps_nothing(library, monkeypatch):
    repo, _owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(candidates, "_lexical_gate_drift", _raiser(RuntimeError("x")))
    assert _stores(lambda: candidates._retrieve_chunks(nb, QUERY)) == 0


def test_a_failed_query_embedding_keeps_nothing(library, monkeypatch):
    repo, _owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates

    def embed(self, query):
        self._note_model_error("embed", RuntimeError("down"),
                               workload_id="retrieval_query_embedding")
        return None

    monkeypatch.setattr(type(candidates), "_embed_query", embed)
    assert _stores(lambda: candidates._retrieve_chunks(nb, QUERY)) == 0


# ---------------------------------------------------------------------------
# The ANN lane (a built scale index)
# ---------------------------------------------------------------------------

@pytest.fixture
def ann_library(repo):
    instance, owner = repo
    from app.models.schemas import NotebookCreate

    nb = instance.create_notebook(NotebookCreate(name="ann")).id
    first = _add_source(instance, nb, [f"{TEXT}{i} " * 5 for i in range(4)])
    second = _add_source(instance, nb, [f"{TEXT} bandgap two {i} " * 5 for i in range(4)])
    instance.rebuild_unified_kg(nb)
    instance.build_scale_index(nb)
    candidates = instance.retrieval.candidates
    index = candidates._scale_index(nb, allow_stale=True)
    assert index is not None and index.chunk_ann_labels
    return instance, owner, nb, [first, second]


def _ann_events(instance, monkeypatch) -> list:
    events: list = []
    log = instance.retrieval.candidates.event_log
    original = log.emit

    def emit(event, *args, **kwargs):
        events.append(dict(event))
        return original(event, *args, **kwargs)

    monkeypatch.setattr(log, "emit", emit)
    return events


def test_the_ann_lane_keeps_a_healthy_recall(ann_library, monkeypatch):
    instance, _owner, nb, sources = ann_library
    candidates = instance.retrieval.candidates
    events = _ann_events(instance, monkeypatch)
    assert _stores(lambda: candidates._retrieve_chunks(
        nb, QUERY, allowed_source_ids=sources)) == 1
    assert any(event.get("stage") == "chunk_ann" for event in events)


@pytest.mark.parametrize("failure", [
    ChunkLexicalSearchTimeout("slow"), RuntimeError("pool timeout"),
], ids=["timeout", "failure"])
def test_an_ann_lane_whose_lexical_half_failed_keeps_nothing(
    ann_library, monkeypatch, failure,
):
    """Every source is in the index, so the lexical half is a supplement
    (no model error, no banner): the recall is degraded all the same."""
    instance, _owner, nb, sources = ann_library
    candidates = instance.retrieval.candidates
    monkeypatch.setattr(candidates.knowledge, "chunk_fts_search", _raiser(failure))
    noted: list = []
    original = type(candidates)._note_model_error
    monkeypatch.setattr(type(candidates), "_note_model_error",
                        lambda self, *a, **k: (noted.append(a[0]), original(self, *a, **k)))
    assert _stores(lambda: candidates._retrieve_chunks(
        nb, QUERY, allowed_source_ids=sources)) == 0
    assert "chunk_fts" not in noted        # the supplement role raised no banner


def test_an_ann_handle_that_did_not_open_keeps_nothing(ann_library, monkeypatch):
    instance, _owner, nb, sources = ann_library
    candidates = instance.retrieval.candidates
    monkeypatch.setattr(candidates.scale_runtime, "open_ann", lambda index, kind: None)
    assert _stores(lambda: candidates._retrieve_chunks(
        nb, QUERY, allowed_source_ids=sources)) == 0


@pytest.mark.parametrize("broken", [False, True], ids=["healthy", "broken"])
def test_a_failed_sidecar_coverage_probe_keeps_nothing(ann_library, monkeypatch, broken):
    """A report re-probes the physical presence of sources the index does not
    hold yet; a failed probe falls back conservatively."""
    instance, owner, nb, sources = ann_library
    candidates = instance.retrieval.candidates
    late = _add_source(instance, nb, [f"{TEXT} bandgap late {i} " * 5 for i in range(2)])
    probes: list = []
    original = candidates.chunks.ids_for_sources

    def probe(*args, **kwargs):
        probes.append(args[1:])
        if broken:
            raise RuntimeError("x")
        return original(*args, **kwargs)

    monkeypatch.setattr(candidates.chunks, "ids_for_sources", probe)
    stores = _stores(lambda: candidates._retrieve_chunks(
        nb, QUERY, allowed_source_ids=[*sources, late]),
        actor_id=owner, run_kind="report_generation")
    assert probes, "the sidecar probe must run on this path"
    assert stores == (0 if broken else 1)


# ---------------------------------------------------------------------------
# The contribution seam
# ---------------------------------------------------------------------------

class _Host:
    """A contributor host whose ``run`` does one scripted thing."""

    def __init__(self, script):
        self.script = script

    def run(self, scored, *, call_context, event_sink, **_kwargs):
        return self.script(list(scored), call_context, event_sink)


def _with_host(library, monkeypatch, script, **settings):
    repo, owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(candidates, "retrieval_contributors", _Host(script))
    for name, value in settings.items():
        monkeypatch.setattr(candidates.settings, name, value)
    return _stores(lambda: candidates._retrieve_chunks(nb, QUERY), actor_id=owner)


def test_a_host_that_answers_keeps_the_recall(library, monkeypatch):
    def answer(scored, _context, sink):
        sink({"kind": "retrieval_contribution", "outcome": "available"})
        return scored
    assert _with_host(library, monkeypatch, answer) == 1


def test_a_host_that_raises_keeps_nothing(library, monkeypatch):
    def explode(_scored, _context, _sink):
        raise RuntimeError("host down")
    assert _with_host(library, monkeypatch, explode) == 0


@pytest.mark.parametrize("outcome", ["timeout", "failed", "invalid", "blocked",
                                     "invalid_context", "unavailable"])
def test_a_contributor_that_did_not_answer_keeps_nothing(library, monkeypatch, outcome):
    def degraded(scored, _context, sink):
        sink({"kind": "retrieval_contribution", "outcome": outcome})
        return scored
    assert _with_host(library, monkeypatch, degraded) == 0


def test_a_failed_admission_read_keeps_nothing(library, monkeypatch):
    repo = library[0]
    monkeypatch.setattr(repo.retrieval.candidates, "hydrate_retrieval_contribution_chunks",
                        _raiser(RuntimeError("pool")))

    def admit(scored, context, _sink):
        assert context.admission_source.read(("missing-chunk",)) == ()
        return scored
    assert _with_host(library, monkeypatch, admit) == 0


def test_a_failed_question_index_keeps_nothing(library, monkeypatch):
    repo = library[0]
    monkeypatch.setattr(repo.retrieval.candidates, "_generated_question_supplement_optional",
                        _raiser(RuntimeError("index")))

    def propose(scored, context, _sink):
        assert context.generated_question_source.propose() == ()
        return scored
    assert _with_host(library, monkeypatch, propose,
                      generated_question_index_mode="shadow",
                      generated_question_trigger_hits=10 ** 6) == 0


def _independent_tail(scored):
    extra = RetrievedChunk(chunk_id="ck-plugin", source_id=scored[0].source_id,
                           source_title="", section_path="", text="plugin")
    return [*scored, extra]


def test_a_failed_matrix_rebuild_keeps_nothing(library, monkeypatch):
    repo = library[0]

    def tail(scored, _context, _sink):
        monkeypatch.setattr(repo.retrieval.candidates, "hydrate_chunk_candidates",
                            _raiser(RuntimeError("matrix")))
        return _independent_tail(scored)
    assert _with_host(library, monkeypatch, tail) == 0


@pytest.mark.parametrize("failing_probe", [1, 2], ids=["before", "after"])
def test_a_failed_cancellation_probe_keeps_nothing(library, monkeypatch, failing_probe):
    def tail(scored, context, _sink):
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            if calls["n"] == failing_probe:
                raise RuntimeError("malformed token")
        monkeypatch.setattr(context.cancellation, "raise_if_cancelled", probe)
        return _independent_tail(scored)
    assert _with_host(library, monkeypatch, tail) == 0


# ---------------------------------------------------------------------------
# What the element arm reads of a served recall
# ---------------------------------------------------------------------------

#: The fields a kept row carries; a served chunk exposes nothing else.
_PROJECTED = frozenset({
    "chunk_id", "source_id", "notebook_id", "element_ids", "score",
    "relevance", "retrieval_supports",
})


class _StrictChunk:
    """A served chunk that fails LOUDLY on any field outside the projection
    -- not with ``AttributeError``, which ``getattr(..., default)`` would
    swallow into a silent default."""

    def __init__(self, chunk):
        object.__setattr__(self, "_fields", {name: getattr(chunk, name)
                                              for name in _PROJECTED})

    def __getattr__(self, name):
        fields = object.__getattribute__(self, "_fields")
        if name in fields:
            return fields[name]
        raise AssertionError(f"the element arm read {name!r} of a served chunk")


@pytest.mark.parametrize("lane", ["fts", "ann", "brute"])
def test_the_element_arm_reads_only_the_projection(repo, monkeypatch, lane):
    from app.models.schemas import NotebookCreate
    from app.services import chunk_recall_memo

    instance, _owner = repo
    candidates = instance.retrieval.candidates
    nb = instance.create_notebook(NotebookCreate(name=lane)).id
    sources = [_add_source(instance, nb, [f"{TEXT}{lane} {i} " * 5 for i in range(4)])]
    if lane == "ann":
        instance.rebuild_unified_kg(nb)
        instance.build_scale_index(nb)
    if lane in {"ann", "fts"}:
        monkeypatch.setattr(candidates, "notebook_copy_stats",
                            lambda _nb: {"copyable": False, "size": {}})
    with retrieval_run(run_kind="ask_reasoning"):
        expected = [repr(hit) for hit in candidates._retrieve_elements(
            nb, QUERY, allowed_source_ids=sources)]
    real = chunk_recall_memo._rebuilt
    monkeypatch.setattr(chunk_recall_memo, "_rebuilt",
                        lambda row: _StrictChunk(real(row)))
    with retrieval_run(run_kind="ask_reasoning") as run:
        # The element arm's caller-supplied list is a producer attestation.
        candidates._retrieve_chunks(nb, QUERY, allowed_source_ids=sources,
                                    producer_explicit=True)
        served = [repr(hit) for hit in candidates._retrieve_elements(
            nb, QUERY, allowed_source_ids=sources)]
    assert run.chunk_recall_hits == 1
    assert served == expected and expected


def test_a_recall_no_element_search_can_read_is_not_kept(repo):
    """A copyable library with no list: its element search scans elements
    whole and never reads a recall back, so nothing is held for it; the same
    recall made with a list (what a peer or producer element search passes)
    is kept."""
    from app.models.schemas import NotebookCreate

    instance, _owner = repo
    candidates = instance.retrieval.candidates
    nb = instance.create_notebook(NotebookCreate(name="small")).id
    source = _add_source(instance, nb, [f"{TEXT}{i} " * 5 for i in range(3)])
    assert candidates.notebook_copy_stats(nb)["copyable"]
    assert _stores(lambda: candidates._retrieve_chunks(nb, QUERY)) == 0
    assert _stores(lambda: candidates._retrieve_chunks(
        nb, QUERY, allowed_source_ids=[source])) == 1
