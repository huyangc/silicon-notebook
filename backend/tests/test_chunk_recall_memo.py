"""The raw-element arm reads back the run's chunk recall (``chunk_recall_memo``).

Pinned:

* the run-state table: bounded by rows and by entries, least recently used
  out first, an oversized recall not kept, a stale reader never deleting a
  fresher entry, a flip dropping exactly that library's unbound recalls, and
  every read and write under the run's lock (concurrent legs);
* in one run the element arm recalls a library + text once, whoever made the
  recall first (a semantic leg or an earlier element search), and its
  candidates are the ones a fresh recall gives; another text, another ceiling
  or a chunk-mode run recalls again; nothing is kept outside a run;
* a ceiling-drift flip: the verify-on-read failure drops the library's
  unbound recalls, and an unbound recall found after the verdict flipped is
  neither served nor kept;
* a recall during which a lane failed open is not kept;
* a hit in a cancelled run raises the Stop;
* end to end, a global 8-library reasoning run and a single-library run are
  byte-identical with the memo on and off (``chunk_recall_memo_cases``; the
  PostgreSQL twin is ``tests/postgres/test_chunk_recall_memo_pg.py``).
"""
from __future__ import annotations

import contextvars
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.cancellation import AskCancelled
from app.services.embedding import FakeEmbedder
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.retrieval_run import (
    CHUNK_RECALL_MEMO_MAX_ENTRIES,
    ChunkRecallEntry,
    RetrievalRunState,
    current_retrieval_run,
    retrieval_run,
)
from app.services.source_scope import (
    current_source_scope,
    default_ceiling_context,
    record_ceiling_drift,
    run_ceiling_binds,
)
from tests import chunk_recall_memo_cases as cases
from tests.model_testkit import bind_all_embedding_clients

TEXT = "bandgap reference topic body detail "


# ---------------------------------------------------------------------------
# The run-state table
# ---------------------------------------------------------------------------

def _entry(rows: int, notebook_id: str = "nb", unbound: bool = False):
    return ChunkRecallEntry(rows=tuple(range(rows)), notebook_id=notebook_id,
                            unbound=unbound)


def _kept(state: RetrievalRunState) -> tuple[int, int]:
    """``(entries, rows)`` the run's recall memo holds right now."""
    with state._lock:
        return len(state._chunk_recalls), state._chunk_recall_rows


def _state(run_kind: str = "ask_reasoning") -> RetrievalRunState:
    return RetrievalRunState(run_kind=run_kind, fanout_limit=None)


def test_the_table_is_bounded_by_rows_least_recently_used_out_first():
    state = _state()
    for name in ("a", "b", "c"):
        state.keep_chunk_recall(name, _entry(4), max_rows=10)
    assert _kept(state) == (2, 8)
    assert state.chunk_recall("a") is None
    assert state.chunk_recall("b") is not None          # refreshed: now newest
    state.keep_chunk_recall("d", _entry(4), max_rows=10)
    assert state.chunk_recall("c") is None
    assert state.chunk_recall("b") is not None
    assert _kept(state) == (2, 8)
    assert (state.chunk_recall_stores, state.chunk_recall_evictions) == (4, 2)
    assert state.event()["chunk_recall_evictions"] == 2


def test_a_recall_larger_than_the_bound_is_not_kept():
    state = _state()
    state.keep_chunk_recall("small", _entry(3), max_rows=10)
    state.keep_chunk_recall("huge", _entry(11), max_rows=10)
    assert state.chunk_recall("huge") is None
    assert _kept(state) == (1, 3)


def test_empty_recalls_are_bounded_by_entries():
    state = _state()
    for index in range(CHUNK_RECALL_MEMO_MAX_ENTRIES + 10):
        state.keep_chunk_recall(index, _entry(0), max_rows=10)
    assert _kept(state) == (CHUNK_RECALL_MEMO_MAX_ENTRIES, 0)
    assert state.chunk_recall(0) is None
    assert state.chunk_recall(CHUNK_RECALL_MEMO_MAX_ENTRIES + 9) is not None


def test_replacing_a_key_keeps_the_row_count_exact():
    state = _state()
    state.keep_chunk_recall("a", _entry(4), max_rows=10)
    state.keep_chunk_recall("a", _entry(2), max_rows=10)
    assert _kept(state) == (1, 2)


def test_a_stale_reader_never_deletes_a_fresher_entry():
    state = _state()
    stale = _entry(2)
    state.keep_chunk_recall("k", stale, max_rows=10)
    fresh = _entry(3)
    state.keep_chunk_recall("k", fresh, max_rows=10)
    state.forget_chunk_recall("k", stale)
    assert state.chunk_recall("k") is fresh
    state.forget_chunk_recall("k", fresh)
    assert _kept(state) == (0, 0)


