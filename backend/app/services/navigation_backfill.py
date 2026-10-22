"""Background judgement of existing sources' navigation pages (SQLite v101 /
PostgreSQL 0082).

The build writes ``chunks.navigation`` with every new chunk generation and
stamps ``sources.navigation_scanned_at`` in the same transaction
(``SourceChunkingService``). The stamp is a generation token: once a source's
current chunks are judged it EQUALS ``chunked_at``. Sources chunked before the
migration -- or while ``NAVIGATION_DETECTION_ENABLED`` was off -- carry a NULL
stamp, or one that differs from ``chunked_at``; this pass judges them with the
same pure detector (``app.services.navigation_chunks``) over the inputs the
detector reads: the source's heading elements (``source_heading_elements``,
never the body elements' text) and its chunks.

Two drivers:

* **after readiness, in the service** -- ``schedule_navigation_backfill``
  (called by ``startup_warmup`` strictly after ``mark_ready()``) logs the
  pending count and submits a pass to the light maintenance pool
  (``navigation-backfill``). Each job handles ONE page of pending sources and
  then submits the next page as a new job (the pending sources are the durable
  queue), so the pass never holds a light-pool worker for long. It never
  delays readiness and never raises into startup. It does nothing while
  detection is off, and stops at the next source once an offline batch scope
  is active (``offline_batch_active()``, checked per source);
* **by hand** -- ``scripts/backfill_navigation_chunks.py``
  (``app.services.navigation_backfill_cli.main``; a separate module so the
  build path that imports this one never imports the CLI composition):
  ``run_backfill`` over every page in one process -- a dry run with per-source
  counts (``--show-sections`` lists the flagged chunks' section labels), a
  manual re-run (``--rescan`` judges every chunked source in scope, not only
  the pending ones) and ``--clear`` (data-level rollback: every flag back to 0,
  every stamp back to NULL). It is a schema guest; its writes on PostgreSQL
  require ``--confirm-service-stopped``.

Per source, under the ingestion service's per-source chunk lock when one is
available (``try_hold_source_chunk_lock`` with no wait): read the
``chunked_at`` token and the chunks, then the headings, judge, and write only
the rows whose flag changed plus the stamp (``navigation_scanned_at =
chunked_at``) in one compare-and-set transaction
(``ChunkStore.apply_navigation_flags``: ``WHERE id=? AND source_id=?`` per row,
the stamp only while ``chunked_at`` is still the token; on PostgreSQL under a
short ``lock_timeout`` so the pass is always the side that yields to a source
deletion). A source whose generation changed in between, or whose write gave
way, is abandoned and stays pending. A source whose lock is held is "busy":
the lock is held by a parse / re-chunk of that source (which stamps it
itself), but also by the checkup's vector backfill, the catalog's source write
barrier and the MCP source probes, which do not -- so busy sources get one
bounded second round at the end of the pass, and whatever is still busy then
stays pending for the next start. Every notebook whose chunks changed is
marked dirty once per page (every chunk-derived cache keys off that sequence).
Resumable and idempotent: the pending sources are the queue.
"""
from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Protocol, Sequence

from app.services.navigation_chunks import (
    NavigationDetection,
    detect_navigation_chunks,
)

#: Sources read per page of the pending scan; also the size of one background
#: job and the bound of the end-of-pass retry round over busy sources. A
#: protocol constant: it bounds one statement's result and one job's length,
#: never the outcome (the pending sources are the queue).
NAVIGATION_BACKFILL_PAGE_SIZE = 200
#: Diagnostic / pool name of the post-readiness pass (light maintenance pool,
#: ``background_jobs._LIGHT_MAINTENANCE_OPERATIONS``).
JOB_NAME = "navigation-backfill"
#: Event kind recorded when a source has too many titles to be judged.
SKIPPED_EVENT = "navigation_detection_skipped"

logger = logging.getLogger("silicon_notebook.navigation_backfill")


class NavigationBackfillStore(Protocol):
    """The chunk store methods this pass depends on (both adapters'
    ``ChunkStore``)."""

    def source_heading_elements(self, source_id: str) -> list: ...
    def navigation_scan_page(
        self, *, after_source_id: str, limit: int,
        notebook_id: str | None = None, pending_only: bool = True,
    ) -> list[tuple[str, str]]: ...
    def navigation_pending_count(self, *, notebook_id: str | None = None) -> int: ...
    def navigation_scan_rows(self, source_id: str) -> tuple[object, list[dict]]: ...
    def apply_navigation_flags(
        self, source_id: str, *, expected_chunked_at: object,
        changes: Sequence[tuple[str, bool]],
    ) -> bool: ...
    def clear_navigation_flags(
        self, *, notebook_id: str | None = None
    ) -> tuple[int, int, list[str]]: ...


