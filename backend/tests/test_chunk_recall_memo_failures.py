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


def _cold_scale_loads(candidates, monkeypatch, outcomes):
    """Every lookup misses the process cache and loads from disk; each load
    answers the next scripted outcome (``None`` = failed, ``"real"``)."""
    catalog = candidates.scale_runtime.catalog
    real = catalog.artifacts.load_scale
    script = list(outcomes)
    monkeypatch.setattr(catalog, "_still_current", lambda *_args: False)

    def load(notebook_id):
        return real(notebook_id) if script.pop(0) == "real" else None

    monkeypatch.setattr(catalog.artifacts, "load_scale", load)
    return script


def test_a_scale_index_that_failed_to_load_keeps_nothing(ann_library, monkeypatch):
    """The manifest is there but the cold load fails once (a torn fold rename,
    EMFILE, MemoryError): that recall fell back to the lane without ANN and is
    not kept; the next one loads, takes the ANN lane and is kept."""
    instance, _owner, nb, sources = ann_library
    candidates = instance.retrieval.candidates
    remaining = _cold_scale_loads(candidates, monkeypatch, [None, "real"])
    ann: list = []
    original = type(candidates)._retrieve_chunks_ann
    monkeypatch.setattr(type(candidates), "_retrieve_chunks_ann",
                        lambda self, *a, **k: ann.append(1) or original(self, *a, **k))
    with retrieval_run(run_kind="ask_reasoning") as run:
        candidates._retrieve_chunks(nb, QUERY, allowed_source_ids=sources)
        assert (run.chunk_recall_stores, run.chunk_recall_degraded_skips) == (0, 1)
        assert ann == []
        candidates._retrieve_chunks(nb, QUERY, allowed_source_ids=sources)
    assert remaining == []
    assert ann == [1]
    assert (run.chunk_recall_stores, run.chunk_recall_degraded_skips) == (1, 1)


@pytest.mark.parametrize("failing", ["stat", "read"])
def test_an_unreadable_scale_manifest_keeps_nothing(ann_library, monkeypatch, failing):
    """The manifest's stat or read fails once: the lane falls back without
    ANN, and that recall is not kept."""
    instance, _owner, nb, sources = ann_library
    candidates = instance.retrieval.candidates
    catalog = candidates.scale_runtime.catalog
    monkeypatch.setattr(catalog, "_still_current", lambda *_args: False)
    catalog._manifest_identity_memo.clear()
    artifacts = catalog.artifacts
    name = "manifest_stat_signature" if failing == "stat" else "read_manifest"
    real = getattr(artifacts, name)
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            if failing == "stat":
                return None
            raise OSError("EIO")
        return real(*args, **kwargs)

    monkeypatch.setattr(artifacts, name, flaky)
    with retrieval_run(run_kind="ask_reasoning") as run:
        candidates._retrieve_chunks(nb, QUERY, allowed_source_ids=sources)
        assert (run.chunk_recall_stores, run.chunk_recall_degraded_skips) == (0, 1)
        candidates._retrieve_chunks(nb, QUERY, allowed_source_ids=sources)
    assert calls["n"] >= 2
    assert (run.chunk_recall_stores, run.chunk_recall_degraded_skips) == (1, 1)


def test_a_library_without_a_scale_index_is_not_degraded(repo):
    """No manifest at all is "no index", the same on every call: kept."""
    from app.models.schemas import NotebookCreate

    instance, _owner = repo
    candidates = instance.retrieval.candidates
    nb = instance.create_notebook(NotebookCreate(name="plain")).id
    source = _add_source(instance, nb, [f"{TEXT}{i} " * 5 for i in range(3)])
    assert candidates._scale_index(nb, allow_stale=True) is None
    assert _stores(lambda: candidates._retrieve_chunks(
        nb, QUERY, allowed_source_ids=[source])) == 1


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
        self.kwargs: dict = {}

    def run(self, scored, *, call_context, **kwargs):
        self.kwargs = kwargs
        return self.script(list(scored), call_context, kwargs)


def _with_host(library, monkeypatch, script, **settings):
    repo, owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(candidates, "retrieval_contributors", _Host(script))
    for name, value in settings.items():
        monkeypatch.setattr(candidates.settings, name, value)
    return _stores(lambda: candidates._retrieve_chunks(nb, QUERY), actor_id=owner)


def test_a_host_that_answers_keeps_the_recall(library, monkeypatch):
    def answer(scored, _context, _kwargs):
        return scored
    assert _with_host(library, monkeypatch, answer) == 1


