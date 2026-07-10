"""Task 18 — on-disk artifact compatibility through ScaleArtifactStore.

The frozen v9 storage fixtures (kg_index / kg_viz manifest.json + npy/npz)
must keep loading byte-compatibly through the filesystem store — an artifact
written by an earlier deploy is served as-is, no format change. The fold
temporary/old/live sequence stays atomic: prepare never touches the live
artifact; only swap replaces it (a fold failure before swap keeps the old
index intact).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import scipy.sparse as sp

from app.repositories.filesystem.scale_artifact_store import ScaleArtifactStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "repository_v9" / "storage"


class _StorageSettings:
    """Minimal storage-dir carrier — the store reads only settings.storage_dir."""

    def __init__(self, storage_dir: str) -> None:
        self.storage_dir = storage_dir


@pytest.fixture
def store(tmp_path):
    return ScaleArtifactStore(_StorageSettings(str(tmp_path / "storage")))


def _copy_fixture(store, kind: str, notebook_id: str) -> Path:
    src = FIXTURES / kind / "nb-fixture"
    dst = (store.scale_dir(notebook_id) if kind == "kg_index"
           else store.viz_dir(notebook_id))
    shutil.copytree(src, dst)
    return Path(dst)


def _write_manifest(directory: Path, payload: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(payload))


def test_v9_scale_artifact_loads_and_probes(store):
    dst = _copy_fixture(store, "kg_index", "nb-old")
    expected = json.loads((dst / "manifest.json").read_text())
    assert store.read_manifest(dst) == expected
    assert store.read_manifest_version(dst) == expected["version"]
    idx = store.load_scale("nb-old")
    assert idx is not None
    assert idx.manifest == expected
    assert list(idx.node_ids)
    assert idx.transition.shape[0] == len(idx.node_ids)


def test_v9_viz_artifact_loads(store):
    dst = _copy_fixture(store, "kg_viz", "nb-old")
    expected = json.loads((dst / "manifest.json").read_text())
    viz = store.load_viz("nb-old")
    assert viz is not None
    assert viz.manifest == expected
    assert len(viz.viz_ids) == viz.viz_adj.shape[0]


def test_manifest_probes_fail_soft_and_preserve_raises(store, tmp_path):
    missing = tmp_path / "nowhere"
    assert store.read_manifest(missing) is None
    assert store.read_manifest_version(missing) is None
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("{not json")
    assert store.read_manifest_version(corrupt) is None   # cheap probe fail-soft
    with pytest.raises(ValueError):
        store.read_manifest(corrupt)                       # full read keeps raising
    (corrupt / "manifest.json").write_text(json.dumps({"n_nodes": 5}))
    assert store.read_manifest_version(corrupt) is None    # no version field
    assert store.read_manifest(corrupt) == {"n_nodes": 5}


def test_save_full_and_load_scale_roundtrip(store):
    transition = sp.csr_matrix(np.array([[0.0, 1.0], [1.0, 0.0]]))
    manifest = store.save_full("nb-new", dict(
        node_ids=["a", "b"],
        transition=transition,
        idf=[1.0, 0.5],
        chunk_index=[1],
        ann_vectors=np.eye(2, 4, dtype=np.float32),
        ann_labels=["a", "b"],
        manifest={"version": ["v", 1], "dim": 4},
    ))
    assert manifest["version"] == ["v", 1]
    live = store.scale_dir("nb-new")
    for artifact in ("graph.npz", "node_ids.npy", "idf.npy", "chunk_index.npy",
                     "ann.bin", "ann_labels.npy", "manifest.json"):
        assert (live / artifact).exists(), artifact
    idx = store.load_scale("nb-new")
    assert list(idx.node_ids) == ["a", "b"]
    assert idx.manifest["dim"] == 4
    assert store.read_manifest_version(live) == ["v", 1]


def test_save_viz_and_load_viz_roundtrip(store):
    adjacency = sp.csr_matrix(np.array([[0, 1], [1, 0]], dtype=np.int8))
    manifest = store.save_viz("nb-viz", dict(
        viz_ids=["a", "b"],
        viz_adj=adjacency,
        viz_deg=np.asarray([1, 1], dtype=np.int32),
        viz_types=["concept", "claim"],
        viz_names=["A", "B"],
        viz_payload={"edges": [["a", "b", "relates"]]},
        manifest={"version": ["v", 2], "n_viz_nodes": 2, "n_viz_edges": 1},
    ))
    assert manifest["n_viz_nodes"] == 2
    viz = store.load_viz("nb-viz")
    assert list(viz.viz_ids) == ["a", "b"]
    assert viz.viz_edges == [["a", "b", "relates"]]
    assert viz.manifest["version"] == ["v", 2]


def test_prepare_fold_directory_resets_leftovers(store):
    live = store.scale_dir("nb1")
    _write_manifest(live, {"version": ["v", 1]})
    leftover = Path(str(live) + ".tmp")
    leftover.mkdir(parents=True)
    (leftover / "junk.bin").write_text("stale")
    temporary = store.prepare_fold_directory("nb1")
    assert Path(temporary) == leftover
    assert Path(temporary).is_dir()
    assert not (Path(temporary) / "junk.bin").exists()     # leftovers cleared
    assert store.read_manifest_version(live) == ["v", 1]   # live untouched


def test_swap_fold_directory_replaces_live_and_cleans_up(store):
    live = store.scale_dir("nb2")
    _write_manifest(live, {"version": ["v", 1]})
    (live / "ann.bin").write_text("old")
    temporary = store.prepare_fold_directory("nb2")
    _write_manifest(Path(temporary), {"version": ["v", 2]})
    (Path(temporary) / "ann.bin").write_text("new")
    store.swap_fold_directory("nb2", temporary)
    assert store.read_manifest_version(live) == ["v", 2]
    assert (live / "ann.bin").read_text() == "new"
    assert not Path(temporary).exists()
    assert not Path(str(live) + ".old").exists()


def test_unswapped_temporary_never_touches_live(store):
    live = store.scale_dir("nb3")
    _write_manifest(live, {"version": ["v", 1]})
    before = (live / "manifest.json").stat().st_mtime_ns
    temporary = store.prepare_fold_directory("nb3")
    _write_manifest(Path(temporary), {"version": ["v", 9]})
    # no swap — a fold failure before the swap leaves the old artifact intact
    assert store.read_manifest_version(live) == ["v", 1]
    assert (live / "manifest.json").stat().st_mtime_ns == before
