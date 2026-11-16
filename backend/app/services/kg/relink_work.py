"""Resumable, source-scoped relink orchestration; stores own all SQL."""
from __future__ import annotations

import time

from app.domain.kg.relink import RELINK_ALGORITHM_VERSION
from app.repositories.ports import NotebookDeletingAbortsMaintenanceError
from app.services.kg.relink_progress import report_relink_progress

# Paging controls memory only; every pending source remains eligible.
RELINK_WORK_PAGE_SIZE = 256


def run_relink(service, notebook_id: str, *, incremental: bool = False) -> dict:
    """Drain pending receipts; explicit maintenance requests a full pass.

    First use/algorithm upgrades seed the historical backlog, without a startup
    migration scan. Graph publications enqueue their source in the same graph
    transaction. Completed receipts survive failure/restart. A keyset sweep is
    finite even during ingestion: racing updates behind the cursor stay pending
    for the next run and never get a false full-library completion mark.
    """
    service.get_notebook(notebook_id)
    work = service.knowledge.relink_work
    started = time.monotonic()
    report_relink_progress({"processed_sources": 0, "total_sources": 0,
                            "pending_sources": 0, "edges_added": 0,
                            "elapsed_seconds": 0, "resumed": False, "preparing": True})
    with service._write() as db:
        state = work.prepare(db, notebook_id, RELINK_ALGORITHM_VERSION, full=not incremental)
    with service._connect() as db:
        initial = work.summary(db, notebook_id)
    total = initial["pending"]
    processed = before_total = after_total = 0
    written = {"edges": 0, "seq_bumps": 0}

    def progress(pending: int) -> None:
        report_relink_progress({
            "processed_sources": processed, "total_sources": max(total, processed),
            "pending_sources": pending, "edges_added": written["edges"],
            "elapsed_seconds": time.monotonic() - started,
            "resumed": state["resumed"],
        })

    progress(total)
    after_source_id = None
    try:
        while True:
            with service._connect() as db:
                page = work.page(db, notebook_id, after_source_id, RELINK_WORK_PAGE_SIZE)
            if not page:
                break
            for row in page:
                if service._notebook_deleting(notebook_id):
                    raise NotebookDeletingAbortsMaintenanceError(notebook_id)
                result = service._relink_one_source(
                    notebook_id, row["source_id"], written, revision=int(row["revision"]),
                )
                after_source_id = row["source_id"]
                if result is not None:
                    before_total += result[0]
                    after_total += result[1]
                    processed += 1
                progress(max(0, total - processed))
    finally:
        if written["edges"]:
            service._invalidate_unified_cache(notebook_id)
    with service._write() as db:
        completed_full = state["full_pending"] and work.finish_full(db, notebook_id)
    with service._connect() as db:
        final = work.summary(db, notebook_id)
    if completed_full:
        # Resumption cannot claim it observed unrelated mutations during the
        # interrupted interval: keep the original start generation conservatively.
        service._mark_relink_attempted(
            notebook_id, state["started_at_seq"],
            0 if state["resumed"] else written["seq_bumps"],
        )
    progress(final["pending"])
    return {
        "isolated_before": final["isolated_before"] if state["full_pending"] else before_total,
        "isolated_after": final["isolated_after"] if state["full_pending"] else after_total,
        "edges_added": final["edges_added"] if state["full_pending"] else written["edges"],
    }
