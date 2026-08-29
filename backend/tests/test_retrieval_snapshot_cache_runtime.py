"""Task 17 — runtime-owned RetrievalSnapshotCache.

The retrieval cache objects the facade constructor used to build inline move
into the runtime's RetrievalSnapshotCache. The facade's `_vector_cache` /
`_unified_cache` handles become write-through descriptors over the SAME
objects (never facade-only copies), and every wired consumer — the
KgMutationCoordinator and the KnowledgeLifecycleService's unified-graph memo —
keeps holding/reading them by identity. (The NotebookScaleProfile copy-stats
memo was one of those consumers until R2-2 moved it into `notebook_scale`'s own
bounded store; `invalidate_kg` still evicts that family, just through a call
instead of a cache key.) `invalidate_kg` is the KG key-family eviction every online mutation
funnels through (moved from the coordinator, itself moved from the facade in
Task 14): the frozen phase matrix in mutation_phases.json replays unchanged.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.vector_cache import VectorCache


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'snap.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_cache_transfer_preserves_object_identity(repo):
    """The snapshot cache owns THE cache objects — the facade handles and the
    coordinator/lifecycle collaborators all alias them, no replacement copies."""
    runtime = repo._runtime
    assert repo._vector_cache is runtime.retrieval_snapshots.vector_cache
    assert repo._unified_cache is runtime.retrieval_snapshots.unified_cache
    assert runtime.kg_mutations.vector_cache is repo._vector_cache
    assert runtime.kg_mutations.unified_cache is repo._unified_cache
    assert runtime.knowledge_lifecycle.unified_cache is repo._unified_cache


def test_facade_vector_cache_setter_updates_all_consumers(repo):
    replacement = VectorCache(max_entries=3)
    repo._vector_cache = replacement
    assert repo._runtime.retrieval_snapshots.vector_cache is replacement
    assert repo._runtime.kg_mutations.vector_cache is replacement


def test_knowledge_query_reads_replaced_vector_cache(repo, monkeypatch):
    """Query projections must not retain the cache object captured at wiring."""
    old_cache = repo._vector_cache
    replacement = VectorCache(max_entries=3)
    notebook_id = "nb-cache-swap"
    version = ("replacement",)
    replacement.get(
        f"{notebook_id}:edge_centrality", version, lambda: {"new-cache": 1.0}
    )
    monkeypatch.setattr(
        repo._runtime.knowledge_query.scale_runtime,
        "version",
        lambda _notebook_id: list(version),
    )

    repo._vector_cache = replacement

    assert repo._edge_centrality_map(notebook_id) == {"new-cache": 1.0}
    assert old_cache.peek(f"{notebook_id}:edge_centrality", version) is False


def test_knowledge_query_invalidates_replaced_notebook_language_map(repo):
    """FTS backfill invalidates the authoritative runtime map after a swap."""
    notebook_id = "nb-language-swap"
    old_map = repo._notebook_langs_cache
    old_map[notebook_id] = ["old"]
    replacement = {notebook_id: ["new"]}

    repo._notebook_langs_cache = replacement
    repo.backfill_chunk_fts(notebook_id)

    assert old_map[notebook_id] == ["old"]
    assert notebook_id not in replacement


def test_facade_unified_cache_setter_updates_all_consumers(repo):
    replacement: dict = {}
    repo._unified_cache = replacement
    assert repo._runtime.retrieval_snapshots.unified_cache is replacement
    assert repo._runtime.kg_mutations.unified_cache is replacement
    assert repo._runtime.knowledge_lifecycle.unified_cache is replacement


def test_snapshot_cache_get_peek_invalidate_delegate_to_vector_cache(repo):
    snapshots = repo._runtime.retrieval_snapshots
    key = "nb-d:matrix:knowledge_embeddings"
    assert snapshots.get(key, ("v", 1), lambda: {"m": 1}) == {"m": 1}
    assert snapshots.peek(key, ("v", 1)) is True
    assert snapshots.peek(key, ("v", 2)) is False  # version mismatch
    snapshots.invalidate(key)
    assert snapshots.peek(key, ("v", 1)) is False


def test_invalidate_kg_evicts_the_frozen_key_families(repo):
    """invalidate_kg preserves the Task-14 eviction exactly: the embedding
    matrices (all four embedding tables), kwtok, EVERY fed_rxgraph AND ppr_graph
    entry (both keyed by the ACTIVE notebook but including base participants, so a
    change in ANY participant must sweep them all — guards the seq-triple reset on
    delete+reingest), entchunk, elemchunk, edge_centrality, clustermap and
    copystats families plus this notebook's unified-cache entries. `edge_support`
    stays out (versioned by canonical_rel_seq) and other notebooks' OTHER per-nb
    entries survive."""
    snapshots = repo._runtime.retrieval_snapshots
    nb, other = "nb-a", "nb-b"
    families = [
        f"{nb}:matrix:knowledge_embeddings",
        f"{nb}:matrix:element_embeddings",
        f"{nb}:matrix:relation_embeddings",
        f"{nb}:matrix:chunk_embeddings",
        f"{nb}:kwtok",
        f"{nb}:ppr_graph",
        f"{nb}:entchunk",
        f"{nb}:elemchunk",
        f"{nb}:edge_centrality",
        f"{nb}:clustermap",
    ]
    for key in families:
        snapshots.get(key, ("v", 1), lambda: {})
    snapshots.get(f"{other}:fed_rxgraph", ("v", 1), lambda: {})
    # A DIFFERENT notebook's ppr_graph that depends on `nb` as a base participant
    # must also be evicted (the seq-triple key can collide after nb's
    # delete+reingest) — mirrors the fed_rxgraph all-evict.
    snapshots.get(f"{other}:ppr_graph", ("v", 1), lambda: {})
    snapshots.get(f"{nb}:edge_support", ("edge_support", 1), lambda: {})
    snapshots.get(f"{other}:kwtok", ("v", 1), lambda: {})
    snapshots.unified_cache[(nb, "concept")] = {"g": 1}
    snapshots.unified_cache[(other, "concept")] = {"g": 2}
    # copystats: same frozen family, new home (R2-2). Warm both notebooks so the
    # sweep's per-notebook scoping is still under test.
    from app.services import notebook_scale

    for notebook_id in (nb, other):
        notebook_scale._memoized_copy_stats(notebook_id, ("v", 1), lambda: {"c": 1})

    snapshots.invalidate_kg(nb)

    for key in families:
        assert not snapshots.peek(key, ("v", 1)), key
    assert not snapshots.peek(f"{other}:fed_rxgraph", ("v", 1))
    assert not snapshots.peek(f"{other}:ppr_graph", ("v", 1))
    assert snapshots.peek(f"{nb}:edge_support", ("edge_support", 1))
    assert snapshots.peek(f"{other}:kwtok", ("v", 1))
    assert (nb, "concept") not in snapshots.unified_cache
    assert (other, "concept") in snapshots.unified_cache
    assert notebook_scale.copy_stats_cached_version(nb) is None
    assert notebook_scale.copy_stats_cached_version(other) == ("v", 1)


def test_invalidate_unified_drops_only_this_notebooks_graph_dict(repo):
    """invalidate_unified (used by the scale build to release full_viz_graph's
    whole-object graph dict after viz_arrays) evicts ONLY this notebook's
    (nb, level) unified-cache entries — NOT other notebooks' graphs, and NOT the
    vector-cache families invalidate_kg sweeps. A no-op body or a broken key
    predicate (k == nb instead of k[0] == nb) fails here."""
    snapshots = repo._runtime.retrieval_snapshots
    nb, other = "nb-u", "nb-v"
    snapshots.unified_cache[(nb, "object")] = {"g": 1}
    snapshots.unified_cache[(nb, "concept")] = {"g": 2}
    snapshots.unified_cache[(other, "object")] = {"g": 3}
    # vector-cache families must stay UNtouched (unlike invalidate_kg)
    snapshots.get(f"{nb}:matrix:knowledge_embeddings", ("v", 1), lambda: {})
    snapshots.get(f"{nb}:clustermap", ("v", 1), lambda: {})

    snapshots.invalidate_unified(nb)

    assert (nb, "object") not in snapshots.unified_cache
    assert (nb, "concept") not in snapshots.unified_cache        # every level for nb
    assert (other, "object") in snapshots.unified_cache          # other notebook untouched
    assert snapshots.peek(f"{nb}:matrix:knowledge_embeddings", ("v", 1))  # vector cache intact
    assert snapshots.peek(f"{nb}:clustermap", ("v", 1))


def test_facade_invalidation_wrapper_reaches_the_snapshot_cache(repo):
    """The facade `_invalidate_unified_cache` wrapper keeps funnelling through
    the coordinator, which now delegates to the snapshot cache — one eviction
    path, same object."""
    snapshots = repo._runtime.retrieval_snapshots
    snapshots.get("nb-w:kwtok", ("kwtok", 0, ""), lambda: {})
    repo._invalidate_unified_cache("nb-w")
    assert snapshots.peek("nb-w:kwtok", ("kwtok", 0, "")) is False
