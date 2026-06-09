from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_invalidate_clears_matrix_keys_for_both_tables(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    nb = "nb-x"
    repo._vector_cache.get(f"{nb}:matrix:knowledge_embeddings", ("knowledge_embeddings", 0, ""), lambda: {})
    repo._vector_cache.get(f"{nb}:matrix:element_embeddings", ("element_embeddings", 0, ""), lambda: {})
    repo._vector_cache.get(f"{nb}:kwtok", ("kwtok", 0, ""), lambda: {})
    assert f"{nb}:matrix:knowledge_embeddings" in repo._vector_cache._store
    assert f"{nb}:matrix:element_embeddings" in repo._vector_cache._store
    assert f"{nb}:kwtok" in repo._vector_cache._store
    repo._invalidate_unified_cache(nb)
    assert f"{nb}:matrix:knowledge_embeddings" not in repo._vector_cache._store
    assert f"{nb}:matrix:element_embeddings" not in repo._vector_cache._store
    assert f"{nb}:kwtok" not in repo._vector_cache._store