def test_a_host_that_raises_keeps_nothing(library, monkeypatch):
    def explode(_scored, _context, _kwargs):
        raise RuntimeError("host down")
    assert _with_host(library, monkeypatch, explode) == 0


def test_the_host_reports_degradation_through_its_own_hook(library, monkeypatch):
    """The lane hands the host the event log's ``emit`` unwrapped -- so when
    there is none the host still falls back to its own sink -- and a separate
    ``on_degraded``."""
    from app.services import chunk_recall_memo

    repo = library[0]
    candidates = repo.retrieval.candidates
    host = _Host(lambda scored, _context, kwargs: (
        kwargs["on_degraded"]("timeout"), scored)[1])
    monkeypatch.setattr(candidates, "retrieval_contributors", host)
    stores = _stores(lambda: candidates._retrieve_chunks(library[2], QUERY),
                     actor_id=library[1])
    assert host.kwargs["event_sink"] == candidates.event_log.emit
    assert host.kwargs["on_degraded"] is chunk_recall_memo.noting_contribution
    assert stores == 0


def test_without_a_sink_the_host_still_reports_to_its_own():
    """``event_sink=None`` keeps the host's own sink as the fallback, while
    ``on_degraded`` is told about a runtime failure either way."""
    from app.extensions import build_extension_runtime
    from tests.test_retrieval_contributor_host import _bundle, _context, _Reader

    events: list = []
    degraded: list = []

    class Raising:
        invocations = frozenset({"selected_evidence"})

        def contribute(self, context):
            raise RuntimeError("down")

    host = build_extension_runtime(
        (_bundle("plugin", Raising()),), event_sink=events.append,
    ).retrieval_contributors
    baseline = ["base"]
    result = host.run(
        baseline, invocation="selected_evidence",
        context_factory=lambda: _context(_Reader()), baseline_identity=str,
        event_sink=None, on_degraded=degraded.append,
    )
    assert result is baseline
    assert [event["outcome"] for event in events] == ["failed"]
    assert degraded == ["failed"]


def test_a_failed_admission_read_inside_the_host_is_reported():
    from app.extensions import build_extension_runtime
    from tests.test_retrieval_contributor_host import (
        _bundle,
        _candidate,
        _context,
        _result,
    )

    class Proposing:
        invocations = frozenset({"selected_evidence"})

        def contribute(self, context):
            return _result(_candidate("new"))

    class BrokenReader:
        def read(self, request):
            raise OSError("pool")

    degraded: list = []
    host = build_extension_runtime((_bundle("plugin", Proposing()),)).retrieval_contributors
    host.run(
        ["base"], invocation="selected_evidence",
        context_factory=lambda: _context(BrokenReader()), baseline_identity=str,
        on_degraded=degraded.append,
    )
    assert degraded == ["admission_read_failed"]


# ---------------------------------------------------------------------------
# The real contribution host: which outcomes degrade a recall
# ---------------------------------------------------------------------------

class _ChunkContributor:
    invocations = frozenset({"chunk_candidates"})

    def __init__(self, behave):
        self.behave = behave

    def contribute(self, context):
        return self.behave()


def _registered(library, monkeypatch, behave, *, availability=None):
    from app.extensions import build_extension_runtime
    from tests.test_retrieval_contributor_host import _bundle

    repo, owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates
    host = build_extension_runtime(
        (_bundle("chunk-plugin", _ChunkContributor(behave), availability=availability),)
    ).retrieval_contributors
    monkeypatch.setattr(candidates, "retrieval_contributors", host)
    with retrieval_run(run_kind="ask_reasoning", actor_id=owner) as run:
        candidates._retrieve_chunks(nb, QUERY)
    return run


def _answer(status=None, failure=None):
    from app.extension_sdk import ContributorResult, ExtensionResultStatus

    return lambda: ContributorResult(
        (), status or ExtensionResultStatus.AVAILABLE, failure)


def _raise(exc):
    def behave():
        raise exc
    return behave


@pytest.mark.parametrize("behave, degraded", [
    (_answer(), False),
    (_raise(RuntimeError("down")), True),
    (_raise(TimeoutError()), True),
    ("runtime_unavailable", True),
    ("failure_unavailable", True),
    ("failure_timeout", True),
    ("failure_rejected", False),
    ("failure_disabled", False),
], ids=["answered", "raised", "timed_out", "runtime_unavailable",
        "failure_unavailable", "failure_timeout", "self_rejected", "self_disabled"])