def test_a_flip_drops_only_that_librarys_unbound_recalls():
    state = _state()
    state.keep_chunk_recall("a-unbound", _entry(2, "a", True), max_rows=20)
    state.keep_chunk_recall("a-bound", _entry(2, "a", False), max_rows=20)
    state.keep_chunk_recall("b-unbound", _entry(2, "b", True), max_rows=20)
    state.forget_unbound_chunk_recalls("a")
    assert state.chunk_recall("a-unbound") is None
    assert state.chunk_recall("a-bound") is not None
    assert state.chunk_recall("b-unbound") is not None
    assert _kept(state) == (2, 4)


def test_a_kept_row_rebuilds_everything_the_element_arm_reads():
    """The light projection keeps the element arm's inputs: ids, library,
    element ids, both scores, and whether only the question index found the
    chunk (``partition_generated_question_chunks`` splits on it)."""
    from app.services import chunk_recall_memo as memo
    from app.services.retrieval import (
        RetrievalSupport,
        RetrievedChunk,
        is_generated_question_only_chunk,
    )

    question_only = RetrievedChunk(
        chunk_id="c1", source_id="s1", source_title="T", section_path="1",
        text="body", element_ids=["e1", "e2"], score=0.4, relevance=0.7,
        notebook_id="nb-b",
        retrieval_supports=(RetrievalSupport("generated_question", "chunk", "c1"),),
    )
    both = RetrievedChunk(
        chunk_id="c2", source_id="s1", source_title="T", section_path="1",
        text="body", element_ids=["e3"], score=0.2, relevance=0.0,
        retrieval_supports=(RetrievalSupport("generated_question", "chunk", "c2"),
                            RetrievalSupport("semantic", "chunk", "c2", 0.2)),
    )
    for original in (question_only, both):
        rebuilt = memo._rebuilt(memo._light_row(original))
        assert (rebuilt.chunk_id, rebuilt.source_id, rebuilt.notebook_id,
                rebuilt.element_ids, rebuilt.score, rebuilt.relevance) == (
            original.chunk_id, original.source_id, original.notebook_id,
            original.element_ids, original.score, original.relevance)
        assert is_generated_question_only_chunk(rebuilt) == (
            is_generated_question_only_chunk(original))
        assert rebuilt.text == "" and rebuilt.element_ids is not original.element_ids


def test_only_run_kinds_with_an_element_reader_keep_recalls():
    assert _state("ask_reasoning").keeps_chunk_recalls()
    assert _state("report_generation").keeps_chunk_recalls()
    assert _state("ask_plugin_engine").keeps_chunk_recalls()
    assert not _state("ask_chunk").keeps_chunk_recalls()
    assert not _state("report_planning").keeps_chunk_recalls()
    assert not _state("something_new").keeps_chunk_recalls()


def test_a_frozen_ceilings_digest_is_cached_per_scope(monkeypatch):
    """The frozen ceiling's digest is computed once per run, scope and
    library -- and never handed to another scope that happens to sit at the
    address a freed one left behind (simulated by seeding the memo slot the
    new scope's id names with the old scope's digest)."""
    from app.services import chunk_recall_memo as memo
    from app.services import source_scope

    first, second = object(), object()
    ceiling = ("src-a", "src-b")
    other = ("src-c", "src-d")
    with retrieval_run(run_kind="ask_reasoning") as run:
        monkeypatch.setattr(source_scope, "current_source_scope", lambda: first)
        digest = memo._frozen_digest(run, "nb", ceiling)
        assert digest == memo._ceiling_digest(ceiling)
        computed: list = []
        real = memo._ceiling_digest
        monkeypatch.setattr(memo, "_ceiling_digest",
                            lambda value: computed.append(value) or real(value))
        assert memo._frozen_digest(run, "nb", ceiling) == digest
        assert computed == []                          # cached
        monkeypatch.setattr(source_scope, "current_source_scope", lambda: second)
        run._value_cache[("chunk_recall_ceiling_digest", id(second), "nb", 2)] = (
            first, digest)
        assert memo._frozen_digest(run, "nb", other) == real(other)


