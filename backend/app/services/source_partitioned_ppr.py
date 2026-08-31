"""Lazy PPR over source-addressable large-graph scale companions.

This is an internal/shadow producer.  It never loads the whole scale CSR and
never filters a whole-library walk after the fact.
"""

from __future__ import annotations

import heapq
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import scipy.sparse as sp

from app.repositories.filesystem.scale_artifact_store import MANIFEST_ABSENT
from app.services.kg.edge_schema import is_queryable_edge_pair
from app.services.kg.scale_index import personalized_ppr
from app.services.kg.source_partition_index import SourcePartitionUnavailable
from app.services.retrieval import RetrievalSupport
from app.services.source_subgraph import SourceSubgraphCapability


@dataclass(frozen=True)
class SourcePartitionedPprHit:
    chunk_id: str
    source_id: str
    score: float
    support: RetrievalSupport


@dataclass(frozen=True)
class SourcePartitionedPprResult:
    capability: SourceSubgraphCapability
    hits: tuple[SourcePartitionedPprHit, ...] = ()
    cache_hit: bool = False
    node_count: int = 0
    logical_edge_count: int = 0
    iterations: int = 0


@dataclass(frozen=True)
class _CombinedGraph:
    transition: Any
    node_index: Mapping[str, int]
    chunks_by_index: Mapping[int, tuple[str, str]]
    logical_edge_count: int


def _disabled(reason: str) -> SourceSubgraphCapability:
    return SourceSubgraphCapability(False, reason)


def _version_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _companion_signature_superseded(cached_signature: Any, current_signature: Any) -> bool:
    """Mirrors ``ScaleArtifactCatalog._signature_superseded`` (W-CLI T-W3) for
    this companion cache (codex PR#643 R4 P1, corner reversed by R5 P2).

    ``current_signature is None`` — the ``artifacts`` adapter has no stat
    probe at all (old test doubles), or this one stat could not be completed —
    is "can't tell", not "changed": fail-soft, keep serving the cached entry,
    same as T-W3's ``signature is None`` early-return.

    ``MANIFEST_ABSENT`` never reaches this predicate (P1, codex PR#643 R12):
    "the companion is provably gone" is not a comparison against the cached
    entry at all, it is an eviction, and ``_graph`` acts on it before it ever
    consults the cache. It is likewise never RECORDED on an entry — an entry
    is only written on a path where the load succeeded, and this value says
    the root was not there.

    ``cached_signature is None`` is NOT given the same pass, though (codex
    #643 R5 P2). That combination is a real, reachable window here too: the
    stat this call takes right before touching the cache (see ``_graph``) can
    land exactly inside the companion's live→``.old``→live swap and read
    ``None``, while the ``load_source_partitions`` call a few lines later —
    now a moment after the rename finished — opens the genuinely new
    generation. The entry adopted from that load is valid but gets recorded
    with an unknown signature. Treating that combination as "can't tell
    either" (the old behavior) means it can NEVER be superseded again by a
    same-``parent_version`` republish, since the recorded signature stays
    ``None`` forever once written — a permanent stale-cache window until this
    process restarts. So only THIS corner reverses: a signature that IS
    readable now, matched against a cached entry recorded as unknown, counts
    as superseded — one extra reload that records a real signature, after
    which ordinary value comparison resumes.
    """
    if current_signature is None:
        return False
    return cached_signature != current_signature


