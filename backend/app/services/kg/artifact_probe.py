"""Bounded metadata inspection for online index diagnostics.

Never unpickles object arrays, decompresses graph data, or opens an ANN handle.
These checks establish file/layout/count consistency, not full payload integrity.
The native HNSW layout is the pinned hnswlib 0.8.0 saveIndex format:
https://github.com/nmslib/hnswlib/blob/v0.8.0/hnswlib/hnswalg.h
"""
from __future__ import annotations

import json
import math
import os
import struct
import zipfile
from pathlib import Path

import numpy as np
from numpy.lib import format as npy_format

_SIZE_T = "Q" if struct.calcsize("P") == struct.calcsize("Q") else "I"
_HNSW_HEADER = struct.Struct("=" + _SIZE_T * 6 + "iI" + _SIZE_T * 3 + "d" + _SIZE_T)
_LINK_SIZE = struct.calcsize("=I")
_LABEL_SIZE = struct.calcsize("=" + _SIZE_T)
_FLOAT_SIZE = np.dtype("float32").itemsize
_AUXILIARY_ARRAYS = frozenset({"viz_deg_order", "viz_edge_src_order", "viz_edge_src_indptr"})
_INTEGER_ARRAYS = frozenset({"viz_deg", "viz_edge_src", "viz_edge_dst", "viz_edge_code", "indices", "indptr", "shape"})


def _array_header(handle, size):
    version = npy_format.read_magic(handle)
    if version == (1, 0):
        shape, _fortran, dtype = npy_format.read_array_header_1_0(handle)
    elif version == (2, 0):
        shape, _fortran, dtype = npy_format.read_array_header_2_0(handle)
    else:
        raise ValueError("unsupported array header version")
    payload_size = size - handle.tell()
    if payload_size < 0 or (dtype.hasobject and payload_size == 0):
        raise ValueError("missing array payload")
    if not dtype.hasobject and payload_size != math.prod(shape) * dtype.itemsize:
        raise ValueError("array payload size disagrees with header")
    return shape, dtype


def _npy_shape(path, *, dtype_kinds=None):
    with path.open("rb") as handle:
        shape, dtype = _array_header(handle, os.fstat(handle.fileno()).st_size)
    if dtype_kinds is not None and dtype.kind not in dtype_kinds:
        raise ValueError("array dtype disagrees with its role")
    return shape


def _zip_shapes(path):
    with zipfile.ZipFile(path) as archive:
        shapes = {}
        for info in archive.infolist():
            if info.filename.endswith(".npy"):
                key = info.filename.removesuffix(".npy")
                if key in _AUXILIARY_ARRAYS:
                    continue  # Invalid auxiliaries are rebuilt by the serving loader.
                with archive.open(info) as handle:
                    shape, dtype = _array_header(handle, info.file_size)
                    if key in _INTEGER_ARRAYS and dtype.kind not in "iu":
                        raise ValueError("non-integral index array")
                    shapes[key] = shape
    return shapes


def _sparse_shape(path):
    shapes = _zip_shapes(path)
    if shapes.get("shape") != (2,):
        raise ValueError("matrix shape is not a pair")
    with zipfile.ZipFile(path) as archive:
        with archive.open("format.npy") as handle:
            shape, dtype = _array_header(handle, archive.getinfo("format.npy").file_size)
            if shape != () or dtype.kind != "S" or dtype.itemsize != len(b"csr"):
                raise ValueError("invalid sparse format header")
            handle.seek(0)
            if npy_format.read_array(handle, allow_pickle=False).item() not in (b"csr", b"csc"):
                raise ValueError("unsupported sparse format")
        with archive.open("shape.npy") as handle:
            _shape, dtype = _array_header(handle, archive.getinfo("shape.npy").file_size)
            if dtype.kind not in "iu":
                raise ValueError("matrix shape is not integer")
            handle.seek(0)
            rows, columns = map(int, npy_format.read_array(handle, allow_pickle=False))
    if rows < 0 or columns < 0:
        raise ValueError("negative matrix shape")
    if shapes.get("data") != shapes.get("indices") or len(shapes.get("data", ())) != 1:
        raise ValueError("sparse arrays disagree")
    if shapes.get("indptr") != (rows + 1,):
        raise ValueError("sparse row pointers disagree")
    return rows, columns


def _count(manifest, key, actual):
    expected = manifest.get(key)
    if isinstance(expected, int) and not isinstance(expected, bool) and expected != actual:
        raise ValueError("manifest count disagrees with array")


def _rows(shape):
    if len(shape) != 1:
        raise ValueError("expected one-dimensional array")
    return shape[0]