def test_concurrent_legs_keep_the_table_consistent():
    """Many legs keeping, reading and forgetting at once (the fan-out runs on
    a thread pool): the row count always equals the kept entries' rows and
    never exceeds the bound."""
    state = _state()
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    errors: list = []

    def leg(worker: int) -> None:
        try:
            for step in range(400):
                key = (worker, step % 7)
                entry = _entry((worker + step) % 5 + 1, f"nb{worker % 3}",
                               unbound=bool(step % 2))
                state.keep_chunk_recall(key, entry, max_rows=40)
                found = state.chunk_recall(key)
                if found is not None and step % 3 == 0:
                    state.forget_chunk_recall(key, found)
                if step % 11 == 0:
                    state.forget_unbound_chunk_recalls(f"nb{worker % 3}")
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    try:
        threads = [threading.Thread(target=leg, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous)
    assert errors == []
    entries, rows = _kept(state)
    assert rows == sum(len(entry.rows) for entry in state._chunk_recalls.values())
    assert rows <= 40 and entries == len(state._chunk_recalls)


# ---------------------------------------------------------------------------
# The service: who recalls, who reads back
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.services.sqlite_repository import (
        SQLiteRepository,
        reset_request_user,
        set_request_user,
    )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    instance = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    owner = instance._runtime.identity.create_user("b00654321", "password-12")
    token = set_request_user(owner)
    try:
        yield instance, owner.id
    finally:
        reset_request_user(token)
        instance.close()


def _add_source(repo, notebook_id: str, texts) -> str:
    body = "\n\n".join(f"## p{index}\n\n{text}" for index, text in enumerate(texts))
    source = repo.upload_sources(notebook_id, [UploadedSourceFile(
        file_name="doc.md", content_type="text/markdown",
        content=body.encode("utf-8"))], scheduler=lambda _source_id: None)[0]
    repo.process_source(source.id)
    return source.id


@pytest.fixture
def library(repo, monkeypatch):
    """A large library (the element arm's bounded chunk route) with two
    sources, and a counter of physical recalls."""
    instance, owner = repo
    notebook_id = instance.create_notebook(NotebookCreate(name="kb")).id
    first = _add_source(instance, notebook_id, [f"{TEXT}{i} " * 5 for i in range(4)])
    second = _add_source(instance, notebook_id,
                         [f"{TEXT} bandgap second {i} " * 5 for i in range(4)])
    candidates = instance.retrieval.candidates
    monkeypatch.setattr(candidates, "notebook_copy_stats",
                        lambda _nb: {"copyable": False, "size": {}})
    recalls: list = []
    original = CandidateRetrievalService._retrieve_chunks_once

    def once(self, *args, **kwargs):
        recalls.append(args[:2])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CandidateRetrievalService, "_retrieve_chunks_once", once)
    return instance, owner, notebook_id, (first, second), recalls


def _elements(candidates, notebook_id, query, **kwargs):
    return [repr(hit) for hit in candidates._retrieve_elements(
        notebook_id, query, **kwargs)]


def _baseline(candidates, notebook_id, query, monkeypatch, **kwargs):
    with monkeypatch.context() as patch:
        patch.setattr(RetrievalRunState, "keeps_chunk_recalls", lambda self: False)
        with retrieval_run(run_kind="ask_reasoning"):
            return _elements(candidates, notebook_id, query, **kwargs)


def test_the_element_arm_reads_back_a_semantic_legs_recall(library, monkeypatch):
    repo, _owner, nb, _sources, recalls = library
    candidates = repo.retrieval.candidates
    expected = _baseline(candidates, nb, "bandgap reference", monkeypatch)
    recalls.clear()
    with retrieval_run(run_kind="ask_reasoning") as run:
        candidates._retrieve_chunks(nb, "bandgap reference")
        found = _elements(candidates, nb, "bandgap reference")
        again = _elements(candidates, nb, "bandgap reference")
    assert found == expected and again == expected and expected
    assert recalls == [(nb, "bandgap reference")]
    assert (run.chunk_recall_hits, run.chunk_recall_stores) == (2, 1)


def test_the_element_arm_reads_back_its_own_earlier_recall(library, monkeypatch):
    repo, _owner, nb, _sources, recalls = library
    candidates = repo.retrieval.candidates
    recalls.clear()
    with retrieval_run(run_kind="ask_reasoning"):
        first = _elements(candidates, nb, "bandgap")
        second = _elements(candidates, nb, "bandgap")
        _elements(candidates, nb, "reference topic")
    assert first == second
    assert recalls == [(nb, "bandgap"), (nb, "reference topic")]


