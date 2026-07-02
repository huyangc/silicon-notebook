"""Single-flight for _scale_index_version's cold (non-cluster aggregate) path.

Production evidence: on a 490k-object/1.13M-node deployment, opening the KG
page fires 3-5 concurrent requests. Each independently cold-misses
_scale_ver_cache and runs the four non-cluster COUNT/MAX aggregates over
GB-scale tables — measured 96s/143s/147s OVERLAPPING (not queued), because
nothing serializes concurrent cold callers for the SAME notebook. Since
PR#157 bumps kg_mutation_seq on every chunk write, every upload re-triggers
one of these cold recomputes, which N concurrent viewers then each redo.

This test file locks in: N concurrent cold callers for the same notebook
must trigger the underlying aggregate computation exactly ONCE, all must
observe the identical resulting version tuple, a seq bump must invalidate
and force exactly one recompute, loader exceptions must propagate (nothing
cached on failure + retry works), and there must be no deadlock when a
loader is itself re-entrant into _scale_index_version for a DIFFERENT
notebook while another thread holds the first notebook's per-nb lock.
"""
import threading
import time

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _add_concept(repo, nb_id, local_id, name):
    repo.store_kg(nb_id, None, [{
        "local_id": local_id, "object_type": "concept",
        "payload": {"name": name, "section_path": ""}, "evidence": [],
    }], [])


class _SlowConn:
    """Wraps a real sqlite3 connection; every knowledge_objects COUNT/MAX
    aggregate blocks on `gate` and increments `calls` — lets the test observe
    (and control the interleaving of) how many times the expensive cold path
    actually runs underneath concurrent callers."""

    def __init__(self, conn, calls, gate_event, release_event, hold_seconds):
        self._conn = conn
        self._calls = calls
        self._gate_event = gate_event
        self._release_event = release_event
        self._hold_seconds = hold_seconds

    def execute(self, sql, *a, **k):
        s = " ".join(str(sql).split())
        if "COUNT(*)" in s and "knowledge_objects" in s:
            self._calls.append(1)
            self._gate_event.set()
            if self._hold_seconds:
                time.sleep(self._hold_seconds)
            else:
                self._release_event.wait(timeout=5)
        return self._conn.execute(sql, *a, **k)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return self._conn.__exit__(*a) if hasattr(self._conn, "__exit__") else None

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_concurrent_cold_callers_compute_once(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")

    calls = []
    gate_event = threading.Event()
    release_event = threading.Event()
    orig_connect = repo._connect

    def _slow_connect():
        return _SlowConn(orig_connect(), calls, gate_event, release_event, hold_seconds=0)

    monkeypatch.setattr(repo, "_connect", _slow_connect)

    results = []
    errors = []
    barrier = threading.Barrier(5)

    def _worker():
        try:
            barrier.wait(timeout=5)
            results.append(tuple(repo._scale_index_version(nb.id)))
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for t in threads:
        t.start()
    # Let the first caller reach the gate (inside the aggregate), then release
    # it — the other 4 should be queued behind the per-nb lock, not each
    # independently running the aggregate.
    assert gate_event.wait(timeout=5)
    time.sleep(0.05)
    release_event.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"worker errors: {errors}"
    assert len(results) == 5
    assert len(set(results)) == 1, "all concurrent cold callers must observe the identical version"
    assert len(calls) == 1, (
        f"expected the expensive aggregate to run exactly once under concurrent "
        f"cold callers, ran {len(calls)} times"
    )


def test_seq_bump_forces_exactly_one_recompute(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    v0 = repo._scale_index_version(nb.id)  # warm

    calls = []
    orig_connect = repo._connect

    def _counting_connect():
        return _SlowConn(orig_connect(), calls, threading.Event(), threading.Event(), hold_seconds=0.001)

    monkeypatch.setattr(repo, "_connect", _counting_connect)

    _add_concept(repo, nb.id, "b", "current mirror")  # bumps seq

    results = []
    barrier = threading.Barrier(4)

    def _worker():
        barrier.wait(timeout=5)
        results.append(tuple(repo._scale_index_version(nb.id)))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(set(results)) == 1
    assert tuple(v0) not in set(results)
    assert len(calls) == 1, f"seq bump must trigger exactly one recompute, got {len(calls)}"


def test_loader_exception_propagates_and_nothing_cached(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")

    orig_connect = repo._connect
    state = {"fail": True}

    class _FailingConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **k):
            s = " ".join(str(sql).split())
            if "COUNT(*)" in s and "knowledge_objects" in s and state["fail"]:
                raise RuntimeError("boom: simulated cold-path failure")
            return self._conn.execute(sql, *a, **k)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self._conn.__exit__(*a) if hasattr(self._conn, "__exit__") else None

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(repo, "_connect", lambda: _FailingConn(orig_connect()))

    # Force a cold path (bump seq so the memo doesn't short-circuit before
    # reaching the failing aggregate).
    repo._mark_unified_kg_dirty(nb.id)
    with pytest.raises(RuntimeError, match="boom"):
        repo._scale_index_version(nb.id)

    # Nothing cached on failure: fix the connection and retry succeeds.
    state["fail"] = False
    v = repo._scale_index_version(nb.id)
    assert v is not None and len(v) > 0


def test_no_deadlock_with_reentrant_call_for_different_notebook(repo, monkeypatch):
    """A loader that (while holding notebook A's per-nb lock) triggers
    _scale_index_version for a DIFFERENT notebook B must not deadlock. This is
    a defensive stress test of the lock design (global lock only guards the
    lock TABLE, never held while a loader runs) rather than a reproduction of
    an existing production call path — see the audit note in
    _scale_index_version's docstring: no current caller holds another lock
    while calling it."""
    nb_a = repo.create_notebook(NotebookCreate(name="a"))
    nb_b = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb_a.id, "a", "MOSFET")
    _add_concept(repo, nb_b.id, "b", "current mirror")

    orig_connect = repo._connect
    entered_a = threading.Event()
    calls_a = []
    reentered = {"done": False}

    class _ReentrantConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **k):
            s = " ".join(str(sql).split())
            if "COUNT(*)" in s and "knowledge_objects" in s and not reentered["done"]:
                reentered["done"] = True  # only re-enter once, from nb_a's path
                calls_a.append(1)
                entered_a.set()
                # Re-enter _scale_index_version for a DIFFERENT notebook while
                # notebook A's per-nb lock is (by construction) held for this
                # in-flight computation.
                repo._scale_index_version(nb_b.id)
            return self._conn.execute(sql, *a, **k)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self._conn.__exit__(*a) if hasattr(self._conn, "__exit__") else None

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(repo, "_connect", lambda: _ReentrantConn(orig_connect()))

    repo._mark_unified_kg_dirty(nb_a.id)

    done = threading.Event()
    result = {}

    def _run():
        result["v"] = repo._scale_index_version(nb_a.id)
        done.set()

    t = threading.Thread(target=_run)
    t.start()
    assert done.wait(timeout=5), "deadlock: cross-notebook reentrant call did not complete in time"
    t.join(timeout=1)
    assert result["v"] is not None
    assert len(calls_a) == 1
