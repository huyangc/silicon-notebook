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
from dataclasses import dataclass

from app.core import readiness

logger = logging.getLogger("silicon_notebook.startup")
_cleanup_lock = threading.Lock()


@dataclass
class _LifecycleState:
    lease: object
    status: str = "reserved"
    repository: object | None = None
    repository_factory: object | None = None


_active_lifecycle: _LifecycleState | None = None


class LifecycleAlreadyActiveError(RuntimeError):
    """Raised when an ASGI lifespan cannot own the process repository."""


def begin_lifecycle() -> object | None:
    """Reserve the one process-wide repository lifecycle before construction.

    An overlapping ASGI lifespan receives no lease and therefore cannot invoke
    the composition factory, reset the winner's readiness, or close its pool.
    The lock protects only ownership transitions; migration and warm-up happen
    after it is released.
    """
    global _active_lifecycle
    with _cleanup_lock:
        if _active_lifecycle is not None:
            return None
        lease = object()
        _active_lifecycle = _LifecycleState(lease=lease)
        # Ownership is established before process-global readiness changes.
        readiness.reset()
        readiness.set_phase("starting", "后端启动中")
        return lease


def _start_lifecycle(lease: object) -> bool:
    with _cleanup_lock:
        state = _active_lifecycle
        if state is None or state.lease is not lease or state.status != "reserved":
            return False
        state.status = "constructing"
        readiness.set_phase("migrating", "应用数据库迁移")
        return True


def _record_repository_factory(lease: object, repository_factory: object) -> bool:
    with _cleanup_lock:
        state = _active_lifecycle
        if state is None or state.lease is not lease or state.status != "constructing":
            return False
        state.repository_factory = repository_factory
        return True


def _bind_repository_and_begin_warmup(lease: object, repo: object) -> bool:
    with _cleanup_lock:
        state = _active_lifecycle
        if state is None or state.lease is not lease or state.status != "constructing":
            return False
        state.repository = repo
        state.status = "warming"
        readiness.set_phase("warming", "预热笔记本计数缓存")
        return True


def _set_warmup_progress(lease: object, done: int, total: int) -> None:
    with _cleanup_lock:
        state = _active_lifecycle
        if state is not None and state.lease is lease and state.status == "warming":
            readiness.set_detail(f"{done}/{total} 笔记本", warmed=done, total=total)


def _mark_lifecycle_ready(lease: object, repo: object) -> bool:
    with _cleanup_lock:
        state = _active_lifecycle
        if (
            state is None
            or state.lease is not lease
            or state.repository is not repo
            or state.status != "warming"
        ):
            return False
        state.status = "ready"
        readiness.mark_ready()
        return True


def _clear_repository_cache(repository: object) -> None:
    cache_clear = getattr(repository, "cache_clear", None)
    if cache_clear is None:
        return
    try:
        cache_clear()
    except Exception:  # cleanup must never replace the startup diagnostic
        logger.error("repository cache cleanup failed")


def _close_repository_instance(repo: object) -> None:
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


def _fail_lifecycle(lease: object, exc: BaseException) -> None:
    """Clean up and release a failed lifecycle without touching another one."""
    global _active_lifecycle
    with _cleanup_lock:
        state = _active_lifecycle
        if state is None or state.lease is not lease:
            return
        state.status = "failing"
        repo = state.repository
        repository_factory = state.repository_factory

    # Keep the reservation while external driver cleanup runs. A retry cannot
    # start and reset readiness until the failed pool/cache are fully gone.
    try:
        if repo is not None:
            _close_repository_instance(repo)
    finally:
        if repository_factory is not None:
            _clear_repository_cache(repository_factory)

    try:
        from app.core.config import get_settings

        safe_error = readiness.startup_error(exc, get_settings().database_url)
    except Exception:  # diagnostics must not strand the failing reservation
        safe_error = f"{type(exc).__name__}: database initialization failed"
    with _cleanup_lock:
        state = _active_lifecycle
        if state is None or state.lease is not lease or state.status != "failing":
            return
        readiness.mark_error(safe_error)
        _active_lifecycle = None
    # Never attach the original exception: a third-party driver may carry raw
    # conninfo in its traceback even though our adapter errors do not.
    logger.error("startup FAILED — service stays not-ready: %s", safe_error)


def run_startup(lease: object | None) -> object | None:
    """Construct the repository (runs migrations + seed), warm the open-path
    count caches for every notebook, then mark the service ready. Any exception
    is captured into the readiness state — it must never crash the server.

    ``lease`` must have been obtained from :func:`begin_lifecycle` before this
    function is called. Returns the exact warmed repository instance. Lifespan
    shutdown must pass both that lease and instance to ``close_repository``.
    """
    if lease is None or not _start_lifecycle(lease):
        return None
    try:
        logger.info("startup: constructing repository (runs schema migrations)…")
        # Imported lazily so module import stays cheap and side-effect free.
        from app.api.deps import repository as repository_factory

        if not _record_repository_factory(lease, repository_factory):
            return None
        repo = repository_factory()  # construct + migrate + seed
        if not _bind_repository_and_begin_warmup(lease, repo):
            # Defensive only: a valid lease cannot be detached during startup.
            # If that invariant is ever broken, do not leak the just-created
            # composition root.
            _close_repository_instance(repo)
            _clear_repository_cache(repository_factory)
            return None
        logger.info("startup: migrations done; warming open-path caches…")

        def _progress(done: int, total: int) -> None:
            _set_warmup_progress(lease, done, total)

        total = repo.warm_open_path_caches(progress=_progress)
        if not _mark_lifecycle_ready(lease, repo):
            return None
        logger.info("startup: READY — %d notebook(s) warmed", total)
        _reproject_legacy_knowhow_tables(repo)
        return repo
    except Exception as exc:  # noqa: BLE001 — surface via readiness, never crash
        _fail_lifecycle(lease, exc)
        return None


def close_repository(
    lease: object | None,
    repository_instance: object | None,
) -> None:
    """Close only the exact repository owned by the active lifespan cycle.

    Missing, stale, mismatched, and already-closed lease/instance pairs are
    strict no-ops. There is deliberately no wildcard form: a lifespan that did
    not acquire ownership can never close another lifespan's repository.
    """
    global _active_lifecycle
    if lease is None or repository_instance is None:
        return
    with _cleanup_lock:
        state = _active_lifecycle
        if (
            state is None
            or state.lease is not lease
            or state.repository is not repository_instance
            or state.status != "ready"
        ):
            return
        state.status = "closing"
        repository_factory = state.repository_factory

    # The reservation remains active while the exact pool is closed and its
    # cache entry cleared, so a fresh cycle cannot race cleanup.
    try:
        _close_repository_instance(repository_instance)
    finally:
        if repository_factory is not None:
            _clear_repository_cache(repository_factory)
        with _cleanup_lock:
            state = _active_lifecycle
            if (
                state is not None
                and state.lease is lease
                and state.repository is repository_instance
                and state.status == "closing"
            ):
                # State/cache cleanup must survive driver close failures.
                readiness.mark_stopped()
                _active_lifecycle = None


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