def test_a_semantic_leg_is_never_served(library):
    repo, _owner, nb, _sources, recalls = library
    candidates = repo.retrieval.candidates
    recalls.clear()
    with retrieval_run(run_kind="ask_reasoning"):
        first = candidates._retrieve_chunks(nb, "bandgap")
        second = candidates._retrieve_chunks(nb, "bandgap")
    assert len(recalls) == 2
    assert [c.text for c in first[0]] == [c.text for c in second[0]]
    assert second[1] and second[2] is not None


def test_another_ceiling_recalls_again(library, monkeypatch):
    """Two producer lists that differ only in which source they name: the
    memo key's ceiling digest is all that tells them apart."""
    repo, _owner, nb, (first, second), recalls = library
    candidates = repo.retrieval.candidates
    expected = _baseline(candidates, nb, "bandgap", monkeypatch,
                         allowed_source_ids=[second])
    recalls.clear()
    with retrieval_run(run_kind="ask_reasoning"):
        narrowed_first = _elements(candidates, nb, "bandgap",
                                   allowed_source_ids=[first])
        narrowed_second = _elements(candidates, nb, "bandgap",
                                    allowed_source_ids=[second])
    assert narrowed_second == expected
    assert narrowed_first != narrowed_second
    assert len(recalls) == 2


def test_nothing_is_kept_in_a_chunk_run_or_outside_a_run(library):
    repo, _owner, nb, _sources, recalls = library
    candidates = repo.retrieval.candidates
    recalls.clear()
    with retrieval_run(run_kind="ask_chunk") as run:
        candidates._retrieve_chunks(nb, "bandgap")
        _elements(candidates, nb, "bandgap")
    assert _kept(run) == (0, 0) and len(recalls) == 2
    candidates._retrieve_chunks(nb, "bandgap")
    _elements(candidates, nb, "bandgap")
    assert len(recalls) == 4


def test_a_recall_made_before_the_fts_circuit_opened_is_not_served_after(library):
    """A recall after the library's FTS circuit opened has no lexical half, so
    one made before (with it) is not what a fresh recall would give."""
    repo, _owner, nb, _sources, recalls = library
    candidates = repo.retrieval.candidates
    recalls.clear()
    with retrieval_run(run_kind="ask_reasoning") as run:
        candidates._retrieve_chunks(nb, "bandgap")
        run.note_chunk_fts_timeout(nb)
        _elements(candidates, nb, "bandgap")
        _elements(candidates, nb, "bandgap")
    assert len(recalls) == 2
    assert run.chunk_recall_hits == 1


def test_a_recall_that_failed_open_is_not_kept(library, monkeypatch):
    repo, _owner, nb, _sources, recalls = library
    candidates = repo.retrieval.candidates
    original = type(candidates)._embed_query
    failing = {"on": True}

    def embed(self, query):
        if failing["on"]:
            self._note_model_error("embed", RuntimeError("down"),
                                   workload_id="retrieval_query_embedding")
            return None
        return original(self, query)

    monkeypatch.setattr(type(candidates), "_embed_query", embed)
    recalls.clear()
    with retrieval_run(run_kind="ask_reasoning") as run:
        candidates._retrieve_chunks(nb, "bandgap")
        failing["on"] = False
        _elements(candidates, nb, "bandgap")
    assert len(recalls) == 2
    assert run.chunk_recall_stores == 1 and run.chunk_recall_hits == 0


def test_a_hit_in_a_cancelled_run_raises_the_stop(library):
    repo, _owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates
    cancel = threading.Event()
    with retrieval_run(run_kind="ask_reasoning", cancel_event=cancel):
        candidates._retrieve_chunks(nb, "bandgap")
        cancel.set()
        with pytest.raises(AskCancelled):
            candidates._retrieve_elements(nb, "bandgap")


def test_concurrent_element_legs_each_read_back_their_own_library(repo, monkeypatch):
    """The peer fan-out shape: legs of different libraries on a pool, each
    reading back the recall the semantic fan-out made for its library."""
    instance, _owner = repo
    candidates = instance.retrieval.candidates
    monkeypatch.setattr(candidates, "notebook_copy_stats",
                        lambda _nb: {"copyable": False, "size": {}})
    notebooks = []
    for index in range(4):
        nb = instance.create_notebook(NotebookCreate(name=f"kb{index}")).id
        _add_source(instance, nb, [f"{TEXT} lib{index} {i} " * 5 for i in range(3)])
        notebooks.append(nb)
    expected = {nb: _baseline(candidates, nb, "bandgap", monkeypatch)
                for nb in notebooks}
    with retrieval_run(run_kind="ask_reasoning") as run:
        for nb in notebooks:
            candidates._retrieve_chunks(nb, "bandgap")
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                nb: pool.submit(contextvars.copy_context().run, _elements,
                                candidates, nb, "bandgap")
                for nb in notebooks * 3
            }
            found = {nb: future.result() for nb, future in futures.items()}
    assert found == expected
    assert run.chunk_recall_hits >= len(notebooks)