def test_a_runtime_non_answer_degrades_the_recall(library, monkeypatch, behave, degraded):
    """Degraded = this call's result may be worse than a repeat under the same
    conditions.  A contributor that failed, timed out or was unavailable AT
    RUN TIME may answer next time; one that declined this request
    (``rejected``) or reports itself switched off (``disabled``) gives the
    same answer again, so the recall is still kept."""
    from app.extension_sdk import (
        ExtensionFailure,
        ExtensionFailureKind,
        ExtensionResultStatus,
    )

    named = {
        "runtime_unavailable": _answer(ExtensionResultStatus.UNAVAILABLE),
        "failure_unavailable": _answer(failure=ExtensionFailure(
            ExtensionFailureKind.UNAVAILABLE, "down")),
        "failure_timeout": _answer(failure=ExtensionFailure(
            ExtensionFailureKind.TIMEOUT, "slow")),
        "failure_rejected": _answer(failure=ExtensionFailure(
            ExtensionFailureKind.REJECTED, "declined")),
        "failure_disabled": _answer(failure=ExtensionFailure(
            ExtensionFailureKind.DISABLED, "off")),
    }
    run = _registered(library, monkeypatch, named.get(behave, behave))
    assert run.chunk_recall_stores == (0 if degraded else 1)
    assert run.chunk_recall_degraded_skips == (1 if degraded else 0)


@pytest.mark.parametrize("status", ["unavailable", "disabled"])
def test_a_stable_availability_verdict_does_not_degrade_the_recall(
    library, monkeypatch, status,
):
    """The availability probe answers for the whole call (a plugin that is
    steadily unavailable, with its reason code): a repeat gets the same
    answer, so the run keeps its recalls instead of keeping none."""
    from app.extension_sdk import Availability, AvailabilityStatus

    verdict = Availability(AvailabilityStatus(status), "feature_off")
    run = _registered(library, monkeypatch, _raise(AssertionError("not run")),
                      availability=lambda _context: verdict)
    assert (run.chunk_recall_stores, run.chunk_recall_degraded_skips) == (1, 0)


def test_a_failed_admission_read_keeps_nothing(library, monkeypatch):
    repo = library[0]
    monkeypatch.setattr(repo.retrieval.candidates, "hydrate_retrieval_contribution_chunks",
                        _raiser(RuntimeError("pool")))

    def admit(scored, context, _kwargs):
        assert context.admission_source.read(("missing-chunk",)) == ()
        return scored
    assert _with_host(library, monkeypatch, admit) == 0


def test_a_failed_question_index_keeps_nothing(library, monkeypatch):
    repo = library[0]
    monkeypatch.setattr(repo.retrieval.candidates, "_generated_question_supplement_optional",
                        _raiser(RuntimeError("index")))

    def propose(scored, context, _kwargs):
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

    def tail(scored, _context, _kwargs):
        monkeypatch.setattr(repo.retrieval.candidates, "hydrate_chunk_candidates",
                            _raiser(RuntimeError("matrix")))
        return _independent_tail(scored)
    assert _with_host(library, monkeypatch, tail) == 0


@pytest.mark.parametrize("failing_probe", [1, 2], ids=["before", "after"])
def test_a_failed_cancellation_probe_keeps_nothing(library, monkeypatch, failing_probe):
    def tail(scored, context, _kwargs):
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


# ---------------------------------------------------------------------------
# The key's own drift probe, the readability decision, the handed-down slot
# ---------------------------------------------------------------------------

def _frozen_scope(nb, sources):
    """A bare frozen include scope (no verdict probes, so it binds its list):
    the lexical gate's answer then depends on the drift verdict, which the
    memo key must resolve itself."""
    from app.models.source_scope import ResolvedSourceScope
    from app.services.source_scope import source_scope_context

    return source_scope_context(nb, ResolvedSourceScope(
        mode="include", source_ids=list(sources), narrowed=False))


