"""Process-global concurrency for KG extraction.

Two SEPARATE singleton thread pools:
- window pool  (max=KG_EXTRACT_WORKERS): every extract_window LLM call; FIFO,
  the single global cap shared across all documents (intra- + inter-doc).
- job pool     (max=KG_JOB_CONCURRENCY): one process_source per document.

They MUST be separate: a job thread blocks waiting on window futures; if it
held a window-pool slot the pools could deadlock. With two pools, even when all
job threads are blocked the window pool keeps draining windows.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import threading
from typing import Any, Callable

from app.core.config import Settings

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_window_pool: cf.ThreadPoolExecutor | None = None
_job_pool: cf.ThreadPoolExecutor | None = None
_window_max = 0
_job_max = 0


def _build(window_workers: int, job_workers: int) -> None:
    global _window_pool, _job_pool, _window_max, _job_max
    _window_max = max(1, window_workers)
    _job_max = max(1, job_workers)
    _window_pool = cf.ThreadPoolExecutor(
        max_workers=_window_max, thread_name_prefix="kg-window")
    _job_pool = cf.ThreadPoolExecutor(
        max_workers=_job_max, thread_name_prefix="kg-job")


def _ensure() -> None:
    if _window_pool is not None and _job_pool is not None:
        return
    with _lock:
        if _window_pool is None or _job_pool is None:
            s = Settings()
            _build(s.kg_extract_workers, s.kg_job_concurrency)


def submit_window(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> cf.Future:
    """Submit one window (LLM call) to the global window pool."""
    _ensure()
    return _window_pool.submit(fn, *args, **kwargs)


def submit_job(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> cf.Future:
    """Submit one document-extraction job to the job pool (fire-and-forget;
    callee handles its own errors/status). A done-callback logs any unexpected
    exception so it is never silently swallowed."""
    _ensure()
    fut = _job_pool.submit(fn, *args, **kwargs)
    fut.add_done_callback(_log_job_exception)
    return fut


def _log_job_exception(fut: cf.Future) -> None:
    try:
        exc = fut.exception()
    except cf.CancelledError:
        return
    if exc is not None:
        _logger.error("kg extraction job failed", exc_info=exc)


def max_workers() -> int:
    _ensure()
    return _window_max


def job_concurrency() -> int:
    _ensure()
    return _job_max


def configure(*, window_workers: int | None = None, job_workers: int | None = None) -> None:
    """Test-only: rebuild both pools with explicit sizes (falls back to settings
    for any omitted value)."""
    with _lock:
        s = Settings()
        _shutdown_locked()
        _build(
            window_workers if window_workers is not None else s.kg_extract_workers,
            job_workers if job_workers is not None else s.kg_job_concurrency,
        )


def reset() -> None:
    """Test-only: shut down both pools so the next use rebuilds them."""
    with _lock:
        _shutdown_locked()


def _shutdown_locked() -> None:
    global _window_pool, _job_pool
    for pool in (_window_pool, _job_pool):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    _window_pool = None
    _job_pool = None
