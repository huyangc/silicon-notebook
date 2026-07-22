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

from app.core import readiness

logger = logging.getLogger("silicon_notebook.startup")


def run_startup() -> None:
    """Construct the repository (runs migrations + seed), warm the open-path
    count caches for every notebook, then mark the service ready. Any exception
    is captured into the readiness state — it must never crash the server."""
    try:
        from app.services.model_provider import (
            validate_process_local_scheduler_deployment,
        )

        validate_process_local_scheduler_deployment()
        readiness.set_phase("migrating", "应用数据库迁移")
        logger.info("startup: constructing repository (runs schema migrations)…")
        # Imported lazily so module import stays cheap and side-effect free.
        from app.api.deps import repository

        repo = repository()  # construct + migrate + seed (the heavy one-time cost)
        logger.info("startup: migrations done; warming open-path caches…")

        readiness.set_phase("warming", "预热笔记本计数缓存")

        def _progress(done: int, total: int) -> None:
            readiness.set_detail(f"{done}/{total} 笔记本", warmed=done, total=total)

        total = repo.warm_open_path_caches(progress=_progress)
        readiness.mark_ready()
        logger.info("startup: READY — %d notebook(s) warmed", total)
        _reproject_legacy_knowhow_tables(repo)
    except Exception as exc:  # noqa: BLE001 — surface via readiness, never crash
        readiness.mark_error(f"{type(exc).__name__}: {exc}")
        logger.exception("startup FAILED — service stays not-ready")


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