@dataclass
class SourceJudgement:
    """One source's outcome, for the CLI's per-source report."""

    source_id: str
    notebook_id: str
    chunks: int = 0
    flagged: int = 0
    changed: int = 0
    outcome: str = "judged"  # judged | abandoned | busy | failed
    skipped: str = ""  # detector skip reason (too_many_titles)
    flagged_sections: list[str] = field(default_factory=list)


@dataclass
class NavigationBackfillResult:
    pending_before: int = 0
    judged: int = 0
    chunks_flagged: int = 0
    chunks_changed: int = 0
    sources_changed: int = 0
    too_many_titles: int = 0
    # Not judged by this pass, left pending: the source chunk lock was still
    # held after the retry round, or the generation changed / the write gave
    # way in between.
    busy: int = 0
    abandoned: int = 0
    failed: int = 0
    stopped_offline_batch: bool = False
    pages: int = 0
    last_source_id: str = ""
    pending_after: int = 0

    def as_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


@dataclass
class _Run:
    """What one pass needs besides the store (shared by every page)."""

    mark_dirty: Callable[[str], None]
    notebook_id: str | None = None
    pending_only: bool = True
    dry_run: bool = False
    hold_lock: Callable[[str], ContextManager[bool]] = lambda _sid: nullcontext(True)
    event_log: Any = None
    show_sections: bool = False
    offline_active: Callable[[], bool] = lambda: False
    page_size: int = NAVIGATION_BACKFILL_PAGE_SIZE
    on_source: Callable[[SourceJudgement], None] = lambda _judgement: None


def emit_navigation_skipped(
    event_log: Any,
    notebook_id: str,
    source_id: str,
    detection: NavigationDetection,
    *,
    stage: str,
) -> None:
    """Content-free event: ids, the reason code and the title count only."""
    if event_log is None:
        return
    try:
        event_log.emit({
            "kind": SKIPPED_EVENT,
            "notebook_id": notebook_id,
            "source_id": source_id,
            "stage": stage,
            "reason": detection.skipped,
            "title_count": detection.title_count,
        })
    except Exception:  # noqa: BLE001 - observability is fail-open
        pass


def _no_lock(_source_id: str) -> ContextManager[bool]:
    return nullcontext(True)


def _default_offline_active() -> Callable[[], bool]:
    from app.services.offline_batch import offline_batch_active

    return offline_batch_active


def judge_source(
    store: NavigationBackfillStore,
    source_id: str,
    notebook_id: str,
    *,
    dry_run: bool,
    hold_lock: Callable[[str], ContextManager[bool]] = _no_lock,
    event_log: Any = None,
    show_sections: bool = False,
) -> SourceJudgement:
    """Judge one source and (unless ``dry_run``) write what changed."""
    judgement = SourceJudgement(source_id=source_id, notebook_id=notebook_id)
    with hold_lock(source_id) as held:
        if not held:
            judgement.outcome = "busy"
            return judgement
        token, chunks = store.navigation_scan_rows(source_id)
        if token is None:
            # Unchunked (a re-parse cleared the marker) or gone since the scan
            # read it: its next build stamps it.
            judgement.outcome = "abandoned"
            return judgement
        headings = store.source_heading_elements(source_id)
        detection = detect_navigation_chunks(
            headings, [(c["section_path"], c["text"]) for c in chunks]
        )
        if detection.skipped:
            judgement.skipped = detection.skipped
            if not dry_run:
                emit_navigation_skipped(
                    event_log, notebook_id, source_id, detection, stage="backfill"
                )
        changes = [
            (chunk["id"], flag)
            for chunk, flag in zip(chunks, detection.flags)
            if bool(chunk["navigation"]) != flag
        ]
        judgement.chunks = len(chunks)
        judgement.flagged = sum(detection.flags)
        judgement.changed = len(changes)
        if show_sections:
            judgement.flagged_sections = [
                chunk["section_path"]
                for chunk, flag in zip(chunks, detection.flags) if flag
            ]
        if dry_run:
            return judgement
        if not store.apply_navigation_flags(
            source_id, expected_chunked_at=token, changes=changes
        ):
            judgement.outcome = "abandoned"
    return judgement


def _judge_safely(store, source_id: str, notebook_id: str, run: _Run) -> SourceJudgement:
    try:
        return judge_source(
            store, source_id, notebook_id, dry_run=run.dry_run,
            hold_lock=run.hold_lock, event_log=run.event_log,
            show_sections=run.show_sections,
        )
    except Exception as exc:  # noqa: BLE001 - one source never ends the pass
        logger.warning("navigation backfill: source failed (%s)", type(exc).__name__)
        return SourceJudgement(source_id=source_id, notebook_id=notebook_id, outcome="failed")


