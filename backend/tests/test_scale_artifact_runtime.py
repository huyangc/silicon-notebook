"""Task 20: scale/viz cache, version and scheduling state has one owner."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in {
        "EMBED_PROVIDER": "dashscope",
        "EMBED_BASE_URL": "https://e.test",
        "EMBED_API_KEY": "k",
        "EMBED_MODEL": "m",
        "EMBED_DIM": "16",
    }.items():
        monkeypatch.setenv(key, value)
    repository = SQLiteRepository(Settings(_env_file=None))
    repository.embedder = FakeEmbedder(dim=16)
    return repository


def _seed(repo):
    notebook = repo.create_notebook(NotebookCreate(name="scale-runtime"))
    repo.store_kg(
        notebook.id,
        None,
        [{
            "local_id": "a",
            "object_type": "concept",
            "payload": {"name": "MOSFET", "section_path": ""},
            "evidence": [],
        }],
        [],
    )
    return notebook


def test_facade_scale_runtime_properties_share_identity(repo):
    scale = repo._runtime.scale_artifacts
    assert scale.catalog is repo._runtime.scale_catalog
    assert scale.builder is repo._runtime.scale_builder
    assert repo._scale_idx_cache is scale.scale_cache
    assert repo._viz_idx_cache is scale.viz_cache
    assert repo._scale_ver_cache is scale.version_memo
    assert repo._scale_ver_lock is scale.version_lock
    assert repo._scale_ver_locks is scale.version_locks
    assert repo._scale_idx_load_lock is scale.load_lock
    assert repo._scale_idx_load_locks is scale.load_locks
    assert repo._scale_building is scale.building
    assert repo._scale_building_lock is scale.building_lock
    assert repo._scale_idle_queue is scale.idle_queue
    assert repo._auto_index_checked is scale.auto_index_checked
    assert repo._viz_building is scale.viz_building
    assert repo._viz_building_lock is scale.viz_building_lock


def test_scale_runtime_callbacks_do_not_retain_repository_facade(repo):
    import functools
    import inspect

    def retains(value, target, seen=None):
        if value is target:
            return True
        if value is None or isinstance(value, (str, bytes, int, float, bool)):
            return False
        seen = seen or set()
        marker = id(value)
        if marker in seen:
            return False
        seen.add(marker)
        if inspect.ismethod(value):
            return retains(value.__self__, target, seen) or retains(
                value.__func__, target, seen
            )
        if isinstance(value, functools.partial):
            return any(
                retains(part, target, seen)
                for part in (value.func, value.args, value.keywords)
            )
        if inspect.isfunction(value):
            stored = [value.__defaults__, value.__kwdefaults__]
            stored.extend(
                cell.cell_contents for cell in (value.__closure__ or ())
            )
            return any(retains(part, target, seen) for part in stored)
        if isinstance(value, dict):
            return any(
                retains(part, target, seen)
                for pair in value.items()
                for part in pair
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(retains(part, target, seen) for part in value)
        return False

    scale = repo._runtime.scale_artifacts
    callbacks = {
        name: getattr(scale, name)
        for name in {
            "get_notebook",
            "notebook_copy_stats",
            "eligible",
            "notify_index_done",
            "unified_status",
        }
    }
    assert [name for name, callback in callbacks.items() if retains(callback, repo)] == []


def test_version_cold_failure_releases_singleflight_and_does_not_poison_memo(
    repo, monkeypatch
):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    projections = repo._runtime.index_projections
    real = projections.version_facts
    calls = 0

    def fail_once(notebook_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("cold version failed")
        return real(notebook_id)

    monkeypatch.setattr(projections, "version_facts", fail_once)
    with pytest.raises(RuntimeError, match="cold version failed"):
        scale.version(notebook.id)
    version = scale.version(notebook.id)
    assert isinstance(version, list)
    assert calls == 2
    assert notebook.id in scale.version_memo


def test_six_concurrent_stale_loads_hit_disk_once(repo, monkeypatch):
    notebook = _seed(repo)
    repo.build_scale_index(notebook.id)
    repo.store_kg(
        notebook.id,
        None,
        [{
            "local_id": "b",
            "object_type": "concept",
            "payload": {"name": "gm", "section_path": ""},
            "evidence": [],
        }],
        [],
    )
    scale = repo._runtime.scale_artifacts
    scale.scale_cache.pop(notebook.id, None)
    store = repo._runtime.scale_artifact_store
    real = store.load_scale
    calls = 0
    calls_lock = threading.Lock()

    def slow_load(notebook_id):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.03)
        return real(notebook_id)

    monkeypatch.setattr(store, "load_scale", slow_load)
    with ThreadPoolExecutor(max_workers=6) as pool:
        loaded = list(pool.map(lambda _: scale.load(notebook.id, allow_stale=True), range(6)))
    assert calls == 1
    assert loaded[0] is not None
    assert all(item is loaded[0] for item in loaded)


def test_exact_and_stale_loads_preserve_disk_identity(repo):
    notebook = _seed(repo)
    repo.build_scale_index(notebook.id)
    scale = repo._runtime.scale_artifacts
    exact = scale.load(notebook.id)
    assert exact is not None
    repo.store_kg(
        notebook.id,
        None,
        [{
            "local_id": "b",
            "object_type": "concept",
            "payload": {"name": "ro", "section_path": ""},
            "evidence": [],
        }],
        [],
    )
    assert scale.load(notebook.id) is None
    stale_a = scale.load(notebook.id, allow_stale=True)
    stale_b = scale.load(notebook.id, allow_stale=True)
    assert stale_a is exact
    assert stale_b is exact


def test_viz_probe_is_read_only_and_never_invokes_builder(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(
        scale.builder,
        "build_viz",
        lambda *_: (_ for _ in ()).throw(AssertionError("probe attempted build")),
    )
    assert scale.viz_probe(notebook.id) == {
        "viz_indexed": False,
        "viz_nodes": 0,
        "viz_edges": 0,
        "viz_stale": False,
    }


def test_auto_index_once_set_short_circuits_before_scale_fact_aggregates(
    repo, monkeypatch
):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    scale.auto_index_checked.add(notebook.id)
    monkeypatch.setattr(
        repo._runtime.index_projections,
        "version_facts",
        lambda *_: (_ for _ in ()).throw(AssertionError("scale facts queried")),
    )
    monkeypatch.setattr(
        scale,
        "notebook_copy_stats",
        lambda *_: (_ for _ in ()).throw(AssertionError("copy aggregates queried")),
    )
    scale.maybe_auto_index(notebook.id)


def test_daemon_and_viz_failures_clear_build_markers(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale, "_start_daemon", lambda _name, target: target())
    monkeypatch.setattr(
        scale.builder,
        "build",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("build failed")),
    )
    monkeypatch.setattr(scale, "_resolve_mode", lambda *_: "full")
    scale._run_scale_op(notebook.id, "full")
    assert notebook.id not in scale.building

    monkeypatch.setattr(
        scale.builder,
        "build_viz",
        lambda *_: (_ for _ in ()).throw(RuntimeError("viz failed")),
    )
    scale._spawn_viz_build(notebook.id)
    assert notebook.id not in scale.viz_building


def test_runtime_never_holds_state_locks_around_loader_builder_or_notification(
    repo, monkeypatch
):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    observations = []
    monkeypatch.setattr(scale, "_start_daemon", lambda _name, target: target())
    monkeypatch.setattr(scale, "_resolve_mode", lambda *_: "full")
    monkeypatch.setattr(
        scale.builder,
        "build",
        lambda *_args, **_kwargs: observations.append(
            ("builder", scale.building_lock.locked())
        ) or {"version": []},
    )
    monkeypatch.setattr(
        scale,
        "notify_index_done",
        lambda *_: observations.append(
            ("notification", scale.building_lock.locked())
        ),
    )
    scale._run_scale_op(notebook.id, "full")
    assert observations == [("builder", False), ("notification", False)]


def test_daemon_start_failures_rearm_all_runtime_markers(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(
        scale,
        "_start_daemon",
        lambda *_: (_ for _ in ()).throw(RuntimeError("thread start failed")),
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        scale._run_scale_op(notebook.id, "full")
    assert notebook.id not in scale.building

    with pytest.raises(RuntimeError, match="thread start failed"):
        scale._spawn_viz_build(notebook.id)
    assert notebook.id not in scale.viz_building

    with pytest.raises(RuntimeError, match="thread start failed"):
        scale._ensure_scheduler()
    assert scale.scheduler_started is False
