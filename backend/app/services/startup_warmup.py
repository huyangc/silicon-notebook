"""Background startup: migrate + warm open-path caches, then flip readiness.

Kicked off in a daemon thread by the FastAPI lifespan so uvicorn serves
``/api/ready`` immediately while every app route stays 503 until warm-up
completes. The first login after a restart therefore never pays migration + the
cold per-notebook count recompute (``knowledge_counts_cache`` starts empty each
process) — that cost moves here, behind the readiness gate, so users only ever
see a "服务启动中" screen instead of a multi-second hang.

Robustness: migration failure keeps the service not-ready (an un-migrated schema
is unusable); per-notebook warm failures are best-effort inside
``warm_open_path_caches`` and never abort readiness. The one-shot knowhow
legacy-model reprojection sweep below runs strictly AFTER ``mark_ready()`` (it
is a background catch-up, not a readiness precondition) and is itself
exception-safe, so it can never flip a successful startup back to "error".
"""
from __future__ import annotations

import logging
import threading

from app.core import readiness

logger = logging.getLogger("silicon_notebook.startup")
_cleanup_lock = threading.Lock()
_active_repository = None
_active_repository_factory = None


def _register_active_repository(repo, repository_factory) -> None:
    """Register the exact composition root owned by the current lifespan."""
    global _active_repository, _active_repository_factory
    with _cleanup_lock:
        if _active_repository is not None and _active_repository is not repo:
            raise RuntimeError("a repository lifecycle is already active")
        _active_repository = repo
        _active_repository_factory = repository_factory


def _take_active_repository(expected=None):
    """Atomically release the active instance without invoking its factory."""
    global _active_repository, _active_repository_factory
    with _cleanup_lock:
        if _active_repository is None:
            return None, None
        if expected is not None and _active_repository is not expected:
            return None, None
        repo = _active_repository
        repository_factory = _active_repository_factory
        _active_repository = None
        _active_repository_factory = None
        return repo, repository_factory


def _clear_repository_cache(repository) -> None:
    cache_clear = getattr(repository, "cache_clear", None)
    if cache_clear is None:
        return
    try:
        cache_clear()
    except Exception:  # cleanup must never replace the startup diagnostic
        logger.error("repository cache cleanup failed")


def _close_repository_instance(repo) -> None:
    try:
        close = getattr(repo, "close", None)
        if close is not None:
            close()
            return
        database = getattr(getattr(repo, "_runtime", None), "database", None)
        database_close = getattr(database, "close", None)
        if database_close is not None:
            database_close()
    except Exception:  # never attach third-party connection diagnostics
        logger.error("repository close failed")


def run_startup():
    """Construct the repository (runs migrations + seed), warm the open-path
    count caches for every notebook, then mark the service ready. Any exception
    is captured into the readiness state — it must never crash the server.

    Returns the exact warmed repository instance. The lifespan passes that same
    object back to ``close_repository``; shutdown never calls the composition
    factory merely to discover what should be closed.
    """
    repo = None
    repository_factory = None
    # A lifespan is a complete ownership cycle even when another cycle in this
    # process previously reached ready. Never inherit that process-global flag.
    readiness.reset()
    try:
        readiness.set_phase("migrating", "应用数据库迁移")
        logger.info("startup: constructing repository (runs schema migrations)…")
        # Imported lazily so module import stays cheap and side-effect free.
        from app.api.deps import repository as repository_factory

        repo = repository_factory()  # construct + migrate + seed
        _register_active_repository(repo, repository_factory)
        logger.info("startup: migrations done; warming open-path caches…")

        readiness.set_phase("warming", "预热笔记本计数缓存")

        def _progress(done: int, total: int) -> None:
            readiness.set_detail(f"{done}/{total} 笔记本", warmed=done, total=total)

        total = repo.warm_open_path_caches(progress=_progress)
        readiness.mark_ready()
        logger.info("startup: READY — %d notebook(s) warmed", total)
        _reproject_legacy_knowhow_tables(repo)
        return repo
    except Exception as exc:  # noqa: BLE001 — surface via readiness, never crash
        from app.core.config import get_settings

        if repo is not None:
            active_repo, active_factory = _take_active_repository(repo)
            if active_repo is not None:
                try:
                    _close_repository_instance(active_repo)
                finally:
                    _clear_repository_cache(active_factory)
        elif repository_factory is not None:
            # Construction itself failed before an exact instance was returned.
            _clear_repository_cache(repository_factory)
        safe_error = readiness.startup_error(exc, get_settings().database_url)
        readiness.mark_error(safe_error)
        # Never attach the original exception: a third-party driver may carry
        # raw conninfo in its traceback even though our adapter errors do not.
        logger.error("startup FAILED — service stays not-ready: %s", safe_error)
        return None


def close_repository(repository_instance=None) -> None:
    """Close only the exact repository owned by the active lifespan cycle.

    With no active registration this is a no-op apart from publishing the
    stopped readiness state. In particular it never invokes the cached factory,
    so shutdown cannot accidentally construct a fresh SQLite repository or
    PostgreSQL pool just to close it.
    """
    repo, repository_factory = _take_active_repository(repository_instance)
    try:
        if repo is not None:
            _close_repository_instance(repo)
    finally:
        if repository_factory is not None:
            _clear_repository_cache(repository_factory)
        # State/cache cleanup must survive driver close failures.
        readiness.mark_stopped()


def _reproject_legacy_knowhow_tables(repo) -> None:
    """One-shot post-readiness migration bridge (knowhow-tables PR-2+3 Task 2,
    design doc §① compatibility note): schedule a background cell-level
    reprojection for every knowhow table still carrying PR-1's fixed
    case/procedure/tool KOs (``KnowhowProjector.reproject_legacy_tables`` —
    see ``app.services.knowhow.projection`` for the detection query and the
    actual ``background_jobs.submit`` dispatch). Deliberately minimal here:
    all the real logic/tests live with the projector; this is just the wire-
    up call, mirroring how the migrate/warm steps above are themselves this
    module's own one-line calls into their owning services.

    Runs strictly AFTER ``mark_ready()`` (never delays the readiness gate —
    scale is bounded, ~100 rows/table, but there is no reason to make a
    first-request-after-restart wait on it either) and swallows every
    exception itself so a bug here can never be mistaken for a migration/
    warm-up failure by the caller's own try/except."""
    try:
        from app.services.knowhow.api import build_projector

        table_ids = build_projector(repo).reproject_legacy_tables()
        if table_ids:
            logger.info(
                "startup: scheduled cell-model reprojection for %d legacy "
                "knowhow table(s)", len(table_ids),
            )
    except Exception:  # noqa: BLE001 — best-effort, must never affect readiness
        logger.exception("startup: legacy knowhow reprojection scan failed (non-fatal)")
