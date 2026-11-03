"""Online diagnostics must inspect metadata without materializing an index."""
import json
import struct

import hnswlib
import numpy as np
import pytest
import scipy.sparse as sp

from app.services.checkup import probe_scale_index_integrity
from app.services.kg import scale_index


@pytest.fixture
def artifact(tmp_path):
    scale_index.save_scale_index(
        str(tmp_path), node_ids=["a", "b", "c"],
        transition=sp.eye(3, format="csr"), idf=[1, 1, 1], chunk_index=[],
        ann_labels=["a", "b", "c"], ann_vectors=np.eye(3, 4, dtype="float32"),
        manifest={"version": [1], "dim": 4, "n_nodes": 3, "n_ann": 3, "n_chunks": 0},
    )
    return tmp_path


def test_online_probe_never_loads_arrays_or_ann(artifact, monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("online probe materialized payload")

    monkeypatch.setattr(scale_index, "load_scale_index", forbidden)
    monkeypatch.setattr(hnswlib, "Index", forbidden)
    monkeypatch.setattr(np, "load", forbidden)
    monkeypatch.setattr(sp, "load_npz", forbidden)
    assert probe_scale_index_integrity(artifact) == 0
    assert calls == []


@pytest.mark.parametrize("damage", ["missing_ann", "truncated_ann", "ann_count", "array_count", "array_payload"])
def test_online_probe_detects_metadata_damage(artifact, damage):
    if damage == "missing_ann":
        (artifact / "ann.bin").unlink()
    elif damage == "truncated_ann":
        with (artifact / "ann.bin").open("r+b") as handle:
            handle.truncate(8)
    elif damage == "ann_count":
        other = hnswlib.Index(space="cosine", dim=4)
        other.init_index(max_elements=2)
        other.add_items(np.eye(2, 4, dtype="float32"), [0, 1])
        other.save_index(str(artifact / "ann.bin"))
    elif damage == "array_count":
        np.save(artifact / "idf.npy", np.ones(2, dtype="float32"))
    else:
        with (artifact / "idf.npy").open("r+b") as handle:
            handle.truncate(handle.seek(0, 2) - 1)
    assert probe_scale_index_integrity(artifact) == 1


def test_ann_probe_cost_does_not_depend_on_declared_capacity(artifact, monkeypatch):
    # A sparse file emulates a large artifact without allocating its payload.
    from app.services.kg.artifact_probe import inspect_scale_metadata

    ann_path = artifact / "ann.bin"
    with ann_path.open("r+b") as handle:
        handle.seek(struct.calcsize("=Q"))
        handle.write(struct.pack("=Q", 1_000_000_000))
        handle.truncate(32 * 1024 ** 3)
    monkeypatch.setattr(hnswlib, "Index", lambda **kw: pytest.fail("ANN allocated"))
    assert inspect_scale_metadata(artifact) is not None


def test_explicit_deep_probe_remains_available(artifact):
    assert probe_scale_index_integrity(artifact, deep=True) == 0
    with (artifact / "ann.bin").open("ab") as handle:
        handle.write(b"unexpected trailing payload")
    assert probe_scale_index_integrity(artifact, deep=True) == 1


def test_malformed_manifest_is_damage(artifact):
    (artifact / "manifest.json").write_text(json.dumps([]))
    assert probe_scale_index_integrity(artifact) == 1


def test_payload_changes_invalidate_identity_without_manifest_changes(artifact):
    from types import SimpleNamespace

    from app.repositories.filesystem.scale_artifact_store import ScaleArtifactStore
    from app.services.kg.scale_artifact_codec import KG_SCALE_ARTIFACT_CODEC

    store = ScaleArtifactStore(SimpleNamespace(storage_dir=str(artifact.parent)), codec=KG_SCALE_ARTIFACT_CODEC)
    # Exercise the real filesystem identity independently of the path layout.
    store.scale_dir = lambda _nb: artifact
    before = store.scale_integrity_identity("nb")
    with (artifact / "ann.bin").open("r+b") as handle:
        handle.truncate(8)
    assert before != store.scale_integrity_identity("nb")


@pytest.mark.parametrize("degree_dtype", ["float32", "int32"])
def test_viz_metadata_preserves_degree_dtype_guard(tmp_path, degree_dtype):
    from app.services.kg.artifact_probe import inspect_viz_metadata

    (tmp_path / "manifest.json").write_text(json.dumps({"n_viz_nodes": 1, "n_viz_edges": 0}))
    np.savez(tmp_path / "viz.npz", viz_ids=np.array(["a"], dtype=object),
             viz_names=np.array(["a"], dtype=object), viz_types=np.array(["concept"], dtype=object),
             viz_deg=np.array([0], dtype=degree_dtype))
    sp.save_npz(tmp_path / "viz_adj.npz", sp.eye(1, format="csr"))
    assert (inspect_viz_metadata(tmp_path) is not None) == (degree_dtype == "int32")


@pytest.mark.parametrize("kind", ["scale", "viz"])
def test_unsupported_zip_compression_is_reported_as_damage(artifact, kind):
    from app.services.kg.artifact_probe import inspect_scale_metadata, inspect_viz_metadata

    if kind == "viz":
        np.savez(artifact / "viz.npz", viz_ids=np.array(["a"], dtype=object))
        path = artifact / "viz.npz"
        inspect = inspect_viz_metadata
    else:
        path = artifact / "graph.npz"
        inspect = inspect_scale_metadata
    payload = bytearray(path.read_bytes())
    local = payload.index(b"PK\x03\x04")
    central = payload.index(b"PK\x01\x02")
    struct.pack_into("<H", payload, local + 8, 99)
    struct.pack_into("<H", payload, central + 10, 99)
    path.write_bytes(payload)
    assert inspect(artifact) is None


@pytest.mark.parametrize("codes_dtype,counts_dtype,valid", [
    ("int32", "int64", True), ("float32", "int64", False),
    ("uint32", "int64", False), ("int32", "float32", False),
])
def test_chunk_source_sidecar_header_dtypes(artifact, codes_dtype, counts_dtype, valid):
    from app.services.kg.artifact_probe import inspect_scale_metadata

    path = artifact / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest.update(has_chunk_ann=True, has_chunk_ann_sources=True, n_chunk_ann=1)
    path.write_text(json.dumps(manifest))
    np.save(artifact / "chunk_ann_labels.npy", np.array(["c"], dtype=object))
    np.save(artifact / "chunk_ann_source_names.npy", np.array(["s"], dtype=object))
    np.save(artifact / "chunk_ann_source_codes.npy", np.array([0], dtype=codes_dtype))
    np.save(artifact / "chunk_ann_source_counts.npy", np.array([1], dtype=counts_dtype))
    assert (inspect_scale_metadata(artifact) is not None) is valid


@pytest.mark.parametrize("kind", ["scale", "viz"])
@pytest.mark.parametrize("format_value", [None, b"xyz", [b"csr"]])
def test_sparse_metadata_requires_a_readable_format(artifact, kind, format_value):
    from app.services.kg.artifact_probe import inspect_scale_metadata, inspect_viz_metadata

    if kind == "viz":
        np.savez(artifact / "viz.npz", viz_ids=np.array(["a"], dtype=object),
                 viz_names=np.array(["a"], dtype=object), viz_types=np.array(["concept"], dtype=object),
                 viz_deg=np.array([0], dtype="int32"))
        (artifact / "manifest.json").write_text(json.dumps({"n_viz_nodes": 1, "n_viz_edges": 0}))
        sp.save_npz(artifact / "viz_adj.npz", sp.eye(1, format="csr"))
        path, inspect = artifact / "viz_adj.npz", inspect_viz_metadata
    else:
        path, inspect = artifact / "graph.npz", inspect_scale_metadata
    with np.load(path) as archive:
        arrays = {key: archive[key] for key in archive.files if key != "format"}
    if format_value is not None:
        arrays["format"] = np.array(format_value)
    np.savez(path, **arrays)
    assert inspect(artifact) is None
