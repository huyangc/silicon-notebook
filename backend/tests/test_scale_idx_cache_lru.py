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


def test_scale_idx_cache_respects_configured_cap():
    from app.services.vector_cache import LargeAwareLRUCache

    cache = LargeAwareLRUCache(
        max_entries=2,
        max_large=2,
        is_large=lambda _value: False,
    )
    for notebook_id in ("nb0", "nb1", "nb2"):
        cache[notebook_id] = object()

    # Cap=2: only the 2 most-recently-accessed notebooks remain cached.
    assert len(cache) == 2
    assert "nb0" not in cache  # evicted (inserted first, never re-touched)
    assert "nb2" in cache      # most recent


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


# --- PR-4: large-library-aware eviction (audit P1-4) ----------------------

def test_large_aware_cache_caps_resident_large_entries():
    """A large ScaleIndex (n_nodes > threshold) is tens of GB; the plain count
    cap lets 8 accumulate to hundreds of GB → OOM. LargeAwareLRUCache caps
    resident LARGE entries to max_large (evicting the LRU large one), while small
    entries keep evicting by max_entries. Removing the large cap keeps L1 resident
    and fails here."""
    from app.services.vector_cache import LargeAwareLRUCache

    def is_large(v):
        return v["n"] > 1_000_000

    c = LargeAwareLRUCache(max_entries=8, max_large=2, is_large=is_large)
    c["L1"] = {"n": 5_000_000}
    c["L2"] = {"n": 5_000_000}
    c["S1"] = {"n": 10}
    c["S2"] = {"n": 10}
    assert "L1" in c and "L2" in c           # 2 large, at the cap
    c["L3"] = {"n": 5_000_000}               # 3rd large → evict the LRU large (L1)
    assert "L1" not in c                      # oldest large evicted
    assert "L2" in c and "L3" in c            # the 2 most-recent large kept
    assert "S1" in c and "S2" in c            # small entries untouched by the large cap


def test_large_aware_cache_recency_protects_touched_large():
    """A .get() on a large entry refreshes its recency, so the NEXT large insert
    evicts a different (now-oldest) large entry — the hot one survives."""
    from app.services.vector_cache import LargeAwareLRUCache
    c = LargeAwareLRUCache(max_entries=8, max_large=2, is_large=lambda v: v["big"])
    c["L1"] = {"big": True}
    c["L2"] = {"big": True}
    assert c.get("L1")["big"] is True        # touch L1 → most-recently-used
    c["L3"] = {"big": True}                   # evict LRU large = L2 now, not L1
    assert "L2" not in c
    assert "L1" in c and "L3" in c


def test_large_aware_cache_missing_count_classifies_small():
    """An entry whose is_large() is False (e.g. an old ScaleIndex manifest with no
    n_nodes key → _manifest_count None) is treated as SMALL, never counted against
    max_large — a missing count must never trigger over-eviction of real large
    indexes."""
    from app.services.vector_cache import LargeAwareLRUCache
    c = LargeAwareLRUCache(max_entries=8, max_large=1, is_large=lambda v: v.get("n", 0) > 5)
    c["L1"] = {"n": 100}                       # large
    c["U1"] = {}                               # unknown → small
    c["U2"] = {}                               # unknown → small
    assert "L1" in c and "U1" in c and "U2" in c   # smalls don't evict the large


def test_scale_idx_cache_is_large_aware_and_config_defaults():
    """The facade wires the scale-index cache as a LargeAwareLRUCache (viz cache
    stays a plain LRUProcessCache), and the two knobs have their documented
    defaults / env overrides."""
    import os
    from app.services.vector_cache import LargeAwareLRUCache, LRUProcessCache
    s = Settings()
    assert s.scale_idx_cache_max_large == 2
    assert s.scale_idx_large_bytes == 8 * 1024 ** 3
    prev = dict(os.environ)
    try:
        os.environ["SCALE_IDX_CACHE_MAX_LARGE"] = "1"
        os.environ["SCALE_IDX_LARGE_BYTES"] = "4000000000"
        s2 = Settings()
        assert s2.scale_idx_cache_max_large == 1
        assert s2.scale_idx_large_bytes == 4_000_000_000
    finally:
        os.environ.clear()
        os.environ.update(prev)


def test_estimated_ann_bytes_counts_all_ann_artifacts():
    """codex PR#359 r1 P1: index size must count kg (n_ann) + chunk (n_chunk_ann)
    + relation (n_relation_ann) ANN, not just n_nodes. A KG-node-light but
    chunk-ANN-heavy source base must estimate large. Missing counts contribute 0
    (older index → classified small, never over-evicted)."""
    from app.services.kg.scale_index import estimated_ann_bytes
    dim = 1024
    m = {"n_nodes": 5, "n_chunk_ann": 30_000_000}         # node-light, chunk-ANN-heavy
    assert estimated_ann_bytes(m, dim) == 30_000_000 * dim * 4
    m2 = {"n_ann": 1_000, "n_chunk_ann": 2_000, "n_relation_ann": 3_000}
    assert estimated_ann_bytes(m2, dim) == 6_000 * dim * 4     # all three summed
    assert estimated_ann_bytes({"n_nodes": 8_000_000}, dim) == 0   # n_nodes alone → 0
    assert estimated_ann_bytes({}, dim) == 0


def test_large_aware_cache_evicts_large_before_small_on_full_cache():
    """codex PR#359 r1 P2: on a full cache with max_large large entries and a small
    LRU entry, inserting another large must evict the OLDEST LARGE first, not drop
    the small LRU (which would leave the cache one under max_entries and cold-reload
    that small index for nothing)."""
    from app.services.vector_cache import LargeAwareLRUCache
    c = LargeAwareLRUCache(max_entries=3, max_large=2, is_large=lambda v: v["big"])
    c["S1"] = {"big": False}     # small — will be the global LRU
    c["L1"] = {"big": True}
    c["L2"] = {"big": True}      # full: 3 entries, 2 large, LRU = S1
    c["L3"] = {"big": True}      # insert 3rd large
    assert "L1" not in c         # oldest LARGE evicted (large cap runs first)
    assert "S1" in c             # small LRU survives (NOT dropped by the total cap)
    assert "L2" in c and "L3" in c
    assert len(c) == 3           # stays full, not max_entries - 1


def test_repo_scale_idx_cache_is_large_aware(repo):
    """The facade wires _scale_idx_cache as LargeAwareLRUCache; _viz_idx_cache
    stays a plain LRUProcessCache (viz arrays are far smaller than ANN matrices)."""
    from app.services.vector_cache import LargeAwareLRUCache, LRUProcessCache
    assert isinstance(repo._scale_idx_cache, LargeAwareLRUCache)
    assert type(repo._viz_idx_cache) is LRUProcessCache
