"""Memory recall and the retrieval stage share one embed of the research question.

Shared by ``test_memory_embed_share.py`` (SQLite) and
``postgres/test_memory_embed_share_pg.py``. Inside one retrieval run, Memory
recall (``MemoryRetriever._hits``) and the retrieval stage
(``CandidateRetrievalService._embed_query``) used to embed the same research
question once each. They now hand the embedder's NATIVE vector over
(``retrieval_run.shared_query_vector``) -- only for the exact same text sent
to the same configured embedder; every other pair embeds on its own.

Every world runs twice: with the runtime truncation on (``EMBED_RUNTIME_DIM``
below the native ``EMBED_DIM``) and off. The oracles are independent of the
code under test:

* Memory hits: ``memory_embed_skip_cases.legacy_hits`` -- the recall as it was
  before either step, embedding with a fresh ``FakeEmbedder`` of the same
  model -- compared as JSON bytes;
* the retrieval stage's query vector: the native fake vector, truncated with
  ``truncate_vec`` exactly as ``_embed_query`` always did;
* the retrieval stage's vector recall: the same chunk recall in a run where
  Memory never ran (so nothing could be shared), compared as bytes.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import numpy as np

from app.core.request_context import reset_request_user, set_request_user
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.embedding import FakeEmbedder
from app.services.retrieval_run import retrieval_run
from app.services.vector_index import truncate_vec
from tests.memory_embed_skip_cases import as_bytes, legacy_hits
from tests.model_testkit import bind_all_embedding_clients

EMBED_DIM = 16
#: Runtime truncation widths each world runs under: on (8 < 16) and off.
RUNTIME_DIMS = (8, 0)
PLAIN = "how is timing closure signed off"
#: Memory's probe strips the quote markers (it embeds ``PLAIN``); the
#: retrieval stage embeds the string as given -- two texts, two embeds.
QUOTED = 'how is "timing closure" signed off'
DOC_MD = (
    "# Timing handbook\n\n"
    "## Sign-off\n\n"
    "Timing closure is signed off after STA on every corner.\n\n"
    "## Physical verification\n\n"
    "DRC and LVS both gate tape-out.\n"
)


class CountingEmbedder:
    """A deterministic fake model that records each query it embeds.

    ``salt`` makes a different model of the same width; ``fail`` is a list of
    per-call outcomes consumed in order (``"raise"``, ``"none"`` or ``"ok"``),
    ``"ok"`` once it runs out.
    """

    def __init__(self, *, salt: str = "", configured: bool = True) -> None:
        self.inner = FakeEmbedder(dim=EMBED_DIM)
        self.dim = EMBED_DIM
        self.salt = salt
        self.configured = configured
        self.queries: list[str] = []
        self.fail: list[str] = []

    def embed_query(self, text: str) -> Optional[list[float]]:
        self.queries.append(text)
        outcome = self.fail.pop(0) if self.fail else "ok"
        if outcome == "raise":
            raise RuntimeError("embedding service down")
        if outcome == "none":
            return None
        return self.inner.embed_query(self.salt + text)

    def embed_texts(self, texts):
        return [self.inner.embed_query(self.salt + text) for text in texts]


class _Oracle:
    """The same model as a ``CountingEmbedder``, without the counting."""

    def __init__(self, salt: str = "", *, down: bool = False) -> None:
        self.inner = FakeEmbedder(dim=EMBED_DIM)
        self.salt = salt
        self.down = down

    def embed_query(self, text: str) -> list[float]:
        if self.down:
            raise RuntimeError("down")
        return self.inner.embed_query(self.salt + text)


@dataclass
class World:
    repo: Any
    embedder: CountingEmbedder
    asker: Any
    notebook_id: str
    runtime_dim: int

    @property
    def retriever(self):
        return self.repo._runtime.memory_retriever

    @property
    def retrieval(self):
        return self.repo._runtime.retrieval_component


def build(repo: Any, runtime_dim: int, *, bind: bool = True) -> World:
    assert int(repo.settings.embed_dim) == EMBED_DIM
    assert int(repo.settings.embed_runtime_dim) == runtime_dim
    embedder = CountingEmbedder()
    if bind:
        bind_all_embedding_clients(repo, embedder)
    asker = repo._runtime.identity.create_user("a00777002", "pw123456")
    world = World(repo, embedder, asker, "", runtime_dim)
    with as_asker(world):
        world.notebook_id = repo.create_notebook(
            NotebookCreate(name="Memory embed share")).id
        source = repo.upload_sources(world.notebook_id, [UploadedSourceFile(
            file_name="timing.md", content_type="text/markdown",
            content=DOC_MD.encode())], scheduler=lambda _source_id: None)[0]
        repo.process_source(source.id)
    if bind:
        assert world.retriever.embedder is embedder
        assert world.retrieval.candidates.embedder is embedder
    return world


@contextmanager
def as_asker(world: World) -> Iterator[None]:
    token = set_request_user(world.asker)
    try:
        yield
    finally:
        reset_request_user(token)


@contextmanager
def one_run(world: World) -> Iterator[None]:
    with as_asker(world), retrieval_run(
        run_kind="ask_reasoning", actor_id=world.asker.id,
    ):
        yield


def remember(world: World) -> None:
    """The asker's confirmed Memory, embedded (it has a stored vector)."""
    service = world.repo._runtime.memory_service
    scheduler = service.embedding_scheduler
    service.embedding_scheduler = lambda fn, job: fn(job)
    try:
        for title, content in (
            ("Signed timing closure", "Timing closure signed off by review."),
            ("Corner policy", "Every corner is checked before sign-off."),
        ):
            item = service.create_candidate(
                world.notebook_id, world.asker.id, None, f"req-{title}", title,
                content, [], "task")
            item = service.confirm(item.id, world.asker.id, {"extract_kg": False})
            stored = service.store.memory_for_user(item.id, world.asker.id)
            assert stored.embedding_status == "ready", stored
    finally:
        service.embedding_scheduler = scheduler


