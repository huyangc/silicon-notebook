"""Runtime owner for scale/viz artifacts, caches and scheduling state.

This service composes the Task-18 projection/catalog and Task-19 builder
objects without recreating either.  It is the single owner of every mutable
process-local scale/viz state object; the repository facade only exposes
write-through compatibility properties and thin method delegates.
"""
from __future__ import annotations

import datetime
import threading
import time
import weakref
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from app.repositories.scale_build_lock import (
    UNSUPPORTED_SCALE_BUILD_LOCK,
    ScaleBuildAlreadyBuilding,
    ScaleBuildBusy,
    ScaleBuildLock,
)


# Outcome of one admission attempt in ``_admit_scale_op`` — the three cases a
# caller must be able to tell apart in order to report an HONEST status:
#   started : a worker thread now owns this notebook's build/fold.
#   queued  : nothing started, but this call left a DURABLE idle entry behind,
#             so the off-peak scheduler will pick the work up on a later tick.
#   refused : nothing started and this call queued nothing — the work is gone
#             unless some *other* entry (created before this call) exists.
# ``_run_scale_op`` keeps the historical bool contract (started or not).
_SCALE_OP_STARTED = "started"
_SCALE_OP_QUEUED = "queued"
_SCALE_OP_REFUSED = "refused"

# Bound for the per-notebook failure/backoff map. Best-effort LRU by last
# failure: an evicted entry only means that notebook's backoff ends early —
# the direction is safe (an extra retry, never a skipped build), and the map
# holds one tiny tuple per notebook, so the ceiling exists to stop unbounded
# growth on a long-lived process, not to be precise.
_SCALE_FAILURE_STATE_MAX = 512


