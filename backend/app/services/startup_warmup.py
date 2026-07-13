"""Background startup: migrate + warm open-path caches, then flip readiness.

Kicked off in a daemon thread by the FastAPI lifespan so uvicorn serves
``/api/ready`` immediately while every app route stays 503 until warm-up
completes. The first login after a restart therefore never pays migration + the
cold per-notebook count recompute (``knowledge_counts_cache`` starts empty each
process) — that cost moves here, behind the readiness gate, so users only ever
see a "服务启动中" screen instead of a multi-second hang.

Robustness: migration failure keeps the service not-ready (an un-migrated schema
is unusable); per-notebook warm failures are best-effort inside
``warm_open_path_caches`` and never abort readiness.
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
    except Exception as exc:  # noqa: BLE001 — surface via readiness, never crash
        readiness.mark_error(f"{type(exc).__name__}: {exc}")
        logger.exception("startup FAILED — service stays not-ready")
