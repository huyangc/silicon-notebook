import json

import numpy as np
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


def _add_source(repo, notebook_id, *, source_id, object_id, chunk_id, day):
    now = f"2026-07-{day:02d}T00:00:00"
    text = f"concept {object_id}"
    vector = json.dumps(repo.embedder.embed_texts([text])[0])
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
        lambda notebook_id, temporary: (_ for _ in ()).throw(
            RuntimeError("injected swap failure")
        ),
    )
    with pytest.raises(RuntimeError, match="injected swap failure"):
        builder.fold(indexed_notebook)
    assert manifest.read_bytes() == before
    assert store.load_scale(indexed_notebook) is not None
    assert indexed_notebook not in repo._scale_building


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
        lambda notebook_id, artifacts: (_ for _ in ()).throw(
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
        "persist",
        "total",
    ]
    assert manifest["n_nodes"] >= 2