def test_a_failed_drift_probe_while_taking_the_key_keeps_nothing(library, monkeypatch):
    """The key's probe runs before the recall's own failure capture opens;
    its failure is caught where it runs, and that call keeps nothing (the
    recall then resolves the verdict itself)."""
    repo, _owner, nb, sources, _recalls = library
    candidates = repo.retrieval.candidates
    probes: list = []
    real = candidates._lexical_gate_drift

    def probe(notebook_id):
        probes.append(notebook_id)
        if len(probes) == 1:
            raise RuntimeError("probe down")
        return real(notebook_id)

    monkeypatch.setattr(candidates, "_lexical_gate_drift", probe)
    with _frozen_scope(nb, sources):
        assert _stores(lambda: candidates._retrieve_chunks(nb, QUERY)) == 0
    assert len(probes) == 2                   # the key's, then the recall's own
    probes.clear()
    monkeypatch.setattr(candidates, "_lexical_gate_drift", real)
    with _frozen_scope(nb, sources):
        assert _stores(lambda: candidates._retrieve_chunks(nb, QUERY)) == 1


def _failing_copyable_read(monkeypatch):
    from app.services.retrieval_run import RetrievalRunState

    real = RetrievalRunState.memoized_value

    def memoized(self, key, compute):
        if isinstance(key, tuple) and key[:1] == ("chunk_recall_copyable",):
            raise RuntimeError("version read failed")
        return real(self, key, compute)

    monkeypatch.setattr(RetrievalRunState, "memoized_value", memoized)


def test_a_failed_readability_check_keeps_nothing_and_returns_the_recall(repo, monkeypatch):
    from app.models.schemas import NotebookCreate

    instance, _owner = repo
    candidates = instance.retrieval.candidates
    nb = instance.create_notebook(NotebookCreate(name="small")).id
    _add_source(instance, nb, [f"{TEXT}{i} " * 5 for i in range(3)])
    with retrieval_run(run_kind="ask_chunk"):
        expected = [c.chunk_id for c in candidates._retrieve_chunks(nb, QUERY)[0]]
    _failing_copyable_read(monkeypatch)
    with retrieval_run(run_kind="ask_reasoning") as run:
        got = [c.chunk_id for c in candidates._retrieve_chunks(nb, QUERY)[0]]
    assert got == expected and got
    assert run.chunk_recall_stores == 0


def test_a_ceiling_with_a_list_is_readable_without_the_copyable_read(library, monkeypatch):
    """The scope hands the element arm a list, which settles readability: the
    copyable read is not made at all."""
    repo, _owner, nb, sources, _recalls = library
    candidates = repo.retrieval.candidates
    _failing_copyable_read(monkeypatch)
    with _frozen_scope(nb, sources):
        assert _stores(lambda: candidates._retrieve_chunks(nb, QUERY)) == 1


def test_the_copyable_answer_is_read_once_per_run_and_library(library, monkeypatch):
    repo, _owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates
    reads: list = []
    real = candidates.notebook_copy_stats

    def stats(notebook_id):
        import inspect

        if any(frame.function == "_recall_readable_by_element_arm"
               for frame in inspect.stack()[1:6]):
            reads.append(notebook_id)
        return real(notebook_id)

    monkeypatch.setattr(candidates, "notebook_copy_stats", stats)
    with retrieval_run(run_kind="ask_reasoning") as run:
        candidates._retrieve_chunks(nb, QUERY)
        candidates._retrieve_chunks(nb, "reference topic")
    assert reads == [nb]
    assert run.chunk_recall_stores == 2


def test_an_element_miss_takes_its_slot_once(library, monkeypatch):
    """The slot the element arm took for its lookup is handed to the recall it
    then makes: one key, one digest, one drift resolution per miss."""
    repo, _owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates
    slots: list = []
    real = type(candidates)._chunk_recall_slot

    def slot(self, *args, **kwargs):
        slots.append(args[:2])
        return real(self, *args, **kwargs)

    monkeypatch.setattr(type(candidates), "_chunk_recall_slot", slot)
    with retrieval_run(run_kind="ask_reasoning") as run:
        candidates._retrieve_elements(nb, QUERY)
        assert run.chunk_recall_stores == 1
        candidates._retrieve_elements(nb, QUERY)
    assert slots == [(nb, QUERY), (nb, QUERY)]   # one per element search
    assert run.chunk_recall_hits == 1


def test_a_handed_slot_is_taken_only_by_its_own_call():
    from app.services import chunk_recall_memo as memo

    allowed = ["src-a"]
    call = ("svc", "nb", "q", 200, allowed, False)
    with memo.handing("slot", call):
        assert memo.handed(("svc", "nb", "q", 200, list(allowed), False)) == (False, None)
        assert memo.handed(("svc", "nb", "other", 200, allowed, False)) == (False, None)
        assert memo.handed(call) == (True, "slot")
        assert memo.handed(call) == (False, None)     # consumed
    assert memo.handed(call) == (False, None)

