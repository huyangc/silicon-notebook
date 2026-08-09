"""In-process orchestration for zero-model KG maintenance jobs.

Relink and unified rebuild mutate the same derived graph products, so they share
one per-notebook slot.  This collaborator owns only claim/status/settlement and
worker-entry orchestration; the relink and rebuild algorithms stay owned by the
knowledge lifecycle service and are injected as late-bound callables.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict

from app.core.event_logging import EventLogger
from app.repositories.ports import KgMaintenanceAlreadyRunning


class KgMaintenanceJobs:
    """Coordinate relink/rebuild jobs without owning either graph algorithm."""

    RELINK_COUNTERS = {
        "isolated_before": 0,
        "edges_added": 0,
        "isolated_after": 0,
    }
    REBUILD_COUNTERS = {"clusters": 0}

    def __init__(
        self,
        *,
        event_log: EventLogger,
        get_notebook: Callable[[str], object],
        new_id: Callable[[str], str],
        relink_notebook_kg: Callable[[str], dict],
        rebuild_unified_kg: Callable[[str], int],
    ) -> None:
        self.event_log = event_log
        self.get_notebook = get_notebook
        self._new_id = new_id
        self._relink_notebook_kg = relink_notebook_kg
        self._rebuild_unified_kg = rebuild_unified_kg

        # Terminal entries remain available for the browser's next bounded poll.
        # Both kinds intentionally share this registry and lock. This stays
        # separate from durable kg_build_jobs: relink/rebuild are maintenance
        # passes, and publishing them as extraction jobs would claim the wrong
        # single-flight domain and expose false 0/0 analysis progress. Production
        # runs one worker, so process-local ownership is the deployment contract;
        # after restart an honest idle response replaces the vanished entry.
        self.jobs: Dict[str, dict] = {}
        self.lock = threading.Lock()

    def claim(
        self, notebook_id: str, kind: str, id_prefix: str, counters: Dict[str, int]
    ) -> dict:
        """Claim before submission so racing clicks cannot enqueue two writers."""
        self.get_notebook(notebook_id)
        with self.lock:
            current = self.jobs.get(notebook_id)
            if current is not None and current["status"] == "running":
                raise KgMaintenanceAlreadyRunning(notebook_id, current["kind"])
            job = {
                "job_id": self._new_id(id_prefix),
                "notebook_id": notebook_id,
                "kind": kind,
                "status": "running",
                **counters,
            }
            self.jobs[notebook_id] = job
            return dict(job)

    def status(
        self, notebook_id: str, kind: str, counters: Dict[str, int]
    ) -> dict:
        """Return this kind's state, treating another kind's claim as idle."""
        self.get_notebook(notebook_id)
        with self.lock:
            job = self.jobs.get(notebook_id)
            if job is None or job["kind"] != kind:
                return {
                    "job_id": "",
                    "notebook_id": notebook_id,
                    "status": "idle",
                    "running": False,
                    **counters,
                }
            return {
                "job_id": job["job_id"],
                "notebook_id": job["notebook_id"],
                "status": job["status"],
                "running": job["status"] == "running",
                **{name: job[name] for name in counters},
            }

    def settle(
        self,
        notebook_id: str,
        job_id: str,
        status: str,
        stats: dict | None = None,
    ) -> None:
        """Publish terminal state only while this exact job still owns the slot."""
        with self.lock:
            job = self.jobs.get(notebook_id)
            if job is None or job["job_id"] != job_id:
                return
            job["status"] = status
            if stats:
                for name in job:
                    if name in ("job_id", "notebook_id", "kind", "status"):
                        continue
                    if name in stats:
                        job[name] = int(stats[name])

    def start_notebook_relink(self, notebook_id: str) -> dict:
        return self.claim(
            notebook_id, "relink", "rlj", dict(self.RELINK_COUNTERS)
        )

    def notebook_relink_status(self, notebook_id: str) -> dict:
        return self.status(notebook_id, "relink", dict(self.RELINK_COUNTERS))

    def run_notebook_relink_job(self, notebook_id: str, job_id: str) -> dict:
        """Run relink and settle on every exit, including ``BaseException``."""
        try:
            stats = self._relink_notebook_kg(notebook_id)
        except Exception:
            self.settle(notebook_id, job_id, "failed")
            self.event_log.logger.exception(
                "relink_notebook_kg failed for %s", notebook_id
            )
            self.event_log.emit({
                "kind": "kg_relink_failed",
                "notebook_id": notebook_id,
            })
            raise
        except BaseException:
            self.settle(notebook_id, job_id, "failed")
            raise
        self.settle(notebook_id, job_id, "succeeded", stats)
        return stats

    def fail_notebook_relink_submission(
        self, notebook_id: str, job_id: str
    ) -> None:
        self.settle(notebook_id, job_id, "failed")

    def start_unified_kg_rebuild(self, notebook_id: str) -> dict:
        return self.claim(
            notebook_id, "rebuild", "ukj", dict(self.REBUILD_COUNTERS)
        )

    def unified_kg_rebuild_status(self, notebook_id: str) -> dict:
        return self.status(notebook_id, "rebuild", dict(self.REBUILD_COUNTERS))

    def run_unified_kg_rebuild_job(self, notebook_id: str, job_id: str) -> int:
        """Run the version-gated rebuild and settle on every exit."""
        try:
            clusters = int(self._rebuild_unified_kg(notebook_id))
        except Exception:
            self.settle(notebook_id, job_id, "failed")
            self.event_log.logger.exception(
                "rebuild_unified_kg failed for %s", notebook_id
            )
            self.event_log.emit({
                "kind": "unified_kg_rebuild_failed",
                "notebook_id": notebook_id,
            })
            raise
        except BaseException:
            self.settle(notebook_id, job_id, "failed")
            raise
        self.settle(
            notebook_id, job_id, "succeeded", {"clusters": clusters}
        )
        return clusters

    def fail_unified_kg_rebuild_submission(
        self, notebook_id: str, job_id: str
    ) -> None:
        self.settle(notebook_id, job_id, "failed")