# ------------------------------------------------------------------ oracles


def reference_vector(world: World, text: str, salt: str = "") -> list[float]:
    """What ``_embed_query`` returns for ``text``: the native fake vector,
    truncated to the runtime width when that is on."""
    native = FakeEmbedder(dim=EMBED_DIM).embed_query(salt + text)
    if world.runtime_dim and len(native) > world.runtime_dim:
        return truncate_vec(
            np.asarray(native, dtype=np.float32), world.runtime_dim).tolist()
    return native


def oracle_memory(world: World, text: str, *, salt: str = "", down: bool = False,
                  statuses=("confirmed",)) -> list[str]:
    with as_asker(world):
        return as_bytes(legacy_hits(
            world.retriever, _Oracle(salt, down=down), world.asker.id,
            world.notebook_id, text, statuses, 8))


def _recall_bytes(result) -> str:
    scored, ids, matrix = result
    items = [item.model_dump_json() if hasattr(item, "model_dump_json") else repr(item)
             for item in scored]
    shape = None if matrix is None else (list(matrix.shape), matrix.tobytes().hex())
    return json.dumps([items, list(ids or []), shape])


def recall_without_memory(world: World, text: str) -> tuple[str, str]:
    """(vector-recall bytes, query-vector bytes) of a run Memory never joins."""
    with one_run(world):
        recall = _recall_bytes(world.retrieval.retrieve_chunk_candidates(
            world.notebook_id, text))
        vector = json.dumps(world.retrieval.candidates._embed_query(text))
    return recall, vector


def _memory(world: World, text: str) -> list[str]:
    return as_bytes(world.retriever.notebook_memory_hits(
        world.asker.id, world.notebook_id, text, 8))


def _agent_memory(world: World, text: str) -> list[str]:
    return as_bytes(world.retriever.agent_memory_hits(
        world.asker.id, world.notebook_id, text, True, 8))


def _vector(world: World, text: str) -> Optional[list[float]]:
    return world.retrieval.candidates._embed_query(text)


def _noted_errors(world: World) -> list[tuple[str, str]]:
    """Start recording the retrieval stage's model-error notes."""
    sink = world.retrieval.candidates.model_error_sink
    noted: list[tuple[str, str]] = []
    original = sink.note_model_error

    def spy(stage, *args, **kwargs):
        noted.append((stage, kwargs.get("workload_id", "")))
        return original(stage, *args, **kwargs)

    sink.note_model_error = spy
    return noted


# -------------------------------------------------------------------- cases