def _tally(result: NavigationBackfillResult, judgement: SourceJudgement) -> None:
    if judgement.outcome == "busy":
        result.busy += 1
    elif judgement.outcome == "abandoned":
        result.abandoned += 1
    elif judgement.outcome == "failed":
        result.failed += 1
    else:
        result.judged += 1
        result.chunks_flagged += judgement.flagged
        result.chunks_changed += judgement.changed
        result.sources_changed += 1 if judgement.changed else 0
        result.too_many_titles += 1 if judgement.skipped else 0


def _judge_batch(
    store, batch: Sequence[tuple[str, str]], run: _Run,
    result: NavigationBackfillResult, busy: list | None,
) -> int:
    """Judge ``batch`` in order; busy sources go to ``busy`` (first round) or
    are counted busy (``busy`` is None: the retry round). Marks every notebook
    whose chunks changed dirty once. Returns how many sources were handled
    (fewer than ``len(batch)`` = stopped by an offline batch)."""
    dirty: list[str] = []
    handled = 0
    for source_id, notebook_id in batch:
        if run.offline_active():
            result.stopped_offline_batch = True
            break
        handled += 1
        result.last_source_id = source_id
        judgement = _judge_safely(store, source_id, notebook_id, run)
        if judgement.outcome == "busy" and busy is not None:
            busy.append((source_id, notebook_id))
            continue
        _tally(result, judgement)
        if judgement.outcome == "judged" and judgement.changed and not run.dry_run:
            if notebook_id not in dirty:
                dirty.append(notebook_id)
        run.on_source(judgement)
    for notebook_id in dirty:
        run.mark_dirty(notebook_id)
    return handled


def run_page(
    store, run: _Run, result: NavigationBackfillResult, *, cursor: str, busy: list
) -> str | None:
    """Judge ONE page of sources after ``cursor``. Returns the next cursor, or
    None when the scan is exhausted (or an offline batch stopped it)."""
    page = store.navigation_scan_page(
        after_source_id=cursor, limit=max(1, int(run.page_size)),
        notebook_id=run.notebook_id, pending_only=run.pending_only,
    )
    if not page:
        return None
    result.pages += 1
    if _judge_batch(store, page, run, result, busy) < len(page):
        return None
    return page[-1][0]


def retry_busy(store, run: _Run, result: NavigationBackfillResult, busy: list) -> None:
    """The one bounded second round over the sources whose lock was held: at
    most one page of them; the rest, and those still busy, stay pending."""
    cap = max(1, int(run.page_size))
    handled = _judge_batch(store, busy[:cap], run, result, None)
    result.busy += len(busy) - handled


def run_backfill(
    store: NavigationBackfillStore,
    *,
    mark_dirty: Callable[[str], None],
    notebook_id: str | None = None,
    pending_only: bool = True,
    dry_run: bool = False,
    hold_lock: Callable[[str], ContextManager[bool]] = _no_lock,
    event_log: Any = None,
    show_sections: bool = False,
    offline_active: Callable[[], bool] | None = None,
    page_size: int = NAVIGATION_BACKFILL_PAGE_SIZE,
    on_source: Callable[[SourceJudgement], None] = lambda _judgement: None,
) -> NavigationBackfillResult:
    """Every page in one call (the CLI): judge the chunked sources in scope
    (only the pending ones unless ``pending_only`` is False), source id order,
    then one retry round over busy sources. ``dry_run`` judges and reports but
    writes nothing and marks nothing dirty."""
    run = _Run(
        mark_dirty=mark_dirty, notebook_id=notebook_id, pending_only=pending_only,
        dry_run=dry_run, hold_lock=hold_lock, event_log=event_log,
        show_sections=show_sections,
        offline_active=offline_active or _default_offline_active(),
        page_size=page_size, on_source=on_source,
    )
    result = NavigationBackfillResult(
        pending_before=store.navigation_pending_count(notebook_id=notebook_id)
    )
    busy: list = []
    cursor: str | None = ""
    while cursor is not None:
        cursor = run_page(store, run, result, cursor=cursor, busy=busy)
    if not result.stopped_offline_batch:
        retry_busy(store, run, result, busy)
    else:
        result.busy += len(busy)
    result.pending_after = store.navigation_pending_count(notebook_id=notebook_id)
    return result


@dataclass
class _PassState:
    """Carried from one page job to the next."""

    result: NavigationBackfillResult
    cursor: str = ""
    busy: list = field(default_factory=list)


