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
from typing import Any, Callable, Optional


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
        snapshots,
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
        self.snapshots = snapshots
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

    def get_notebook(self, notebook_id: str):
        return self.notebooks.get_notebook(notebook_id)

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
            self.snapshots.vector_cache,
        ).copy_stats(notebook_id)

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
        if tier == "base" or exists:
            return True
        if total_chunks is None:
            total_chunks = self.projections.total_chunk_count(notebook_id)
        if total_chunks > self.settings.index_suggest_chunk_threshold:
            return True
        return not self.notebook_copy_stats(notebook_id)["copyable"]

    def _resolve_index_owner(self, notebook_id: str) -> str | None:
        from app.core.request_context import request_user_id

        user_id = request_user_id()
        if user_id:
            return user_id
        try:
            with self.projections.connect() as db:
                row = db.execute(
                    "SELECT created_by FROM notebooks WHERE id = ?",
                    (notebook_id,),
                ).fetchone()
            return row["created_by"] if row else None
        except Exception:  # noqa: BLE001 - notification is fail-open
            return None

    def _notebook_name(self, notebook_id: str) -> str:
        try:
            with self.projections.connect() as db:
                row = db.execute(
                    "SELECT name FROM notebooks WHERE id = ?", (notebook_id,)
                ).fetchone()
            return row["name"] if row else ""
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
            version = self.projections.version_facts(notebook_id) + list(settings_tail)
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
            return scale
        cur = self.version(notebook_id)
        cached = self.viz_cache.get(notebook_id)
        if cached is not None and cached.manifest.get("version") == cur:
            return cached
        index = self.artifacts.load_viz(notebook_id)
        if index is not None:
            if index.manifest.get("version") == cur:
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

    def viz_probe(self, notebook_id: str) -> dict:
        """Read persisted state only; this method never schedules a build."""
        cur = self.version(notebook_id)
        scale = self.load(notebook_id)
        if scale is not None and getattr(scale, "viz_ids", None) is not None:
            manifest = scale.manifest
            return {
                "viz_indexed": True,
                "viz_nodes": int(
                    manifest.get("n_viz_nodes", len(scale.viz_ids))
                ),
                "viz_edges": int(
                    manifest.get("n_viz_edges", len(scale.viz_edges or []))
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
        manifest = index.manifest
        fresh = manifest.get("version") == cur
        return {
            "viz_indexed": fresh,
            "viz_nodes": int(manifest.get("n_viz_nodes", 0)),
            "viz_edges": int(manifest.get("n_viz_edges", 0)),
            "viz_stale": not fresh,
        }

    def build(
        self,
        notebook_id: str,
        on_stage: Optional[Callable[[str, int], None]] = None,
    ) -> dict:
        return self.builder.build(notebook_id, on_stage=on_stage)

    def fold(self, notebook_id: str, assume_locked: bool = False) -> dict:
        return self.builder.fold(notebook_id, assume_locked=assume_locked)

    def build_viz(self, notebook_id: str) -> Optional[dict]:
        return self.builder.build_viz(notebook_id)

    # ------------------------------ status and scheduling

    def status(self, notebook_id: str) -> dict:
        notebook = self.get_notebook(notebook_id)
        out_dir = self.artifacts.scale_dir(notebook_id)
        building = notebook_id in self.building
        exists = (out_dir / "manifest.json").exists()
        delta = self.builder._index_delta(notebook_id)
        total_chunks = self.projections.total_chunk_count(notebook_id)
        eligible = self.eligible(
            notebook_id,
            tier=notebook.tier,
            exists=exists,
            total_chunks=total_chunks,
        )
        result = {
            "exists": exists,
            "building": building,
            "eligible": eligible,
            "delta_chunks": int(delta["delta_chunks"]),
            "total_chunks": int(total_chunks),
            "unindexed_sources": len(delta["delta_sources"]),
            "delta_searchable": bool(self.settings.scale_search_include_delta),
        }
        if building:
            result["state"] = "building"
        elif notebook_id in self.idle_queue:
            result["state"] = "queued"
        elif not exists:
            result["state"] = (
                "suggested"
                if total_chunks > self.settings.index_suggest_chunk_threshold
                else "unindexed"
            )
        else:
            manifest = self.artifacts.read_manifest(out_dir)
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
        result.update(
            {
                "stale": False,
                "last_built_at": "",
                "n_nodes": 0,
                "n_chunks": 0,
                "n_ann": 0,
                "n_chunk_ann": 0,
                "has_chunk_ann": False,
            }
        )
        return result

    def index_status(self, notebook_id: str) -> dict:
        notebook = self.get_notebook(notebook_id)
        unified = self.unified_status(notebook_id)
        return {
            "kg": {
                "ready": bool(notebook.kg_ready),
                "building": bool(notebook.kg_building),
                "pending_sources": int(notebook.kg_pending_sources),
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
                with self.projections.connect() as db:
                    row = db.execute(
                        "SELECT last_rebuild_at FROM unified_kg_state "
                        "WHERE notebook_id=?",
                        (notebook_id,),
                    ).fetchone()
                last_rebuild = (
                    str(row["last_rebuild_at"])
                    if row and row["last_rebuild_at"]
                    else ""
                )
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

    def _run_scale_op(self, notebook_id: str, mode: str) -> None:
        with self.building_lock:
            if notebook_id in self.building:
                return
            self.building.add(notebook_id)

        def run() -> None:
            succeeded = False
            try:
                operation = self._resolve_mode(notebook_id, mode)
                if operation == "fold":
                    self.fold(notebook_id, assume_locked=True)
                else:
                    self.build(notebook_id)
                succeeded = True
            except Exception:  # noqa: BLE001 - daemon is fail-open
                try:
                    self.event_log.logger.exception(
                        "scale op failed for %s", notebook_id
                    )
                except Exception:
                    pass
            finally:
                with self.building_lock:
                    self.building.discard(notebook_id)
                if succeeded:
                    self.notify_index_done(notebook_id)

        try:
            self._start_daemon(f"scaleidx-{notebook_id}", run)
        except Exception:
            with self.building_lock:
                self.building.discard(notebook_id)
            raise

    def _process_idle_queue(self, force: bool = False) -> None:
        if not force:
            hour = datetime.datetime.now().hour
            lower = self.settings.scale_index_offpeak_start_hour
            upper = self.settings.scale_index_offpeak_end_hour
            in_window = (
                lower <= hour < upper
                if lower <= upper
                else hour >= lower or hour < upper
            )
            if not in_window:
                return
        with self.building_lock:
            queued = dict(self.idle_queue)
            self.idle_queue.clear()
        for notebook_id, mode in queued.items():
            self._run_scale_op(notebook_id, mode)

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
        self, notebook_id: str, when: str = "now", mode: str = "auto"
    ) -> dict:
        self.get_notebook(notebook_id)
        if not self.eligible(notebook_id):
            raise ValueError(
                "notebook too small and not base-tier; scale index not applicable"
            )
        if when == "idle":
            with self.building_lock:
                self.idle_queue[notebook_id] = mode
            self._ensure_scheduler()
            return {"status": "queued", "notebook_id": notebook_id}
        with self.building_lock:
            already_building = notebook_id in self.building
        if already_building:
            return {"status": "already_building", "notebook_id": notebook_id}
        self._run_scale_op(notebook_id, mode)
        return {"status": "building", "notebook_id": notebook_id}

    def cancel(self, notebook_id: str) -> dict:
        self.get_notebook(notebook_id)
        with self.building_lock:
            if notebook_id in self.building:
                building = True
                removed = False
            else:
                building = False
                removed = self.idle_queue.pop(notebook_id, None) is not None
        if building:
            return {
                "cancelled": False,
                "state": self.status(notebook_id)["state"],
                "reason": "building_not_interruptible",
            }
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
        if notebook_id in self.building or notebook_id in self.idle_queue:
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