def memory_first_embeds_once_and_scores_alike(world: World) -> None:
    remember(world)
    recall_ref, vector_ref = recall_without_memory(world, PLAIN)
    assert json.loads(vector_ref) == reference_vector(world, PLAIN)
    world.embedder.queries.clear()
    with one_run(world):
        notebook = _memory(world, PLAIN)
        agent = _agent_memory(world, PLAIN)
        vector = _vector(world, PLAIN)
        recall = _recall_bytes(world.retrieval.retrieve_chunk_candidates(
            world.notebook_id, PLAIN))
    # One embed for the whole run: Memory's, read back by the retrieval stage.
    assert world.embedder.queries == [PLAIN]
    # Memory scored the NATIVE vector, byte for byte as the legacy recall.
    assert notebook == oracle_memory(world, PLAIN) and notebook
    assert agent == oracle_memory(world, PLAIN, statuses=("confirmed", "candidate"))
    # ... and the vector really shapes those scores (a truncated or missing
    # query vector would score every stored vector 0).
    assert notebook != oracle_memory(world, PLAIN, down=True)
    # The retrieval stage truncated its own copy, as a fresh embed would be.
    assert vector == reference_vector(world, PLAIN)
    assert len(vector) == (world.runtime_dim or EMBED_DIM)
    assert json.dumps(vector) == vector_ref
    # Its vector recall is byte-identical to a run Memory never joined.
    assert recall == recall_ref and json.loads(recall)[0]


def retrieval_first_shares_only_an_untruncated_vector(world: World) -> None:
    remember(world)
    with one_run(world):
        vector = _vector(world, PLAIN)
        notebook = _memory(world, PLAIN)
    assert vector == reference_vector(world, PLAIN)
    # Untruncated, the stage's vector IS the native one and Memory reads it;
    # truncated, it is never offered and Memory embeds on its own.
    expected = [PLAIN] if not world.runtime_dim else [PLAIN, PLAIN]
    assert world.embedder.queries == expected
    assert notebook == oracle_memory(world, PLAIN) and notebook