class NavigationBackfill:
    """The post-readiness pass, wired to one repository's runtime."""

    def __init__(
        self,
        *,
        store: NavigationBackfillStore,
        settings: Any,
        mark_dirty: Callable[[str], None],
        hold_lock: Callable[[str], ContextManager[bool]] = _no_lock,
        event_log: Any = None,
        is_ready: Callable[[], bool] | None = None,
        offline_active: Callable[[], bool] | None = None,
        submit: Callable[..., Any] | None = None,
        page_size: int = NAVIGATION_BACKFILL_PAGE_SIZE,
    ) -> None:
        if is_ready is None:
            from app.core import readiness

            is_ready = readiness.is_ready
        self._store = store
        self._settings = settings
        self._mark_dirty = mark_dirty
        self._hold_lock = hold_lock
        self._event_log = event_log
        self._is_ready = is_ready
        self._offline_active = offline_active or _default_offline_active()
        self._submit = submit
        self._page_size = page_size

    @classmethod
    def for_repository(cls, repo: Any, **kwargs: Any) -> "NavigationBackfill":
        runtime = repo._runtime
        ingestion = getattr(runtime, "source_ingestion", None)

        def hold_lock(source_id: str) -> ContextManager[bool]:
            # No wait: whoever holds it (a parse / re-chunk of this source, the
            # checkup's vector backfill, the catalog write barrier, an MCP
            # probe) is left alone; the source gets one more try at the end.
            if ingestion is None:
                return nullcontext(True)
            return ingestion.try_hold_source_chunk_lock(source_id, timeout=0.0)

        return cls(
            store=runtime.chunk_store,
            settings=runtime.settings,
            mark_dirty=runtime.source_chunking.mark_unified_dirty,
            hold_lock=hold_lock,
            event_log=runtime.event_log,
            **kwargs,
        )

    def _submit_job(self, *args: Any) -> Any:
        submit = self._submit
        if submit is None:
            from app.services import background_jobs

            submit = background_jobs.submit
        return submit(self.run_pass, *args, name=JOB_NAME)

    def schedule(self) -> Any:
        """Submit the first page job when anything is pending. Returns the job
        handle, or None when nothing was submitted. Never raises."""
        try:
            if not self._settings.navigation_detection_enabled:
                logger.info("navigation backfill: detection disabled; not scheduled")
                return None
            if self._offline_active():
                return None
            pending = int(self._store.navigation_pending_count())
            logger.info("navigation backfill: %d source(s) to judge", pending)
            if not pending or not self._is_ready():
                return None
            return self._submit_job()
        except Exception as exc:  # noqa: BLE001 - must never affect readiness
            logger.warning("navigation backfill: scheduling failed (%s)", type(exc).__name__)
            return None

    def run_pass(self, state: _PassState | None = None) -> NavigationBackfillResult | None:
        """One page job: judge one page, then submit the next page as a new job
        and return. After the last page: the busy retry round and the final
        log line. Refuses while not ready or with detection off; logs counts
        and an exception CLASS only. Returns the pass's running result."""
        if not self._is_ready() or not self._settings.navigation_detection_enabled:
            logger.info("navigation backfill: not ready or disabled; pass refused")
            return None
        run = _Run(
            mark_dirty=self._mark_dirty, hold_lock=self._hold_lock,
            event_log=self._event_log, offline_active=self._offline_active,
            page_size=self._page_size,
        )
        try:
            if state is None:
                state = _PassState(result=NavigationBackfillResult(
                    pending_before=int(self._store.navigation_pending_count())
                ))
            result = state.result
            cursor = run_page(self._store, run, result, cursor=state.cursor, busy=state.busy)
            if cursor is not None:
                state.cursor = cursor
                self._submit_job(state)
                return result
            if result.stopped_offline_batch:
                result.busy += len(state.busy)
            else:
                retry_busy(self._store, run, result, state.busy)
            result.pending_after = int(self._store.navigation_pending_count())
        except Exception as exc:  # noqa: BLE001 - what is left waits for the next start
            logger.warning("navigation backfill failed (%s)", type(exc).__name__)
            return None
        logger.info(
            "navigation backfill: judged=%d sources_changed=%d chunks_changed=%d "
            "busy=%d abandoned=%d failed=%d pages=%d pending_after=%d",
            result.judged, result.sources_changed, result.chunks_changed,
            result.busy, result.abandoned, result.failed, result.pages,
            result.pending_after,
        )
        return result


def schedule_navigation_backfill(repo: Any) -> Any:
    """Post-readiness entry point for ``startup_warmup``. Never raises."""
    try:
        return NavigationBackfill.for_repository(repo).schedule()
    except Exception as exc:  # noqa: BLE001 - must never affect readiness
        logger.warning("navigation backfill: scheduling failed (%s)", type(exc).__name__)
        return None
