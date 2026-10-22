"""Memory recall embeds the query only when some candidate can use the vector.

Shared by ``test_memory_embed_skip.py`` (SQLite) and
``postgres/test_memory_embed_skip_pg.py``. ``MemoryRetriever._hits`` used to
embed the research question before looking at its candidates, so an asker with
no Memory in the library -- the common case -- paid one serial embed round trip
for nothing. A candidate takes part in vector scoring exactly when its stored
vector decodes to something other than ``None``; with no such candidate the
query vector cannot change any score, so the embed is skipped.

The oracle is the pre-change ``_hits`` (``legacy_hits``, kept verbatim below):
in every world the new hits must equal it byte for byte, and the embed count
must be 0 (no Memory; Memory whose embedding is pending or failed), 1 (some
candidate has a vector) or 0 (Memory channel closed, as before).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from app.core.query_syntax import quoted_phrases, strip_accepted_quote_markers
from app.core.request_context import reset_request_user, set_request_user
from app.models.memory import MemoryHit
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.memory_retrieval import (
    MEMORY_AUTHORITY,
    MEMORY_SEMANTIC_ELIGIBILITY,
    MemoryRetriever,
    rerank_eligible_memory_hits,
)
from app.services.retrieval import RELEVANCE_FLOOR, _fuse, _tokens, cosine, keyword_basis
from app.services.source_scope import memory_access_context, memory_channel_allowed
from app.services.vector_index import decode_vector
from tests.model_testkit import bind_all_embedding_clients

EMBED_DIM = 16
#: Quoted on purpose: the probe the store sees -- and so the text embedded --
#: is the marker-stripped string, while scoring keeps the original.
QUERY = 'how is "timing closure" signed off'
PROBE = "how is timing closure signed off"


class CountingEmbedder:
    """FakeEmbedder that records every query it is asked to embed."""

    configured = True

    def __init__(self) -> None:
        self.inner = FakeEmbedder(dim=EMBED_DIM)
        self.dim = EMBED_DIM
        self.queries: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self.inner.embed_query(text)

    def embed_texts(self, texts):
        return self.inner.embed_texts(texts)


def legacy_hits(
    retriever: MemoryRetriever,
    embedder: Any,
    user_id: str,
    notebook_id: str,
    query: str,
    statuses: Sequence[str],
    limit: int,
) -> list[MemoryHit]:
    """``MemoryRetriever._hits`` as it was before the skip: always embeds."""
    clean_query = (query or "").strip()
    limit = max(1, min(int(limit), 50))
    if not clean_query or not memory_channel_allowed():
        return []
    candidate_query = strip_accepted_quote_markers(clean_query).strip() or clean_query
    rows = retriever.store.memory_retrieval_rows(
        user_id,
        notebook_id,
        statuses,
        candidate_query,
        lexical_limit=retriever.LEXICAL_CANDIDATES,
        vector_limit=retriever.VECTOR_CANDIDATES,
        phrase_queries=quoted_phrases(clean_query),
    )
    try:
        query_vector = embedder.embed_query(candidate_query)
    except Exception:
        query_vector = None
    basis = keyword_basis(clean_query)
    hits: list[MemoryHit] = []
    for row in rows:
        item = row["record"]
        text = f"{item.title}\n{item.content_md}\n{' '.join(item.tags)}"
        lexical = basis.coverage(set(_tokens(text)), text)
        vector = decode_vector(row.get("vector"))
        has_vector = query_vector is not None and vector is not None
        semantic = cosine(query_vector, vector.tolist()) if has_vector else 0.0
        if lexical <= 0.0 and semantic < MEMORY_SEMANTIC_ELIGIBILITY:
            continue
        relevance = _fuse(lexical, semantic, has_vector)
        if relevance < RELEVANCE_FLOOR:
            continue
        hits.append(
            MemoryHit(
                memory_id=item.id,
                title=item.title,
                text=item.content_md,
                status=item.status,
                authority=MEMORY_AUTHORITY[item.status],
                score=relevance,
                provenance=dict(item.provenance),
            )
        )
    return rerank_eligible_memory_hits(hits)[:limit]


def as_bytes(hits: Sequence[MemoryHit]) -> list[str]:
    return [hit.model_dump_json() for hit in hits]


@dataclass
class World:
    repo: Any
    retriever: MemoryRetriever
    embedder: CountingEmbedder
    asker: Any
    notebook_id: str


def build(repo: Any) -> World:
    embedder = CountingEmbedder()
    bind_all_embedding_clients(repo, embedder)
    asker = repo._runtime.identity.create_user("a00777001", "pw123456")
    token = set_request_user(asker)
    try:
        notebook_id = repo.create_notebook(NotebookCreate(name="Memory embed skip")).id
    finally:
        reset_request_user(token)
    retriever = repo._runtime.memory_retriever
    assert retriever.embedder is embedder
    return World(repo, retriever, embedder, asker, notebook_id)


def _remember(world: World, title: str, content: str, *, embed: str,
              confirm: bool = True) -> str:
    """One Memory entry of the asker; ``embed`` is ``ready``, ``pending`` or
    ``failed`` -- the last two leave no stored vector."""
    service = world.repo._runtime.memory_service
    scheduler = service.embedding_scheduler

    def failing(fn, job):
        raise RuntimeError("embedding queue down")

    service.embedding_scheduler = {
        "ready": lambda fn, job: fn(job),
        "pending": lambda fn, job: None,
        "failed": failing,
    }[embed]
    try:
        item = service.create_candidate(
            world.notebook_id, world.asker.id, None, f"req-{title}", title, content,
            [], "task")
        if confirm:
            item = service.confirm(item.id, world.asker.id, {"extract_kg": False})
    finally:
        service.embedding_scheduler = scheduler
    return item.id


def _without_vectors(world: World) -> None:
    _remember(world, "Timing sign-off", "Timing closure is signed off after STA.",
              embed="pending")
    _remember(world, "Closure checklist", "Run timing closure before tape-out.",
              embed="failed")
    _remember(world, "Lunch", "Banana inventory.", embed="pending")
    _remember(world, "Draft timing note", "Timing closure draft, unconfirmed.",
              embed="pending", confirm=False)


def _with_vectors(world: World) -> None:
    _without_vectors(world)
    _remember(world, "Signed timing closure", "Timing closure signed off by review.",
              embed="ready")


def _planes(world: World) -> list[tuple[str, Any, tuple[str, ...]]]:
    r = world.retriever
    uid, nb = world.asker.id, world.notebook_id
    return [
        ("notebook", lambda: r.notebook_memory_hits(uid, nb, QUERY, 8), ("confirmed",)),
        ("agent", lambda: r.agent_memory_hits(uid, nb, QUERY, True, 8),
         ("confirmed", "candidate")),
    ]


def _run(world: World) -> dict[str, tuple[list[str], list[str], list[str]]]:
    """Per plane: (new hits, legacy hits, queries the new call embedded)."""
    out = {}
    oracle = FakeEmbedder(dim=EMBED_DIM)
    token = set_request_user(world.asker)
    try:
        for plane, call, statuses in _planes(world):
            world.embedder.queries.clear()
            new = as_bytes(call())
            embedded = list(world.embedder.queries)
            old = as_bytes(legacy_hits(world.retriever, oracle, world.asker.id,
                                       world.notebook_id, QUERY, statuses, 8))
            out[plane] = (new, old, embedded)
    finally:
        reset_request_user(token)
    return out


def no_memory_never_embeds(world: World) -> None:
    for plane, (new, old, embedded) in _run(world).items():
        assert embedded == [], plane
        assert new == old == [], plane


def memory_without_vectors_never_embeds_and_scores_alike(world: World) -> None:
    _without_vectors(world)
    for plane, (new, old, embedded) in _run(world).items():
        assert embedded == [], plane
        assert new == old, plane
        assert new, f"{plane}: the lexical arm must still find the timing entries"
    # The candidate plane really sees the unconfirmed entry too: the lexical arm
    # runs on every row whatever the vector state.
    runs = _run(world)
    assert len(runs["agent"][0]) > len(runs["notebook"][0])


def memory_with_a_vector_embeds_once(world: World) -> None:
    _with_vectors(world)
    for plane, (new, old, embedded) in _run(world).items():
        assert embedded == [PROBE], plane
        assert new == old, plane
    # The vector really shapes the scores: the same rows scored without a
    # query vector come out differently, so skipping here would be a regression.
    token = set_request_user(world.asker)
    try:
        class _Down:
            def embed_query(self, text):
                raise RuntimeError("down")

        lexical_only = as_bytes(legacy_hits(
            world.retriever, _Down(), world.asker.id, world.notebook_id, QUERY,
            ("confirmed",), 8))
    finally:
        reset_request_user(token)
    assert lexical_only != _run(world)["notebook"][0]


def closed_channel_is_unchanged(world: World) -> None:
    _with_vectors(world)
    reads: list[str] = []
    store = world.retriever.store
    original = store.memory_retrieval_rows

    def spy(*args, **kwargs):
        reads.append("rows")
        return original(*args, **kwargs)

    store.memory_retrieval_rows = spy
    try:
        with memory_access_context(False):
            for plane, (new, old, embedded) in _run(world).items():
                assert embedded == [], plane
                assert new == old == [], plane
    finally:
        del store.memory_retrieval_rows
    # Closed before the store is touched (the legacy oracle reads nothing either).
    assert reads == []
    # And re-opened, the very same world embeds once again.
    assert all(embedded == [PROBE] for _, _, embedded in _run(world).values())
