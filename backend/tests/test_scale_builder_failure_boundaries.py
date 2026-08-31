import json

import numpy as np
import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(key, value)
    repository = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


def _add_source(repo, notebook_id, *, source_id, object_id, chunk_id, day):
    now = f"2026-07-{day:02d}T00:00:00"
    text = f"concept {object_id}"
    vector = json.dumps(repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([text])[0])
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (source_id, notebook_id, source_id, "md", "ready", now, now),
        )
        db.execute(
            "INSERT INTO chunks "
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (chunk_id, notebook_id, source_id, text, "", "[]", now),
        )
        db.execute(
            "INSERT INTO chunk_embeddings "
            "(chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            (chunk_id, notebook_id, vector, now),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                object_id,
                notebook_id,
                "concept",
                "approved",
                "",
                json.dumps({"name": text}),
                "[]",
                source_id,
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO knowledge_embeddings "
            "(object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            (object_id, notebook_id, vector, now),
        )


@pytest.fixture
def indexed_notebook(repo):
    notebook = repo.create_notebook(NotebookCreate(name="indexed"))
    _add_source(
        repo,
        notebook.id,
        source_id="s1",
        object_id="o1",
        chunk_id="c1",
        day=1,
    )
    repo.rebuild_unified_kg(notebook.id)
    repo.build_scale_index(notebook.id)
    _add_source(
        repo,
        notebook.id,
        source_id="s2",
        object_id="o2",
        chunk_id="c2",
        day=2,
    )
    return notebook.id


def test_runtime_owns_scale_builder(repo):
    builder = repo._runtime.scale_builder
    assert builder.projections is repo._runtime.index_projections
    assert builder.artifacts is repo._runtime.scale_artifact_store


def _strongly_references(value, target, seen=None):
    """Inspect callback storage without traversing arbitrary domain objects."""
    import functools
    import inspect

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
        return _strongly_references(value.__self__, target, seen) or \
            _strongly_references(value.__func__, target, seen)
    if isinstance(value, functools.partial):
        return any(
            _strongly_references(part, target, seen)
            for part in (value.func, value.args, value.keywords)
        )
    if inspect.isfunction(value):
        stored = [value.__defaults__, value.__kwdefaults__]
        stored.extend(
            cell.cell_contents
            for cell in (value.__closure__ or ())
            if cell is not None
        )
        return any(
            _strongly_references(part, target, seen) for part in stored
        )
    if isinstance(value, dict):
        return any(
            _strongly_references(part, target, seen)
            for pair in value.items()
            for part in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _strongly_references(part, target, seen) for part in value
        )
    return False


def test_scale_builder_callbacks_do_not_retain_repository_facade(repo):
    builder = repo._runtime.scale_builder
    callbacks = {
        name: getattr(builder, name)
        for name in {
            "get_notebook",
            "version",
            "load_scale",
            "full_viz_graph",
            "relations_for_notebook",
            "cluster_map",
            "incremental_fuse_source",
            "invalidate_scale_cache",
            "cache_viz",
            "notify_index_done",
        }
    }
    callbacks.update(
        {
            f"projections.{name}": getattr(builder.projections, name)
            for name in {
                "connect",
                "in_batches",
                "ent_chunk_map",
                "mention_extra_edges",
                "vector_matrix",
            }
        }
    )
    retained = [
        name
        for name, callback in callbacks.items()
        if _strongly_references(callback, repo)
    ]
    assert retained == []


def test_fold_failure_before_swap_keeps_old_artifact(
    repo, indexed_notebook, monkeypatch
):
    store = repo._runtime.scale_artifact_store
    builder = repo._runtime.scale_builder
    manifest = store.scale_dir(indexed_notebook) / "manifest.json"
    before = manifest.read_bytes()
    monkeypatch.setattr(
        store,
        "swap_fold_directory",
        lambda notebook_id, temporary, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected swap failure")
        ),
    )
    with pytest.raises(RuntimeError, match="injected swap failure"):
        builder.fold(indexed_notebook)
    assert manifest.read_bytes() == before
    assert store.load_scale(indexed_notebook) is not None
    assert indexed_notebook not in repo._scale_building


