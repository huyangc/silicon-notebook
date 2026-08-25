"""Scale-index full-build, delta-fold, and viz-build orchestration.

The builder composes the runtime-owned projection and artifact adapters.  It
deliberately receives late-bound callbacks for the compatibility seams that
still live on ``SQLiteRepository``; it never retains the facade itself.
"""
from __future__ import annotations

import gc
import time
from typing import Any, Callable, Optional, Sequence

import numpy as np

from app.services.kg import scale_index as scale_index_module
from app.services.kg import viz_index as viz_index_module
from app.services.vector_index import resolve_runtime_dim


def _warn_if_scale_index_uses_full_width(
    settings,
    logger,
    notebook_id: str,
) -> None:
    """Emit the native-width warning without requiring an index build."""
    embed_dim = int(settings.embed_dim)
    runtime_dim = resolve_runtime_dim(settings)
    effective_dim = (
        runtime_dim if 0 < runtime_dim < embed_dim else embed_dim
    )
    if effective_dim < 4096:
        return
    logger.warning(
        "scale-index build for %s: vectors/ANN build at effective width %s "
        "(EMBED_DIM=%s, EMBED_RUNTIME_DIM=%s) — a large index, ~%sx the "
        "memory of a 1024-dim one. Lower EMBED_RUNTIME_DIM below EMBED_DIM "
        "(e.g. 1024) to shrink it if the similarity space allows.",
        notebook_id,
        effective_dim,
        settings.embed_dim,
        runtime_dim,
        effective_dim // 1024,
    )