def _viz_metadata(directory, manifest):
    shapes = _zip_shapes(directory / "viz.npz")
    nodes = _rows(shapes["viz_ids"])
    for key in ("viz_deg", "viz_types", "viz_names"):
        if shapes[key] != (nodes,):
            raise ValueError("viz node arrays disagree")
    if _sparse_shape(directory / "viz_adj.npz") != (nodes, nodes):
        raise ValueError("viz adjacency disagrees")
    _count(manifest, "n_viz_nodes", nodes)
    compact = {"viz_edge_src", "viz_edge_dst", "viz_edge_code", "viz_edge_types"}
    if compact.intersection(shapes):
        edges = _rows(shapes["viz_edge_src"])
        if shapes["viz_edge_dst"] != (edges,) or shapes["viz_edge_code"] != (edges,):
            raise ValueError("viz edge arrays disagree")
        _rows(shapes["viz_edge_types"])
        _count(manifest, "n_viz_edges", edges)
    elif "viz_edges" in shapes:
        # Legacy JSON payload cannot be counted without decoding all edges.
        edges = 0
    else:
        edges = 0
        _count(manifest, "n_viz_edges", edges)
    return {**manifest, "n_viz_nodes": nodes, "n_viz_edges": manifest.get("n_viz_edges", edges)}


def _read_manifest(directory):
    with (directory / "manifest.json").open() as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    return manifest


def inspect_viz_metadata(directory):
    """Return a readable viz manifest with header-derived counts, or None."""
    try:
        directory = Path(directory)
        return _viz_metadata(directory, _read_manifest(directory))
    except Exception:  # noqa: BLE001 — any unreadable artifact is unavailable
        return None


def ann_metadata_valid(path, rows, dim):
    """Read only HNSW's fixed header and stat, never its vector/link payload."""
    with Path(path).open("rb") as handle:
        raw = handle.read(_HNSW_HEADER.size)
        if len(raw) != _HNSW_HEADER.size:
            return False
        (offset, capacity, count, stride, label_offset, data_offset,
         level, entry, max_m, max_m0, m, multiplier, _ef) = _HNSW_HEADER.unpack(raw)
        minimum = _HNSW_HEADER.size + count * (stride + _LINK_SIZE)
        return (
            count == rows and capacity >= count and offset == 0
            and stride == label_offset + _LABEL_SIZE
            and (dim <= 0 or label_offset == data_offset + dim * _FLOAT_SIZE)
            and data_offset == max_m0 * _LINK_SIZE + _LINK_SIZE
            and max_m == m and max_m0 == 2 * m and m > 0
            and math.isfinite(multiplier) and multiplier > 0
            and (count == 0 or (level >= 0 and entry < count))
            and os.fstat(handle.fileno()).st_size >= minimum
        )


def inspect_scale_metadata(directory):
    """Inspect scale array headers/counts without materializing any payload."""
    try:
        directory = Path(directory)
        manifest = _read_manifest(directory)
        nodes = _rows(_npy_shape(directory / "node_ids.npy"))
        labels = _rows(_npy_shape(directory / "ann_labels.npy"))
        # Fold can retain an older chunk-index prefix after source deletion;
        # its length is not the manifest's current DB chunk count.
        _rows(_npy_shape(directory / "chunk_index.npy"))
        if _npy_shape(directory / "idf.npy") != (nodes,):
            return None
        if _sparse_shape(directory / "graph.npz") != (nodes, nodes):
            return None
        for key, actual in (("n_nodes", nodes), ("n_ann", labels)):
            _count(manifest, key, actual)
        if labels and not ann_metadata_valid(directory / "ann.bin", labels, int(manifest.get("dim") or 0)):
            return None
        for kind in ("chunk", "relation"):
            path = directory / f"{kind}_ann_labels.npy"
            if manifest.get(f"has_{kind}_ann") and path.exists():
                _count(manifest, f"n_{kind}_ann", _rows(_npy_shape(path)))
        if manifest.get("has_chunk_ann_sources"):
            names = _rows(_npy_shape(directory / "chunk_ann_source_names.npy"))
            codes = _rows(_npy_shape(directory / "chunk_ann_source_codes.npy", dtype_kinds="i"))
            counts = _rows(_npy_shape(directory / "chunk_ann_source_counts.npy", dtype_kinds="iu"))
            if codes != _rows(_npy_shape(directory / "chunk_ann_labels.npy")) or counts != names:
                return None
        if manifest.get("has_viz"):
            if (directory / "viz.npz").exists() and (directory / "viz_adj.npz").exists():
                manifest = _viz_metadata(directory, manifest)
            else:
                manifest = {**manifest, "has_viz": False}
        return manifest
    except Exception:  # noqa: BLE001 — includes ZIP codecs and malformed headers
        return None