def test_fold_second_rename_failure_rolls_old_artifact_back(
    repo, indexed_notebook, monkeypatch
):
    from app.repositories.filesystem import scale_artifact_store as store_module

    store = repo._runtime.scale_artifact_store
    builder = repo._runtime.scale_builder
    live_dir = store.scale_dir(indexed_notebook)
    manifest = live_dir / "manifest.json"
    before = manifest.read_bytes()
    real_rename = store_module.os.rename
    rename_calls = []
    injected = OSError("injected second rename failure")

    def fail_second_rename(source, target):
        rename_calls.append((source, target))
        if len(rename_calls) == 2:
            raise injected
        return real_rename(source, target)

    monkeypatch.setattr(store_module.os, "rename", fail_second_rename)
    with pytest.raises(OSError, match="injected second rename failure") as caught:
        builder.fold(indexed_notebook)

    assert caught.value is injected
    assert len(rename_calls) == 3
    assert manifest.read_bytes() == before
    assert store.load_scale(indexed_notebook) is not None
    assert not live_dir.with_name(live_dir.name + ".old").exists()
    staging = live_dir.with_name(live_dir.name + ".tmp")
    assert staging.is_dir()
    assert store.prepare_fold_directory(indexed_notebook) == staging
    assert list(staging.iterdir()) == []


def test_fold_rollback_failure_preserves_old_and_primary_exception(
    repo, indexed_notebook, monkeypatch
):
    from app.repositories.filesystem import scale_artifact_store as store_module

    store = repo._runtime.scale_artifact_store
    builder = repo._runtime.scale_builder
    live_dir = store.scale_dir(indexed_notebook)
    before = (live_dir / "manifest.json").read_bytes()
    real_rename = store_module.os.rename
    rename_calls = []
    primary = OSError("injected publish failure")
    rollback = OSError("injected rollback failure")

    def fail_publish_and_rollback(source, target):
        rename_calls.append((source, target))
        if len(rename_calls) == 2:
            raise primary
        if len(rename_calls) == 3:
            raise rollback
        return real_rename(source, target)

    monkeypatch.setattr(store_module.os, "rename", fail_publish_and_rollback)
    with pytest.raises(OSError, match="injected publish failure") as caught:
        builder.fold(indexed_notebook)

    assert caught.value is primary
    assert len(rename_calls) == 3
    old_dir = live_dir.with_name(live_dir.name + ".old")
    assert not live_dir.exists()
    assert (old_dir / "manifest.json").read_bytes() == before
    assert live_dir.with_name(live_dir.name + ".tmp").is_dir()
    assert any("rollback" in note for note in getattr(primary, "__notes__", []))


def test_full_save_failure_keeps_existing_cached_artifact(
    repo, indexed_notebook, monkeypatch
):
    builder = repo._runtime.scale_builder
    cached = repo._scale_index(indexed_notebook, allow_stale=True)
    assert cached is not None
    repo._scale_idx_cache[indexed_notebook] = cached
    monkeypatch.setattr(
        repo._runtime.scale_artifact_store,
        "save_full",
        lambda notebook_id, artifacts, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected full-save failure")
        ),
    )
    with pytest.raises(RuntimeError, match="injected full-save failure"):
        builder.build(indexed_notebook)
    assert repo._scale_idx_cache.get(indexed_notebook) is cached


def test_builder_gather_graph_preserves_compact_array_dtypes(repo):
    notebook = repo.create_notebook(NotebookCreate(name="arrays"))
    _add_source(
        repo,
        notebook.id,
        source_id="s1",
        object_id="o1",
        chunk_id="c1",
        day=1,
    )
    _, (source, target, weight), _, _, _ = repo._runtime.scale_builder.gather_graph(
        notebook.id, as_arrays=True
    )
    assert source.dtype == np.int32
    assert target.dtype == np.int32
    assert weight.dtype == np.float32


def test_builder_stage_callback_order_and_failure_isolation(repo):
    notebook = repo.create_notebook(NotebookCreate(name="stages"))
    _add_source(
        repo,
        notebook.id,
        source_id="s1",
        object_id="o1",
        chunk_id="c1",
        day=1,
    )
    repo.rebuild_unified_kg(notebook.id)
    calls = []

    def record_then_raise(stage, latency_ms):
        calls.append((stage, latency_ms))
        raise RuntimeError("observer failure")

    manifest = repo._runtime.scale_builder.build(
        notebook.id, on_stage=record_then_raise
    )
    assert [stage for stage, _ in calls] == [
        "kg_matrix",
        "ann_build",
        "synonym",
        "gather",
        "transition",
        "chunk_matrix",
        "relation_matrix",
        "viz_arrays",
        "source_partitions",
        "persist",
        "total",
    ]
    assert manifest["n_nodes"] >= 2


# Fast inner-loop opt-out: these tests build real HNSW/ANN scale indexes.
# Skip them with `pytest -m "not slow"`; full runs (default) still include them.
import pytest as _pytest_slow  # noqa: E402
pytestmark = _pytest_slow.mark.slow