class SourcePartitionedPprService:
    """Single-flight, bounded-handle runtime for selected source partitions."""

    _CACHE_ENTRIES = 2
    _MAX_RESULTS = 100

    def __init__(self, *, settings: Any, artifacts: Any, projections: Any) -> None:
        self._settings = settings
        self._artifacts = artifacts
        self._projections = projections
        # Value is (graph, companion disk signature at load time) — the
        # signature lets a cache HIT be revalidated against the live root
        # without a second cache dimension; see ``_graph``.
        self._cache: OrderedDict[tuple[str, str, str], tuple[_CombinedGraph, Any]] = (
            OrderedDict()
        )
        self._lock = threading.RLock()

    def _companion_signature(self, notebook_id: str) -> Any:
        """One stat of the companion root's ``manifest.json``. Called at most
        once per ``_graph()`` call (T-W3's "one load, one stat" discipline) and
        reused for both the hit check and the entry this call may write.

        THREE outcomes, never collapsed (P1, codex PR#643 R12):

        * a stat signature tuple — the companion generation now on disk;
        * ``MANIFEST_ABSENT`` — the root was probed and there is provably no
          live companion (a same-version ``import`` that omitted the root
          retired it, an operator deleted it). A positive fact, acted on;
        * ``None`` — nothing could be concluded: the ``artifacts`` adapter
          has no probe at all (old test doubles), or this one stat failed
          (permission, transient I/O). Fail-soft, exactly as before.
        """
        probe = getattr(self._artifacts, "manifest_stat_signature", None)
        if not callable(probe):
            return None
        signature = probe(self._artifacts.source_partition_dir(notebook_id))
        if signature is not MANIFEST_ABSENT:
            return signature
        # codex PR#643 R22 P2: one ENOENT is NOT durable absence. The
        # publication sequence is two renames — ``live → .old``, then
        # ``tmp → live`` — so a stat landing between them sees the root
        # transiently invisible on a perfectly ordinary republish. The
        # discriminator is ``.old``: during that window (and during a
        # retirement that has not reached ``finalize_swap`` yet) the previous
        # generation's manifest sits at ``{root}.old``; a DURABLE retirement
        # ends with both gone. So confirm against ``.old`` before treating
        # the absence as fact — a second stat paid only in this corner, not
        # on the hot path. A lingering interrupted-cleanup ``.old`` beside a
        # deleted live root keeps this fail-soft until the operator resolves
        # that documented recovery state (mv it back, or remove it) — the
        # conservative side of the trade, and it converges either way.
        old_probe = probe(
            str(self._artifacts.source_partition_dir(notebook_id)) + ".old"
        )
        if old_probe is not MANIFEST_ABSENT:
            return None
        # codex PR#643 R23 P2: ``.old`` absent is still not the last word —
        # between the two probes the publisher can complete ``tmp → live``
        # AND delete ``.old`` (finalize), making both reads miss a
        # generation that is now live. One recheck of the live path settles
        # it: a manifest there is the new generation's real signature
        # (superseded-vs-recorded comparison then reloads it, exactly
        # right); still absent means absent at BOTH probes of the live path
        # with no ``.old`` in between — durable retirement.
        recheck = probe(self._artifacts.source_partition_dir(notebook_id))
        if recheck is MANIFEST_ABSENT:
            return MANIFEST_ABSENT
        return recheck

    @property
    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    def invalidate(self, notebook_id: str) -> None:
        with self._lock:
            for key in [key for key in self._cache if key[0] == notebook_id]:
                self._cache.pop(key, None)

    def _combine(self, partitions: Sequence[Any]) -> _CombinedGraph:
        if not partitions:
            raise SourcePartitionUnavailable("source_partition_artifact_unavailable")
        max_nodes = (
            int(self._settings.source_subgraph_max_objects)
            + int(self._settings.source_subgraph_max_chunks)
            + int(self._settings.source_subgraph_max_cluster_memberships)
        )
        max_nnz = 2 * (
            int(self._settings.source_subgraph_max_relations)
            + int(self._settings.source_subgraph_max_memberships)
            + int(self._settings.source_subgraph_max_cluster_memberships)
        )
        if (
            sum(len(partition.node_ids) for partition in partitions) > max_nodes
            or sum(int(partition.transition.nnz) for partition in partitions) > max_nnz
        ):
            raise SourcePartitionUnavailable("source_partition_union_limit_exceeded")

        # The dominant user path (one selected paper) reuses the verified CSR
        # directly: no COO conversion, Python edge tuples or second CSR.
        if len(partitions) == 1:
            partition = partitions[0]
            node_index = {
                node_id: index for index, node_id in enumerate(partition.node_ids)
            }
            return _CombinedGraph(
                transition=partition.transition,
                node_index=node_index,
                chunks_by_index={
                    node_index[node_id]: (chunk_id, partition.source_id)
                    for node_id, chunk_id in partition.chunks_by_node.items()
                },
                logical_edge_count=int(partition.transition.nnz) // 2,
            )

        node_ids: list[str] = []
        seen_nodes: set[str] = set()
        object_types: dict[str, str] = {}
        chunks_by_node: dict[str, tuple[str, str]] = {}

        for partition in partitions:
            for node_id in partition.node_ids:
                if node_id not in seen_nodes:
                    seen_nodes.add(node_id)
                    node_ids.append(node_id)
            for object_id, object_type in partition.object_types.items():
                existing = object_types.get(object_id)
                if existing is not None and existing != object_type:
                    raise SourcePartitionUnavailable("source_partition_type_conflict")
                object_types[object_id] = object_type
            for node_id, chunk_id in partition.chunks_by_node.items():
                chunks_by_node[node_id] = (chunk_id, partition.source_id)

        if len(node_ids) > max_nodes:
            raise SourcePartitionUnavailable("source_partition_union_limit_exceeded")
        node_index = {node_id: index for index, node_id in enumerate(node_ids)}
        directed_rows: list[np.ndarray] = []
        directed_cols: list[np.ndarray] = []
        for partition in partitions:
            local_to_global = np.fromiter(
                (node_index[node_id] for node_id in partition.node_ids),
                dtype=np.int64,
                count=len(partition.node_ids),
            )
            coo = partition.transition.tocoo(copy=False)
            directed_rows.append(local_to_global[coo.row])
            directed_cols.append(local_to_global[coo.col])

        # Relation rows are owned by one selected source, but endpoints are
        # admitted only after both are authorized by the selected object union.
        cross_capacity = 2 * sum(
            len(partition.relation_endpoints) for partition in partitions
        )
        local_nnz = sum(array.size for array in directed_rows)
        if local_nnz + cross_capacity > max_nnz:
            raise SourcePartitionUnavailable("source_partition_union_limit_exceeded")
        cross_rows = np.empty(cross_capacity, dtype=np.int64)
        cross_cols = np.empty(cross_capacity, dtype=np.int64)
        cross_size = 0
        for partition in partitions:
            for left, right, edge_type in partition.relation_endpoints:
                if (
                    left in object_types
                    and right in object_types
                    and is_queryable_edge_pair(
                        edge_type, object_types[left], object_types[right]
                    )
                ):
                    left_index = node_index[left]
                    right_index = node_index[right]
                    cross_rows[cross_size : cross_size + 2] = (
                        left_index,
                        right_index,
                    )
                    cross_cols[cross_size : cross_size + 2] = (
                        right_index,
                        left_index,
                    )
                    cross_size += 2
        if cross_size:
            directed_rows.append(cross_rows[:cross_size])
            directed_cols.append(cross_cols[:cross_size])

        rows = (
            np.concatenate(directed_rows)
            if directed_rows
            else np.empty(0, dtype=np.int64)
        )
        cols = (
            np.concatenate(directed_cols)
            if directed_cols
            else np.empty(0, dtype=np.int64)
        )
        adjacency = sp.csr_matrix(
            (np.ones(rows.size, dtype=np.float32), (rows, cols)),
            shape=(len(node_ids), len(node_ids)),
        )
        adjacency.sum_duplicates()
        adjacency.data.fill(1.0)
        adjacency.eliminate_zeros()
        if adjacency.nnz > max_nnz:
            raise SourcePartitionUnavailable("source_partition_union_limit_exceeded")
        column_sums = np.asarray(adjacency.sum(axis=0)).ravel()
        column_sums[column_sums == 0] = 1.0
        transition = (adjacency @ sp.diags(1.0 / column_sums)).tocsr()
        chunks_by_index = {
            node_index[node_id]: value
            for node_id, value in chunks_by_node.items()
            if node_id in node_index
        }
        return _CombinedGraph(
            transition=transition.astype(np.float32, copy=False),
            node_index=node_index,
            chunks_by_index=chunks_by_index,
            logical_edge_count=int(adjacency.nnz) // 2,
        )

    def _graph(
        self,
        notebook_id: str,
        source_ids: tuple[str, ...],
        parent_version: Any,
    ) -> tuple[_CombinedGraph, bool]:
        signature = self._projections.source_subgraph_signature(notebook_id, source_ids)
        if (
            not isinstance(signature, tuple)
            or len(signature) < 4
            or not int(signature[2] or 0)
        ):
            raise SourcePartitionUnavailable("source_index_unavailable")
        per_source = tuple(signature[3] or ())
        if tuple(str(item[0]) for item in per_source) != source_ids:
            raise SourcePartitionUnavailable("source_scope_drift")
        source_signatures = {
            str(item[0]): (signature[0], signature[1], signature[2], (item,))
            for item in per_source
        }
        key = (
            notebook_id,
            _version_key(parent_version),
            _version_key(signature),
        )
        # codex PR#643 R4 P1 / W-CLI T-W3 mirror: ``signature`` above is
        # entirely DB-derived (kg_mutation_seq, extraction-run/backfill rows)
        # — it never changes when a CROSS-PROCESS builder (the offline CLI,
        # another replica) republishes the companion under the SAME
        # parent_version. Without this, a warmed cache entry here would keep
        # serving the retired generation until incidental LRU eviction, in
        # this process, for as long as it stays up — violating the dedicated-
        # LRU invalidation contract (docs/development.md:37; same-process
        # rebuild/fold already call ``invalidate()`` explicitly and are
        # unaffected by this). One stat of the companion root's manifest
        # before touching the cache gives that generation a comparable
        # signature.
        disk_signature = self._companion_signature(notebook_id)
        if disk_signature is MANIFEST_ABSENT:
            # P1, codex PR#643 R12: the companion root is provably GONE — a
            # same-version ``import`` whose package omitted it retired it
            # (``retire_live_directory``), or an operator removed it. Nothing
            # about this heals: the cache key above is entirely DB-derived and
            # does not move, and the probe will keep answering "absent", so
            # the fail-soft branch below would hand back the retired
            # generation's ``_CombinedGraph`` for the life of this process.
            # Drop this notebook's entries and take the "no companion" path —
            # the same ``source_partition_artifact_unavailable`` a cold read
            # of a missing root produces (``validate_partition_root``), which
            # is capability-unavailable and never authorizes whole-graph
            # post-filtering (docs/development.md:37).
            self.invalidate(notebook_id)
            raise SourcePartitionUnavailable("source_partition_artifact_unavailable")
        # Hold through lazy load/combine: concurrent cold requests for one
        # identity open one selected partition set and retain one CSR.
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                cached_graph, cached_signature = cached
                if not _companion_signature_superseded(
                    cached_signature, disk_signature
                ):
                    self._cache.move_to_end(key)
                    return cached_graph, True
                # Superseded: drop it now so a concurrent reader under this
                # same key cannot be handed the retired object while this
                # call reloads below.
                self._cache.pop(key, None)
            if len(self._cache) >= self._CACHE_ENTRIES:
                self._cache.popitem(last=False)
            partitions = self._artifacts.load_source_partitions(
                notebook_id,
                source_ids,
                expected_parent_version=parent_version,
                expected_source_signatures=source_signatures,
                max_nodes=(
                    int(self._settings.source_subgraph_max_objects)
                    + int(self._settings.source_subgraph_max_chunks)
                    + int(self._settings.source_subgraph_max_cluster_memberships)
                ),
                max_nnz=2
                * (
                    int(self._settings.source_subgraph_max_relations)
                    + int(self._settings.source_subgraph_max_memberships)
                    + int(self._settings.source_subgraph_max_cluster_memberships)
                ),
            )
            graph = self._combine(partitions)
            self._cache[key] = (graph, disk_signature)
            return graph, False

    def retrieve(
        self,
        notebook_id: str,
        source_ids: Sequence[str],
        *,
        parent_version: Any,
        object_seeds: Mapping[str, float] | None = None,
        chunk_seeds: Mapping[str, float] | None = None,
        max_results: int = 20,
    ) -> SourcePartitionedPprResult:
        if not bool(getattr(self._settings, "source_partitioned_ppr_enabled", False)):
            return SourcePartitionedPprResult(_disabled("partitioned_ppr_disabled"))
        try:
            requested = int(max_results)
        except (TypeError, ValueError):
            requested = 0
        if requested <= 0:
            return SourcePartitionedPprResult(SourceSubgraphCapability(True))
        allowed = tuple(sorted(set(str(value) for value in source_ids if value)))
        if not allowed:
            return SourcePartitionedPprResult(_disabled("empty_source_scope"))
        if len(allowed) > int(self._settings.source_subgraph_max_sources):
            return SourcePartitionedPprResult(_disabled("source_limit_exceeded"))
        try:
            graph, cache_hit = self._graph(notebook_id, allowed, parent_version)
        except SourcePartitionUnavailable as exc:
            return SourcePartitionedPprResult(_disabled(exc.reason))
        except Exception:
            return SourcePartitionedPprResult(
                _disabled("source_partition_artifact_load_failed")
            )

        reset = np.zeros(graph.transition.shape[0], dtype=np.float64)

        def weight(value: Any) -> float:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return 0.0
            return parsed if np.isfinite(parsed) and parsed > 0 else 0.0

        for object_id, value in (object_seeds or {}).items():
            index = graph.node_index.get(str(object_id))
            if index is not None:
                reset[index] += weight(value)
        passage_weight = weight(
            getattr(self._settings, "ppr_passage_node_weight", 0.05)
        )
        for chunk_id, value in (chunk_seeds or {}).items():
            index = graph.node_index.get(f"chunk:{chunk_id}")
            if index is not None:
                reset[index] += weight(value) * passage_weight
        if not np.any(reset > 0):
            return SourcePartitionedPprResult(
                capability=SourceSubgraphCapability(True),
                cache_hit=cache_hit,
                node_count=graph.transition.shape[0],
                logical_edge_count=graph.logical_edge_count,
            )

        stats: dict[str, int] = {}
        try:
            scores = personalized_ppr(
                graph.transition,
                reset,
                damping=float(getattr(self._settings, "ppr_damping", 0.5)),
                tol=float(getattr(self._settings, "ppr_tol", 1e-6)),
                max_iter=int(self._settings.source_partitioned_ppr_max_iterations),
                stats=stats,
            )
        except Exception:
            return SourcePartitionedPprResult(_disabled("partitioned_ppr_run_failed"))
        chunk_items = tuple(graph.chunks_by_index.items())
        chunk_indexes = np.fromiter(
            (item[0] for item in chunk_items), dtype=np.int64, count=len(chunk_items)
        )
        values = scores[chunk_indexes] if chunk_indexes.size else np.empty(0)
        low = float(values.min()) if values.size else 0.0
        high = float(values.max()) if values.size else 0.0
        span = high - low
        normalized = (values - low) / span if span > 0 else np.zeros_like(values)
        result_count = min(requested, self._MAX_RESULTS, len(chunk_items))
        if result_count and len(chunk_items) > result_count:
            provisional = np.argpartition(-normalized, result_count - 1)[:result_count]
            threshold = float(normalized[provisional].min())
            above = np.flatnonzero(normalized > threshold)
            remaining = result_count - int(above.size)
            ties = np.flatnonzero(normalized == threshold)
            selected_ties = heapq.nsmallest(
                remaining,
                (int(index) for index in ties),
                key=lambda index: chunk_items[index][1][0],
            )
            selected = np.concatenate(
                (above, np.asarray(selected_ties, dtype=np.int64))
            )
        else:
            selected = np.arange(len(chunk_items))
        ranked = sorted(
            (
                chunk_items[int(index)][1][0],
                chunk_items[int(index)][1][1],
                float(normalized[int(index)]),
            )
            for index in selected
        )
        ranked.sort(key=lambda row: (-row[2], row[0]))
        hits = tuple(
            SourcePartitionedPprHit(
                chunk_id=chunk_id,
                source_id=source_id,
                score=score,
                support=RetrievalSupport(origin="ppr", support_kind="ppr", score=score),
            )
            for chunk_id, source_id, score in ranked[:result_count]
        )
        return SourcePartitionedPprResult(
            capability=SourceSubgraphCapability(True),
            hits=hits,
            cache_hit=cache_hit,
            node_count=graph.transition.shape[0],
            logical_edge_count=graph.logical_edge_count,
            iterations=int(stats.get("iters", 0)),
        )