def different_texts_embed_each(world: World) -> None:
    remember(world)
    limit = int(world.repo.settings.embed_truncate_chars)
    long_text = PLAIN + " " + "corner " * (limit // 7 + 1)
    assert len(long_text) > limit
    cases = [
        # Memory probes the marker-stripped string, the stage the original.
        (QUOTED, PLAIN, QUOTED),
        # Memory strips the surrounding whitespace, the stage does not.
        (f"  {PLAIN}  ", PLAIN, f"  {PLAIN}  "),
        # The stage cuts to EMBED_TRUNCATE_CHARS, Memory embeds it whole.
        (long_text, long_text.strip(), long_text[:limit]),
    ]
    for text, memory_text, stage_text in cases:
        world.embedder.queries.clear()
        with one_run(world):
            notebook = _memory(world, text)
            vector = _vector(world, text)
        assert world.embedder.queries == [memory_text, stage_text], text[:40]
        assert notebook == oracle_memory(world, text), text[:40]
        assert vector == reference_vector(world, stage_text), text[:40]


def different_embedders_embed_each(world: World) -> None:
    remember(world)
    other = CountingEmbedder(salt="other model:")
    world.retriever.replace_embedder(other)
    with one_run(world):
        notebook = _memory(world, PLAIN)
        vector = _vector(world, PLAIN)
    assert other.queries == [PLAIN] and world.embedder.queries == [PLAIN]
    assert notebook == oracle_memory(world, PLAIN, salt="other model:")
    assert vector == reference_vector(world, PLAIN)


def unconfigured_embedder_never_shares(world: World) -> None:
    remember(world)
    world.embedder.configured = False
    world.embedder.queries.clear()
    with one_run(world):
        first = _memory(world, PLAIN)
        second = _memory(world, PLAIN)
        vector = _vector(world, PLAIN)
    # Memory embeds with whatever stands in, each time, as before; the stage
    # does not embed at all.
    assert world.embedder.queries == [PLAIN, PLAIN]
    assert vector is None
    assert first == second == oracle_memory(world, PLAIN) and first


def offline_mode_never_shares(world: World) -> None:
    """No model services at all: both seats are the provider's offline stand-in."""
    remember(world)
    offline = world.repo._runtime.models._offline_embedding("retrieval_query_embedding")
    calls: list[str] = []
    original = offline.embed_query

    def counting(text):
        calls.append(text)
        return original(text)

    offline.embed_query = counting
    try:
        assert not world.retriever.embedder.configured
        with one_run(world):
            first = _memory(world, PLAIN)
            second = _memory(world, PLAIN)
            vector = _vector(world, PLAIN)
    finally:
        del offline.embed_query
    assert calls == [PLAIN, PLAIN]
    assert vector is None
    assert first == second == oracle_memory(world, PLAIN) and first


def failed_embeds_are_unchanged(world: World) -> None:
    remember(world)
    lexical_only = oracle_memory(world, PLAIN, down=True)
    assert lexical_only and lexical_only != oracle_memory(world, PLAIN)
    noted = _noted_errors(world)
    # Memory's embed raises or returns None: its recall falls back to the
    # lexical scores silently, nothing is offered, and the stage embeds (and
    # succeeds) on its own.
    for outcome in ("raise", "none"):
        world.embedder.queries.clear()
        world.embedder.fail = [outcome]
        with one_run(world):
            notebook = _memory(world, PLAIN)
            vector = _vector(world, PLAIN)
        assert world.embedder.queries == [PLAIN, PLAIN], outcome
        assert notebook == lexical_only, outcome
        assert vector == reference_vector(world, PLAIN), outcome
    assert noted == []
    # Both fail: the stage reports its own failure, exactly once, as before.
    world.embedder.queries.clear()
    world.embedder.fail = ["raise", "raise"]
    with one_run(world):
        notebook = _memory(world, PLAIN)
        vector = _vector(world, PLAIN)
    assert world.embedder.queries == [PLAIN, PLAIN]
    assert notebook == lexical_only and vector is None
    assert noted == [("embed", "retrieval_query_embedding")]
    # The stage fails first: it reports, and Memory embeds on its own and
    # scores the native vector.
    noted.clear()
    world.embedder.queries.clear()
    world.embedder.fail = ["raise"]
    with one_run(world):
        vector = _vector(world, PLAIN)
        notebook = _memory(world, PLAIN)
    assert world.embedder.queries == [PLAIN, PLAIN]
    assert vector is None and notebook == oracle_memory(world, PLAIN)
    assert noted == [("embed", "retrieval_query_embedding")]


def outside_a_run_nothing_is_shared(world: World) -> None:
    remember(world)
    world.embedder.queries.clear()
    with as_asker(world):
        notebook = _memory(world, PLAIN)
        vector = _vector(world, PLAIN)
        again = _memory(world, PLAIN)
    assert world.embedder.queries == [PLAIN, PLAIN, PLAIN]
    assert notebook == again == oracle_memory(world, PLAIN) and notebook
    assert vector == reference_vector(world, PLAIN)


def no_vector_memory_is_unchanged(world: World) -> None:
    """No Memory: the recall does not embed (the first step), the stage once."""
    recall_ref, vector_ref = recall_without_memory(world, PLAIN)
    world.embedder.queries.clear()
    with one_run(world):
        notebook = _memory(world, PLAIN)
        vector = _vector(world, PLAIN)
        recall = _recall_bytes(world.retrieval.retrieve_chunk_candidates(
            world.notebook_id, PLAIN))
    assert world.embedder.queries == [PLAIN]
    assert notebook == []
    assert json.dumps(vector) == vector_ref and recall == recall_ref


def _table_size() -> int:
    from app.services.retrieval_run import current_retrieval_run

    return len(current_retrieval_run()._query_vectors)


def report_run_keeps_the_hand_over_bounded(world: World) -> None:
    """A report recalls Memory once per section, each with its own text that
    nothing reads back; the run must not keep every one of those vectors."""
    from app.services.retrieval_run import QUERY_VECTOR_SHARE_LIMIT

    remember(world)
    sizes = []
    with one_run(world):
        for index in range(12):
            section = f"{PLAIN} section {index} corner review"
            assert _memory(world, section) == oracle_memory(world, section)
            _vector(world, f"section {index} sub query")
            sizes.append(_table_size())
        # The Ask-shaped hand-over still works at the end of a long run.
        world.embedder.queries.clear()
        notebook = _memory(world, PLAIN)
        vector = _vector(world, PLAIN)
        sizes.append(_table_size())
    assert max(sizes) == QUERY_VECTOR_SHARE_LIMIT, sizes
    assert world.embedder.queries == [PLAIN]
    assert notebook == oracle_memory(world, PLAIN) and notebook
    assert vector == reference_vector(world, PLAIN)


def scout_shares_across_libraries(world: World) -> None:
    """A report scout probes each library with the same question: the stage
    embed (memoised after the first library) and one Memory recall per
    library. Every later recall reuses the first native vector it can."""
    remember(world)
    world.embedder.queries.clear()
    hits = []
    with one_run(world):
        for _library in range(3):
            world.retrieval.retrieve_chunk_candidates(world.notebook_id, PLAIN)
            hits.append(_memory(world, PLAIN))
    # Untruncated, the stage's own vector serves every recall; truncated, the
    # first recall embeds once and the others reuse that.
    expected = [PLAIN] if not world.runtime_dim else [PLAIN, PLAIN]
    assert world.embedder.queries == expected
    assert all(item == oracle_memory(world, PLAIN) for item in hits) and hits[0]
