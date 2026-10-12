"""Quiesce the process pools that run fire-and-forget work before a PostgreSQL
test schema is dropped (see ``_isolated_postgres_scope`` in this directory's
conftest).

Why: ``DROP SCHEMA ... CASCADE`` takes ACCESS EXCLUSIVE on the schema's tables
one at a time. Work a test queued -- e.g. the Memory KG ingest that
``create_memory_from_answer`` submits to the KG job pool -- can still be inside
a transaction then; once it holds a row lock on a table the drop has not
reached and writes one the drop already holds, PostgreSQL aborts the drop with
``DeadlockDetected`` in the teardown (``test_promotion_provenance_pg``, CI on
PR #844 and #846). Closing the repository does not prevent it: the pool close
leaves a checked-out connection's transaction running. The root conftest's
maintenance drain runs too late for this -- autouse fixtures are torn down
after the scope.

Covered: the KG job/window pools (``app.services.kg.scheduler``) and every
maintenance pool (``app.services.background_jobs._executors``). NOT covered:
work on unpooled threads (non-maintenance ``background_jobs.submit`` daemon
threads, ``embed-*`` threads, ask/report threads, unfired ``threading.Timer``
projections) -- nothing in this process counts it.
"""
from __future__ import annotations

import time
from contextlib import ExitStack

# Bound for the pre-drop drain. Background work in the PostgreSQL lane runs on
# fake models, so a pool that is still busy after this long is stuck, not slow.
QUIESCE_SECONDS = 30.0

# The first timeout in this process, kept so every later scope fails at once
# naming it instead of waiting out the bound again behind the same stuck job
# (a local run without ``--maxfail`` would otherwise spend 30 s per remaining
# scope). Reset only by tests of this module.
_first_timeout: str | None = None


class BackgroundWorkStuck(RuntimeError):
    """Pooled background work did not go idle before a schema drop."""


def _all_pools_idle_atomically() -> bool:
    """One consistent reading of every pool: True only if nothing is queued,
    waiting or running anywhere.

    All the locks are held together, so nothing can submit, start or finish
    between the readings. That is what makes a chain safe to observe: a job
    that submits follow-up work (to its own pool, another maintenance pool, a
    maintenance pool created just now, or the KG pool) is counted as running
    until it returns, and the follow-up is counted from the moment it is
    submitted -- so at any single instant some link of a live chain is
    visible. Holding the locks together cannot deadlock: production code
    never holds two of them at once (each is taken alone, around counter
    updates only), and ``_executor_lock`` is taken first here as in
    ``_maintenance_executor``.
    """
    from app.services import background_jobs
    from app.services.kg import scheduler as kg_scheduler

    with background_jobs._executor_lock:
        executors = tuple(background_jobs._executors.values())
        with ExitStack() as held:
            held.enter_context(kg_scheduler._active_lock)
            for executor in executors:
                held.enter_context(executor._condition)
            kg_idle = (
                kg_scheduler._window_active == 0 and kg_scheduler._job_active == 0
                and kg_scheduler._window_waiting == 0
                and kg_scheduler._job_waiting == 0
            )
            return kg_idle and all(
                not executor._pending and not executor._active
                for executor in executors
            )


def _busy_summary() -> str:
    from app.services import background_jobs
    from app.services.kg import scheduler as kg_scheduler

    with background_jobs._executor_lock:
        executors = tuple(background_jobs._executors.values())
    pools = ", ".join(
        f"{executor.pool}(queued={len(executor._pending)}, running={executor._active})"
        for executor in executors
    )
    return f"kg={kg_scheduler.stats()}; maintenance=[{pools}]"


def _stuck(timeout: float) -> BackgroundWorkStuck:
    global _first_timeout
    _first_timeout = (
        f"pooled background work was still busy {timeout:.0f}s into a PostgreSQL "
        f"scope teardown: {_busy_summary()}"
    )
    return BackgroundWorkStuck(
        _first_timeout + " -- the schema is left in place rather than dropped "
        "under (and deadlocking with) that work"
    )


def quiesce_process_background_work(timeout: float = QUIESCE_SECONDS) -> None:
    """Wait, bounded, until every pool is idle in one consistent reading.

    Each round waits for the KG pools, then for every maintenance pool in a
    FRESH snapshot (a pool created while waiting is included next round), then
    takes the atomic reading; only that reading ends the wait, so follow-up
    work between any two pools is never missed. On timeout it raises
    ``BackgroundWorkStuck`` and remembers why; after that, a later call does
    not wait again: it returns if the pools have since gone idle and otherwise
    fails at once, citing the first timeout.
    """
    from app.services import background_jobs
    from app.services.kg import scheduler as kg_scheduler

    if _first_timeout is not None:
        if _all_pools_idle_atomically():
            return
        raise BackgroundWorkStuck(
            "still stuck since an earlier scope teardown in this process; not "
            f"waiting again. First timeout: {_first_timeout}. Now: {_busy_summary()}"
        )
    deadline = time.monotonic() + timeout
    while True:
        if not kg_scheduler.wait_idle(max(0.0, deadline - time.monotonic())):
            raise _stuck(timeout)
        with background_jobs._executor_lock:
            executors = tuple(background_jobs._executors.values())
        for executor in executors:
            if not executor.wait_idle(deadline):
                raise _stuck(timeout)
        if _all_pools_idle_atomically():
            return
        if time.monotonic() >= deadline:
            raise _stuck(timeout)
