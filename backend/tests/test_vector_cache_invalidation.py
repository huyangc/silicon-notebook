from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_invalidate_clears_matrix_keys_for_all_embedding_tables(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    nb = "nb-x"
    tables = (
        "knowledge_embeddings",
        "element_embeddings",
        "relation_embeddings",
        "chunk_embeddings",
    )
    for table in tables:
        repo._vector_cache.get(f"{nb}:matrix:{table}", (table, 0, ""), lambda: {})
    repo._vector_cache.get(f"{nb}:kwtok", ("kwtok", 0, ""), lambda: {})
    for table in tables:
        assert f"{nb}:matrix:{table}" in repo._vector_cache._store
    assert f"{nb}:kwtok" in repo._vector_cache._store
    repo._invalidate_unified_cache(nb)
    for table in tables:
        assert f"{nb}:matrix:{table}" not in repo._vector_cache._store
    assert f"{nb}:kwtok" not in repo._vector_cache._store


def test_invalidation_rides_the_runtime_snapshot_cache(tmp_path, monkeypatch):
    """Task 17: the facade `_vector_cache` handle and the runtime's
    RetrievalSnapshotCache alias the SAME VectorCache object, so the facade
    `_invalidate_unified_cache` wrapper (coordinator → snapshot cache) evicts
    entries populated through either handle — no facade-only copy exists."""
    repo = _repo(tmp_path, monkeypatch)
    snapshots = repo._runtime.retrieval_snapshots
    assert repo._vector_cache is snapshots.vector_cache
    nb = "nb-y"
    repo._vector_cache.get(f"{nb}:kwtok", ("kwtok", 0, ""), lambda: {})
    assert f"{nb}:kwtok" in snapshots.vector_cache._store
    repo._invalidate_unified_cache(nb)
    assert f"{nb}:kwtok" not in repo._vector_cache._store