class ScaleIndexBuilder:
    def __init__(
        self,
        *,
        settings,
        projections,
        artifacts,
        event_log,
        get_notebook: Callable[[str], Any],
        version: Callable[[str], list],
        load_scale: Callable[[str], Any],
        full_viz_graph: Callable[[str], dict],
        relations_for_notebook: Callable[[str], list],
        cluster_map: Callable[[str], dict],
        incremental_fuse_source: Callable[[str, str], None],
        invalidate_scale_cache: Callable[[str], None],
        cache_viz: Callable[[str, Any], None],
        building: set,
        building_lock,
        notify_index_done: Callable[[str], None],
        now: Callable[[], str],
        invalidate_source_partition_cache: Callable[[str], None] | None = None,
        invalidate_unified_cache: "Callable[[str], None] | None" = None,
    ) -> None:
        self.settings = settings
        self.projections = projections
        self.artifacts = artifacts
        self.event_log = event_log
        self.get_notebook = get_notebook
        self.version = version
        self.load_scale = load_scale
        self.full_viz_graph = full_viz_graph
        self.relations_for_notebook = relations_for_notebook
        self.cluster_map = cluster_map
        self.incremental_fuse_source = incremental_fuse_source
        self.invalidate_scale_cache = invalidate_scale_cache
        self.invalidate_source_partition_cache = invalidate_source_partition_cache
        self.cache_viz = cache_viz
        # full_viz_graph('object') caches the whole 8M-object graph dict in the
        # facade's unified_cache and never drops it during the build. Once
        # viz_arrays has extracted the compact numpy arrays the dict is dead
        # weight (~12-20GB at 8M objects), so build() invalidates it right after.
        # None = unwired (older callers / tests that don't exercise the cache).
        self.invalidate_unified_cache = invalidate_unified_cache
        self.building = building
        self.building_lock = building_lock
        self.notify_index_done = notify_index_done
        self.now = now

    def _rebuild_source_partitions(
        self, notebook_id: str, parent_version: Any
    ) -> dict | None:
        """Publish optional companions after the main artifact is durable.

        Failure is fail-open for the legacy scale index and fail-closed for the
        new capability: an old companion's parent identity no longer matches,
        so runtime returns unavailable instead of traversing stale/full graph.
        """
        if not bool(
            getattr(
                self.settings,
                "source_partitioned_graph_artifacts_enabled",
                False,
            )
        ):
            return None
        try:
            all_sources = self.projections.source_ids(notebook_id)
            source_ids = self.projections.visible_source_ids(
                notebook_id, all_sources
            )
            manifest = self.artifacts.save_source_partitions(
                notebook_id,
                parent_version=parent_version,
                source_ids=source_ids,
                load_rows=lambda source_id: (
                    self.projections.source_graph_partition_rows(
                        notebook_id, source_id
                    )
                ),
            )
            if self.invalidate_source_partition_cache is not None:
                self.invalidate_source_partition_cache(notebook_id)
            return manifest
        except Exception:  # noqa: BLE001 - optional artifact is fail-open
            self.event_log.logger.exception(
                "source-partition artifact build failed for %s", notebook_id
            )
            return None

    def _build_ann(self, vectors):
        """Build an hnsw index from a (n, dim) float32 matrix — same frozen
        params as the KG ANN (cosine / M=16 / ef=hnsw_ef_construction /
        random_seed=42). build() pre-builds the chunk/relation ANN inline so
        each matrix is freed BEFORE persist instead of riding resident for
        save_scale_index to rebuild (relation matrix alone is ~33GB at 8M rows).
        Returns None for an empty/absent matrix."""
        if vectors is None or getattr(vectors, "shape", (0,))[0] == 0:
            return None
        import hnswlib

        idx = hnswlib.Index(space="cosine", dim=int(vectors.shape[1]))
        idx.init_index(
            max_elements=vectors.shape[0],
            ef_construction=self.settings.hnsw_ef_construction,
            M=16,
            random_seed=42,
        )
        idx.add_items(vectors, np.arange(vectors.shape[0]))
        return idx

    def _chunk_ann_source_codes(
        self,
        notebook_id: str,
        chunk_ids: Sequence[str],
        *,
        source_names: Sequence[str] = (),
    ) -> tuple[list[str], "np.ndarray", "np.ndarray"]:
        """Build a compact row-aligned source sidecar for chunk HNSW labels.

        The mapping is read in bounded pages and encoded as int32, avoiding an
        8M-entry Python id→source dictionary during an offline index build.
        ``-1`` marks an embedding whose chunk row disappeared during the build;
        such a row is never admitted by a source-filtered query.
        """
        names = list(source_names)
        source_to_code = {source_id: code for code, source_id in enumerate(names)}
        codes = np.full(len(chunk_ids), -1, dtype=np.int32)
        page_size = 10_000
        for offset in range(0, len(chunk_ids), page_size):
            page = list(chunk_ids[offset:offset + page_size])
            mapped = self.projections.chunk_sources_for_ids(notebook_id, page)
            for index, chunk_id in enumerate(page, start=offset):
                source_id = mapped.get(chunk_id)
                if not source_id:
                    continue
                code = source_to_code.get(source_id)
                if code is None:
                    code = len(names)
                    names.append(source_id)
                    source_to_code[source_id] = code
                codes[index] = code
        counts = np.bincount(
            codes[codes >= 0], minlength=len(names)
        ).astype(np.int64, copy=False)
        return names, codes, counts

    def gather_graph(
        self,
        notebook_id: str,
        source_ids: Sequence[str] | None = None,
        synonym_edges: Sequence[tuple[str, str, float]] | None = None,
        as_arrays: bool = False,
    ) -> tuple:
        rows = self.projections.graph_rows(
            notebook_id,
            source_ids,
            synonym_edges=synonym_edges,
            as_arrays=as_arrays,
        )
        return (
            rows.node_ids,
            rows.edges,
            rows.chunk_ids,
            rows.kg_node_ids,
            rows.membership_counts,
        )

    def _notify_stage(
        self,
        notebook_id: str,
        on_stage: Callable[[str, int], None] | None,
        stage_name: str,
        latency_ms: int,
    ) -> None:
        if on_stage is None:
            return
        try:
            on_stage(stage_name, latency_ms)
        except Exception:  # noqa: BLE001 - progress observers are fail-open
            self.event_log.logger.warning(
                "build_scale_index on_stage callback failed for stage %s",
                stage_name,
                exc_info=False,
            )

    def build(
        self,
        notebook_id: str,
        on_stage: Callable[[str, int], None] | None = None,
    ) -> dict:
        """Build the complete persisted scale index for one notebook."""
        self.get_notebook(notebook_id)
        # OOM guard (audit P2-6): building every vector matrix / hnsw at full
        # native width costs ~4x the truncated path's memory — the difference
        # between fitting and OOM on a multi-million-vector base library. The
        # EFFECTIVE build width is EMBED_RUNTIME_DIM only when it actually
        # truncates (0 < runtime < EMBED_DIM); otherwise it is EMBED_DIM. So warn
        # whenever that effective width is large — runtime unset (0) OR runtime
        # >= EMBED_DIM (a no-op truncation, e.g. EMBED_RUNTIME_DIM == EMBED_DIM,
        # which the validator permits — codex PR#353 r4) both build full width.
        # Loud, NOT fatal: a natively small-dim model legitimately needs none.
        _warn_if_scale_index_uses_full_width(
            self.settings,
            self.event_log.logger,
            notebook_id,
        )
        build_started = time.perf_counter()
        timings: dict[str, int] = {}

        def timed(stage_name: str, fn: Callable[[], Any]):
            started = time.perf_counter()
            result = fn()
            latency_ms = round((time.perf_counter() - started) * 1000)
            timings[stage_name] = latency_ms
            self.event_log.emit(
                {
                    "kind": "scale_index_build",
                    "notebook_id": notebook_id,
                    "stage": stage_name,
                    "status": "done",
                    "latency_ms": latency_ms,
                }
            )
            self._notify_stage(notebook_id, on_stage, stage_name, latency_ms)
            return result

        ann_ids_raw, ann_matrix_raw = timed(
            "kg_matrix",
            lambda: self.projections.embedding_matrix(
                notebook_id, "knowledge_embeddings", "object_id"
            ),
        )
        ann_ids = list(ann_ids_raw) if ann_ids_raw else []
        if ann_ids and ann_matrix_raw is not None:
            ann_labels = ann_ids
            ann_vectors = np.asarray(ann_matrix_raw, dtype=np.float32)
        else:
            ann_labels = []
            ann_vectors = np.empty(
                (0, max(1, self.settings.embed_dim)), dtype=np.float32
            )

        def build_kg_ann():
            if ann_vectors.shape[0] == 0:
                return None
            import hnswlib

            index = hnswlib.Index(
                space="cosine", dim=int(ann_vectors.shape[1])
            )
            index.init_index(
                max_elements=ann_vectors.shape[0],
                ef_construction=self.settings.hnsw_ef_construction,
                M=16,
                random_seed=42,
            )
            index.add_items(ann_vectors, np.arange(ann_vectors.shape[0]))
            return index

        kg_ann_index = timed("ann_build", build_kg_ann)
        # Capture the built dim NOW, before ann_vectors is freed post-synonym.
        # ann_vectors is never persisted (it only fed the KG hnsw + the synonym
        # KNN), so it must not ride ~33GB resident through chunk/relation load
        # and persist — kg_ann_index already holds the vectors.
        from app.services.vector_index import resolve_runtime_dim as _resolve_dim

        built_dim = (
            int(ann_vectors.shape[1])
            if getattr(ann_vectors, "size", 0)
            else (_resolve_dim(self.settings) or self.settings.embed_dim)
        )
        gc.collect()

        def synonym_edges():
            if kg_ann_index is None or not self.settings.ppr_emb_synonym_enabled:
                return []
            from app.services.kg.ppr import emb_synonym_edges

            return emb_synonym_edges(
                ann_labels,
                ann_vectors,
                self.settings.ppr_emb_synonym_threshold,
                self.settings.ppr_emb_synonym_topk,
                self.settings.ppr_emb_synonym_max_entities,
                prebuilt_index=kg_ann_index,
                ef_construction=self.settings.hnsw_ef_construction,
            )

        synonyms = timed("synonym", synonym_edges)
        # Free the KG matrix for real: ann_vectors is np.asarray(ann_matrix_raw,
        # float32) which ALIASES the (already-float32) matrix, so dropping only
        # ann_vectors leaves ~33GB pinned via ann_matrix_raw through
        # chunk/relation load + persist. Drop the load-side aliases too; the
        # build_kg_ann/synonym_edges closure cells for ann_vectors are cleared
        # by this `del` (shared cell), and kg_ann_index keeps its own hnsw copy.
        del ann_vectors, ann_matrix_raw, ann_ids_raw
        gc.collect()
        (
            node_ids,
            (edge_src, edge_tgt, edge_weight),
            chunk_ids,
            kg_node_ids,
            membership_counts,
        ) = timed(
            "gather",
            lambda: self.gather_graph(
                notebook_id, synonym_edges=synonyms, as_arrays=True
            ),
        )
        del synonyms

        kg_id_set = set(kg_node_ids)
        id_to_idx = {node_id: i for i, node_id in enumerate(node_ids)}
        chunk_index = [
            id_to_idx[chunk_id]
            for chunk_id in chunk_ids
            if chunk_id in id_to_idx
        ]
        idf = []
        for node_id in node_ids:
            if node_id in kg_id_set:
                count = membership_counts.get(node_id, 0)
                idf.append(1.0 / count if count > 0 else 1.0)
            else:
                idf.append(1.0)
        del kg_id_set, membership_counts, id_to_idx  # id_to_idx dead after chunk_index

        transition, _ = timed(
            "transition",
            lambda: scale_index_module.build_transition_arrays(
                node_ids, edge_src, edge_tgt, edge_weight
            ),
        )
        del edge_src, edge_tgt, edge_weight
        gc.collect()

        def _load_build_chunk_ann():
            ids_raw, matrix_raw = self.projections.embedding_matrix(
                notebook_id, "chunk_embeddings", "chunk_id"
            )
            labels = list(ids_raw) if ids_raw else []
            vecs = (
                np.asarray(matrix_raw, dtype=np.float32)
                if labels and matrix_raw is not None
                else None
            )
            # Build the ANN and drop the matrix INSIDE the stage: persist never
            # rebuilds it, and the matrix (~7GB chunks at 8M) must not survive
            # past here. `vecs`/`matrix_raw` are locals, freed on return. The
            # "chunk_matrix" stage now times load+build (still one stage), so
            # build_ms keeps the ANN-build cost attributable.
            ann = self._build_ann(vecs)
            names, codes, counts = self._chunk_ann_source_codes(
                notebook_id, labels
            )
            return labels, ann, names, codes, counts

        (
            chunk_ann_labels,
            chunk_ann_index,
            chunk_ann_source_names,
            chunk_ann_source_codes,
            chunk_ann_source_counts,
        ) = timed(
            "chunk_matrix", _load_build_chunk_ann
        )
        gc.collect()

        def _load_build_relation_ann():
            ids_raw, matrix_raw = self.projections.embedding_matrix(
                notebook_id, "relation_embeddings", "relation_id"
            )
            labels = list(ids_raw) if ids_raw else []
            vecs = (
                np.asarray(matrix_raw, dtype=np.float32)
                if labels and matrix_raw is not None
                else None
            )
            # Same as chunk: build + drop the matrix inside the stage. The
            # relation matrix (~33GB at 8M rows) is the single biggest slice the
            # old persist held resident.
            return labels, self._build_ann(vecs)

        relation_ann_labels, relation_ann_index = timed(
            "relation_matrix", _load_build_relation_ann
        )
        gc.collect()

        viz_artifacts = timed(
            "viz_arrays",
            lambda: viz_index_module.arrays_from_graph(
                self.full_viz_graph(notebook_id)
            ),
        )
        viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload = (
            viz_artifacts
        )
        # The compact viz arrays above are independent numpy structures; the
        # source graph dict full_viz_graph() left in unified_cache is now dead
        # weight and must not ride resident through persist.
        if self.invalidate_unified_cache is not None:
            self.invalidate_unified_cache(notebook_id)
        gc.collect()

        # built_dim was captured right after ann_build, before ann_vectors was
        # freed (see above).
        manifest = {
            "version": self.version(notebook_id),
            "pipeline_identity": list(
                self.projections.pipeline_identity(notebook_id)
            ),
            "dim": built_dim,
            "n_nodes": len(node_ids),
            "n_kg_nodes": len(kg_node_ids),
            "n_chunks": len(chunk_ids),
            "n_hubs": len(node_ids) - len(kg_node_ids) - len(chunk_ids),
            "n_ann": len(ann_labels),
            "n_viz_nodes": len(viz_ids),
            "n_viz_edges": len(viz_payload.get("edges", [])),
            "watermark_sources": sorted(
                self.projections.source_ids(notebook_id)
            ),
            "built_at": self.now(),
            # Wall clock from entry to this manifest being assembled. This is a
            # deliberate approximation (hence "约" in user-facing copy): it
            # excludes the subsequent persist I/O (cannot include the duration
            # of writing the very manifest it would be embedded in) and also
            # excludes the source-partition artifact rebuild that runs after
            # this manifest is saved (see below, guarded by
            # source_partitioned_graph_artifacts_enabled). Named distinctly
            # from ``build_ms`` below (per-stage timings dict) — they differ
            # by one letter and are easy to confuse.
            "total_build_ms": round((time.perf_counter() - build_started) * 1000),
            "build_ms": dict(timings),
        }
        persist_started = time.perf_counter()
        saved_manifest = self.artifacts.save_full(
            notebook_id,
            {
                "node_ids": node_ids,
                "transition": transition,
                "idf": idf,
                "chunk_index": chunk_index,
                # ann_vectors intentionally omitted — freed post-synonym; the KG
                # index is passed prebuilt below and save uses manifest["dim"].
                "ann_labels": ann_labels,
                "manifest": manifest,
                "viz_ids": viz_ids,
                "viz_adj": viz_adj,
                "viz_deg": viz_deg,
                "viz_types": viz_types,
                "viz_names": viz_names,
                "viz_payload": viz_payload,
                # chunk/relation matrices freed after their ANN was built; pass
                # the prebuilt indexes (save writes them straight to .bin).
                "chunk_ann_labels": chunk_ann_labels,
                "chunk_ann_source_names": chunk_ann_source_names,
                "chunk_ann_source_codes": chunk_ann_source_codes,
                "chunk_ann_source_counts": chunk_ann_source_counts,
                "relation_ann_labels": relation_ann_labels,
                "prebuilt_ann": kg_ann_index,
                "prebuilt_chunk_ann": chunk_ann_index,
                "prebuilt_relation_ann": relation_ann_index,
                "ef_construction": self.settings.hnsw_ef_construction,
            },
        )
        if bool(
            getattr(
                self.settings,
                "source_partitioned_graph_artifacts_enabled",
                False,
            )
        ):
            timed(
                "source_partitions",
                lambda: self._rebuild_source_partitions(
                    notebook_id, saved_manifest.get("version")
                ),
            )
        persist_ms = round((time.perf_counter() - persist_started) * 1000)
        timings["persist"] = persist_ms
        self.event_log.emit(
            {
                "kind": "scale_index_build",
                "notebook_id": notebook_id,
                "stage": "persist",
                "status": "done",
                "latency_ms": persist_ms,
            }
        )
        self._notify_stage(notebook_id, on_stage, "persist", persist_ms)
        total_ms = round((time.perf_counter() - build_started) * 1000)
        timings["total"] = total_ms
        self.event_log.emit(
            {
                "kind": "scale_index_build",
                "notebook_id": notebook_id,
                "stage": "total",
                "status": "done",
                "latency_ms": total_ms,
            }
        )
        self._notify_stage(notebook_id, on_stage, "total", total_ms)
        # Preserve the old checkpoint: only a successful save invalidates the
        # live LRU entry. The callback resolves the existing facade-owned LRU.
        self.invalidate_scale_cache(notebook_id)
        return {**saved_manifest, "build_ms": dict(timings)}

    def _index_delta(self, notebook_id: str) -> dict:
        current_sources = self.projections.source_ids(notebook_id)
        try:
            manifest = self.artifacts.read_manifest(
                self.artifacts.scale_dir(notebook_id)
            )
        except Exception:  # noqa: BLE001 — 损坏 manifest:read_manifest 刻意 raise。等价于
            manifest = None  # 「无可用索引」(下面 None/missing 分支同款)→ 视作全量待建,须重建。
        if manifest is None:
            return {
                "delta_sources": sorted(current_sources),
                "delta_chunks": self.projections.total_chunk_count(notebook_id),
                "indexed": False,
            }
        watermark = set(manifest.get("watermark_sources", []))
        delta_sources = sorted(
            source_id
            for source_id in current_sources
            if source_id not in watermark
        )
        if not delta_sources:
            return {"delta_sources": [], "delta_chunks": 0, "indexed": True}
        return {
            "delta_sources": delta_sources,
            "delta_chunks": self.projections.delta_chunk_count(
                notebook_id, delta_sources
            ),
            "indexed": True,
        }

    def fold(self, notebook_id: str, assume_locked: bool = False) -> dict:
        fold_started = time.perf_counter()
        idx = self.load_scale(notebook_id)
        if idx is None:
            return self.build(notebook_id)

        pipeline_identity = list(
            self.projections.pipeline_identity(notebook_id)
        )
        if idx.manifest.get("pipeline_identity") != pipeline_identity:
            self.event_log.emit(
                {
                    "kind": "scale_fold_refused",
                    "notebook_id": notebook_id,
                    "reason": "pipeline_mismatch",
                }
            )
            return self.build(notebook_id)

        from app.services.vector_index import resolve_runtime_dim

        effective_dim = (
            resolve_runtime_dim(self.settings) or self.settings.embed_dim
        )
        if int(idx.manifest.get("dim", effective_dim)) != int(effective_dim):
            self.event_log.emit(
                {
                    "kind": "scale_fold_refused",
                    "notebook_id": notebook_id,
                    "reason": "dim_mismatch",
                    "manifest_dim": int(idx.manifest.get("dim", 0)),
                    "runtime_dim": int(effective_dim),
                }
            )
            return self.build(notebook_id)

        delta = self._index_delta(notebook_id)
        if not delta["delta_sources"]:
            return idx.manifest
        if not assume_locked:
            with self.building_lock:
                if notebook_id in self.building:
                    return {"status": "already_building"}
                self.building.add(notebook_id)

        completed = False
        try:
            for source_id in delta["delta_sources"]:
                try:
                    self.incremental_fuse_source(notebook_id, source_id)
                except Exception:  # noqa: BLE001 - fold continues without hubs
                    self.event_log.logger.exception(
                        "fold incremental_fuse failed for %s", source_id
                    )

            (
                delta_nodes,
                delta_edges,
                delta_chunks,
                delta_kg_ids,
                delta_membership,
            ) = self.gather_graph(
                notebook_id, source_ids=delta["delta_sources"]
            )
            kg_ids = set(delta_kg_ids)
            delta_idf = {
                object_id: (1.0 / count if count > 0 else 1.0)
                for object_id, count in delta_membership.items()
            }
            node_ids, transition, idf, chunk_index = (
                scale_index_module.fold_arrays(
                    list(idx.node_ids),
                    idx.transition,
                    idx.idf,
                    idx.chunk_index,
                    delta_nodes,
                    delta_edges,
                    delta_chunks,
                    delta_idf,
                )
            )
            live_dir = self.artifacts.scale_dir(notebook_id)
            temporary = self.artifacts.prepare_fold_directory(notebook_id)
            scale_index_module.save_fold_core(
                str(temporary), node_ids, transition, idf, chunk_index
            )

            dim = int(
                idx.manifest.get(
                    "dim", resolve_runtime_dim(self.settings) or self.settings.embed_dim
                )
            )

            def delta_vectors(table, column, ids):
                return self.projections.embedding_matrix(
                    notebook_id, table, column, object_ids=ids
                )

            kg_vector_ids, kg_matrix = delta_vectors(
                "knowledge_embeddings", "object_id", list(kg_ids)
            )
            ann = scale_index_module.add_items_to_ann(
                idx.ann_path,
                dim,
                kg_matrix if len(kg_matrix) else [],
                len(idx.ann_labels),
            )
            ann_labels = list(idx.ann_labels) + list(kg_vector_ids)
            scale_index_module.save_fold_ann(
                str(temporary), "ann.bin", "ann_labels.npy", ann, ann_labels
            )

            manifest = dict(idx.manifest)
            manifest["built_at"] = self.now()
            if idx.chunk_ann_path and idx.chunk_ann_labels is not None:
                chunk_vector_ids, chunk_matrix = delta_vectors(
                    "chunk_embeddings", "chunk_id", list(delta_chunks)
                )
                chunk_ann = scale_index_module.add_items_to_ann(
                    idx.chunk_ann_path,
                    dim,
                    chunk_matrix if len(chunk_matrix) else [],
                    len(idx.chunk_ann_labels),
                )
                chunk_labels = list(idx.chunk_ann_labels) + list(
                    chunk_vector_ids
                )
                scale_index_module.save_fold_ann(
                    str(temporary),
                    "chunk_ann.bin",
                    "chunk_ann_labels.npy",
                    chunk_ann,
                    chunk_labels,
                )
                manifest["has_chunk_ann"] = True
                manifest["n_chunk_ann"] = len(chunk_labels)
                if (
                    idx.chunk_ann_source_names is not None
                    and idx.chunk_ann_source_codes is not None
                ):
                    source_names, delta_codes, _ = (
                        self._chunk_ann_source_codes(
                            notebook_id,
                            chunk_vector_ids,
                            source_names=idx.chunk_ann_source_names,
                        )
                    )
                    source_codes = np.concatenate([
                        np.asarray(
                            idx.chunk_ann_source_codes, dtype=np.int32
                        ),
                        delta_codes,
                    ])
                    source_counts = np.bincount(
                        source_codes[source_codes >= 0],
                        minlength=len(source_names),
                    ).astype(np.int64, copy=False)
                else:
                    # A fold also upgrades a pre-sidecar artifact.  Mapping the
                    # complete label list is bounded internally and avoids
                    # forcing an otherwise unnecessary full ANN rebuild after
                    # rollout of source-filtered Top-K queries.
                    source_names, source_codes, source_counts = (
                        self._chunk_ann_source_codes(
                            notebook_id,
                            chunk_labels,
                        )
                    )
                if len(source_codes) == len(chunk_labels):
                    scale_index_module.save_fold_chunk_sources(
                        str(temporary),
                        source_names,
                        source_codes,
                        source_counts,
                    )
                    manifest["has_chunk_ann_sources"] = True

            if idx.relation_ann_path and idx.relation_ann_labels is not None:
                relation_ids = self._delta_relation_ids(
                    notebook_id, delta["delta_sources"]
                )
                relation_vector_ids, relation_matrix = delta_vectors(
                    "relation_embeddings", "relation_id", relation_ids
                )
                relation_ann = scale_index_module.add_items_to_ann(
                    idx.relation_ann_path,
                    dim,
                    relation_matrix if len(relation_matrix) else [],
                    len(idx.relation_ann_labels),
                )
                relation_labels = list(idx.relation_ann_labels) + list(
                    relation_vector_ids
                )
                scale_index_module.save_fold_ann(
                    str(temporary),
                    "relation_ann.bin",
                    "relation_ann_labels.npy",
                    relation_ann,
                    relation_labels,
                )
                manifest["has_relation_ann"] = True
                manifest["n_relation_ann"] = len(relation_labels)

            scale_index_module.copy_fold_viz(str(live_dir), str(temporary))
            manifest.update(
                {
                    "version": self.version(notebook_id),
                    "pipeline_identity": pipeline_identity,
                    "watermark_sources": sorted(
                        self.projections.source_ids(notebook_id)
                    ),
                    "n_nodes": len(node_ids),
                    "n_chunks": int(
                        self.projections.total_chunk_count(notebook_id)
                    ),
                    "n_ann": len(ann_labels),
                    # Wall clock from entry to this manifest being assembled —
                    # mirrors build()'s total_build_ms (see there for why it
                    # excludes both the manifest write itself and the
                    # subsequent source-partition artifact rebuild).
                    "total_build_ms": round(
                        (time.perf_counter() - fold_started) * 1000
                    ),
                }
            )
            scale_index_module.save_fold_manifest(str(temporary), manifest)

            with self.building_lock:
                self.artifacts.swap_fold_directory(notebook_id, temporary)
                self.invalidate_scale_cache(notebook_id)
            if bool(
                getattr(
                    self.settings,
                    "source_partitioned_graph_artifacts_enabled",
                    False,
                )
            ):
                self._rebuild_source_partitions(
                    notebook_id, manifest.get("version")
                )
            completed = True
            return manifest
        finally:
            if not assume_locked:
                with self.building_lock:
                    self.building.discard(notebook_id)
                if completed:
                    self.notify_index_done(notebook_id)

    def _delta_relation_ids(
        self, notebook_id: str, source_ids: Sequence[str]
    ) -> list[str]:
        relation_ids = []
        with self.projections.connect() as db:
            for batch in self.projections.in_batches(source_ids):
                relation_ids.extend(
                    self.projections.relation_ids_for_source_batch(
                        db, notebook_id, batch
                    )
                )
        return relation_ids

    def _derive_object_graph_lite(self, notebook_id: str) -> dict:
        self.get_notebook(notebook_id)
        from app.services.kg_merge import derive_unified_graph

        with self.projections.connect() as db:
            rows = self.projections.active_object_graph_rows(db, notebook_id)
        nodes = [
            {
                "id": row["id"],
                "object_type": row["object_type"],
                "payload": {"name": row["name"] or ""},
            }
            for row in rows
        ]
        edges = [
            {
                "source_object_id": relation["source_object_id"],
                "target_object_id": relation["target_object_id"],
                "edge_type": relation["edge_type"],
            }
            for relation in self.relations_for_notebook(notebook_id)
        ]
        return derive_unified_graph(
            nodes, edges, self.cluster_map(notebook_id)
        )

    def build_viz(self, notebook_id: str) -> Optional[dict]:
        self.get_notebook(notebook_id)
        # Capture the freshness stamps BEFORE deriving the graph. If a cluster
        # write commits between here and the fetch below, the artifact is stamped
        # with the PRE-derive version/cseq — so viz_index()/viz_probe() see it as
        # stale and re-run, instead of mislabelling a pre-rebuild graph as current
        # (codex PR#356 r2 P1a — the derive-then-stamp race). cluster_seq is the
        # cluster_mutation_seq the rebuild bumps but version() (a version_facts memo
        # key) doesn't expose, so viz freshness would otherwise miss a same-second
        # cluster-only rewrite (codex PR#356 r1 P1).
        ver = self.version(notebook_id)
        cseq = int(self.projections.version_signal(notebook_id)[1])
        full = self._derive_object_graph_lite(notebook_id)
        if not full["nodes"]:
            return None
        viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload = (
            viz_index_module.arrays_from_graph(full)
        )
        manifest = {
            "version": ver,
            "cluster_seq": cseq,
            "n_viz_nodes": len(viz_ids),
            "n_viz_edges": len(viz_payload.get("edges", [])),
        }
        self.artifacts.save_viz(
            notebook_id,
            {
                "viz_ids": viz_ids,
                "viz_adj": viz_adj,
                "viz_deg": viz_deg,
                "viz_types": viz_types,
                "viz_names": viz_names,
                "viz_payload": viz_payload,
                "manifest": manifest,
            },
        )
        self.cache_viz(notebook_id, self.artifacts.load_viz(notebook_id))
        return manifest
