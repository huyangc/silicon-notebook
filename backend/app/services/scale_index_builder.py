"""Scale-index full-build, delta-fold, and viz-build orchestration.

The builder composes the runtime-owned projection and artifact adapters.  It
deliberately receives late-bound callbacks for the compatibility seams that
still live on ``SQLiteRepository``; it never retains the facade itself.
"""
from __future__ import annotations

import gc
import secrets
import time
from typing import Any, Callable, Optional, Sequence

import numpy as np

from app.domain.kg.source_partition import new_build_id
from app.repositories.scale_build_lock import ScaleBuildLockLost
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
        # Re-verification of this build's cross-process claim, consulted in the
        # last instant before the artifact swap. Retargeted by
        # ``ScaleArtifactRuntime`` alongside ``building``/``building_lock``; a
        # builder used directly (tests, no runtime) holds no claim and so has
        # nothing to re-verify.
        self.verify_scale_build_lock: Callable[[str], bool] = lambda _nb: True
        # This build's claim-unique staging-path token (P1, codex PR#643 R1).
        # Retargeted by ``ScaleArtifactRuntime`` to read the registered claim's
        # ``claim_token``; a builder used directly falls back to a fresh random
        # token per call — no claim to derive one from, so no attempt could
        # collide with another anyway.
        self.scale_build_claim_token: Callable[[str], str] = (
            lambda _nb: secrets.token_hex(8)
        )

    def _rebuild_source_partitions(
        self,
        notebook_id: str,
        parent_version: Any,
        *,
        parent_build_id: Any,
        claim_token: str,
        verify_held: Callable[[], bool],
    ) -> dict | None:
        """Publish optional companions after the main artifact is durable.

        Failure is fail-open for the legacy scale index and fail-closed for the
        new capability: an old companion's parent identity no longer matches,
        so runtime returns unavailable instead of traversing stale/full graph.
        ``parent_build_id`` is what makes that true even for a republish under
        an UNCHANGED version (P1, codex PR#643 R26) — the main manifest this
        build just swapped in carries a fresh ``build_id``, so a companion
        this call fails to republish keeps the previous one and stops pairing.

        ``ScaleBuildLockLost`` is the one exception this does NOT swallow into
        that fail-open (codex PR#643 R1 P2): the companion runs strictly AFTER
        the main root's swap, so by the time this loses its claim the main
        index is already the new generation — silently returning ``None``
        here would read as "nothing changed" when in fact half of a two-root
        publish happened. It is re-raised with that distinction spelled out
        so it reaches the same operator-facing translation the main swap's
        loss does (``ScaleBuildCliFailure`` in the offline CLI).
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
                parent_build_id=parent_build_id,
                source_ids=source_ids,
                load_rows=lambda source_id: (
                    self.projections.source_graph_partition_rows(
                        notebook_id, source_id
                    )
                ),
                claim_token=claim_token,
                verify_held=verify_held,
            )
            return manifest
        except ScaleBuildLockLost as error:
            raise ScaleBuildLockLost(
                f"{error} The main scale index for {notebook_id!r} was "
                "already published (this companion rebuild runs after the "
                "main swap); only the source-partition companion was left "
                "unpublished. Re-run `fold` or `build` to complete it."
            ) from error
        except Exception:  # noqa: BLE001 - optional artifact is fail-open
            self.event_log.logger.exception(
                "source-partition artifact build failed for %s", notebook_id
            )
            return None
        finally:
            # Dropped on EVERY exit, not only the successful one (P1, codex
            # PR#643 R26). The main root above is already published under a
            # NEW ``build_id``, so if this companion publish did not happen
            # the warm entries in this process are pairs the cold gate would
            # now refuse — and nothing else drops them: the companion cache
            # key is database-derived (unchanged by a same-version republish)
            # and its disk probe watches the COMPANION root, which a failed
            # publish leaves byte-identical. Cheap either way: this pops one
            # notebook's entries out of a bounded dict.
            #
            # Residual, in the same shape R4 already registered for the
            # companion side: this only reaches THIS process. A build in
            # another process (the offline CLI's ``build``) whose companion
            # step fails the same way leaves a serving replica's warm entry
            # in place until eviction, because that replica sees neither this
            # call nor any change to the companion root it stats. Closing it
            # would mean stat-ing the MAIN manifest on the companion read
            # path too; not done here — the cold path is exact, and the
            # operator-facing repair (re-run the index command) is the
            # documented response to a half-published generation anyway.
            if self.invalidate_source_partition_cache is not None:
                self.invalidate_source_partition_cache(notebook_id)

    def _paged_ann(self, notebook_id: str, table: str, id_column: str,
                   on_load_ms=None):
        """Build one hnsw index straight from bounded embedding pages.

        Replaces "load one whole-notebook matrix, then add_items it" for all
        three build-time ANN legs (WR-9). hnswlib copies each page into its own
        graph, so a page can be dropped the instant ``add_items`` returns and
        the load-side matrix never exists — the ~33GB relation matrix and the
        ~7GB chunk one stop coexisting with the index they feed. hnswlib's own
        internal copy is a second, unavoidable one; ``np.asarray`` on the old
        whole matrix was only an ALIAS, so it is the load-side copy that is
        actually removed here.

        ``init_index`` needs both a width and a capacity up front: the width
        comes from the first page (nothing else knows the post-truncation dim),
        and the capacity from a COUNT upper bound, since how many rows survive
        decoding is unknown until the scan ends. Rows inserted below the
        cursor after that COUNT are absorbed by ``resize_index`` rather than
        raising — the same drift the keyset scan already tolerates. That
        growth is GEOMETRIC (``max(needed, capacity * 2)``), not
        page-at-a-time: ``hnswlib.resize_index`` reallocates the whole element
        store and copies every element already inserted, so growing by one
        page per page turns the overflow tail into a quadratic re-copy (and
        transiently needs 2x the index's memory each time). Doubling caps it
        at O(log) reallocations for any amount of drift; the wasted capacity
        is bounded by one doubling of whatever the COUNT under-estimated by.

        ``on_load_ms`` is invoked ONCE, with the accumulated read+decode
        milliseconds, at the instant the page stream is exhausted — i.e. from
        inside this method, which is the only place that knows loading has
        finished. The caller uses it to emit the load stage before it emits
        the insert stage. Registered residual: with a paged feed the two costs
        genuinely INTERLEAVE (read a page, insert it, repeat), so the two
        stage events still land close together in wall-clock time, unlike the
        pre-paging shape where minutes of loading preceded minutes of
        inserting. A leg therefore stays silent on the CLI for its whole
        duration — see ``batch_ingest._index_stage_progress``, which is the
        only liveness signal an operator has on a tens-of-minutes build.

        Returns ``(labels, index, load_ms, add_ms)``: labels row-aligned with
        the index in scan order (byte-identical to the old whole-matrix path,
        which used the same scan), and the two costs split so the build's
        frozen ``*_matrix`` / ``ann_build`` stage timings keep meaning what
        they used to. ``index`` is None when the notebook has no usable
        vectors."""
        import hnswlib

        labels: list[str] = []
        index = None
        load_ms = 0.0
        add_ms = 0.0
        capacity = 0
        started = time.perf_counter()
        upper_bound = self.projections.embedding_row_count(notebook_id, table)
        pages = self.projections.embedding_pages(
            notebook_id, table, id_column,
            page_rows=int(self.settings.graph_fetch_page_rows),
        )
        while True:
            page = next(pages, None)
            load_ms += (time.perf_counter() - started) * 1000
            if page is None:
                break
            page_ids, page_matrix = page
            started = time.perf_counter()
            if index is None:
                capacity = max(1, upper_bound, len(page_ids))
                index = hnswlib.Index(
                    space="cosine", dim=int(page_matrix.shape[1])
                )
                index.init_index(
                    max_elements=capacity,
                    ef_construction=self.settings.hnsw_ef_construction,
                    M=16,
                    random_seed=42,
                )
            offset = len(labels)
            if offset + len(page_ids) > capacity:
                capacity = max(offset + len(page_ids), capacity * 2)
                index.resize_index(capacity)
            index.add_items(page_matrix, np.arange(offset, offset + len(page_ids)))
            labels.extend(page_ids)
            add_ms += (time.perf_counter() - started) * 1000
            started = time.perf_counter()
        if on_load_ms is not None:
            on_load_ms(round(load_ms))
        return labels, index, round(load_ms), round(add_ms)

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

        def record(stage_name: str, latency_ms: int) -> None:
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

        def timed(stage_name: str, fn: Callable[[], Any]):
            started = time.perf_counter()
            result = fn()
            record(stage_name, round((time.perf_counter() - started) * 1000))
            return result

        # KG leg: hnsw fed straight from bounded embedding pages, so the KG
        # matrix never exists as one object. The two frozen stage names are
        # kept and now carry the honest split of the same total — `kg_matrix`
        # is the paged read+decode, `ann_build` the hnsw insertions — instead
        # of the old load-everything-then-insert pair. `kg_matrix` is emitted
        # from INSIDE _paged_ann, the moment the page stream is exhausted, so
        # the load stage still precedes the insert stage in emission order
        # rather than both being synthesized afterwards. Registered residual:
        # the two costs interleave per page now, so the two events land close
        # together in wall-clock time (details in _paged_ann's docstring).
        ann_labels, kg_ann_index, _kg_load_ms, _kg_add_ms = self._paged_ann(
            notebook_id, "knowledge_embeddings", "object_id",
            on_load_ms=lambda ms: record("kg_matrix", ms),
        )
        record("ann_build", _kg_add_ms)
        # The built dim now comes off the index itself (the matrix that used to
        # answer this is gone by construction, not merely freed early).
        from app.services.vector_index import resolve_runtime_dim as _resolve_dim

        built_dim = (
            int(kg_ann_index.dim) if kg_ann_index is not None
            else (_resolve_dim(self.settings) or self.settings.embed_dim)
        )
        gc.collect()

        def synonym_edges():
            if kg_ann_index is None or not self.settings.ppr_emb_synonym_enabled:
                return []
            from app.domain.kg.ppr_pairs import emb_synonym_edges_paged

            # The query set comes from the INDEX ITSELF (get_items, bounded
            # label pages) — deliberately NOT a second database pass: an
            # embedding updated between the passes would keep its id yet
            # query its NEW vector against the OLD one stored in the index,
            # minting edges no consistent snapshot supports (codex #676 R1
            # P2). Querying the index's own stored vectors closes that class
            # by construction; see emb_synonym_edges_paged's docstring.
            return emb_synonym_edges_paged(
                ann_labels,
                kg_ann_index,
                self.settings.ppr_emb_synonym_threshold,
                self.settings.ppr_emb_synonym_topk,
                on_hnsw_error=lambda exc: self.event_log.emit(
                    {
                        "kind": "scale_index_synonym_degraded",
                        "notebook_id": notebook_id,
                        "error": type(exc).__name__,
                    }
                ),
            )

        synonyms = timed("synonym", synonym_edges)
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
            # Paged feed (batch-3 W4 T-W4-3.3): the chunk matrix (~7GB at 8M
            # rows) is never assembled at all now — each page is inserted and
            # dropped. The "chunk_matrix" stage still times load+build as one
            # stage, so build_ms keeps the ANN-build cost attributable.
            labels, ann, _load_ms, _add_ms = self._paged_ann(
                notebook_id, "chunk_embeddings", "chunk_id"
            )
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
            # Same paged feed as chunk. The relation matrix (~33GB at 8M rows)
            # was the single biggest slice the pipeline ever held resident;
            # with the paged feed it is never allocated.
            labels, ann, _load_ms, _add_ms = self._paged_ann(
                notebook_id, "relation_embeddings", "relation_id"
            )
            return labels, ann

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
            # W-CLI R1 P1-2: which library this artifact describes. The offline
            # CLI's ``import`` publishes a directory tree into whatever
            # ``--notebook`` the operator typed, and nothing else in the package
            # names its origin — a typo published library A's index into library
            # B and it started serving, because the retrieval side reads the
            # manifest that arrived, never the database's version.
            "notebook_id": notebook_id,
            # P1, codex PR#643 R26: THIS build's generation, minted fresh
            # here and copied into every companion manifest this build
            # publishes. ``version`` cannot serve that purpose — a
            # same-version republish is an explicitly supported scenario, so
            # a half-published pair (import interrupted between the two
            # roots, or an online rebuild that loses its claim after the main
            # swap) would carry two equal versions and be accepted as a pair.
            "build_id": new_build_id(),
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
            # W-CLI T-W3: the hnswlib/numpy/scipy versions this artifact was
            # built with. Optional by construction (older artifacts have no
            # such key); the load side warns on an hnswlib mismatch and the
            # offline CLI's ``import`` refuses one outright.
            scale_index_module.MANIFEST_LIBRARY_KEY: (
                scale_index_module.runtime_library_versions()
            ),
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
        # Minted once and reused for BOTH roots this build may publish (the
        # main index below and the source-partition companion further down):
        # one claim, one staging token (P1, codex PR#643 R1).
        claim_token = self.scale_build_claim_token(notebook_id)
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
            # Re-verify the cross-process build claim in the last instant
            # before the swap (the store calls this immediately before its
            # first rename); a lost claim abandons the build instead of
            # publishing over whoever owns the directory now.
            claim_token=claim_token,
            verify_held=lambda: self.verify_scale_build_lock(notebook_id),
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
                    notebook_id,
                    saved_manifest.get("version"),
                    parent_build_id=saved_manifest.get("build_id"),
                    claim_token=claim_token,
                    # A FRESH re-verification, not the value already used
                    # above: this companion rebuild can run long after the
                    # main swap (codex PR#643 R1 P2).
                    verify_held=lambda: self.verify_scale_build_lock(
                        notebook_id
                    ),
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

    def fold(
        self,
        notebook_id: str,
        assume_locked: bool = False,
        *,
        on_completed: Callable[[], None] | None = None,
    ) -> dict:
        """``on_completed`` fires only when this call actually FOLDED.

        Every early return above the staging block — no index yet, pipeline or
        dim drift (both fall back to a full rebuild), an empty delta — leaves it
        unfired, which is the same ``completed`` gate the in-builder
        notification below has always used. The caller that took the claim
        (``ScaleArtifactRuntime.fold``) notifies from outside it, where
        ``building`` is already released, instead of announcing a finished index
        for a fold that did nothing (codex W-CLI R1 P2-2).
        """
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
        # Minted once and reused for BOTH roots this fold may publish (the
        # main index and, further down, the source-partition companion) —
        # one claim, one staging token (P1, codex PR#643 R1).
        claim_token = self.scale_build_claim_token(notebook_id)
        try:
            for source_id in delta["delta_sources"]:
                try:
                    self.incremental_fuse_source(notebook_id, source_id)
                except Exception as exc:  # noqa: BLE001 - fold continues without hubs
                    # 结构化事件同 source_ingestion 侧(批 3·W2 PR-3):
                    # 只进日志的融合失败在事件流里隐形。
                    self.event_log.logger.exception(
                        "fold incremental_fuse failed for %s", source_id
                    )
                    self.event_log.emit({
                        "kind": "incremental_fuse_failed",
                        "notebook_id": notebook_id,
                        "source_id": source_id,
                        # 只记异常类名(codex #673 R1 P2/AGENTS 红线:异常原文可能带
                        # 私有路径/凭据/来源摘录,截断不等于脱敏;定位靠同一
                        # logger.exception 的服务端日志)。
                        "error": type(exc).__name__,
                    })

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
            temporary = self.artifacts.prepare_fold_directory(
                notebook_id, claim_token
            )
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
                    # Written, never inherited from ``idx`` — same reason as
                    # ``library_versions`` below, and the binding the offline
                    # ``import`` checks against ``--notebook`` (W-CLI R1 P1-2).
                    "notebook_id": notebook_id,
                    # Minted fresh, never inherited from ``idx`` (P1, codex
                    # PR#643 R26): a fold publishes a NEW main root, and the
                    # companion rebuild below is a separate publish that can
                    # fail or lose the claim. Reusing the base's id would let
                    # the previous generation's companion keep pairing with
                    # this new main index. A fold that only manages to swap
                    # the main root therefore leaves the companion naturally
                    # mismatched, which is the intended fail-soft.
                    "build_id": new_build_id(),
                    "pipeline_identity": pipeline_identity,
                    # W-CLI T-W3: refreshed, never inherited from ``idx``. A
                    # fold appends to the .bin with THIS process's hnswlib, so
                    # the published artifact belongs to this process's library
                    # set even when the base was built elsewhere.
                    scale_index_module.MANIFEST_LIBRARY_KEY: (
                        scale_index_module.runtime_library_versions()
                    ),
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

            # Same last-instant re-verification as the full rebuild — a fold's
            # swap is equally destructive. codex PR#643 R1 P2-3 used to read
            # this OUTSIDE ``building_lock`` and hand the swap a frozen
            # snapshot, worried that a PostgreSQL round trip inside that
            # process-global lock could stall every notebook's status poll
            # and admission for a full ``statement_timeout``. That traded
            # away the guarantee it was meant to preserve: if the session is
            # lost after the snapshot but before the rename, the snapshot
            # stays ``True`` forever, so a competing importer that acquires
            # the now-released claim can be overwritten by this unclaimed
            # fold (codex PR#643 R6 P1). The swap gets the LIVE verifier
            # back instead — exactly like ``build()``'s swap above — and the
            # freeze-window worry is addressed at its source:
            # ``PostgresScaleBuildLock.verify_held`` caps its own
            # ``pg_locks`` query to a short statement_timeout (see there), so
            # holding ``building_lock`` through this check is bounded by that
            # cap rather than by an unbounded network stall.
            with self.building_lock:
                self.artifacts.swap_fold_directory(
                    notebook_id,
                    temporary,
                    verify_held=lambda: self.verify_scale_build_lock(
                        notebook_id
                    ),
                )
                self.invalidate_scale_cache(notebook_id)
            if bool(
                getattr(
                    self.settings,
                    "source_partitioned_graph_artifacts_enabled",
                    False,
                )
            ):
                self._rebuild_source_partitions(
                    notebook_id,
                    manifest.get("version"),
                    parent_build_id=manifest.get("build_id"),
                    claim_token=claim_token,
                    # A SEPARATE live re-verification from the main swap's
                    # above — each lambda re-reads the lock at its own
                    # instant: this companion rebuild can run long after the
                    # main swap's check already passed (codex PR#643 R1 P2).
                    verify_held=lambda: self.verify_scale_build_lock(
                        notebook_id
                    ),
                )
            completed = True
            return manifest
        finally:
            if completed and on_completed is not None:
                on_completed()
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

        # Consumed INSIDE the connection scope: active_object_graph_rows
        # streams keyset pages (batch-3 W4 T-W4-3.1) rather than one
        # whole-table fetchall.
        with self.projections.connect() as db:
            nodes = [
                {
                    "id": row["id"],
                    "object_type": row["object_type"],
                    "payload": {"name": row["name"] or ""},
                }
                for row in self.projections.active_object_graph_rows(
                    db, notebook_id
                )
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
        # P2, codex PR#643 R12: the viz root is published through staging +
        # swap like every other root, under this build's claim. The two hooks
        # are the SAME ones ``build``/``fold`` already use — retargeted by
        # ``ScaleArtifactRuntime`` to read whichever claim is registered for
        # this notebook, which for a standalone viz rebuild is the one
        # ``ScaleArtifactRuntime.build_viz`` took before calling this. A
        # builder used directly (no runtime, no claim) keeps the defaults: a
        # fresh random staging token and a verification that always passes.
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
            claim_token=self.scale_build_claim_token(notebook_id),
            verify_held=lambda: self.verify_scale_build_lock(notebook_id),
        )
        self.cache_viz(notebook_id, self.artifacts.load_viz(notebook_id))
        return manifest