# ---------------------------------------------------------------------------
# Ceiling drift: an unbound recall outlives no flip
# ---------------------------------------------------------------------------

def _ceiling(repo, nb, owner):
    """The production default ceiling (verdict probes included), under which
    an unconstrained run pushes the list down."""
    return default_ceiling_context(nb, owner, repo._runtime.ceiling_readers())


def test_a_drift_flip_drops_the_librarys_unbound_recalls(library):
    repo, owner, nb, sources, recalls = library
    candidates = repo.retrieval.candidates
    with _ceiling(repo, nb, owner), retrieval_run(
        run_kind="ask_reasoning", actor_id=owner,
    ) as run:
        scope = current_source_scope()
        assert run_ceiling_binds(scope, nb) is False       # pushed down
        candidates._retrieve_chunks(nb, "bandgap")
        assert _kept(run)[0] == 1
        late = _add_source(repo, nb, [f"{TEXT} bandgap bandgap " * 6])
        recalls.clear()
        candidates._retrieve_chunks(nb, "bandgap")         # sees the outsider
        assert nb in scope._ceiling_bound_libraries        # flipped
        assert len(recalls) == 2                           # re-ran bound
        assert _kept(run) == (0, 0)
        recalls.clear()
        found = candidates._retrieve_elements(nb, "bandgap")
        assert len(recalls) == 1                           # recalled, bound
    assert late not in {hit.source_id for hit in found}
    assert {hit.source_id for hit in found} <= set(sources)


def test_an_unbound_recall_found_after_a_flip_is_not_served(library, monkeypatch):
    """The verdict flips between the element arm taking its key and reading
    the memo: the unbound recall is not served, and is dropped."""
    repo, owner, nb, _sources, recalls = library
    candidates = repo.retrieval.candidates
    with _ceiling(repo, nb, owner), retrieval_run(
        run_kind="ask_reasoning", actor_id=owner,
    ) as run:
        scope = current_source_scope()
        candidates._retrieve_chunks(nb, "bandgap")
        assert _kept(run)[0] == 1
        original = RetrievalRunState.chunk_recall

        def flip_then_read(self, key):
            record_ceiling_drift(scope, nb)
            return original(self, key)

        monkeypatch.setattr(RetrievalRunState, "chunk_recall", flip_then_read)
        recalls.clear()
        candidates._retrieve_elements(nb, "bandgap")
        assert len(recalls) == 1                           # not served
        assert run.chunk_recall_hits == 0
        assert current_retrieval_run() is run
    assert all(not entry.unbound for entry in run._chunk_recalls.values())


def test_a_recall_read_across_a_flip_is_not_kept_as_unbound(library, monkeypatch):
    """The verdict flips after the recall took its (unbound) key but before it
    read: the read itself binds, and a bound read must not be kept under a key
    that names no ceiling."""
    repo, owner, nb, _sources, _recalls = library
    candidates = repo.retrieval.candidates
    with _ceiling(repo, nb, owner), retrieval_run(
        run_kind="ask_reasoning", actor_id=owner,
    ) as run:
        scope = current_source_scope()
        assert run_ceiling_binds(scope, nb) is False
        original = CandidateRetrievalService._retrieve_chunks_once

        def flip_then_read(self, *args, **kwargs):
            record_ceiling_drift(scope, nb)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(CandidateRetrievalService, "_retrieve_chunks_once",
                            flip_then_read)
        candidates._retrieve_chunks(nb, "bandgap")
    assert run.chunk_recall_stores == 0
    assert _kept(run) == (0, 0)


# ---------------------------------------------------------------------------
# End to end, memo on vs off
# ---------------------------------------------------------------------------

@pytest.fixture(params=cases.LANES)
def world(request, tmp_path, monkeypatch):
    from tests import global_citation_e2e_kit as kit

    instance = kit.make_repo(tmp_path, monkeypatch)
    try:
        yield cases.build(instance, lane=request.param)
    finally:
        instance.close()


def test_a_global_run_reuses_the_recall_byte_identically(world, monkeypatch):
    cases.global_run_reuses_the_recall_byte_identically(world, monkeypatch)


def test_a_single_library_run_is_byte_identical(world, monkeypatch):
    cases.single_library_run_is_byte_identical(world, monkeypatch)
