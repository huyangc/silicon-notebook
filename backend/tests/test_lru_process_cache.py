"""C7: LRUProcessCache — the bounded, thread-safe dict-like sibling of
VectorCache used by _scale_idx_cache / _viz_idx_cache (SQLiteRepository).
Unlike VectorCache it has no version-keying / single-flight loader contract,
just plain dict.get/__setitem__/pop semantics with an LRU eviction cap.
"""
import threading

import pytest

from app.services.vector_cache import LRUProcessCache


def test_get_missing_returns_default():
    c = LRUProcessCache(max_entries=3)
    assert c.get("nope") is None
    assert c.get("nope", "fallback") == "fallback"


def test_setitem_and_get_round_trip():
    c = LRUProcessCache(max_entries=3)
    c["a"] = "value-a"
    assert c.get("a") == "value-a"
    assert c["a"] == "value-a"


def test_getitem_missing_raises_keyerror():
    c = LRUProcessCache(max_entries=3)
    with pytest.raises(KeyError):
        c["nope"]


def test_pop_removes_and_returns_default_when_absent():
    c = LRUProcessCache(max_entries=3)
    c["a"] = 1
    assert c.pop("a") == 1
    assert c.get("a") is None
    assert c.pop("a", "gone") == "gone"
    assert c.pop("never-existed") is None


def test_cap_respected_evicts_oldest_on_insert():
    c = LRUProcessCache(max_entries=3)
    c["a"] = 1
    c["b"] = 2
    c["c"] = 3
    assert len(c) == 3
    c["d"] = 4  # over cap -> evict oldest (a, never touched since insert)
    assert len(c) == 3
    assert "a" not in c
    assert "b" in c and "c" in c and "d" in c


def test_eviction_order_is_lru_not_fifo():
    """Touching (get) an entry must refresh its recency — the LEAST recently
    used entry is evicted, not simply the oldest-inserted one."""
    c = LRUProcessCache(max_entries=3)
    c["a"] = 1
    c["b"] = 2
    c["c"] = 3
    # Touch "a" via get -> "a" is now most-recently-used; "b" becomes oldest.
    assert c.get("a") == 1
    c["d"] = 4  # should evict "b", not "a" (a was just touched)
    assert "b" not in c
    assert "a" in c and "c" in c and "d" in c


def test_setitem_hit_also_refreshes_recency():
    """Re-assigning an EXISTING key must also count as a touch (move_to_end),
    not just reads via get/__getitem__."""
    c = LRUProcessCache(max_entries=3)
    c["a"] = 1
    c["b"] = 2
    c["c"] = 3
    c["a"] = "updated"  # re-set (not a fresh insert) -> should refresh a's recency
    c["d"] = 4  # should evict "b" (oldest untouched), not "a"
    assert "b" not in c
    assert c["a"] == "updated"


def test_evicted_entry_reloads_correctly():
    """Simulates the real _scale_idx_cache/_viz_idx_cache usage: after
    eviction, a subsequent .get() miss + reload-and-reinsert must produce a
    fresh, correct value (no stale/corrupted state left behind by eviction)."""
    c = LRUProcessCache(max_entries=2)
    reload_calls = {"n": 0}

    def load(key):
        reload_calls["n"] += 1
        return f"{key}-loaded-{reload_calls['n']}"

    c["nb1"] = load("nb1")
    c["nb2"] = load("nb2")
    c["nb3"] = load("nb3")  # evicts nb1 (oldest, cap=2)
    assert "nb1" not in c
    assert reload_calls["n"] == 3

    # Simulate the repo's cache-miss-then-reload pattern.
    cached = c.get("nb1")
    assert cached is None
    fresh = load("nb1")
    c["nb1"] = fresh
    assert c.get("nb1") == fresh
    assert reload_calls["n"] == 4


def test_default_max_entries_is_eight():
    c = LRUProcessCache()
    for i in range(10):
        c[f"k{i}"] = i
    assert len(c) == 8


def test_thread_safety_concurrent_inserts_no_corruption():
    c = LRUProcessCache(max_entries=5)
    errors = []

    def worker(i):
        try:
            for j in range(50):
                c[f"k{i}-{j}"] = (i, j)
                c.get(f"k{i}-{j}")
        except Exception as e:  # pragma: no cover - diagnostic aid
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(c) <= 5


def test_scale_idx_cache_max_setting_default_and_env(monkeypatch):
    from app.core.config import Settings
    s = Settings(_env_file=None)
    assert s.scale_idx_cache_max == 8
    monkeypatch.setenv("SCALE_IDX_CACHE_MAX", "3")
    s2 = Settings(_env_file=None)
    assert s2.scale_idx_cache_max == 3


def test_startup_scale_preload_setting_defaults_on_and_can_be_disabled(monkeypatch):
    from app.core.config import Settings

    monkeypatch.delenv("STARTUP_PRELOAD_SCALE_INDEXES", raising=False)
    assert Settings(_env_file=None).startup_preload_scale_indexes is True
    monkeypatch.setenv("STARTUP_PRELOAD_SCALE_INDEXES", "false")
    assert Settings(_env_file=None).startup_preload_scale_indexes is False
