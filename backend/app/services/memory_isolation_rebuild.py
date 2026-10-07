"""Post-upgrade isolated rebuild for ruling M1 (plan 2026-09-29 §3.2).

Migration PostgreSQL ``0067_memory_kg_isolation.sql`` / SQLite ``_migration_87``
removed every pre-isolation derived row it could identify deterministically,
set ``unified_kg_state.memory_isolation_version = 0`` on the notebooks that
held a Memory source (F), and 2 on every other notebook with clusters (G,
public libraries and copies included: "not yet checked"). What a migration
cannot do -- decide G in bounded pages, re-cluster, re-derive the canonical
relations, mention bridge, communities, visualization and scale index -- is
done here, once per notebook, after the service is ready.

Contract (each point is pinned by ``tests/test_memory_isolation_rebuild.py``):

* **After readiness, never before.** ``startup_warmup`` calls
  :meth:`MemoryIsolationRebuild.schedule` strictly after ``mark_ready()``, and
  :meth:`run_pass` itself refuses to start while the process is not ready.
* **One worker, the ordinary rebuild path.** One background job
  (``unifiedkg-memory-isolation``, the heavy maintenance pool) walks the
  pending notebooks in id order, one at a time. Each notebook goes through the
  exact path of ``POST /notebooks/{id}/unified-kg/rebuild``:
  ``start_unified_kg_rebuild`` (the per-notebook maintenance slot shared with
  relink and KG delete, so a user click and this worker never run two writers)
  then ``run_unified_kg_rebuild_job`` (``rebuild_unified_kg(force=False)``: the
  migration moved the notebook's ``kg_mutation_seq``, which is part of the
  cluster input version, so it genuinely re-clusters, and it chains canonical
  relations, mention bridge, communities, visualization and the automatic
  index refresh). No request user is bound: the job runs as the process's
  system context. A public library that holds a Memory source is rebuilt the
  same way; no manual step.
* **Seed check first.** A marker-2 notebook whose last Memory was deleted
  before the upgrade may still carry a cluster minted from it, or a
  description / community text written while it was a member. Each is checked
  (``seed_check_signal``, cheapest first: ``dirty`` -- the graph changed since
  its last rebuild; ``seed`` -- a published cluster whose name no member
  carries or whose encoded seed object is gone; ``stale_reference`` -- a
  mention row or community member naming an object that no longer exists; the
  last two in bounded pages): a signal -> the migration's reset, marker 0 and
  the merge candidates naming nothing that exists purged, in one transaction
  (rebuilt in the same pass); none -> 1. Event ``memory_isolation_seed_checked``
  (outcome queued / clean, plus the signal name).
* **Bridge candidates first.** Before a notebook's rebuild the merge
  candidates naming the bridge id of one of its Memory concepts
  (``kg_merge.purge_bridge_canonical_ids``: ``K-`` + the normalised name, which the
  migration's SQL cannot derive) are removed where no cluster carries that id,
  so no confirmed/rejected decision keyed on a Memory name feeds the shared
  clustering.
* **Pre-isolation scale indexes last.** When SCALE_INDEX_AUTO_ENABLED, every
  non-copyable notebook whose KG is isolated (marker 1) and whose published
  scale index predates the isolation
  (manifest without the isolation field: no reader serves it, E4-6) -- or
  that has only a standalone visualisation of that kind and more than
  VIZ_SYNC_BUILD_MAX_OBJECTS objects -- with or
  without Memory, any tier, gets a FULL build queued through the automatic
  path after the KG rebuilds (event ``memory_isolation_scale_queued``). No
  model call; one build per such notebook; a start re-queues only what is
  still unstamped. Copyable notebooks are rebuilt by the operator.
* **Marker only on success.** The marker goes to 1 only after the rebuild
  returned normally (``MemoryIsolationStore.mark_isolated``). The rebuild
  end-write ``unified_kg_store.finish_rebuild_state`` carries the same write
  (E4-2, same PR) so a manual rebuild clears it too; the skip path of an
  unchanged input never reaches that end-write, which is why this worker
  still marks. A failed notebook keeps 0 and is retried at the next start.
* **No slot held while waiting.** A notebook whose slot is busy (a
  user-started rebuild or relink) is deferred; the pass ends (releasing its
  heavy-pool slot -- waiting holds no slot at all) and a daemon timer
  re-submits a fresh pass after ``busy_retry_seconds``. The bound is
  ``busy_retry_rounds`` re-arms per SERVICE INSTANCE (i.e. per process start),
  not per notebook; after that the next start picks the rest up. A failure
  inside the timer's callback (e.g. the pending count cannot be read) is
  caught, reported as ``memory_isolation_rebuild_failed`` (stage ``rearm``)
  and re-arms again while rounds are left, else stops with a log line --
  never a silent break of the chain. Shutdown does not cancel a waiting
  timer; once readiness is withdrawn (``readiness.mark_stopped``) its
  :meth:`schedule` is a no-op.
* **Resumable.** Progress is the per-notebook marker itself; an interrupted
  pass resumes at the next start with whatever is still 0, and the rebuild's
  own checkpoints make a half-done notebook cheap to resume.
* **Content-free events.** ``memory_isolation_rebuild_started`` /
  ``_completed`` / ``_deferred`` / ``_failed`` and ``memory_isolation_seed_checked``
  carry the notebook id, a stage, outcome or signal name, a cluster count or an
  exception CLASS name -- never a message, a name
  or any text of the notebook.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List

from app.repositories.ports import KgMaintenanceAlreadyRunning

JOB_NAME = "unifiedkg-memory-isolation"
DEFAULT_BUSY_RETRY_SECONDS = 30.0
DEFAULT_BUSY_RETRY_ROUNDS = 20


def store_for_settings(settings: Any) -> Any:
    """The ruling-M1 marker store of the configured backend, chosen by the
    same rule as ``create_repository`` (the database URL's scheme)."""
    from app.core.database_url import database_identity

    scheme = database_identity(settings.database_url).scheme
    if scheme == "postgresql":
        from app.repositories.postgres.memory_isolation_store import (
            MemoryIsolationStore,
        )
    elif scheme == "sqlite":
        from app.repositories.sqlite.memory_isolation_store import (
            MemoryIsolationStore,
        )
    else:
        raise ValueError(f"no memory-isolation store for database scheme {scheme!r}")
    return MemoryIsolationStore


def _daemon_timer(delay: float, callback: Callable[[], Any]) -> None:
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()


class MemoryIsolationRebuild:
    """Drive the one-time isolated rebuild of every pending notebook."""

    def __init__(
        self,
        *,
        database: Any,
        store: Any,
        start_rebuild: Callable[[str], dict],
        run_rebuild: Callable[[str, str], int],
        event_log: Any,
        is_ready: Callable[[], bool] | None = None,
        busy_retry_seconds: float = DEFAULT_BUSY_RETRY_SECONDS,
        busy_retry_rounds: int = DEFAULT_BUSY_RETRY_ROUNDS,
        timer: Callable[[float, Callable[[], Any]], Any] = _daemon_timer,
        scale_artifacts: Any = None,
        scale_auto_enabled: bool = False,
        scale_when: str = "idle",
    ) -> None:
        if is_ready is None:
            from app.core import readiness

            is_ready = readiness.is_ready
        self._database = database
        self._store = store
        self._start_rebuild = start_rebuild
        self._run_rebuild = run_rebuild
        self._event_log = event_log
        self._is_ready = is_ready
        self._busy_retry_seconds = float(busy_retry_seconds)
        self._rearms_left = int(busy_retry_rounds)
        self._timer = timer
        self._lock = threading.Lock()
        self._scale = scale_artifacts
        self._scale_auto = bool(scale_auto_enabled) and scale_artifacts is not None
        self._scale_when = scale_when

    @classmethod
    def for_repository(cls, repo: Any, **kwargs: Any) -> "MemoryIsolationRebuild":
        """The marker store follows the repository's own backend selection
        (``app.repositories.factory.create_repository``: the scheme of
        ``settings.database_url``) -- never the class or module name of an
        object that may be wrapped."""
        runtime = repo._runtime
        settings = repo.settings
        kwargs.setdefault("scale_artifacts", getattr(runtime, "scale_artifacts", None))
        kwargs.setdefault("scale_auto_enabled", bool(
            getattr(settings, "scale_index_auto_enabled", False)))
        kwargs.setdefault("scale_when", getattr(settings, "scale_index_auto_when", "idle"))
        return cls(
            database=runtime.database,
            store=store_for_settings(settings),
            start_rebuild=repo.start_unified_kg_rebuild,
            run_rebuild=repo.run_unified_kg_rebuild_job,
            event_log=runtime.event_log,
            **kwargs,
        )

    # ------------------------------------------------------------- reads
    def pending_notebook_ids(self) -> List[str]:
        with self._database.connect() as db:
            return self._store.pending_notebook_ids(db)

    def pending_count(self) -> int:
        with self._database.connect() as db:
            return self._store.pending_count(db)

    def is_pending(self, notebook_id: str) -> bool:
        with self._database.connect() as db:
            return self._store.is_pending(db, notebook_id)

    def seed_check_notebook_ids(self) -> List[str]:
        with self._database.connect() as db:
            return self._store.seed_check_notebook_ids(db)

    def seed_check_count(self) -> int:
        with self._database.connect() as db:
            return self._store.seed_check_count(db)

    def pre_isolation_scale_notebook_ids(self) -> List[str]:
        """Notebooks whose PUBLISHED scale index predates the isolation
        (``IndexProjectionStore.built_before_memory_isolation`` on the
        published manifest, through the runtime's ``projections`` seat; read
        through the artifact store's own inventory
        ``indexed_notebook_ids`` / ``read_manifest`` -- no table), and that are
        NOT copyable: the ones the automatic index path owns. A copyable (small)
        notebook's pre-isolation artifact is rebuilt by the operator
        (docs/operations.md). Only a notebook whose KG is already isolated
        (marker 1, or no state row; one primary-key read): a notebook still
        awaiting its isolated rebuild or its check (0 / 2: deferred, failed,
        not reached yet) would bake its leftover cluster names and texts into
        an index stamped as isolated -- its own rebuild's chained index
        refresh, or a later pass, builds it. Empty unless
        SCALE_INDEX_AUTO_ENABLED. A manifest or a notebook that cannot be read
        is reported and skipped."""
        if not self._scale_auto:
            return []
        artifacts = self._scale.artifacts
        scale_roots = list(artifacts.indexed_notebook_ids())
        found: List[str] = []
        for notebook_id in scale_roots:
            if self._needs_full_build(notebook_id, artifacts.scale_dir(notebook_id)):
                found.append(notebook_id)
        # A large notebook with a standalone visualisation and NO scale root:
        # its pre-isolation viz is refused and, being over
        # VIZ_SYNC_BUILD_MAX_OBJECTS, never rebuilt on a read ("no preview"
        # until a build) -- queue the same full build. A notebook within the
        # budget rebuilds its viz on its first read and is not queued.
        limit = int(getattr(self._scale.settings, "viz_sync_build_max_objects", 0))
        known = set(scale_roots)
        # The published standalone viz roots come from the artifact store's own
        # inventory (``viz_notebook_ids``: scratch / rollback directories
        # excluded by the same rule as ``indexed_notebook_ids``).
        for notebook_id in artifacts.viz_notebook_ids():
            if notebook_id in known:
                continue
            if self._needs_full_build(notebook_id, artifacts.viz_dir(notebook_id),
                                      min_objects=limit):
                found.append(notebook_id)
        return sorted(found)

    def _needs_full_build(self, notebook_id: str, directory: Any,
                          min_objects: int | None = None) -> bool:
        """The published artifact in ``directory`` predates the isolation, the
        notebook's KG is isolated (marker 1 or no state row: one primary-key
        read), it is not copyable and, for a standalone viz, it holds more than
        ``min_objects`` objects. Reported and False on any read failure."""
        artifacts = self._scale.artifacts
        try:
            manifest = artifacts.read_manifest(directory)
            if manifest is None or not (
                    self._scale.projections.built_before_memory_isolation(manifest)):
                return False
            with self._database.connect() as db:
                if self._store.not_isolated(db, notebook_id):
                    return False
            if min_objects is not None and int(
                    self._scale.projections.effective_object_count(notebook_id)
            ) <= min_objects:
                return False
            return not self._scale.notebook_copy_stats(notebook_id)["copyable"]
        except Exception as exc:  # noqa: BLE001 - one notebook never stops the pass
            self._emit_failed(notebook_id, "scale_probe", exc)
            return False

    # ------------------------------------------------------------- driving
    def schedule(self) -> Any:
        """Submit one background pass when anything is pending. Returns the
        job handle (``background_jobs.submit``'s thread-like handle), or None
        when nothing was submitted (not ready, nothing pending, nothing to
        check and no pre-isolation scale index to queue: no job, no log)."""
        if not self._is_ready():
            return None
        pending = self.pending_count()
        checks = self.seed_check_count()
        if pending <= 0 and checks <= 0 and not self.pre_isolation_scale_notebook_ids():
            return None
        from app.services import background_jobs

        self._log_info(
            "memory-isolation rebuild: %d notebook(s) pending, %d awaiting the "
            "seed check; scheduling one background pass", pending, checks,
        )
        return background_jobs.submit(self.run_pass, name=JOB_NAME)

    def run_pass(self) -> Dict[str, int]:
        """Rebuild every pending notebook once, sequentially. Returns counts
        per outcome. Never raises for a single notebook's failure; never
        sleeps -- a busy notebook re-arms a later pass instead."""
        tally = {"completed": 0, "failed": 0, "deferred": 0, "skipped": 0}
        if not self._is_ready():
            self._log_info(
                "memory-isolation rebuild: service not ready; pass refused"
            )
            return tally
        for notebook_id in self.seed_check_notebook_ids():
            self._seed_check_one(notebook_id)
        for notebook_id in self.pending_notebook_ids():
            tally[self._rebuild_one(notebook_id)] += 1
        # after the KG rebuilds, so a queued build reads the isolated graph
        for notebook_id in self.pre_isolation_scale_notebook_ids():
            self._queue_scale_build(notebook_id)
        self._log_info(
            "memory-isolation rebuild pass: completed=%d failed=%d deferred=%d "
            "skipped=%d", tally["completed"], tally["failed"],
            tally["deferred"], tally["skipped"],
        )
        if tally["deferred"]:
            self._rearm()
        return tally

    def _rearm(self) -> None:
        with self._lock:
            if self._rearms_left <= 0:
                self._log_info(
                    "memory-isolation rebuild: busy notebooks stay pending "
                    "until the next start"
                )
                return
            self._rearms_left -= 1
        self._timer(self._busy_retry_seconds, self._rearmed_schedule)

    def _rearmed_schedule(self) -> Any:
        """The timer's callback: :meth:`schedule`, but an exception (a
        database read failing, the job pool refusing) never ends the chain
        silently -- it is reported and the pass re-armed while rounds are
        left."""
        try:
            return self.schedule()
        except Exception as exc:  # noqa: BLE001 - a timer thread has no caller
            self._emit({"kind": "memory_isolation_rebuild_failed",
                        "notebook_id": "", "stage": "rearm",
                        "error_class": type(exc).__name__})
            self._rearm()
            return None

    def _rebuild_one(self, notebook_id: str) -> str:
        try:
            if not self.is_pending(notebook_id):
                # Rebuilt meanwhile (a manual rebuild clears the marker too).
                return "skipped"
        except Exception as exc:  # noqa: BLE001 - one notebook never stops the pass
            self._emit_failed(notebook_id, "probe", exc)
            return "failed"
        self._emit({"kind": "memory_isolation_rebuild_started",
                    "notebook_id": notebook_id})
        try:
            self._purge_bridge_candidates(notebook_id)
        except Exception as exc:  # noqa: BLE001
            self._emit_failed(notebook_id, "purge", exc)
            return "failed"
        try:
            job = self._start_rebuild(notebook_id)
        except KgMaintenanceAlreadyRunning:
            self._emit({"kind": "memory_isolation_rebuild_deferred",
                        "notebook_id": notebook_id, "stage": "claim"})
            return "deferred"
        except Exception as exc:  # noqa: BLE001
            self._emit_failed(notebook_id, "claim", exc)
            return "failed"
        try:
            clusters = int(self._run_rebuild(notebook_id, job["job_id"]))
        except KgMaintenanceAlreadyRunning:
            # Another process (offline recluster CLI) holds the notebook's
            # derived-generation claim: gated, not failed.
            self._emit({"kind": "memory_isolation_rebuild_deferred",
                        "notebook_id": notebook_id, "stage": "rebuild"})
            return "deferred"
        except Exception as exc:  # noqa: BLE001
            self._emit_failed(notebook_id, "rebuild", exc)
            return "failed"
        try:
            with self._database.write() as db:
                self._store.mark_isolated(db, notebook_id)
        except Exception as exc:  # noqa: BLE001
            self._emit_failed(notebook_id, "mark", exc)
            return "failed"
        self._emit({"kind": "memory_isolation_rebuild_completed",
                    "notebook_id": notebook_id, "clusters": clusters})
        return "completed"

    def _seed_check_one(self, notebook_id: str) -> None:
        """Marker 2 (outside F, clusters present): any signal
        (``seed_check_signal``) -> queued for the isolated rebuild (marker 0,
        with the migration's reset, and the merge candidates naming nothing
        that exists purged, in the same transaction); none -> 1. The check
        reads the notebook in bounded pages, never a statement in proportion
        to the notebook. A failure keeps 2 (retried next start). The event
        carries the signal's name only."""
        try:
            with self._database.connect() as db:
                signal = self._store.seed_check_signal(db, notebook_id)
            with self._database.write() as db:
                if signal is not None:
                    if self._store.queue_for_rebuild(db, notebook_id):
                        self._store.purge_stale_merge_candidates(db, notebook_id)
                else:
                    self._store.mark_seed_checked(db, notebook_id)
        except Exception as exc:  # noqa: BLE001 - one notebook never stops the pass
            self._emit_failed(notebook_id, "seed_check", exc)
            return
        self._emit({"kind": "memory_isolation_seed_checked",
                    "notebook_id": notebook_id,
                    "outcome": "queued" if signal is not None else "clean",
                    "signal": signal or ""})

    def _queue_scale_build(self, notebook_id: str) -> None:
        """A FULL scale build through the automatic path (the runtime's own
        ``trigger``: its dedupe / idle-window / failure-backoff state machine,
        not a manual request; no model call). A later start re-queues it only
        while the published manifest still predates the isolation."""
        try:
            outcome = self._scale.trigger(
                notebook_id, when=self._scale_when, mode="full")
        except Exception as exc:  # noqa: BLE001 - one notebook never stops the pass
            self._emit_failed(notebook_id, "scale", exc)
            return
        self._emit({"kind": "memory_isolation_scale_queued",
                    "notebook_id": notebook_id,
                    "status": str((outcome or {}).get("status", ""))})

    def _purge_bridge_candidates(self, notebook_id: str) -> None:
        """Idempotent: a retried or re-armed notebook purges again, finding
        nothing the second time."""
        from app.services.kg_merge import purge_bridge_canonical_ids

        with self._database.write() as db:
            objects = self._store.memory_objects(db, notebook_id)
            if not objects:
                return
            self._store.purge_bridge_candidates(
                db, notebook_id, purge_bridge_canonical_ids(objects))

    # ------------------------------------------------------------- events
    def _emit_failed(self, notebook_id: str, stage: str, exc: BaseException) -> None:
        self._emit({"kind": "memory_isolation_rebuild_failed",
                    "notebook_id": notebook_id, "stage": stage,
                    "error_class": type(exc).__name__})

    def _emit(self, event: dict) -> None:
        try:
            self._event_log.emit(event)
        except Exception:  # noqa: BLE001 - telemetry must never fail the pass
            pass

    def _log_info(self, msg: str, *args: Any) -> None:
        try:
            self._event_log.logger.info(msg, *args)
        except Exception:  # noqa: BLE001
            pass
