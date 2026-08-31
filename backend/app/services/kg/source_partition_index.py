"""Source-addressable CSR companions for large selected-source PPR.

The companion root is bound to one published scale-index identity.  Every
source lives below a deterministic hashed directory, so a narrowed request can
open exactly the selected partitions without reading a notebook-wide source
map.  Missing, legacy or identity-mismatched artifacts fail closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import scipy.sparse as sp

from app.services.kg.edge_schema import is_queryable_edge_pair
from app.services.kg.scale_index import build_transition
from app.services.source_subgraph import GRAPH_CONTRACT_VERSION

# Sunk to app.domain.kg.source_partition in B3 (app.repositories'
# maintenance modules import it directly there); re-exported here
# unchanged for this module's own use and existing importers such as
# filesystem/scale_artifact_store.py. ``build_generation_mismatch`` lives
# there for the same reason (P1, codex PR#643 R26) — the store, both
# maintenance backends and the offline CLI all apply the same gate.
from app.domain.kg.source_partition import (  # noqa: F401
    SOURCE_PARTITION_FORMAT_VERSION,
    build_generation_mismatch,
)

_PARTITION_FILES = (
    "graph.npz",
    "node_ids.npy",
    "chunk_nodes.npy",
    "object_types.npy",
    "relation_endpoints.npy",
)


class SourcePartitionUnavailable(RuntimeError):
    """A capability failure, never permission to use the whole graph."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SourceGraphPartition:
    source_id: str
    parent_version: Any
    source_signature: Any
    source_generation: str
    projection_version: int
    node_ids: tuple[str, ...]
    transition: Any
    object_ids: frozenset[str]
    object_types: Mapping[str, str]
    chunks_by_node: Mapping[str, str]
    relation_endpoints: tuple[tuple[str, str, str], ...]
    # The ``build_id`` of the main manifest this partition was produced
    # beside (P1, codex PR#643 R26). Last and defaulted so a caller that
    # predates the key — a test double, an older direct user — keeps
    # constructing this the way it always did; ``None`` persists as "no id"
    # and pairs on ``parent_version`` alone.
    parent_build_id: Any = None


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _identity(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source_partition_key(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_source_partition(
    raw: Mapping[str, Any],
    *,
    source_id: str,
    parent_version: Any,
    parent_build_id: Any = None,
    max_memberships: int = 60_000,
) -> SourceGraphPartition:
    """Normalize one source-only offline projection into a safe CSR."""
    reasons = tuple(str(item) for item in raw.get("reasons") or ())
    if reasons:
        raise SourcePartitionUnavailable(reasons[0])
    state = raw.get("source")
    if not isinstance(state, Mapping) or str(state.get("source_id") or "") != source_id:
        raise SourcePartitionUnavailable("source_scope_drift")
    generation = str(state.get("generation") or "")
    projection_version = int(state.get("projection_version") or 0)
    if not generation or projection_version != GRAPH_CONTRACT_VERSION:
        raise SourcePartitionUnavailable("source_projection_unavailable")
    if str(state.get("projection_status") or "") == "incomplete":
        raise SourcePartitionUnavailable("source_projection_incomplete")

    objects = {
        str(row["object_id"]): str(row.get("object_type") or "")
        for row in raw.get("objects") or ()
    }
    fact_types: dict[str, set[str]] = {}
    fact_objects: dict[str, str] = {}
    facts = list(raw.get("facts") or ())
    expected = int(state.get("expected_objects") or 0)
    if len(facts) != expected:
        raise SourcePartitionUnavailable("source_projection_incomplete")
    for row in facts:
        if int(row.get("projection_version") or 0) != GRAPH_CONTRACT_VERSION:
            raise SourcePartitionUnavailable("fact_projection_version_unsupported")
        fact_id = str(row["fact_id"])
        object_id = str(row["object_id"])
        if object_id not in objects:
            # Source facts intentionally outlive fused global objects.  A fact
            # whose current global row is deprecated/not source-indexed remains
            # part of completeness accounting but cannot enter this graph.
            continue
        object_type = str(row.get("object_type") or "")
        fact_objects[fact_id] = object_id
        fact_types.setdefault(object_id, set()).add(object_type)
    for object_id in objects:
        types = {value for value in fact_types.get(object_id, ()) if value}
        if len(types) != 1:
            raise SourcePartitionUnavailable(
                "fact_object_missing" if not types else "fact_type_conflict"
            )
        objects[object_id] = next(iter(types))

    element_objects: dict[str, set[str]] = {}
    for row in raw.get("fact_elements") or ():
        object_id = fact_objects.get(str(row["fact_id"]))
        element_id = str(row.get("element_id") or "")
        if object_id in objects and element_id:
            element_objects.setdefault(element_id, set()).add(object_id)

    chunks: dict[str, tuple[str, ...]] = {}
    for row in raw.get("chunks") or ():
        chunk_id = str(row["chunk_id"])
        chunks[chunk_id] = tuple(
            dict.fromkeys(
                str(value) for value in _json_list(row.get("element_ids")) if value
            )
        )

    memberships: set[tuple[str, str]] = set()
    for chunk_id, element_ids in chunks.items():
        for element_id in element_ids:
            for object_id in element_objects.get(element_id, ()):
                memberships.add((object_id, f"chunk:{chunk_id}"))
                if len(memberships) > max(1, int(max_memberships)):
                    raise SourcePartitionUnavailable("membership_limit_exceeded")

    relation_endpoints: list[tuple[str, str, str]] = []
    local_relations: list[tuple[str, str]] = []
    for row in raw.get("relations") or ():
        evidence = _json_list(row.get("evidence"))
        if not evidence or not all(
            isinstance(item, dict) and str(item.get("source_id") or "") == source_id
            for item in evidence
        ):
            continue
        left = str(row.get("source_object_id") or "")
        right = str(row.get("target_object_id") or "")
        edge_type = str(row.get("edge_type") or "")
        left_type = objects.get(left)
        right_type = objects.get(right)
        # Cross-partition edges cannot be type-validated until the selected
        # union is loaded.  Store endpoints only; combine() revalidates both
        # authorization and the endpoint type registry with union metadata.
        if left_type and right_type:
            if is_queryable_edge_pair(edge_type, left_type, right_type):
                local_relations.append((left, right))
        else:
            relation_endpoints.append((left, right, edge_type))

    cluster_groups: dict[str, list[str]] = {}
    for row in raw.get("clusters") or ():
        object_id = str(row.get("member_object_id") or "")
        if object_id in objects:
            cluster_groups.setdefault(str(row["canonical_id"]), []).append(object_id)

    node_ids = [*objects]
    chunks_by_node = {f"chunk:{chunk_id}": chunk_id for chunk_id in chunks}
    node_ids.extend(chunks_by_node)
    for canonical_id in cluster_groups:
        node_ids.append(f"cluster:{canonical_id}")

    pairs: set[tuple[str, str]] = set()

    def add(left: str, right: str) -> None:
        if left and right and left != right:
            pairs.add((left, right) if left < right else (right, left))

    for left, right in local_relations:
        add(left, right)
    for left, right in memberships:
        add(left, right)
    for canonical_id, members in cluster_groups.items():
        hub = f"cluster:{canonical_id}"
        for object_id in dict.fromkeys(members):
            add(hub, object_id)
    edges = [(a, b, 1.0) for a, b in pairs] + [(b, a, 1.0) for a, b in pairs]
    transition, _ = build_transition(node_ids, edges)
    # Persist object types beside ids so a selected multi-source union can
    # validate cross-partition relation endpoint pairs.
    object_ids = frozenset(objects)
    partition = SourceGraphPartition(
        source_id=source_id,
        parent_version=parent_version,
        source_signature=raw.get("signature"),
        source_generation=generation,
        projection_version=projection_version,
        node_ids=tuple(node_ids),
        transition=transition.astype(np.float32, copy=False),
        object_ids=object_ids,
        object_types=objects,
        chunks_by_node=chunks_by_node,
        relation_endpoints=tuple(
            (left, right, edge_type) for left, right, edge_type in relation_endpoints
        ),
        parent_build_id=parent_build_id,
    )
    return partition


def save_source_partition(directory: Path, partition: SourceGraphPartition) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    sp.save_npz(directory / "graph.npz", partition.transition)
    np.save(directory / "node_ids.npy", np.asarray(partition.node_ids, dtype=object))
    np.save(
        directory / "chunk_nodes.npy",
        np.asarray(list(partition.chunks_by_node.items()), dtype=object),
    )
    np.save(
        directory / "object_types.npy",
        np.asarray(list(partition.object_types.items()), dtype=object),
    )
    np.save(
        directory / "relation_endpoints.npy",
        np.asarray(partition.relation_endpoints, dtype=object).reshape((-1, 3)),
    )
    file_sha256 = {name: _file_sha256(directory / name) for name in _PARTITION_FILES}
    manifest = {
        "format_version": SOURCE_PARTITION_FORMAT_VERSION,
        "source_id": partition.source_id,
        "parent_version": partition.parent_version,
        # Written even when it is ``None`` (P1, codex PR#643 R26): a null and
        # an absent key mean the same thing to ``build_generation_mismatch``
        # — "this side carries no id" — so there is nothing to gain from
        # omitting it, and a present key makes the generation binding
        # visible to an operator reading the manifest.
        "parent_build_id": partition.parent_build_id,
        "source_signature": partition.source_signature,
        "source_generation": partition.source_generation,
        "projection_version": partition.projection_version,
        "n_nodes": len(partition.node_ids),
        "n_chunks": len(partition.chunks_by_node),
        "n_objects": len(partition.object_ids),
        "n_relations": len(partition.relation_endpoints),
        "transition_nnz": int(partition.transition.nnz),
        "file_sha256": file_sha256,
    }
    with open(directory / "manifest.json", "w") as handle:
        json.dump(manifest, handle, ensure_ascii=False)


def load_source_partition(
    root: Path,
    *,
    source_id: str,
    expected_parent_version: Any,
    expected_source_signature: Any,
    expected_parent_build_id: Any = None,
) -> SourceGraphPartition:
    directory, manifest = inspect_source_partition_manifest(
        root,
        source_id=source_id,
        expected_parent_version=expected_parent_version,
        expected_source_signature=expected_source_signature,
        expected_parent_build_id=expected_parent_build_id,
    )
    try:
        hashes = manifest.get("file_sha256")
        if not isinstance(hashes, dict) or set(hashes) != set(_PARTITION_FILES):
            raise SourcePartitionUnavailable("source_partition_artifact_corrupt")
        for name in _PARTITION_FILES:
            expected_hash = hashes.get(name)
            if (
                not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or not hmac.compare_digest(
                    expected_hash, _file_sha256(directory / name)
                )
            ):
                raise SourcePartitionUnavailable("source_partition_artifact_corrupt")
        node_ids = tuple(
            str(value)
            for value in np.load(directory / "node_ids.npy", allow_pickle=True)
        )
        transition = sp.load_npz(directory / "graph.npz")
        chunks = {
            str(node): str(chunk)
            for node, chunk in np.load(
                directory / "chunk_nodes.npy", allow_pickle=True
            ).tolist()
        }
        object_types = {
            str(object_id): str(object_type)
            for object_id, object_type in np.load(
                directory / "object_types.npy", allow_pickle=True
            ).tolist()
        }
        relations = tuple(
            tuple(str(value) for value in row)
            for row in np.load(
                directory / "relation_endpoints.npy", allow_pickle=True
            ).tolist()
        )
    except SourcePartitionUnavailable:
        raise
    except FileNotFoundError as exc:
        raise SourcePartitionUnavailable(
            "source_partition_artifact_unavailable"
        ) from exc
    except Exception as exc:
        raise SourcePartitionUnavailable("source_partition_artifact_corrupt") from exc
    counts = {
        "n_nodes": len(node_ids),
        "n_chunks": len(chunks),
        "n_objects": len(object_types),
        "n_relations": len(relations),
        "transition_nnz": int(transition.nnz),
    }
    if any(
        isinstance(manifest.get(key), bool)
        or not isinstance(manifest.get(key), int)
        or manifest.get(key) != actual
        for key, actual in counts.items()
    ):
        raise SourcePartitionUnavailable("source_partition_artifact_corrupt")
    if (
        transition.shape != (len(node_ids), len(node_ids))
        or not sp.isspmatrix_csr(transition)
        or len(set(node_ids)) != len(node_ids)
        or not str(manifest.get("source_generation") or "")
        or int(manifest.get("projection_version") or 0) != GRAPH_CONTRACT_VERSION
    ):
        raise SourcePartitionUnavailable("source_partition_artifact_corrupt")
    node_set = set(node_ids)
    if (
        not set(chunks).issubset(node_set)
        or not set(object_types).issubset(node_set)
        or set(chunks) & set(object_types)
        or any(node != f"chunk:{chunk}" for node, chunk in chunks.items())
        or any(node.startswith(("chunk:", "cluster:")) for node in object_types)
        or transition.indices.size
        and (
            int(transition.indices.min()) < 0
            or int(transition.indices.max()) >= len(node_ids)
        )
        or not np.all(np.isfinite(transition.data))
        or np.any(transition.data < 0)
    ):
        raise SourcePartitionUnavailable("source_partition_artifact_corrupt")
    column_sums = np.asarray(transition.sum(axis=0)).ravel()
    nonzero_columns = column_sums > 0
    if np.any(~np.isclose(column_sums[nonzero_columns], 1.0, rtol=1e-5, atol=1e-6)):
        raise SourcePartitionUnavailable("source_partition_artifact_corrupt")
    partition = SourceGraphPartition(
        source_id=source_id,
        parent_version=manifest.get("parent_version"),
        parent_build_id=manifest.get("parent_build_id"),
        source_signature=manifest.get("source_signature"),
        source_generation=str(manifest.get("source_generation") or ""),
        projection_version=int(manifest.get("projection_version") or 0),
        node_ids=node_ids,
        transition=transition,
        object_ids=frozenset(object_types),
        object_types=object_types,
        chunks_by_node=chunks,
        relation_endpoints=relations,
    )
    return partition


def inspect_source_partition_manifest(
    root: Path,
    *,
    source_id: str,
    expected_parent_version: Any,
    expected_source_signature: Any,
    expected_parent_build_id: Any = None,
) -> tuple[Path, dict[str, Any]]:
    """Validate one small header without opening any partition payload."""
    directory = root / source_partition_key(source_id)
    try:
        with open(directory / "manifest.json") as handle:
            manifest = json.load(handle)
    except FileNotFoundError as exc:
        raise SourcePartitionUnavailable(
            "source_partition_artifact_unavailable"
        ) from exc
    except Exception as exc:
        raise SourcePartitionUnavailable("source_partition_artifact_corrupt") from exc
    if not isinstance(manifest, dict):
        raise SourcePartitionUnavailable("source_partition_artifact_corrupt")
    if manifest.get("format_version") != SOURCE_PARTITION_FORMAT_VERSION:
        raise SourcePartitionUnavailable("source_partition_artifact_legacy")
    if str(manifest.get("source_id") or "") != source_id:
        raise SourcePartitionUnavailable("source_partition_identity_mismatch")
    if _identity(manifest.get("parent_version")) != _identity(expected_parent_version):
        raise SourcePartitionUnavailable("source_partition_identity_mismatch")
    # The version above is reusable across republishes; the build id is not
    # (P1, codex PR#643 R26). Same existing exit — a mismatched pair is
    # capability-unavailable, never a new error shape.
    if build_generation_mismatch(
        expected_parent_build_id, manifest.get("parent_build_id")
    ):
        raise SourcePartitionUnavailable("source_partition_identity_mismatch")
    if _identity(manifest.get("source_signature")) != _identity(
        expected_source_signature
    ):
        raise SourcePartitionUnavailable("source_partition_identity_mismatch")
    for key in ("n_nodes", "n_chunks", "n_objects", "n_relations", "transition_nnz"):
        value = manifest.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SourcePartitionUnavailable("source_partition_artifact_corrupt")
    return directory, manifest


def validate_partition_root(
    root: Path,
    expected_parent_version: Any,
    expected_parent_build_id: Any = None,
) -> None:
    try:
        with open(root / "manifest.json") as handle:
            manifest = json.load(handle)
    except FileNotFoundError as exc:
        raise SourcePartitionUnavailable(
            "source_partition_artifact_unavailable"
        ) from exc
    except Exception as exc:
        raise SourcePartitionUnavailable("source_partition_artifact_corrupt") from exc
    if manifest.get("format_version") != SOURCE_PARTITION_FORMAT_VERSION:
        raise SourcePartitionUnavailable("source_partition_artifact_legacy")
    if _identity(manifest.get("parent_version")) != _identity(expected_parent_version):
        raise SourcePartitionUnavailable("source_partition_identity_mismatch")
    # Same generation gate as the per-source headers above, applied to the
    # companion ROOT so an unpaired companion is refused before any selected
    # header is opened (P1, codex PR#643 R26).
    if build_generation_mismatch(
        expected_parent_build_id, manifest.get("parent_build_id")
    ):
        raise SourcePartitionUnavailable("source_partition_identity_mismatch")
