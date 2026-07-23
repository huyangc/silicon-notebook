"""C7 integration: SQLiteRepository._scale_idx_cache / _viz_idx_cache are now
LRUProcessCache instances (bounded, was an unbounded plain dict). Verify the
cap holds under real multi-notebook scale-index usage and that an evicted
notebook's index reloads correctly (from disk) on the next access — not
stale/corrupted, just a cache miss like any cold start.
"""
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_and_build(repo, name):
    nb = repo.create_notebook(NotebookCreate(name=name))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": f"{name} MOSFET", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    return nb.id


def test_scale_idx_cache_is_lru_process_cache_instance(repo):
    from app.services.vector_cache import LRUProcessCache
    assert isinstance(repo._scale_idx_cache, LRUProcessCache)
    assert isinstance(repo._viz_idx_cache, LRUProcessCache)


def test_scale_idx_cache_respects_configured_cap(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "scale_idx_cache_max", 2)
    # Cache max is baked in at __init__ time (matches VectorCache's own
    # construction-time max_entries convention) — rebuild the cache object
    # the way __init__ does, to test the cap in isolation without needing to
    # spin up 3 real scale indexes (slow) just to prove eviction.
    from app.services.vector_cache import LRUProcessCache
    repo._scale_idx_cache = LRUProcessCache(max_entries=repo.settings.scale_idx_cache_max)

    nb_ids = [_seed_and_build(repo, f"nb{i}") for i in range(3)]
    for nb_id in nb_ids:
        idx = repo._scale_index(nb_id)
        assert idx is not None
    # Cap=2: only the 2 most-recently-accessed notebooks remain cached.
    assert len(repo._scale_idx_cache) <= 2
    assert nb_ids[0] not in repo._scale_idx_cache  # evicted (accessed first, never re-touched)
    assert nb_ids[2] in repo._scale_idx_cache      # most recent


def test_evicted_scale_index_reloads_correctly_from_disk(repo, monkeypatch):
    """An evicted notebook's _scale_index() call must transparently reload
    from the on-disk persisted index (build_scale_index already wrote it) —
    not return None / stale data — exactly like a cold-cache first access."""
    from app.services.vector_cache import LRUProcessCache
    repo._scale_idx_cache = LRUProcessCache(max_entries=1)

    nb_a = _seed_and_build(repo, "alpha")
    idx_a_first = repo._scale_index(nb_a)
    assert idx_a_first is not None
    assert nb_a in repo._scale_idx_cache

    nb_b = _seed_and_build(repo, "beta")
    idx_b = repo._scale_index(nb_b)
    assert idx_b is not None
    # cap=1 -> nb_a's cache entry evicted when nb_b was inserted.
    assert nb_a not in repo._scale_idx_cache
    assert nb_b in repo._scale_idx_cache

    # Re-access nb_a: must reload transparently (from disk) with the SAME
    # manifest version/content — not a stale/corrupted/None result.
    idx_a_reloaded = repo._scale_index(nb_a)
    assert idx_a_reloaded is not None
    assert idx_a_reloaded.manifest.get("version") == idx_a_first.manifest.get("version")
    assert list(idx_a_reloaded.node_ids) == list(idx_a_first.node_ids)
    # Cache now holds nb_a again (evicting nb_b, cap=1).
    assert nb_a in repo._scale_idx_cache
    assert nb_b not in repo._scale_idx_cache


def test_evicted_viz_index_reloads_correctly(repo, monkeypatch):
    from app.services.vector_cache import LRUProcessCache
    repo._viz_idx_cache = LRUProcessCache(max_entries=1)

    nb_a = repo.create_notebook(NotebookCreate(name="viz-a"))
    repo.store_kg(nb_a.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "Alpha Concept", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb_a.id)
    manifest_a = repo.build_viz_index(nb_a.id)
    assert manifest_a is not None
    assert nb_a.id in repo._viz_idx_cache

    nb_b = repo.create_notebook(NotebookCreate(name="viz-b"))
    repo.store_kg(nb_b.id, None, [
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "Beta Concept", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb_b.id)
    repo.build_viz_index(nb_b.id)
    assert nb_a.id not in repo._viz_idx_cache  # evicted, cap=1
    assert nb_b.id in repo._viz_idx_cache

    reloaded = repo._viz_index(nb_a.id)
    assert reloaded is not None
    assert reloaded.manifest.get("version") == manifest_a["version"]
    assert nb_a.id in repo._viz_idx_cache


# Fast inner-loop opt-out: these tests build real HNSW/ANN scale indexes.
# Skip them with `pytest -m "not slow"`; full runs (default) still include them.
import pytest as _pytest_slow  # noqa: E402
pytestmark = _pytest_slow.mark.slow
