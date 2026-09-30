"""Post-readiness sweep of ownerless Memory sources (plan 2026-09-29 E5-3, audit N-5).

A Memory's derived source (``sources.source_type = 'memory'``) is the root of
every row derived from it: elements, element vectors, knowledge objects and
relations, evidence, cluster members. It is normally removed with the Memory
(``MemoryService`` -> ``remove_memory_source``), but two things left sources
behind that no lifecycle path can find any more:

* notebook copies made before Memory was excluded from copies cleared
  ``sources.memory_id`` and kept ``source_type = 'memory'`` (N-5): the source
  points at nothing, so ``source_id_for_memory`` never returns it;
* a Memory row deleted (or no longer ``confirmed``) by a path that did not
  tear its source down leaves a source whose Memory is gone.

Because retrieval keeps Memory sources fail-closed for everyone when they have
no readable owner, such a source is unreachable *and* unowned; this sweep
deletes it, through the ordinary ``delete_source`` path so every derived row
goes with it.

Contract (pinned by ``tests/test_memory_orphan_sweep.py``):

* **After readiness, never before.** ``startup_warmup`` calls
  :meth:`MemoryOrphanSweep.schedule` strictly after ``mark_ready()``;
  :meth:`run_pass` itself refuses to run while the process is not ready.
* **One worker, the ordinary delete path.** One background thread walks the
  orphans in id order, ``ORPHAN_SWEEP_PAGE_SIZE`` at a time, and removes each
  with ``delete_source`` (the same call the API and ``remove_memory_source``
  use: extraction state, embeddings, KG rows, source row, files, images, cache
  invalidation, dirty mark). It never calls a model: ``delete_source`` skips
  the corpus-summary refresh for hidden synthetic source types.
* **Paging never changes the result.** The read is keyset-paged on the source
  id; the page size only sets how many rows one statement returns. A source
  that vanishes between the read and the delete (another writer) counts as
  already gone: exactly the ``KeyError(source_id)`` of ``delete_source``'s own
  lookup; any other ``KeyError`` is a failure.
* **Idempotent, resumable.** There is no progress marker: the orphans
  themselves are the queue. A second run finds none, runs no job and emits no
  event; an interrupted or failed pass leaves exactly what it did not delete
  for the next start.
* **Failures are isolated, and bounded.** One source that fails to delete is
  logged, skipped and left for the next start; ``MAX_CONSECUTIVE_FAILURES``
  failures in a row (a systemic fault: database away, lock held) end the pass,
  so the rest also waits for the next start instead of failing one by one.
* **Content-free events.** ``memory_orphan_sweep_started`` / ``_completed`` /
  ``_failed`` carry source ids, counts and an exception CLASS name -- never a
  title, text, path or message. A read that fails (statement timeout, lost
  connection) emits ``_failed`` with the class name only and ends the pass, so
  ``_started`` is always followed by ``_completed`` and a failed read is never
  silent; the rest is left for the next start.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

#: Diagnostic name of the one background thread (not one of the gated
#: maintenance pools: one bounded pass per process start).
JOB_NAME = "memory-orphan-sweep"

#: Orphans read (and deleted) per page. Each page costs one anti-join pass over
#: ``sources`` (about 0.5-1.5 s on 1M sources), so a page is sized to make a few
#: thousand orphans a handful of passes. A protocol constant: it bounds one
#: statement's result, never the outcome -- ``test_memory_orphan_sweep`` runs
#: the same fixture with a page of 1 and with this value and requires the same
#: end state.
ORPHAN_SWEEP_PAGE_SIZE = 1000

#: Consecutive failed deletes that end a pass (see the module docstring).
MAX_CONSECUTIVE_FAILURES = 3


class MemoryOrphanSweep:
    """Delete every ownerless Memory source once, after readiness."""

    def __init__(
        self,
        *,
        store: Any,
        delete_source: Callable[[str], None],
        event_log: Any,
        is_ready: Callable[[], bool] | None = None,
        page_size: int = ORPHAN_SWEEP_PAGE_SIZE,
    ) -> None:
        if is_ready is None:
            from app.core import readiness

            is_ready = readiness.is_ready
        self._store = store
        self._delete_source = delete_source
        self._event_log = event_log
        self._is_ready = is_ready
        self._page_size = max(1, int(page_size))

    @classmethod
    def for_repository(cls, repo: Any, **kwargs: Any) -> "MemoryOrphanSweep":
        runtime = repo._runtime
        return cls(
            store=runtime.memory_store,
            delete_source=repo.delete_source,
            event_log=runtime.event_log,
            **kwargs,
        )

    # ------------------------------------------------------------- driving
    def has_orphans(self) -> bool:
        return bool(self._store.has_orphan_memory_sources())

    def schedule(self) -> Any:
        """Submit one background pass when any orphan exists. Returns the job
        handle, or None when nothing was submitted (not ready, or nothing to
        sweep: no job, no log)."""
        if not self._is_ready() or not self.has_orphans():
            return None
        from app.services import background_jobs

        self._log_info(
            "memory orphan sweep: ownerless Memory sources found; scheduling one "
            "background pass"
        )
        return background_jobs.submit(self.run_pass, name=JOB_NAME)

    def run_pass(self) -> Dict[str, int]:
        """Delete every orphan once. Returns ``deleted`` / ``gone`` / ``failed``
        counts. Never raises for a single source's failure."""
        tally = {"deleted": 0, "gone": 0, "failed": 0}
        if not self._is_ready():
            self._log_info("memory orphan sweep: service not ready; pass refused")
            return tally
        after = ""
        consecutive = 0
        started = False
        while consecutive < MAX_CONSECUTIVE_FAILURES:
            try:
                page = self._store.orphan_memory_source_ids(self._page_size, after)
            except Exception as exc:  # noqa: BLE001 - a failed read ends the pass, visibly
                self._emit({"kind": "memory_orphan_sweep_failed",
                            "error_class": type(exc).__name__})
                break
            if not page:
                break
            if not started:
                started = True
                self._emit({"kind": "memory_orphan_sweep_started"})
            for source_id in page:
                after = source_id
                outcome = self._delete_one(source_id)
                tally[outcome] += 1
                consecutive = consecutive + 1 if outcome == "failed" else 0
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    break
        if started:
            self._emit({"kind": "memory_orphan_sweep_completed", **tally})
            self._log_info(
                "memory orphan sweep pass: deleted=%d gone=%d failed=%d",
                tally["deleted"], tally["gone"], tally["failed"],
            )
        return tally

    def _delete_one(self, source_id: str) -> str:
        try:
            self._delete_source(source_id)
        except KeyError as exc:
            # Only ``delete_source``'s own ``get_source(source_id)`` miss means "another
            # writer removed it since the read"; any other KeyError is a real failure.
            if exc.args == (source_id,):
                return "gone"
            self._emit({"kind": "memory_orphan_sweep_failed",
                        "source_id": source_id,
                        "error_class": type(exc).__name__})
            return "failed"
        except Exception as exc:  # noqa: BLE001 - one source never stops the pass
            self._emit({"kind": "memory_orphan_sweep_failed",
                        "source_id": source_id,
                        "error_class": type(exc).__name__})
            return "failed"
        return "deleted"

    # ------------------------------------------------------------- events
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
