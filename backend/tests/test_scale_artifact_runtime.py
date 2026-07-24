"""Task 20: scale/viz cache, version and scheduling state has one owner."""
from __future__ import annotations

import gc
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return _make_repo(tmp_path, monkeypatch)


def _make_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(key, value)
    repository = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
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


def test_retained_scale_runtime_does_not_transitively_retain_repository(
    tmp_path, monkeypatch
):
    repository = _make_repo(tmp_path, monkeypatch)
    notebook = repository.create_notebook(NotebookCreate(name="retention"))
    notebook_id = notebook.id
    scale = repository._runtime.scale_artifacts

    assert scale.unified_status(notebook_id)["dirty"] is False

    repository_ref = weakref.ref(repository)
    del notebook
    del repository
    gc.collect()

    assert repository_ref() is None
    with pytest.raises(RuntimeError, match="knowledge lifecycle is not wired"):
        scale.unified_status(notebook_id)


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


def test_startup_preload_loads_ann_and_safe_ppr_core_before_progress(repo):
    notebook = _seed(repo)
    repo.build_scale_index(notebook.id)
    scale = repo._runtime.scale_artifacts
    scale.scale_cache.pop(notebook.id, None)
    scale.snapshots.vector_cache.invalidate(f"{notebook.id}:scale_combined")
    progress = []

    result = repo._preload_scale_retrieval_artifacts(
        progress=lambda done, total: progress.append((done, total))
    )

    loaded = scale.scale_cache.get(notebook.id)
    assert loaded is not None
    assert loaded.ann_handle is not None
    assert loaded._ppr_transition.dtype.name == "float32"
    assert loaded._ppr_chunk_ids == {
        loaded.node_ids[int(position)] for position in loaded.chunk_index
    }
    # Combined graphs share the general VectorCache and are intentionally not
    # claimed as startup-resident: mounted multi-index composition can require
    # multi-GB copies.  The reusable self-only core lives on ScaleIndex instead.
    assert f"{notebook.id}:scale_combined" not in scale.snapshots.vector_cache.keys()
    assert result == {"indexes": 1, "ann_handles": 1, "ppr_cores": 1}
    assert progress == [(0, 1), (1, 1)]


def test_startup_preload_rejects_declared_enabled_ann_with_missing_labels(
    repo, monkeypatch
):
    notebook = _seed(repo)
    repo.build_scale_index(notebook.id)
    scale = repo._runtime.scale_artifacts
    index = scale.load(notebook.id, allow_stale=True)
    index.manifest["has_chunk_ann"] = True
    index.chunk_ann_labels = None
    monkeypatch.setattr(scale.settings, "chunk_ann_enabled", True)
    monkeypatch.setattr(scale, "load", lambda *_args, **_kwargs: index)

    with pytest.raises(RuntimeError, match="declared scale ANN"):
        scale._preload_one_retrieval_index(notebook.id)


def test_startup_preload_refuses_to_evict_earlier_indexes(repo, monkeypatch):
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale.artifacts, "indexed_notebook_ids", lambda: ["a", "b"])
    monkeypatch.setattr(scale.projections, "notebook_tier", lambda _id: "personal")
    monkeypatch.setattr(scale.settings, "scale_idx_cache_max", 1)
    monkeypatch.setattr(
        scale,
        "load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("capacity must fail before loading")
        ),
    )

    with pytest.raises(RuntimeError, match="SCALE_IDX_CACHE_MAX"):
        scale.preload_retrieval_artifacts()


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


# ---------------------------------------------------------------------------
# Task 4 delegation tests: ScaleArtifactRuntime holds ZERO notebook-metadata
# SQL — `self.projections` (== repo._runtime.index_projections) owns the three
# reads it used to run inline (owner / name / unified last-rebuild).  These
# spies pin each fail-open notification / mode-resolution path to its store
# method so the SQL can't be re-inlined.  Primary assertions tagged `# MUT`.
# ---------------------------------------------------------------------------


def _mute_pending_bus(monkeypatch):
    # notify_index_done is fail-open: a prior test can leave the asyncio loop
    # closed so pending_bus.mark_dirty raises and aborts the notify before the
    # projection reads run.  Stub the bus so the delegation path is reached
    # deterministically regardless of suite ordering.
    from app.services.pending_bus import pending_bus

    monkeypatch.setattr(pending_bus, "mark_dirty", lambda *a, **k: None)
    monkeypatch.setattr(pending_bus, "emit", lambda *a, **k: None)


def test_t4deleg_notebook_owner_delegate(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    _mute_pending_bus(monkeypatch)
    calls = []

    def spy(notebook_id):
        calls.append(notebook_id)
        return "user-sentinel"

    monkeypatch.setattr(repo._runtime.index_projections, "notebook_owner", spy)
    # notify_index_done -> _resolve_index_owner (no request user in tests) ->
    # projections.notebook_owner.
    scale.notify_index_done(notebook.id)
    assert calls and calls[0] == notebook.id  # MUT


def test_t4deleg_notebook_name_delegate(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    _mute_pending_bus(monkeypatch)
    calls = []

    def spy(notebook_id):
        calls.append(notebook_id)
        return "SENTINEL-NAME"

    monkeypatch.setattr(repo._runtime.index_projections, "notebook_name", spy)
    # Real notebook_owner returns a truthy owner, so notify_index_done proceeds
    # to _notebook_name -> projections.notebook_name for the emit payload.
    scale.notify_index_done(notebook.id)
    assert calls and calls[0] == notebook.id  # MUT


def test_t4deleg_unified_last_rebuild_at_delegate(repo, monkeypatch):
    notebook = _seed(repo)
    repo.build_scale_index(notebook.id)
    scale = repo._runtime.scale_artifacts
    calls = []

    def spy(notebook_id):
        calls.append(notebook_id)
        # A last-rebuild strictly newer than the fresh index's built_at forces
        # a full rebuild — proving the delegated value is consumed by the mode.
        return "9999-12-31T23:59:59"

    monkeypatch.setattr(
        repo._runtime.index_projections, "unified_last_rebuild_at", spy
    )
    mode = scale._resolve_mode(notebook.id, "fold")
    assert calls and calls[0] == notebook.id and mode == "full"  # MUT