def _utc_now_iso() -> str:
    """UTC ISO8601 stamp for idle-queue entries — style aligned with the
    manifest ``built_at`` stamp (``isoformat(timespec="microseconds")``), but
    fixed to UTC so the frontend renders it in the browser's local timezone
    rather than the server's."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="microseconds"
    )


def offpeak_window_state(
    now: datetime.datetime, start_hour: int, end_hour: int
) -> tuple[bool, Optional[datetime.datetime]]:
    """Single source of truth for the off-peak window judgement.

    ``now`` must be a timezone-aware local time (callers pass
    ``datetime.datetime.now().astimezone()``). The in-window predicate is
    logically identical to the inline check this replaces in
    ``_process_idle_queue`` (``start_hour == end_hour`` is deliberately
    always out-of-window). When out of window, returns the next local
    datetime at which the window opens (today's ``start_hour`` if that is
    still ahead of ``now``, otherwise tomorrow's).

    Out-of-range ``start_hour``/``end_hour`` (not in ``0..23``) fail open to
    ``(False, None)`` rather than raising: this feeds the ``/scale-index/status``
    read path, and a misconfigured deployment env var must not turn a status
    poll into a 500 — it should just look like "not currently queued for an
    off-peak window" (the pre-transparency behaviour was silently never
    draining the idle queue, which this matches). ``start_hour == end_hour``
    is the same "always out of window" case as above, but is reported as
    ``(False, None)`` too — a window that never opens has no meaningful next
    start time to promise the caller.

    This function works in whatever fixed UTC-offset ``now.tzinfo`` carries
    (deployments target a single fixed local offset) and does not observe
    daylight-saving transitions; that is a deliberate simplification, not a
    bug, for a deployment target without DST.
    """
    if not (0 <= start_hour <= 23) or not (0 <= end_hour <= 23):
        return False, None
    if start_hour == end_hour:
        return False, None
    hour = now.hour
    in_window = (
        start_hour <= hour < end_hour
        if start_hour <= end_hour
        else hour >= start_hour or hour < end_hour
    )
    if in_window:
        return True, None
    candidate = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + datetime.timedelta(days=1)
    return False, candidate


class ScaleArtifactRuntime:
    def __init__(
        self,
        *,
        settings,
        event_log,
        projections,
        artifacts,
        catalog,
        builder,
        scale_cache,
        viz_cache,
        version_memo,
        version_lock,
        version_locks,
        load_lock,
        load_locks,
        building,
        building_lock,
        idle_queue,
        scheduler_started: bool,
        auto_index_checked,
        viz_building,
        viz_building_lock,
        notebooks,
        facts_repo,
        copy_stats_memo,
        require_indexing_write: Callable[[str], None] = lambda _notebook_id: None,
        scale_build_lock: Callable[[str], Optional[ScaleBuildLock]] | None = None,
    ) -> None:
        self.settings = settings
        self.event_log = event_log
        self.projections = projections
        self.artifacts = artifacts
        self.catalog = catalog
        self.builder = builder

        # These are the pre-existing objects, transferred by identity.  There
        # is deliberately no fallback allocation here.
        self.scale_cache = scale_cache
        self.viz_cache = viz_cache
        self.version_memo = version_memo
        self.version_lock = version_lock
        self.version_locks = version_locks
        self.load_lock = load_lock
        self.load_locks = load_locks
        self.building = building
        self.building_lock = building_lock
        self.idle_queue = idle_queue
        self.scheduler_started = scheduler_started
        self.auto_index_checked = auto_index_checked
        self.viz_building = viz_building
        self.viz_building_lock = viz_building_lock

        self.notebooks = notebooks
        self.facts_repo = facts_repo
        # runtime-owned copy-stats memo(codex PR#634 R2 P2-2)。此前这里是整份
        # ``snapshots``(RetrievalSnapshotCache),R2-2 之后它唯一的读者消失、被删;
        # 现在只注入真正被消费的那一件东西 —— 下面每次现构造的 Profile 需要它,
        # 而 Profile 自己不许私建(私建 = 完全没有缓存)。
        self.copy_stats_memo = copy_stats_memo
        # Z5: process-wide admission ceiling for scale build/fold execution.
        # Each notebook's build was previously a bare daemon thread with no
        # cross-notebook cap — an off-peak scheduler tick draining a long idle
        # queue, or several notebooks publishing at once, could start dozens
        # of concurrent builds and exhaust memory/CPU on the same box.
        #
        # The ticket is taken by the ADMITTING thread, non-blockingly, and is
        # released by the worker it hands the ticket to. A blocking acquire
        # inside the spawned worker would have capped only how many builds
        # EXECUTE while letting every queued notebook hold a parked daemon
        # thread — a 20-entry off-peak drain, or repeated cancel+retrigger,
        # would still exhaust threads/memory (codex PR#627 R1 P1). So work
        # that cannot get a ticket is parked as DATA (``_scale_pending`` /
        # ``idle_queue``), never as a thread: the live thread count is bounded
        # by this semaphore's capacity.
        self._scale_build_semaphore = threading.BoundedSemaphore(
            max(1, int(getattr(settings, "scale_build_concurrency", 2)))
        )
        # Notebooks admitted for IMMEDIATE execution that lost the race for a
        # concurrency slot: no thread, no ``building`` claim, just a record of
        # "run this as soon as a slot frees". Same entry shape as
        # ``idle_queue`` (mode, queued_at_iso) and guarded by the same
        # ``building_lock``, but a different promise: the idle queue waits for
        # the off-peak window, this one only for a slot, so it is drained by
        # the completion handoff (``_handoff_free_slot``) and by every
        # scheduler tick regardless of the window.
        self._scale_pending: dict[str, tuple[str, str]] = {}
        # Test seam for the backoff clock (monotonic, immune to wall-clock
        # jumps). Patching the module-global ``time.monotonic`` would be
        # process-wide and would also skew unrelated worker threads.
        self._monotonic: Callable[[], float] = time.monotonic
        # Z5: per-notebook failure backoff so a notebook whose build keeps
        # failing does not get retried back-to-back by the scheduler/
        # publish-triggered follow-up, each attempt burning a concurrency slot
        # only to fail again. Maps notebook_id -> (consecutive_failures,
        # monotonic time before which an AUTOMATIC retry is refused). Manual
        # retries (the user explicitly pressing "rebuild now") are exempt —
        # see the ``manual`` parameter on ``_run_scale_op``.
        self._scale_failure_lock = threading.Lock()
        self._scale_failure_state: dict[str, tuple[int, float]] = {}
        # W-CLI: the per-notebook build claim that also excludes OTHER
        # processes (the offline build CLI, a second service replica). Backends
        # without one hand back the UNSUPPORTED sentinel, so this seam is never
        # a bare None at the call sites below.
        self._scale_build_lock = scale_build_lock
        # Claims held by in-flight builds in THIS process, so the swap step can
        # re-verify the very handle its build was admitted on. Deliberately not
        # guarded by ``building_lock``: the fold path re-verifies from inside a
        # ``with self.building_lock`` block, and reusing that lock here would
        # deadlock the swap it is supposed to protect.
        self._scale_lock_handles_lock = threading.Lock()
        self._scale_build_lock_handles: dict[str, ScaleBuildLock] = {}
        if getattr(require_indexing_write, "__self__", None) is not None:
            self._require_indexing_write_ref = weakref.WeakMethod(
                require_indexing_write
            )
            self._require_indexing_write_fn = None
        else:
            self._require_indexing_write_ref = None
            self._require_indexing_write_fn = require_indexing_write
        self._lifecycle_ref: weakref.ReferenceType | None = None

        # Retarget Task 18/19 to this owner's canonical state and methods.
        self.catalog.version = self.version
        self.catalog.scale_cache = lambda: self.scale_cache
        self.catalog.load_lock = lambda: self.load_lock
        self.catalog.load_locks = lambda: self.load_locks
        self.builder.version = self.version
        self.builder.load_scale = lambda notebook_id: self.load(
            notebook_id, allow_stale=True
        )
        self.builder.invalidate_scale_cache = (
            lambda notebook_id: self.scale_cache.pop(notebook_id, None)
        )
        self.builder.cache_viz = self._cache_viz
        self.builder.building = self.building
        self.builder.building_lock = self.building_lock
        self.builder.notify_index_done = self.notify_index_done
        self.builder.verify_scale_build_lock = self.verify_scale_build_lock

    def get_notebook(self, notebook_id: str):
        return self.notebooks.get_notebook(notebook_id)

    def require_indexing_write(self, notebook_id: str) -> None:
        callback = self._require_indexing_write_fn
        if self._require_indexing_write_ref is not None:
            callback = self._require_indexing_write_ref()
        if callback is None:
            # A detached runtime cannot safely authorize a new writer.
            raise RuntimeError("indexing write admission is unavailable")
        callback(notebook_id)

    @property
    def lifecycle(self):
        return self._lifecycle_ref() if self._lifecycle_ref is not None else None

    @lifecycle.setter
    def lifecycle(self, value) -> None:
        self._lifecycle_ref = weakref.ref(value) if value is not None else None

    def notebook_copy_stats(self, notebook_id: str) -> dict:
        from app.services.notebook_scale import NotebookScaleProfile

        return NotebookScaleProfile(
            self.settings,
            self.facts_repo,
            lambda current: tuple(self.version(current)),
            self.copy_stats_memo,
        ).copy_stats(notebook_id)

    def set_building(self, value) -> None:
        self.building = value
        self.builder.building = value

    def set_building_lock(self, value) -> None:
        self.building_lock = value
        self.builder.building_lock = value

    def set_scheduler_started(self, value) -> None:
        self.scheduler_started = bool(value)

    def dequeue_idle(self, notebook_id: str) -> bool:
        with self.building_lock:
            return self.idle_queue.pop(notebook_id, None) is not None

    def build_viz_graph_arrays(self, notebook_id: str):
        from app.services.kg.viz_index import arrays_from_graph

        return arrays_from_graph(self.lifecycle._unified_graph_full(notebook_id, "object"))

    @staticmethod
    def viz_arrays_from_graph(full: dict):
        from app.services.kg.viz_index import arrays_from_graph

        return arrays_from_graph(full)

    def eligible(
        self,
        notebook_id: str,
        *,
        tier: str | None = None,
        exists: bool | None = None,
        total_chunks: int | None = None,
    ) -> bool:
        if tier is None:
            tier = self.get_notebook(notebook_id).tier
        if exists is None:
            exists = (
                self.artifacts.scale_dir(notebook_id) / "manifest.json"
            ).exists()
        if tier == "base" or exists or self._is_mounted_by_anyone(notebook_id):
            return True
        if total_chunks is None:
            total_chunks = self.projections.total_chunk_count(notebook_id)
        if total_chunks > self.settings.index_suggest_chunk_threshold:
            return True
        return not self.notebook_copy_stats(notebook_id)["copyable"]

    def _is_mounted_by_anyone(self, notebook_id: str) -> bool:
        """被任何笔记本当作参考库挂着 —— 本身即构成建索引资格。否则挂一个大的个人
        笔记本会因为没有 scale 索引而在 PPR 侧被大库守卫拒绝(返回空),静默失效。
        NotebookScaleProfile.index_eligible 是刻意的镜像实现,两处必须保持一致
        (见 IndexProjectionStore.is_mounted_by_anyone 的 docstring)。"""
        return self.projections.is_mounted_by_anyone(notebook_id)

    def _resolve_index_owner(self, notebook_id: str) -> str | None:
        from app.core.request_context import request_user_id

        user_id = request_user_id()
        if user_id:
            return user_id
        try:
            return self.projections.notebook_owner(notebook_id)
        except Exception:  # noqa: BLE001 - notification is fail-open
            return None

    def _notebook_name(self, notebook_id: str) -> str:
        try:
            return self.projections.notebook_name(notebook_id)
        except Exception:  # noqa: BLE001 - notification is fail-open
            return ""

    def notify_index_done(self, notebook_id: str) -> None:
        try:
            from app.services.pending_bus import pending_bus

            user_id = self._resolve_index_owner(notebook_id)
            if not user_id:
                return
            pending_bus.mark_dirty(user_id)
            pending_bus.emit(
                user_id,
                {
                    "event": "index_done",
                    "notebook_id": notebook_id,
                    "notebook_name": self._notebook_name(notebook_id),
                },
            )
        except Exception:  # noqa: BLE001 - notification is fail-open
            try:
                self.event_log.logger.exception(
                    "index_done notify failed for %s", notebook_id
                )
            except Exception:
                pass

    def unified_status(self, notebook_id: str) -> dict:
        lifecycle = self.lifecycle
        if lifecycle is None:
            raise RuntimeError("knowledge lifecycle is not wired")
        return lifecycle.unified_kg_status(notebook_id)

    def _cache_viz(self, notebook_id: str, index: Any) -> None:
        self.viz_cache[notebook_id] = index

    # ------------------------------ version/read catalog

    def version(self, notebook_id: str) -> list:
        from app.services.kg.edge_schema import EDGE_SCHEMA_VERSION

        seq, cseq, settings_tail = self.projections.version_signal(notebook_id)
        cached = self.version_memo.get(notebook_id)
        if (
            cached is not None
            and cached[0] == seq
            and cached[1] == cseq
            and cached[2] == settings_tail
        ):
            return list(cached[3])

        with self.version_lock:
            nb_lock = self.version_locks.get(notebook_id)
            if nb_lock is None:
                nb_lock = threading.Lock()
                self.version_locks[notebook_id] = nb_lock

        with nb_lock:
            seq, cseq, settings_tail = self.projections.version_signal(notebook_id)
            cached = self.version_memo.get(notebook_id)
            if (
                cached is not None
                and cached[0] == seq
                and cached[1] == cseq
                and cached[2] == settings_tail
            ):
                return list(cached[3])
            version = (
                self.projections.version_facts(notebook_id)
                + list(settings_tail)
                + ["edge_schema", EDGE_SCHEMA_VERSION]
            )
            self.version_memo[notebook_id] = (
                seq,
                cseq,
                settings_tail,
                list(version),
            )
            return version

    def load(self, notebook_id: str, allow_stale: bool = False):
        return self.catalog.load(notebook_id, allow_stale=allow_stale)

    def open_ann(self, index, kind: str):
        return self.catalog.open_ann(index, kind)

    def _prepare_ppr_core(self, index) -> bool:
        """Prepare only the reusable, single-index PPR substrate.

        Cross-notebook combined graphs are deliberately *not* materialized here:
        the legacy composition path copies multi-million-entry Python maps and
        can transiently reconstruct every edge.  Eagerly doing that for every
        mount would turn readiness into an OOM hazard.  The safe reusable pieces
        are attached to the ScaleIndex itself, whose dedicated LRU is capacity-
        checked by startup: a configured float32 CSR and the chunk-id set.  The
        self-only fast path can then rebuild its tiny wrapper in O(1), even if a
        shared VectorCache entry is later evicted.
        """
        if not self.settings.graph_ppr_enabled:
            return False
        prepare_key = "f32" if self.settings.ppr_float32 else "native"
        if getattr(index, "_ppr_prepare_key", None) == prepare_key:
            return True
        transition = index.transition
        if self.settings.ppr_float32:
            import numpy as np

            transition = transition.astype(np.float32, copy=False)
        setattr(index, "_ppr_transition", transition)
        setattr(
            index,
            "_ppr_chunk_ids",
            {
                index.node_ids[int(position)]
                for position in index.chunk_index
                if 0 <= int(position) < len(index.node_ids)
            },
        )
        # Publish the idempotence marker last so a concurrent reader never
        # treats a partially prepared core as complete.
        setattr(index, "_ppr_prepare_key", prepare_key)
        return True

    def _preload_one_retrieval_index(self, notebook_id: str) -> dict[str, int]:
        index = self.load(notebook_id, allow_stale=True)
        if index is None:
            raise RuntimeError("published scale index is not loadable")

        ann_handles = 0
        ann_specs = [
            ("kg", None, index.ann_labels, True),
            (
                "chunk",
                "has_chunk_ann",
                index.chunk_ann_labels,
                self.settings.chunk_ann_enabled,
            ),
            (
                "relation",
                "has_relation_ann",
                index.relation_ann_labels,
                self.settings.relation_retrieval_enabled,
            ),
        ]
        for kind, manifest_flag, labels, enabled in ann_specs:
            if not enabled:
                continue
            if manifest_flag and index.manifest.get(manifest_flag) and labels is None:
                raise RuntimeError("declared scale ANN artifact is not loadable")
            if not labels:
                continue
            if self.open_ann(index, kind) is None:
                raise RuntimeError("required scale ANN artifact is not loadable")
            ann_handles += 1

        return {
            "ann_handles": ann_handles,
            "ppr_cores": int(self._prepare_ppr_core(index)),
        }

    def preload_retrieval_artifacts(
        self,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, int]:
        """Strictly preload every live notebook's published retrieval index.

        The scale-index LRU is a memory safety boundary.  "Preload all" would be
        dishonest if earlier entries were evicted while later ones loaded, so an
        undersized ``SCALE_IDX_CACHE_MAX`` fails startup before allocating the
        first large artifact.  Corrupt/missing required files likewise fail: the
        readiness gate must not claim the cold path is warm when it is not.

        Optional ANN families are opened only when their online retrieval feature
        is enabled.  KG ANN is always relevant to indexed KG/PPR retrieval.  The
        reusable single-index PPR substrate is prepared on the ScaleIndex itself;
        cross-notebook graph combinations stay lazy because eager full copies are
        not a memory-safe form of preload.
        """
        started = time.perf_counter()
        candidates = self.artifacts.indexed_notebook_ids()
        notebook_ids = [
            notebook_id
            for notebook_id in candidates
            if self.projections.notebook_tier(notebook_id) is not None
        ]
        total = len(notebook_ids)
        capacity = int(self.settings.scale_idx_cache_max)
        if total > capacity:
            self.event_log.logger.error(
                "startup scale preload refused: indexes=%d exceeds "
                "SCALE_IDX_CACHE_MAX=%d",
                total,
                capacity,
            )
            raise RuntimeError(
                "published scale-index count exceeds SCALE_IDX_CACHE_MAX"
            )

        # The scale cache has a second, byte-oriented admission rail for large
        # indexes.  Checking only ``max_entries`` is insufficient: loading a
        # third large index would evict the first one and still let this method
        # report a successful preload.  Read manifests before loading any
        # ScaleIndex so this guard does not transiently allocate ANN matrices.
        # Keep the estimator shared with LargeAwareLRUCache's classifier rather
        # than reproducing its row/dimension formula here.
        from app.services.kg.scale_index import estimated_ann_bytes
        from app.services.vector_index import resolve_runtime_dim

        runtime_dim = resolve_runtime_dim(self.settings) or self.settings.embed_dim
        large_count = 0
        for notebook_id in notebook_ids:
            manifest = self.artifacts.read_manifest(
                self.artifacts.scale_dir(notebook_id)
            )
            if manifest is None:
                # Preserve the strict readiness contract, while avoiding a
                # second, less actionable failure later in the load loop.
                raise RuntimeError("published scale-index manifest is not readable")
            if estimated_ann_bytes(manifest, runtime_dim) > int(
                self.settings.scale_idx_large_bytes
            ):
                large_count += 1
        large_capacity = int(self.settings.scale_idx_cache_max_large)
        if large_count > large_capacity:
            self.event_log.logger.error(
                "startup scale preload refused: large_indexes=%d exceeds "
                "SCALE_IDX_CACHE_MAX_LARGE=%d",
                large_count,
                large_capacity,
            )
            raise RuntimeError(
                "published large scale-index count exceeds "
                "SCALE_IDX_CACHE_MAX_LARGE"
            )

        ann_handles = 0
        ppr_cores = 0
        if progress is not None:
            progress(0, total)
        for done, notebook_id in enumerate(notebook_ids, start=1):
            item = self._preload_one_retrieval_index(notebook_id)
            ann_handles += item["ann_handles"]
            ppr_cores += item["ppr_cores"]
            if progress is not None:
                progress(done, total)

        self.event_log.emit({
            "kind": "startup_scale_preload",
            "status": "done",
            "indexes": total,
            "ann_handles": ann_handles,
            "ppr_cores": ppr_cores,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        })
        return {
            "indexes": total,
            "ann_handles": ann_handles,
            "ppr_cores": ppr_cores,
        }

    # ------------------------------ viz read/build

    def _start_daemon(self, name: str, target: Callable[[], None]) -> None:
        threading.Thread(target=target, name=name, daemon=True).start()

    def _spawn_viz_build(self, notebook_id: str) -> None:
        with self.viz_building_lock:
            if notebook_id in self.viz_building:
                return
            self.viz_building.add(notebook_id)

        def run() -> None:
            try:
                self.build_viz(notebook_id)
            except Exception:  # noqa: BLE001 - background task is fail-open
                try:
                    self.event_log.logger.exception(
                        "viz index build failed for %s", notebook_id
                    )
                except Exception:
                    pass
            finally:
                with self.viz_building_lock:
                    self.viz_building.discard(notebook_id)

        try:
            self._start_daemon(f"vizidx-{notebook_id}", run)
        except Exception:
            with self.viz_building_lock:
                self.viz_building.discard(notebook_id)
            raise

    def viz_index(self, notebook_id: str):
        scale = self.load(notebook_id)
        if scale is not None and getattr(scale, "viz_ids", None) is not None:
            # Scale-embedded viz: freshness rides on the scale index's own version
            # (load() returns it only when version-fresh). Intentionally NOT
            # cluster_seq-checked — the scale manifest carries no cluster_seq, and
            # for the large notebooks that hold a scale index a cluster rebuild
            # spans minutes so version_facts' MAX(created_at) advances and load()
            # already sees it stale (the same-second blind spot cluster_seq closes
            # is unreachable here — codex PR#356 r2 finding 2). The cluster_seq
            # check below governs the STANDALONE viz artifact, which is what a
            # notebook without a fresh scale index serves. cur_cseq is computed
            # only past this early return, so a scale-embedded serve pays no extra
            # version_signal read (finding 3).
            return scale
        cur = self.version(notebook_id)
        cur_cseq = int(self.projections.version_signal(notebook_id)[1])
        cached = self.viz_cache.get(notebook_id)
        if cached is not None and self._viz_manifest_fresh(cached.manifest, cur, cur_cseq):
            return cached
        index = self.artifacts.load_viz(notebook_id)
        if index is not None:
            if self._viz_manifest_fresh(index.manifest, cur, cur_cseq):
                self.viz_cache[notebook_id] = index
                return index
            self._spawn_viz_build(notebook_id)
            return index
        count = self.projections.effective_object_count(notebook_id)
        if int(count) <= self.settings.viz_sync_build_max_objects:
            self.build_viz(notebook_id)
            return self.viz_cache.get(notebook_id)
        self._spawn_viz_build(notebook_id)
        return None

    @staticmethod
    def _viz_manifest_fresh(manifest: dict, cur, cur_cseq: int) -> bool:
        """A persisted STANDALONE-viz manifest is fresh iff its coarse version
        matches AND its cluster_mutation_seq matches. cluster_seq guards a
        same-second, same-count cluster-only rewrite that version() (a version_facts
        memo with no cseq) cannot see, which would otherwise leave the standalone
        viz served as current forever (codex PR#356 r1 P1). Scale-EMBEDDED viz does
        not go through here — it inherits the scale index's version-only freshness
        by design (see viz_index). Manifests written before cluster_seq existed
        default to cur_cseq → version-only check, so a deploy never force-rebuilds
        every existing viz (no thundering herd; a bounded accepted staleness for a
        pre-cseq manifest whose version still matches after a same-second edit)."""
        return (
            manifest.get("version") == cur
            and int(manifest.get("cluster_seq", cur_cseq)) == cur_cseq
        )

    def viz_probe(self, notebook_id: str) -> dict:
        """Read persisted state only; this method never schedules a build."""
        scale = self.load(notebook_id)
        if scale is not None and getattr(scale, "viz_ids", None) is not None:
            # Scale-embedded viz: version-fresh by construction (load() gates on
            # version); cluster_seq is not consulted — see viz_index (finding 2).
            manifest = scale.manifest
            return {
                "viz_indexed": True,
                "viz_nodes": int(
                    manifest.get("n_viz_nodes", len(scale.viz_ids))
                ),
                # Fallback for a manifest written before n_viz_edges existed:
                # len() over the compact edge set is the same edge count the old
                # list-of-triples gave (VizEdgeSet defines __len__).
                "viz_edges": int(
                    manifest.get(
                        "n_viz_edges",
                        len(scale.viz_edges) if scale.viz_edges is not None else 0,
                    )
                ),
                "viz_stale": False,
            }
        index = self.artifacts.load_viz(notebook_id)
        if index is None:
            return {
                "viz_indexed": False,
                "viz_nodes": 0,
                "viz_edges": 0,
                "viz_stale": False,
            }
        # Standalone viz artifact: the cluster_seq check (and its version_signal
        # read) is paid ONLY here, not on every scale-embedded status poll (finding 3).
        cur = self.version(notebook_id)
        cur_cseq = int(self.projections.version_signal(notebook_id)[1])
        manifest = index.manifest
        fresh = self._viz_manifest_fresh(manifest, cur, cur_cseq)
        return {
            "viz_indexed": fresh,
            "viz_nodes": int(manifest.get("n_viz_nodes", 0)),
            "viz_edges": int(manifest.get("n_viz_edges", 0)),
            "viz_stale": not fresh,
        }

    # ------------------------------ cross-process build claim

    def _acquire_scale_build_lock(
        self, notebook_id: str
    ) -> Optional[ScaleBuildLock]:
        """Try this notebook's cross-process build claim; ``None`` = busy.

        Fails CLOSED. An unreachable or erroring lock backend cannot authorize
        a build: the whole point of the claim is that a second writer would
        publish over the same artifact directory, and a build admitted without
        one is exactly the race the claim exists to prevent. The caller's busy
        path leaves the durable queue entry alone, so the work returns on a
        later tick rather than being lost.
        """
        acquire = self._scale_build_lock
        if acquire is None:
            return UNSUPPORTED_SCALE_BUILD_LOCK
        try:
            return acquire(notebook_id)
        except Exception:  # noqa: BLE001 - an unusable lock means "not now"
            try:
                self.event_log.logger.exception(
                    "scale build lock probe failed for %s", notebook_id
                )
            except Exception:
                pass
            return None

    @staticmethod
    def _release_scale_build_handle(handle: Optional[ScaleBuildLock]) -> None:
        if handle is None:
            return
        try:
            handle.release()
        except Exception:  # noqa: BLE001 - release is best-effort by contract
            pass

    def _register_scale_build_lock(
        self, notebook_id: str, handle: ScaleBuildLock
    ) -> None:
        """Publish the handle the swap step will re-verify against.

        Must happen BEFORE the worker that owns the handle can run, or a build
        could reach its swap with no claim registered and re-verify nothing.
        """
        with self._scale_lock_handles_lock:
            self._scale_build_lock_handles[notebook_id] = handle

    def _discard_scale_build_lock(self, notebook_id: str) -> None:
        """Unregister and release this notebook's claim (idempotent)."""
        with self._scale_lock_handles_lock:
            handle = self._scale_build_lock_handles.pop(notebook_id, None)
        self._release_scale_build_handle(handle)

    def verify_scale_build_lock(self, notebook_id: str) -> bool:
        """Whether this notebook's registered claim is still provably held.

        Wired into the builder and consulted by the artifact store immediately
        before the swap. No registered claim means this build never took one
        (an unwired builder used directly in a test); there is nothing to
        re-verify and nothing that could have been lost.
        """
        with self._scale_lock_handles_lock:
            handle = self._scale_build_lock_handles.get(notebook_id)
        if handle is None:
            return True
        try:
            return bool(handle.verify_held())
        except Exception:  # noqa: BLE001 - unverifiable == lost
            return False

    @contextmanager
    def _claim_scale_build(self, notebook_id: str) -> Iterator[None]:
        """Claim one notebook for a SYNCHRONOUS build/fold on this thread.

        Closes the historical gap where ``build()`` claimed nothing at all and
        ``fold()`` claimed only in-process: the facade's ``build_scale_index``
        (offline batch ingest) now excludes both the service's own workers and
        any other process. The two claims are taken cross-process first so a
        holder in this very process is reported by the same code path as a
        holder elsewhere.
        """
        handle = self._acquire_scale_build_lock(notebook_id)
        if handle is None:
            raise ScaleBuildBusy(
                f"another process is building the scale index for {notebook_id}"
            )
        try:
            with self.building_lock:
                if notebook_id in self.building:
                    raise ScaleBuildAlreadyBuilding(
                        f"a scale index build for {notebook_id} is already running"
                    )
                self.building.add(notebook_id)
        except BaseException:
            self._release_scale_build_handle(handle)
            raise
        self._register_scale_build_lock(notebook_id, handle)
        try:
            yield
        finally:
            # Cross-process claim first: releasing it before the in-process one
            # means the only window another admission can see is "in-process
            # busy", which every caller already handles.
            self._discard_scale_build_lock(notebook_id)
            with self.building_lock:
                self.building.discard(notebook_id)

    def build(
        self,
        notebook_id: str,
        on_stage: Optional[Callable[[str, int], None]] = None,
        *,
        assume_locked: bool = False,
    ) -> dict:
        """``assume_locked`` is for the admitted worker, which already holds
        both claims (``_admit_scale_op`` took them and handed them over)."""
        if assume_locked:
            return self.builder.build(notebook_id, on_stage=on_stage)
        with self._claim_scale_build(notebook_id):
            return self.builder.build(notebook_id, on_stage=on_stage)

    def fold(self, notebook_id: str, assume_locked: bool = False) -> dict:
        if assume_locked:
            return self.builder.fold(notebook_id, assume_locked=True)
        try:
            with self._claim_scale_build(notebook_id):
                # The claim above IS this call's in-process mutex, so the
                # builder must not take a second (nested) one. That also moves
                # the completion notification the builder used to emit up here
                # — the same shape the admitted worker's tail already has.
                result = self.builder.fold(notebook_id, assume_locked=True)
        except ScaleBuildAlreadyBuilding:
            return {"status": "already_building"}
        self.notify_index_done(notebook_id)
        return result

    def build_viz(self, notebook_id: str) -> Optional[dict]:
        return self.builder.build_viz(notebook_id)

    # ------------------------------ status and scheduling

    def state_signature(self, notebook_id: str) -> tuple:
        """H7 体检 memo 的**廉价**失效键:变则 status() 的 state 可能变,但**不**跑 _index_delta 的
        全量 source-id 扫(codex P2:大库上每次 /checkup 都全量扫过期判定太贵)。构成——

        - ``version_signal``:数据变更信号(unified_kg_state 单行读)。**唯一的 chunk 写入者会 bump
          kg_mutation_seq**(见 knowledge_counts_cache 头注释),故新 chunk / KG 都在此反映;runtime_dim
          等 settings 也在其 tail 里 → 覆盖 delta_over 的 chunk 侧与 dim_stale。
        - manifest 的 ``(exists, mtime_ns)``:捕获任何 manifest 重写——build / fold / rebuild(**即便
          version 串不变**:rebuild 刻意保持 kg_mutation_seq 稳定却换了 watermark_sources,单靠 seq 会
          漏这次 delta 归零)、以及 .tmp+swap 原子换目录。
        - ``building`` / ``queued``:纯内存态转换(无 DB 信号),build 起止直接改这两个集合。

        盲区仅剩「不 bump seq 的 embedding 变更改 version_facts」——但 status() 自身的 version() 也
        memo 在 version_signal 上、**同一盲区**,故本键不会比 status() 更陈旧。异常由调用方(Checkup
        service)fail-soft 兜住,这里不吞。"""
        out_dir = self.artifacts.scale_dir(notebook_id)
        try:
            manifest_ident = (True, (out_dir / "manifest.json").stat().st_mtime_ns)
        except OSError:
            manifest_ident = (False, 0)  # 不存在/取不到 → 未建索引态(H7=0)
        return (
            tuple(self.projections.version_signal(notebook_id)),
            manifest_ident,
            notebook_id in self.building,
            # 等 slot 的库既不在 building 也不在 idle_queue,少了这一位,「未建索引」
            # 与「已排队等 slot」在体检 memo 上无从分辨(codex batch-0 Z5 P2-3)。
            notebook_id in self._scale_pending,
            notebook_id in self.idle_queue,
        )

    def _queue_snapshot(self, notebook_id: str) -> tuple[int, int, str]:
        """1-based queue position, queue length and this entry's queued_at,
        all read under one lock acquisition so they describe the same
        instant (fail-open to position 0 if the entry raced out).

        Position is derived by sorting on the first-enqueue timestamp (tie
        broken by notebook id), NOT on dict insertion order: the worker-start
        failure path pops an entry and ``setdefault``-restores it at the end
        of the dict, which would silently move an insertion-order position
        while ``queued_at`` stayed anchored to the first enqueue (codex R4
        P2). Timestamps are uniform UTC ISO strings, so lexicographic order
        is chronological order."""
        with self.building_lock:
            entries = list(self.idle_queue.items())
        return self._entry_snapshot(entries, notebook_id)

    def _pending_snapshot(self, notebook_id: str) -> tuple[int, int, str]:
        """The same triple over ``_scale_pending``.

        Two distinct queues that share the entry shape and the display shape:
        the idle queue waits for the off-peak window, this one for a
        concurrency slot. Position describes arrival order — the slot handoff
        walks the pending map in that order, but a concurrent admission can
        still take a freed slot first, so it is an estimate, exactly like the
        idle queue's.
        """
        with self.building_lock:
            entries = list(self._scale_pending.items())
        return self._entry_snapshot(entries, notebook_id)

    @staticmethod
    def _entry_snapshot(
        entries: list[tuple[str, tuple[str, str]]], notebook_id: str
    ) -> tuple[int, int, str]:
        ordered = sorted(entries, key=lambda item: (item[1][1], item[0]))
        for index, (entry_id, entry) in enumerate(ordered):
            if entry_id == notebook_id:
                return index + 1, len(entries), entry[1]
        return 0, len(entries), ""

    def status(self, notebook_id: str) -> dict:
        # status() consumes ONLY the notebook's tier; read it with a cheap PK
        # query instead of rebuilding the full NotebookSummary (from_row's 5
        # subqueries). On notebook open this endpoint (/scale-index/status) fired
        # a SECOND from_row on top of GET /notebooks/{id}; this removes it.
        # Preserve get_notebook's missing-notebook contract (KeyError).
        tier = self.projections.notebook_tier(notebook_id)
        if tier is None:
            raise KeyError(notebook_id)
        out_dir = self.artifacts.scale_dir(notebook_id)
        # One coherent snapshot of the three membership tests: a notebook
        # parked for a concurrency slot has no worker and no claim, and must be
        # reported as queued — 18 notebooks behind a 2-slot ceiling all
        # claiming「构建中…」is a lie the user cannot even cancel (codex
        # batch-0 Z5 P2-3). Reading them under one lock also stops the
        # pending→building transition from momentarily looking like neither.
        with self.building_lock:
            building = notebook_id in self.building
            pending = notebook_id in self._scale_pending
            queued = pending or notebook_id in self.idle_queue
        exists = (out_dir / "manifest.json").exists()
        delta = self.builder._index_delta(notebook_id)
        total_chunks = self.projections.total_chunk_count(notebook_id)
        eligible = self.eligible(
            notebook_id,
            tier=tier,
            exists=exists,
            total_chunks=total_chunks,
        )
        delta_sources = list(delta["delta_sources"])
        result = {
            "exists": exists,
            "building": building,
            "eligible": eligible,
            "delta_chunks": int(delta["delta_chunks"]),
            "total_chunks": int(total_chunks),
            "unindexed_sources": len(
                self.projections.visible_source_ids(notebook_id, delta_sources)
            ),
            "has_unindexed_content": bool(delta_sources),
            "delta_searchable": bool(self.settings.scale_search_include_delta),
        }
        if building:
            result["state"] = "building"
        elif queued:
            result["state"] = "queued"
            if pending:
                # A slot waiter shares the queued SHAPE but not the off-peak
                # fields: it is waiting for a concurrency slot, not for the
                # window, and will start as soon as one frees. Omitting them
                # makes the client fall back to its neutral "将在服务器空闲时
                # 构建" wording, which is exactly what happens here.
                position, length, queued_at = self._pending_snapshot(notebook_id)
                result.update(
                    {
                        "queue_position": position,
                        "queue_length": length,
                        "queued_at": queued_at,
                    }
                )
            else:
                position, length, queued_at = self._queue_snapshot(notebook_id)
                in_window, next_start = offpeak_window_state(
                    datetime.datetime.now().astimezone(),
                    self.settings.scale_index_offpeak_start_hour,
                    self.settings.scale_index_offpeak_end_hour,
                )
                result.update(
                    {
                        "queue_position": position,
                        "queue_length": length,
                        "queued_at": queued_at,
                        "offpeak_in_window": in_window,
                        "offpeak_next_start_at": (
                            next_start.astimezone(
                                datetime.timezone.utc
                            ).isoformat(timespec="microseconds")
                            if next_start is not None
                            else ""
                        ),
                    }
                )
            if exists:
                # 排队更新的库(fold/rebuild 排在低峰)磁盘上已有上一版 manifest ——
                # 排队态恰是最需要「上次构建耗时」的地方,读取失败按现有损坏兜底口径
                # 处理(fail-open,字段缺省 0/空,由下面的公共默认块补上)。
                try:
                    prior_manifest = self.artifacts.read_manifest(out_dir)
                except Exception:  # noqa: BLE001 — 同上面损坏 manifest 的兜底口径
                    prior_manifest = None
                if prior_manifest is not None:
                    result["last_built_at"] = str(
                        prior_manifest.get("built_at", "")
                    )
                    result["last_build_ms"] = int(
                        prior_manifest.get("total_build_ms", 0)
                    )
        elif not exists:
            result["state"] = (
                "suggested"
                if total_chunks > self.settings.index_suggest_chunk_threshold
                else "unindexed"
            )
        else:
            try:
                manifest = self.artifacts.read_manifest(out_dir)
            except Exception:  # noqa: BLE001 — 损坏 manifest:read_manifest 刻意 raise,这里兜住
                manifest = None
            if manifest is None:
                # manifest 文件在、却读不出来(损坏)→ 索引不可用、须重建。**不把异常抛出
                # status()**:否则前端 /index-status 拿不到、「索引与构建」块卡在「加载中」,而
                # H8 重建 CTA 嵌在该块里就够不着——恰好在损坏(最需要重建)时修不了(codex P1)。
                # H8 由 /checkup 独立报「已损坏」;这里只保证状态可达、重建动作可发起
                # (承 P0:损坏返结构化结果、绝不 raise 进状态/热路径)。
                result["state"] = "stale"
                result.update({"stale": True, "stale_reason": "corrupt", "last_built_at": ""})
                return result
            version_stale = manifest.get("version") != self.version(notebook_id)
            delta_over = (
                delta["delta_chunks"] > self.settings.index_stale_delta_threshold
            )
            from app.services.vector_index import resolve_runtime_dim

            runtime_dim = resolve_runtime_dim(self.settings) or self.settings.embed_dim
            dim_stale = int(manifest.get("dim", runtime_dim)) != int(runtime_dim)
            result["state"] = (
                "stale" if (version_stale or delta_over or dim_stale) else "indexed"
            )
            if dim_stale:
                result["stale_reason"] = "dim_mismatch"
            result.update(
                {
                    "stale": bool(version_stale or delta_over or dim_stale),
                    "last_built_at": str(manifest.get("built_at", "")),
                    "last_build_ms": int(manifest.get("total_build_ms", 0)),
                    "manifest_dim": int(manifest.get("dim", 0)),
                    "runtime_dim": int(runtime_dim),
                    "n_nodes": int(manifest.get("n_nodes", 0)),
                    "n_chunks": int(manifest.get("n_chunks", 0)),
                    "n_ann": int(manifest.get("n_ann", 0)),
                    "n_chunk_ann": int(manifest.get("n_chunk_ann", 0)),
                    "has_chunk_ann": bool(manifest.get("has_chunk_ann", False)),
                }
            )
            return result
        # setdefault (not update): the queued branch above may already have
        # populated last_built_at/last_build_ms from a prior on-disk manifest
        # — this shared tail must not clobber that with the unindexed defaults.
        for key, default in (
            ("stale", False),
            ("last_built_at", ""),
            ("last_build_ms", 0),
            ("n_nodes", 0),
            ("n_chunks", 0),
            ("n_ann", 0),
            ("n_chunk_ann", 0),
            ("has_chunk_ann", False),
        ):
            result.setdefault(key, default)
        return result

    def index_status(self, notebook_id: str) -> dict:
        notebook = self.get_notebook(notebook_id)
        unified = self.unified_status(notebook_id)
        return {
            "kg": {
                "ready": bool(notebook.kg_ready),
                "building": bool(notebook.kg_building),
                "pending_sources": int(notebook.kg_pending_sources),
                "job": (
                    notebook.kg_build.model_dump(mode="json")
                    if notebook.kg_build
                    else None
                ),
            },
            "unified_kg": {
                "dirty": bool(unified.get("dirty", False)),
                "building": bool(unified.get("viz_building", False)),
                "last_rebuild_at": unified.get("last_rebuild_at", ""),
            },
            "scale_index": self.status(notebook_id),
        }

    def _resolve_mode(self, notebook_id: str, mode: str) -> str:
        if mode not in ("fold", "full"):
            mode = (
                "fold"
                if self.load(notebook_id, allow_stale=True) is not None
                else "full"
            )
        if mode != "fold":
            return mode
        index = self.load(notebook_id, allow_stale=True)
        if index is not None:
            from app.services.vector_index import resolve_runtime_dim

            runtime_dim = resolve_runtime_dim(self.settings) or self.settings.embed_dim
            if int(index.manifest.get("dim", runtime_dim)) != int(runtime_dim):
                return "full"
            built_at = str(index.manifest.get("built_at", ""))
            if built_at:
                last_rebuild = self.projections.unified_last_rebuild_at(notebook_id)
                if last_rebuild and last_rebuild > built_at:
                    return "full"
        try:
            if (
                len(self.builder._index_delta(notebook_id)["delta_sources"])
                > self.settings.scale_fold_max_delta_sources
            ):
                return "full"
        except Exception:  # noqa: BLE001 - probe stays fail-open
            pass
        return mode

    def _scale_backoff_active(self, notebook_id: str) -> bool:
        """Whether an AUTOMATIC retry for ``notebook_id`` is still backed off.

        Doubles as the reclamation point for long-dead entries: nothing else
        visits ``_scale_failure_state``, so a notebook that failed once and was
        never retried would otherwise keep its tuple until the process exits.

        The entry is NOT dropped the instant its window opens: the streak is
        what makes the backoff exponential, and forgetting it on plain expiry
        would silently degrade every escalation to a constant first-step delay
        (each retry would start again from streak 0) — exactly the back-to-back
        failure burn this mechanism exists to stop. It is dropped only once the
        entry has *also* been expired for a further full cap window, which is
        far longer than any legitimate retry gap (the scheduler polls every few
        minutes), so a genuinely consecutive failure still escalates.
        """
        now = self._monotonic()
        with self._scale_failure_lock:
            entry = self._scale_failure_state.get(notebook_id)
            if entry is None:
                return False
            _streak, retry_not_before = entry
            if now < retry_not_before:
                return True
            cap = max(
                1, int(self.settings.scale_build_failure_backoff_max_seconds)
            )
            if now >= retry_not_before + cap:
                self._scale_failure_state.pop(notebook_id, None)
            return False

    def _scale_record_success(self, notebook_id: str) -> None:
        with self._scale_failure_lock:
            self._scale_failure_state.pop(notebook_id, None)

    def _scale_record_failure(self, notebook_id: str) -> None:
        """Exponential backoff: doubles per consecutive failure, capped.

        Starts at ``scale_build_failure_backoff_seconds`` (default 60s) and
        never exceeds ``scale_build_failure_backoff_max_seconds`` (default
        1800s/30min). Only gates AUTOMATIC retries (see ``manual`` on
        ``_run_scale_op``) — a user pressing "rebuild now" is always admitted.

        The map is capped at ``_SCALE_FAILURE_STATE_MAX``: re-inserting the key
        refreshes its position, so the eviction order is "least recently
        failed" and the survivors are the notebooks whose backoff still means
        something.
        """
        with self._scale_failure_lock:
            streak, _ = self._scale_failure_state.pop(notebook_id, (0, 0.0))
            streak += 1
            base = max(1, int(self.settings.scale_build_failure_backoff_seconds))
            cap = max(base, int(self.settings.scale_build_failure_backoff_max_seconds))
            delay = min(base * (2 ** (streak - 1)), cap)
            self._scale_failure_state[notebook_id] = (
                streak, self._monotonic() + delay
            )
            while len(self._scale_failure_state) > _SCALE_FAILURE_STATE_MAX:
                self._scale_failure_state.pop(
                    next(iter(self._scale_failure_state)), None
                )

    def _queue_full_followup(self, notebook_id: str) -> None:
        """Register/refresh the durable "rebuild fully once free" idle entry.

        Caller must hold ``building_lock``. The first enqueue time is kept on
        re-registration for the same reason ``trigger(when="idle")`` keeps it:
        queue position is anchored to the first enqueue (codex R3 P2).
        """
        prior = self.idle_queue.get(notebook_id)
        self.idle_queue[notebook_id] = (
            "full",
            prior[1] if prior is not None else _utc_now_iso(),
        )

    def _park_pending(self, notebook_id: str, mode: str) -> None:
        """Park an immediate request that could not get a concurrency slot.

        Caller must hold ``building_lock``. Re-parking updates the mode (the
        newer request wins, as in ``trigger(when="idle")``) and keeps the
        original stamp so the handoff order stays anchored to first arrival.
        """
        prior = self._scale_pending.get(notebook_id)
        self._scale_pending[notebook_id] = (
            mode,
            prior[1] if prior is not None else _utc_now_iso(),
        )

    def _slot_available(self) -> bool:
        """Peek at the concurrency ceiling without committing to a build.

        Used to keep a drain from paying a full admission check (a DB read)
        per parked notebook when nothing can start anyway. Losing the peeked
        slot to a concurrent admission is harmless: that thread is starting
        the work this drain would have started.
        """
        if not self._scale_build_semaphore.acquire(blocking=False):
            return False
        self._scale_build_semaphore.release()
        return True

    def _run_scale_op(
        self,
        notebook_id: str,
        mode: str,
        *,
        supersede_idle: bool = False,
        claim_idle: bool = False,
        claim_pending: bool = False,
        queue_full_if_busy: bool = False,
        manual: bool = False,
    ) -> bool:
        """Whether this call STARTED an operation (see ``_admit_scale_op``)."""
        return (
            self._admit_scale_op(
                notebook_id,
                mode,
                supersede_idle=supersede_idle,
                claim_idle=claim_idle,
                claim_pending=claim_pending,
                queue_full_if_busy=queue_full_if_busy,
                manual=manual,
            )
            == _SCALE_OP_STARTED
        )

    def _admit_scale_op(
        self,
        notebook_id: str,
        mode: str,
        *,
        supersede_idle: bool = False,
        claim_idle: bool = False,
        claim_pending: bool = False,
        queue_full_if_busy: bool = False,
        manual: bool = False,
    ) -> str:
        """Claim and launch one scale-index operation.

        Returns ``_SCALE_OP_STARTED`` / ``_SCALE_OP_QUEUED`` /
        ``_SCALE_OP_REFUSED``: callers that report a status to a user (see
        ``rebuild_after_publication``) must be able to distinguish "nothing
        started but the work is durably queued" from "nothing started and
        nothing is queued", because those are different promises.

        A manual ``when=now`` request supersedes an older off-peak request for
        the same notebook.  Removing that idle entry and claiming ``building``
        happen under the same lock so the status endpoint can never fall back
        to a stale ``queued`` state after the immediate build finishes.

        The scheduler uses ``claim_idle`` to consume only an operation it can
        start (``claim_pending`` is the same contract over the slot-parked
        map).  A notebook that is already building keeps its queued follow-up,
        and the queued mode is read while holding the claim lock so an updated
        request cannot be replaced by an older scheduler snapshot.

        Execution admission is a NON-BLOCKING ticket taken here, under the
        claim lock, and released by the worker this call spawns. No ticket
        means no thread: the request is parked in ``_scale_pending`` (or left
        in whichever queue it came from) and returns ``queued``. That is what
        keeps live worker threads bounded by ``scale_build_concurrency``
        instead of by the queue length (codex PR#627 R1 P1).

        An idle request created *after* a claim is deliberately preserved:
        it may represent content committed while the current generation is
        building.  Likewise, an already-running build keeps its queued
        follow-up because this call did not actually start a replacement.

        ``manual=True`` (only the user's explicit "rebuild now" request, via
        ``trigger``) is exempt from the failure backoff check below and is
        always admitted immediately; every other caller (the off-peak
        scheduler, the post-publish follow-up, the completion-tail coalesced
        follow-up) is an AUTOMATIC retry and is refused while a prior failure
        for this notebook is still inside its backoff window (see
        ``_scale_record_failure``).

        The backoff gates EXECUTION, never QUEUEING. Refusing before claiming
        ``building`` or consuming an idle entry leaves durable queued work
        untouched, so the scheduler simply retries once the window opens. By
        the same argument a caller that would have queued a follow-up
        (``queue_full_if_busy``) must still get its entry recorded here:
        publication-triggered rebuilds own no other durable record, so
        swallowing the registration would strand the newly published
        generation until the next unrelated write — with the HTTP layer
        cheerfully reporting ``queued_followup`` for a queue that has no such
        entry (codex batch-0 Z5 P1-1).
        """
        # The admission check belongs at the runtime claim boundary, not only
        # at HTTP/facade entry points: startup idle drains, automatic folds and
        # daemon follow-ups all converge here.  Check before consuming an idle
        # entry or claiming ``building`` so a pending/missing pipeline leaves
        # durable queued work untouched and starts no artifact writer.
        self.require_indexing_write(notebook_id)
        if not manual and self._scale_backoff_active(notebook_id):
            if not queue_full_if_busy:
                return _SCALE_OP_REFUSED
            with self.building_lock:
                self._queue_full_followup(notebook_id)
            # 与 trigger(when="idle") 对称:入列要推一次待办快照,否则铃铛里的
            # 「已排队」要等重连才出现(codex R5 P2)。busy 分支不推是因为该库此刻
            # 已经以「构建中」占着一个待办项。
            from app.services.pending_bus import publish_snapshot

            publish_snapshot(self._resolve_index_owner(notebook_id))
            return _SCALE_OP_QUEUED
        # W-CLI: the cross-process claim is probed BEFORE the concurrency
        # ticket, so a notebook an offline builder already owns never burns a
        # slot. It is non-blocking; failing it is indistinguishable from (and
        # handled exactly like) "this process is already building it".
        lock_handle = self._acquire_scale_build_lock(notebook_id)
        if lock_handle is None:
            return self._queue_for_busy_claim(
                notebook_id, queue_full_if_busy=queue_full_if_busy
            )
        return self._admit_claimed_scale_op(
            notebook_id,
            mode,
            lock_handle,
            supersede_idle=supersede_idle,
            claim_idle=claim_idle,
            claim_pending=claim_pending,
            queue_full_if_busy=queue_full_if_busy,
        )

    def _queue_for_busy_claim(
        self, notebook_id: str, *, queue_full_if_busy: bool
    ) -> str:
        """Outcome for a notebook whose build claim is held by someone else.

        Deliberately the same shape as the in-process ``building`` branch of
        ``_admit_claimed_scale_op``: work already parked in a queue stays
        exactly where it is, a caller that owes a durable follow-up gets one
        registered, and nothing else is touched. In particular no failure is
        recorded — an offline build that legitimately runs for 40 minutes must
        not push this notebook's automatic-retry backoff to its ceiling.

        No pending-bus snapshot is pushed either, mirroring that same branch:
        a busy notebook's queue entry surfaces with the next snapshot rather
        than inventing a notification the in-process case never sent.
        """
        if queue_full_if_busy:
            with self.building_lock:
                self._queue_full_followup(notebook_id)
            return _SCALE_OP_QUEUED
        return _SCALE_OP_REFUSED

    def _admit_claimed_scale_op(
        self,
        notebook_id: str,
        mode: str,
        lock_handle: ScaleBuildLock,
        *,
        supersede_idle: bool,
        claim_idle: bool,
        claim_pending: bool,
        queue_full_if_busy: bool,
    ) -> str:
        """Second half of admission, holding this notebook's build claim.

        Same ownership discipline as the concurrency ticket: from here on every
        exit either hands ``lock_handle`` to the worker (whose finally releases
        it) or releases it right here. The ``handed_off`` flag plus the finally
        below is what makes that literal instead of a rule to remember.
        """
        removed_idle_entry = None
        removed_pending_entry = None
        parked = False
        handed_off = False
        try:
            with self.building_lock:
                if notebook_id in self.building:
                    if queue_full_if_busy:
                        self._queue_full_followup(notebook_id)
                        return _SCALE_OP_QUEUED
                    return _SCALE_OP_REFUSED
                # Does the work this call claims to consume still exist? Checked
                # before taking a ticket so a stale drain cannot borrow one.
                if claim_idle and notebook_id not in self.idle_queue:
                    return _SCALE_OP_REFUSED
                if claim_pending and notebook_id not in self._scale_pending:
                    return _SCALE_OP_REFUSED
                if not self._scale_build_semaphore.acquire(blocking=False):
                    # Every slot is busy. Park the request as data — parking it
                    # as a blocked thread is what made the ceiling meaningless
                    # (codex PR#627 R1 P1). Work already parked in a queue stays
                    # exactly where it is; only a fresh immediate request needs a
                    # record of its own. A freed slot comes back to it through
                    # ``_handoff_free_slot`` / the scheduler tick.
                    if claim_idle or claim_pending:
                        # Already parked in a queue a drain is walking; leave it.
                        return _SCALE_OP_QUEUED
                    if supersede_idle:
                        # An immediate request outranks this notebook's own
                        # off-peak entry even when it has to wait for a slot: it
                        # will run strictly sooner, so no work is lost.
                        self.idle_queue.pop(notebook_id, None)
                    self._park_pending(notebook_id, mode)
                    parked = True
                else:
                    # A ticket is held from here on: every exit must either hand
                    # it to the worker (which releases it in its finally) or
                    # release it.
                    if claim_idle:
                        removed_idle_entry = self.idle_queue.pop(notebook_id)
                        mode = removed_idle_entry[0]
                    elif claim_pending:
                        removed_pending_entry = self._scale_pending.pop(
                            notebook_id
                        )
                        mode = removed_pending_entry[0]
                    elif supersede_idle:
                        removed_idle_entry = self.idle_queue.pop(
                            notebook_id, None
                        )
                    if removed_pending_entry is None:
                        # This request is about to run, so its own parked copy
                        # (if any) must not start a second time for the same
                        # notebook.
                        removed_pending_entry = self._scale_pending.pop(
                            notebook_id, None
                        )
                    self.building.add(notebook_id)
            # owner 复用完成通知那套解析,fail-open 在 publish_snapshot 内。
            from app.services.pending_bus import publish_snapshot

            if parked:
                # The completion handoff normally wakes parked work; the
                # scheduler is the floor under it if no worker tail ever runs
                # (hard-killed process, or a slot held by a build that outlives
                # this request).
                self._ensure_scheduler()
                # 与 trigger(when="idle") 对称:新出现的「已排队」项要立刻推给铃铛,
                # 否则要等重连才看得到(codex R5 P2)。
                publish_snapshot(self._resolve_index_owner(notebook_id))
                return _SCALE_OP_QUEUED
            # building 已登记后推一次待办快照,「索引构建中」项才会立刻出现在已连接
            # 的铃铛里;此前只有 notify_index_done 会刷新,运行期间要重连才看得到。
            publish_snapshot(self._resolve_index_owner(notebook_id))

            def run() -> None:
                # This worker owns the concurrency ticket AND the build claim
                # its admitting thread took; the finally below is the single
                # release site for both.
                succeeded = False
                try:
                    operation = self._resolve_mode(notebook_id, mode)
                    if operation == "fold":
                        self.fold(notebook_id, assume_locked=True)
                    else:
                        self.build(notebook_id, assume_locked=True)
                    succeeded = True
                except Exception:  # noqa: BLE001 - daemon is fail-open
                    try:
                        self.event_log.logger.exception(
                            "scale op failed for %s", notebook_id
                        )
                    except Exception:
                        pass
                finally:
                    self._scale_build_semaphore.release()
                    if succeeded:
                        self._scale_record_success(notebook_id)
                    else:
                        self._scale_record_failure(notebook_id)
                    # Cross-process claim before the in-process one: the only
                    # window another admission can observe is then "in-process
                    # busy", which every caller already handles, instead of
                    # "free here but still locked in the database".
                    self._discard_scale_build_lock(notebook_id)
                    with self.building_lock:
                        self.building.discard(notebook_id)
                    if succeeded:
                        self.notify_index_done(notebook_id)
                    # A corpus publication that landed while this build was
                    # running records a coalesced full follow-up. Claim it
                    # immediately rather than waiting for the off-peak
                    # scheduler; the completed artifact may span the old and
                    # new generations.
                    try:
                        self._run_scale_op(
                            notebook_id, "auto", claim_idle=True
                        )
                    except Exception:  # noqa: BLE001 - daemon tail is fail-open
                        try:
                            self.event_log.logger.exception(
                                "scale follow-up start failed for %s",
                                notebook_id,
                            )
                        except Exception:
                            pass
                    # The slot this worker just released is the only thing the
                    # parked queues are waiting for, and nothing else is
                    # watching for it — without this handoff a pending entry
                    # would sit until the next scheduler tick (or, outside the
                    # off-peak window, until some unrelated request arrived).
                    try:
                        self._handoff_free_slot()
                    except Exception:  # noqa: BLE001 - daemon tail is fail-open
                        try:
                            self.event_log.logger.exception(
                                "scale slot handoff failed after %s", notebook_id
                            )
                        except Exception:
                            pass

            # Registered BEFORE the worker can exist: a build must never reach
            # its swap with no claim published for the store to re-verify.
            self._register_scale_build_lock(notebook_id, lock_handle)
            handed_off = True
            try:
                self._start_daemon(f"scaleidx-{notebook_id}", run)
            except Exception:
                # The worker never got either of the two things this thread
                # took for it. Both go back, and so does every displaced queue
                # entry; note that no failure is RECORDED here — the build was
                # never attempted, so the backoff window must not move.
                handed_off = False
                with self._scale_lock_handles_lock:
                    self._scale_build_lock_handles.pop(notebook_id, None)
                self._scale_build_semaphore.release()
                with self.building_lock:
                    self.building.discard(notebook_id)
                    # Starting the immediate worker failed before it could do
                    # any work.  Restore the displaced requests unless a newer
                    # one was queued in the meantime.
                    if removed_idle_entry is not None:
                        self.idle_queue.setdefault(
                            notebook_id, removed_idle_entry
                        )
                    if removed_pending_entry is not None:
                        self._scale_pending.setdefault(
                            notebook_id, removed_pending_entry
                        )
                raise
            return _SCALE_OP_STARTED
        finally:
            if not handed_off:
                self._release_scale_build_handle(lock_handle)

    def rebuild_after_publication(self, notebook_id: str) -> dict:
        """Start a full build or coalesce one immediate post-build follow-up.

        The reported status is the admission outcome, never a guess: this
        notebook's newly published generation has no other durable record, so
        promising ``queued_followup`` without an idle entry behind it would be
        a silent data-freshness loss (codex batch-0 Z5 P1-1).
        """
        self.get_notebook(notebook_id)
        if not self.eligible(notebook_id):
            return {"status": "not_applicable", "notebook_id": notebook_id}
        outcome = self._admit_scale_op(
            notebook_id,
            "full",
            supersede_idle=True,
            queue_full_if_busy=True,
        )
        if outcome == _SCALE_OP_STARTED:
            return {"status": "building", "notebook_id": notebook_id}
        if outcome == _SCALE_OP_QUEUED:
            # Backup recovery if the current daemon/process does not reach its
            # immediate finally-tail; the normal path claims the entry first.
            self._ensure_scheduler()
            return {"status": "queued_followup", "notebook_id": notebook_id}
        # Defensive: with ``queue_full_if_busy`` every non-started path above
        # registers an entry, so this is unreachable today. It exists so a
        # future refusal reason cannot inherit the "it is queued" promise.
        self.event_log.logger.warning(
            "post-publication scale rebuild neither started nor queued for %s",
            notebook_id,
        )
        return {"status": "refused", "notebook_id": notebook_id}

    def _handoff_free_slot(self) -> None:
        """Hand the concurrency slot this worker just released to parked work.

        Admission never blocks a thread on the ceiling, so a freed slot has no
        thread waiting to grab it — this is the wake-up. Deliberately capped at
        ONE start: this tail freed exactly one slot, and the other slots have
        (or will have) tails of their own. Draining greedily here would let a
        single finishing worker spawn the entire queue back-to-back — bounded
        in *executing* builds, but not in live threads, which is the property
        this whole design exists to keep (codex PR#627 R1 P1).
        """
        if not self._slot_available():
            return
        self._process_idle_queue(force=False, limit=1)

    def _drain_pending_slots(self, limit: Optional[int] = None) -> int:
        """Start parked immediate work while slots remain; returns starts.

        Unlike the idle queue this ignores the off-peak window: these requests
        were admitted for immediate execution and only lost a race for a slot,
        so making them wait for 02:00 would be a different (and much slower)
        promise than the one their caller was given.
        """
        with self.building_lock:
            candidates = sorted(
                self._scale_pending.items(),
                key=lambda item: (item[1][1], item[0]),
            )
        started = 0
        for notebook_id, entry in candidates:
            if limit is not None and started >= limit:
                break
            # Re-checked every iteration: one start consumes the slot, and
            # stopping here keeps a full queue from paying an admission check
            # (a DB read) per parked notebook on every tick.
            if not self._slot_available():
                break
            try:
                if self._run_scale_op(notebook_id, entry[0], claim_pending=True):
                    started += 1
            except Exception:  # noqa: BLE001 - one item must not drain the tick
                try:
                    self.event_log.logger.exception(
                        "scale pending item failed for %s", notebook_id
                    )
                except Exception:
                    pass
        return started

    def _process_idle_queue(
        self, force: bool = False, limit: Optional[int] = None
    ) -> None:
        """Start as much queued work as the ceiling allows.

        ``limit`` caps how many operations this pass may start (the completion
        handoff passes 1 — see ``_handoff_free_slot``); the scheduler tick
        leaves it unset and fills every free slot.
        """
        # Slot-parked work drains on every tick: it is waiting for a slot, not
        # for the window (see ``_drain_pending_slots``).
        started = self._drain_pending_slots(limit=limit)
        if limit is not None and started >= limit:
            return
        if not force:
            in_window, _next_start = offpeak_window_state(
                datetime.datetime.now().astimezone(),
                self.settings.scale_index_offpeak_start_hour,
                self.settings.scale_index_offpeak_end_hour,
            )
            if not in_window:
                return
        with self.building_lock:
            queued_notebook_ids = list(self.idle_queue)
        for notebook_id in queued_notebook_ids:
            if not self._slot_available():
                return
            try:
                # Claim each entry independently.  Busy notebooks stay queued,
                # and one daemon-start failure restores that entry without
                # preventing the remaining notebooks from being considered.
                if self._run_scale_op(notebook_id, "auto", claim_idle=True):
                    started += 1
                    if limit is not None and started >= limit:
                        return
            except Exception:  # noqa: BLE001 - one item must not drain the tick
                try:
                    self.event_log.logger.exception(
                        "scale scheduler item failed for %s", notebook_id
                    )
                except Exception:
                    pass

    def _ensure_scheduler(self) -> None:
        with self.building_lock:
            if self.scheduler_started:
                return
            self.scheduler_started = True

        def loop() -> None:
            while True:
                time.sleep(
                    max(30, self.settings.scale_index_scheduler_poll_seconds)
                )
                try:
                    self._process_idle_queue(force=False)
                except Exception:  # noqa: BLE001 - scheduler is fail-open
                    try:
                        self.event_log.logger.exception(
                            "scale scheduler tick failed"
                        )
                    except Exception:
                        pass

        try:
            self._start_daemon("scaleidx-scheduler", loop)
        except Exception:
            with self.building_lock:
                self.scheduler_started = False
            raise

    def trigger(
        self,
        notebook_id: str,
        when: str = "now",
        mode: str = "auto",
        *,
        manual: bool = False,
    ) -> dict:
        """``manual=True`` is for the explicit, deliberate "rebuild now"
        entry points only (the HTTP rebuild endpoint and the MCP build tool
        both pass it) — it exempts this call from the failure backoff in
        ``_run_scale_op``. Internal policy-driven callers
        (``maybe_enqueue_fold``, ``maybe_auto_index``) leave it ``False``
        even when they resolve ``when="now"``, because they are automatic
        retries, not a person/agent asking once."""
        self.get_notebook(notebook_id)
        if not self.eligible(notebook_id):
            raise ValueError(
                "notebook too small and not base-tier; scale index not applicable"
            )
        if when == "idle":
            with self.building_lock:
                # 重复排队(常见于连续加来源触发 maybe_enqueue_fold)只更新 mode,
                # 保留首次入队时刻:dict 对既有 key 赋值不改插入序,位次锚定在首次
                # 入队,时间戳必须与它同锚点,否则「入队序位次 + 刷新的时间戳」自相
                # 矛盾(codex R3 P2)。
                prior = self.idle_queue.get(notebook_id)
                self.idle_queue[notebook_id] = (
                    mode,
                    prior[1] if prior is not None else _utc_now_iso(),
                )
            self._ensure_scheduler()
            # 入列也要推一次待办快照(镜像 _run_scale_op 对 building 的处理):
            # 已连接的铃铛靠 SSE 增量,不推的话「已排队」要等重连才出现(codex R5 P2)。
            from app.services.pending_bus import publish_snapshot

            publish_snapshot(self._resolve_index_owner(notebook_id))
            return {"status": "queued", "notebook_id": notebook_id}
        outcome = self._admit_scale_op(
            notebook_id,
            mode,
            supersede_idle=True,
            manual=manual,
        )
        # "queued" here means the ceiling was full and the request is parked
        # for the next free slot — the same word ``when="idle"`` already
        # returns, and the truth: reporting "already_building" would name a
        # build that is not running (nothing was claimed).
        return {
            "status": {
                _SCALE_OP_STARTED: "building",
                _SCALE_OP_QUEUED: "queued",
            }.get(outcome, "already_building"),
            "notebook_id": notebook_id,
        }

    def cancel(self, notebook_id: str) -> dict:
        """Stop this notebook's pending index work, if it has not started.

        Three cases: executing (not interruptible — the worker is inside the
        builder), parked for a concurrency slot, or queued for the off-peak
        window. The last two are plain records with no thread behind them, so
        cancelling is just dropping the record — nothing has been attempted,
        nothing needs to be woken, and no failure/backoff state is touched.
        """
        self.get_notebook(notebook_id)
        with self.building_lock:
            if notebook_id in self.building:
                building = True
                removed = False
            else:
                building = False
                # Drop both records: "cancel" means this notebook's pending
                # index work stops, and leaving either one would have the
                # scheduler silently start the very build the user cancelled.
                removed = self._scale_pending.pop(notebook_id, None) is not None
                removed = (
                    self.idle_queue.pop(notebook_id, None) is not None or removed
                )
        if building:
            return {
                "cancelled": False,
                "state": self.status(notebook_id)["state"],
                "reason": "building_not_interruptible",
            }
        if removed:
            # 出列同样推快照,否则铃铛里的「已排队」要等下一次无关快照才消失
            # (与入列的 publish 对称,codex R5 P2 的同类面)。
            from app.services.pending_bus import publish_snapshot

            publish_snapshot(self._resolve_index_owner(notebook_id))
        return {
            "cancelled": bool(removed),
            "state": self.status(notebook_id)["state"],
            "reason": "" if removed else "not_queued",
        }

    def maybe_enqueue_fold(self, notebook_id: str) -> None:
        if not self.settings.scale_auto_fold_on_add:
            return
        try:
            if self.load(notebook_id, allow_stale=True) is None:
                return
            self.trigger(notebook_id, when="idle", mode="fold")
        except Exception:
            self.event_log.logger.exception(
                "auto scale-fold enqueue failed for %s", notebook_id
            )

    def maybe_auto_index(self, notebook_id: str) -> None:
        if not self.settings.scale_index_auto_enabled:
            return
        if notebook_id in self.auto_index_checked:
            return
        if (
            notebook_id in self.building
            or notebook_id in self.idle_queue
            or notebook_id in self._scale_pending
        ):
            self.auto_index_checked.add(notebook_id)
            return
        try:
            stats = self.notebook_copy_stats(notebook_id)
            if stats["copyable"]:
                return
            if self.status(notebook_id)["state"] not in (
                "unindexed",
                "suggested",
                "stale",
            ):
                return
            try:
                self.trigger(
                    notebook_id,
                    when=self.settings.scale_index_auto_when,
                    mode="auto",
                )
            except Exception:  # noqa: BLE001 - automatic path is fail-open
                pass
        except Exception:  # noqa: BLE001 - retrieval fallback must not fail
            try:
                self.event_log.logger.exception(
                    "maybe_auto_index failed for %s", notebook_id
                )
            except Exception:
                pass
        finally:
            self.auto_index_checked.add(notebook_id)

    def rearm_auto_index(self, notebook_id: str) -> None:
        self.auto_index_checked.discard(notebook_id)
