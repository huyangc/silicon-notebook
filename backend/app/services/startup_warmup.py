"""Background startup: migrate, recover and warm online caches before readiness.

Kicked off in a daemon thread by the FastAPI lifespan so uvicorn serves
``/api/ready`` immediately while every app route stays 503 until warm-up
completes. The first login after a restart therefore never pays migration + the
cold per-notebook count recompute (``knowledge_counts_cache`` starts empty each
process). It then strictly preloads every published scale index, enabled ANN
handle, and safely reusable single-index PPR core. Those costs move behind the
readiness gate so users see a startup screen instead of a first-query stall;
cross-notebook combined graphs stay lazy because eagerly copying every mounted
10M-node combination is not memory-safe.

Crash recovery lives here, not in ``SQLiteRepository.__init__``: this module is
the ONLY place that owns the "this process is the server, everything still
marked in-progress is last boot's wreckage" claim. Offline CLIs construct their
own repository and must not make that claim (they would flip rows the running
server is still working on). It runs BEFORE ``mark_ready()`` so no route can
accept new work while the wreckage is still standing.

Robustness: migration or scale preload failure keeps the service not-ready;
per-notebook count warm failures remain best-effort inside
``warm_open_path_caches``. Set ``STARTUP_PRELOAD_SCALE_INDEXES=false`` only as
an explicit recovery escape hatch. The one-shot knowhow
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
    # Stop callback for the extension-admission refresher this cycle started,
    # filed here for the same reason as the two above: shutdown has the lease,
    # not the handle. Keeping it per-cycle rather than reaching for the
    # module-global stop is what makes a defensive early return incapable of
    # reaping a *successor* lifecycle's refresher.
    admission_refresher_stop: object | None = None
    # Stop callback for the notebook-delete-job sweeper this cycle started
    # (batch 3·W1 PR-3 §T-4) — same per-cycle filing reason as the admission
    # refresher immediately above.
    notebook_delete_sweeper_stop: object | None = None


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


# Sentinel: an entering lifespan may pass THROUGH without owning any lifecycle —
# a test that pre-marked readiness ready and drives a preconstructed repository.
LIFESPAN_PASSTHROUGH = object()


def begin_lifecycle_or_passthrough() -> object | None:
    """Atomically classify an entering ASGI lifespan under the single lifecycle
    lock, so the ready/owner decision cannot observe two different moments.

    Replaces the raced ``readiness.is_ready() and not is_lifecycle_active()``
    composition (two separate locks): between the two reads a lifespan could pass
    through after readiness was stopped, or reserve while another owner already
    existed — and a pass-through's shutdown could then close the winner's cached
    repository. Deciding everything in one critical section removes that window.

    Returns exactly one of:

    - ``None`` — a lifecycle is already owned; the caller must fail before yield.
    - :data:`LIFESPAN_PASSTHROUGH` — already ready with no active lifecycle: a
      pre-marked-ready context that owns nothing and must not construct, reset
      readiness, or close anyone's repository; it just yields. (No reservation
      happens here, so readiness stays ready for the pass-through's lifetime —
      no concurrent entry can reserve and become a repository the pass-through's
      shutdown would wrongly close.)
    - a fresh lease — not ready and unowned: reserve the process lifecycle here
      (identical effect to :func:`begin_lifecycle`) and return the lease so the
      caller can drive ``run_startup`` and later ``close_repository``.
    """
    global _active_lifecycle
    with _cleanup_lock:
        if _active_lifecycle is not None:
            return None
        if readiness.is_ready():
            return LIFESPAN_PASSTHROUGH
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


def _begin_index_preload(lease: object) -> bool:
    with _cleanup_lock:
        state = _active_lifecycle
        if state is None or state.lease is not lease or state.status != "warming":
            return False
        state.status = "preloading_indexes"
        readiness.set_phase("preloading_indexes", "预加载大库检索索引")
        return True


def _set_index_preload_progress(lease: object, done: int, total: int) -> None:
    with _cleanup_lock:
        state = _active_lifecycle
        if (
            state is not None
            and state.lease is lease
            and state.status == "preloading_indexes"
        ):
            readiness.set_detail(
                f"{done}/{total} 个索引",
                preloaded=done,
                total_indexes=total,
            )


def _mark_lifecycle_ready(lease: object, repo: object) -> bool:
    with _cleanup_lock:
        state = _active_lifecycle
        if (
            state is None
            or state.lease is not lease
            or state.repository is not repo
            or state.status not in {"warming", "preloading_indexes"}
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
    # The refresher goes first: a failure after warm-up began (the only window
    # in which one exists) must not leave a thread polling the pool this is
    # about to close, nor survive into the retry that replaces it.
    _stop_admission_refresher(lease)
    _stop_notebook_delete_sweeper(lease)
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
        # Cleanup is complete, but the failed ASGI context still owns this
        # lifecycle until its finally block releases the exact lease. Keeping
        # the tombstone prevents another context from becoming ready while the
        # failed one is still alive and serving the process-global gate.
        state.repository = None
        state.repository_factory = None
        state.status = "failed"
    # Never attach the original exception: a third-party driver may carry raw
    # conninfo in its traceback even though our adapter errors do not.
    logger.error("startup FAILED — service stays not-ready: %s", safe_error)


def _deployment_plugins_are_loaded() -> bool:
    """True when this process actually froze a deployment plugin.

    A pure in-memory question — the registry was frozen before the repository
    was composed — and the answer is almost always "no": the default
    deployment loads only built-in bundles, which the admin gate can never
    disable. Asking it here is what keeps a stock install (and the whole test
    suite) at zero background threads and zero periodic queries for a feature
    it does not use.
    """
    from app.bootstrap import application_extension_runtime

    runtime = application_extension_runtime()
    return any(
        manifest.trust == "deployment"
        for manifest in runtime.registry.manifests()
    )


def _start_admission_refresher(lease: object, repo: object) -> None:
    """Begin converging the admin extension-disable snapshot in this process.

    The snapshot itself is NOT best-effort — the composition root primed it
    loudly while building ``repo``. The two quiet returns below are the two
    legitimate "nothing to converge" shapes: a narrow test double with no
    ``extension_toggles`` seat, and a deployment with zero loaded deployment
    plugins. A *failure* to start the thread, by contrast, propagates and
    fails this startup: a serving process with deployment plugins loaded and
    no convergence mechanism would silently keep admitting a plugin another
    worker's admin disabled, breaking the documented one-refresh-cycle
    convergence guarantee — the same fail-loud posture extension discovery
    and route mounting already take.

    The stop handle is filed on this cycle's lifecycle state, so every later
    exit stops exactly the thread this cycle started.
    """
    store = getattr(getattr(repo, "_runtime", None), "extension_toggles", None)
    if store is None:
        return
    from app.core.config import get_settings
    from app.services.extension_toggles import (
        start_extension_admission_refresher,
    )

    if not _deployment_plugins_are_loaded():
        logger.info(
            "startup: no deployment plugins loaded; extension admission "
            "refresher not started"
        )
        return
    stop = start_extension_admission_refresher(
        store, get_settings().extension_admission_refresh_seconds
    )
    if not _record_admission_refresher(lease, stop):
        # Defensive: the lease was detached between the bind above and this
        # line, so nobody's shutdown owns this thread. Reap it here rather
        # than leaving it polling a repository no one will close.
        _run_admission_refresher_stop(stop)


def _record_admission_refresher(lease: object, stop: object) -> bool:
    with _cleanup_lock:
        state = _active_lifecycle
        if state is None or state.lease is not lease:
            return False
        state.admission_refresher_stop = stop
        return True


def _run_admission_refresher_stop(stop: object) -> None:
    try:
        stop()  # type: ignore[operator]
    except Exception:  # shutdown hygiene must never mask the real diagnostic
        logger.error("extension admission refresher stop failed")


def _stop_admission_refresher(lease: object) -> None:
    """Stop the convergence thread this exact lifecycle started, if any.

    Lease-scoped rather than process-global on purpose: a stale caller (a
    defensive early return whose lease was detached) must not be able to reap
    the refresher of whichever cycle owns the process now. Idempotent — the
    handle is taken out of the state, so the overlapping paths (an early
    return whose lifespan then also closes) cannot double-stop.

    It runs BEFORE the pool is closed: the thread borrows a connection every
    tick, and a refresh landing on a closing pool would log a database error
    that has nothing to do with the shutdown actually in progress.
    """
    with _cleanup_lock:
        state = _active_lifecycle
        if state is None or state.lease is not lease:
            return
        stop = state.admission_refresher_stop
        state.admission_refresher_stop = None
    if stop is not None:
        _run_admission_refresher_stop(stop)


def _sweep_notebook_delete_jobs_on_start(repo: object) -> None:
    """One-shot delete-job sweep at startup (batch 3·W1 PR-3 §T-4), run in
    the SAME place and for the SAME reason as ``_sweep_agent_profile_chains``
    immediately below: only the server startup path may claim "everything
    still 'running'/'queued'/'waiting' is last boot's wreckage", and it must
    run before ``mark_ready()`` admits new work. ``getattr``-shaped so a
    narrow test double with no ``notebook_delete`` seat is not an error."""
    runner = getattr(getattr(repo, "_runtime", None), "notebook_delete", None)
    sweep = getattr(runner, "sweep_once", None)
    if not callable(sweep):
        return
    try:
        submitted = int(sweep() or 0)
    except Exception:
        logger.exception("startup: notebook delete job sweep failed")
        return
    if submitted:
        logger.info(
            "startup: resubmitted %d stranded notebook delete job(s)", submitted
        )


def _start_notebook_delete_sweeper(lease: object, repo: object) -> None:
    """Begin the periodic notebook-delete-job sweep in this process (batch
    3·W1 PR-3 §T-4's two drivers, run every ``NOTEBOOK_DELETE_SWEEP_
    SECONDS``). Mirrors ``_start_admission_refresher``'s placement and
    failure posture exactly: started only after the lease is bound (so an
    early-return path before the bind owns no thread to reap), best-effort
    (a narrow test double or a zero/negative interval is not a startup
    failure — unlike the admission refresher, an idle delete sweeper is
    always a legitimate steady state, not a missing dependency)."""
    runner = getattr(getattr(repo, "_runtime", None), "notebook_delete", None)
    if runner is None:
        return
    from app.core.config import get_settings
    from app.services.notebook_delete import start_notebook_delete_sweeper

    interval = get_settings().notebook_delete_sweep_seconds
    if not interval > 0:
        return
    stop = start_notebook_delete_sweeper(runner, interval)
    if not _record_notebook_delete_sweeper(lease, stop):
        _run_notebook_delete_sweeper_stop(stop)


def _record_notebook_delete_sweeper(lease: object, stop: object) -> bool:
    with _cleanup_lock:
        state = _active_lifecycle
        if state is None or state.lease is not lease:
            return False
        state.notebook_delete_sweeper_stop = stop
        return True


def _run_notebook_delete_sweeper_stop(stop: object) -> None:
    try:
        stop()  # type: ignore[operator]
    except Exception:  # shutdown hygiene must never mask the real diagnostic
        logger.error("notebook delete sweeper stop failed")


def _stop_notebook_delete_sweeper(lease: object) -> None:
    """Stop the sweep thread this exact lifecycle started, if any. Same
    lease-scoped idempotence and "before the pool closes" ordering as
    ``_stop_admission_refresher`` — the sweeper's tick borrows a connection
    too."""
    with _cleanup_lock:
        state = _active_lifecycle
        if state is None or state.lease is not lease:
            return
        stop = state.notebook_delete_sweeper_stop
        state.notebook_delete_sweeper_stop = None
    if stop is not None:
        _run_notebook_delete_sweeper_stop(stop)


def _sweep_agent_profile_chains(repo: object) -> None:
    """Settle understanding-consolidation rows stranded by a previous process.

    ``getattr``-shaped like the relation-completion resume below it: a runtime
    that does not compose the service (a narrow test double) is not an error,
    and startup must never fail on crash-recovery hygiene.
    """
    service = getattr(getattr(repo, "_runtime", None), "agent_profile_jobs", None)
    sweep = getattr(service, "sweep_on_start", None)
    if not callable(sweep):
        return
    try:
        swept = int(sweep() or 0)
    except Exception:
        logger.exception("startup: agent profile chain sweep failed")
        return
    if swept:
        logger.info("startup: settled %d stranded understanding chain(s)", swept)


def _sweep_global_ask_jobs(repo: object) -> None:
    """Recover persisted work only on server startup, allowing narrow test runtimes."""
    store = getattr(getattr(repo, "_runtime", None), "global_ask_store", None)
    if store is not None:
        store.recover()


def _pool_budget_warning(settings: object) -> str | None:
    """Return a one-time startup warning when the PostgreSQL connection pool
    is sized smaller than the worst-case background job concurrency this
    process can run at once, or ``None`` when the budget looks safe.

    Three independent knobs each open their own PostgreSQL connections and
    have never been checked against each other: the heavy maintenance pool
    (``background_maintenance_concurrency`` — full KG rebuilds, the
    ``index-pipeline-*`` full-library reindex, merge review, conflict
    resolution), the light maintenance pool (``background_light_job_concurrency``),
    and KG extraction's own per-notebook fan-out (``kg_job_concurrency``). Each
    pool independently caps its OWN job count; nothing today caps how many of
    them run at once, so their worst-case connection demand is additive. The
    production defaults (4 + 4 + 8 = 16) already exceed the default
    ``postgres_pool_max_size`` of 10 — this is deliberately only a warning,
    not a refusal to start: most deployments never saturate every pool at
    once, and turning a rare peak-load slowdown into a hard boot failure would
    trade a recoverable problem for an unrecoverable one.

    The whole body is one best-effort block: this diagnostic must never be
    able to fail — let alone change the *shape* of — a startup that would
    otherwise succeed or fail for an unrelated reason. Several existing
    tests intentionally construct a minimal settings double (e.g.
    ``SimpleNamespace(database_url=...)``) that carries none of the pool
    knobs; an unguarded attribute read here would raise ``AttributeError``
    from inside ``run_startup``'s try block and get misreported as a
    (redacted) startup failure cause instead of the test's own injected one.
    """
    try:
        from app.core.database_url import database_identity

        identity = database_identity(str(settings.database_url))
        if identity.scheme != "postgresql":
            return None  # pool sizing is meaningless for the SQLite backend

        heavy = int(settings.background_maintenance_concurrency)
        light = int(settings.background_light_job_concurrency)
        kg = int(settings.kg_job_concurrency)
        # 本批新增的两路有界 DB 消费者也计入保守预算(codex #627 R4 P2):搜索闸的
        # 每个执行位与 scale build 的每个并发位都各占一条连接,且与三个维护池相互
        # 独立——漏算它们会把 1+1+1 vs pool=5 这类实际会被 4 路搜索打穿的配置误判
        # 为安全。部署文档把这两个旋钮明确绑到池容量上,预算口径必须一致。
        search = int(settings.search_concurrency_limit)
        scale = int(settings.scale_build_concurrency)
        # 批 3·W1 的删除池同理(codex #659 R4):独立执行池,每个并发位跑的
        # 六相位作业全程吃数据库连接,漏算会让「其余各池刚好贴着池容量」的
        # 配置在删除启动时被打穿而无预警。
        delete = int(getattr(settings, "notebook_delete_concurrency", 1))
        # 全局问答占两层连接:每个任务线程自己做权限复核与进度保存,它再把
        # 联邦检索交给一个进程级共享的有界线程池——``global_ask_retrieval_concurrency``
        # 是全局问答联邦检索的进程级共享执行器大小,那个池的每个执行位也各占
        # 一条连接。只算任务并发会把「库多时检索池满员」的真实峰值漏掉一半。
        global_ask_jobs = int(getattr(settings, "global_ask_max_concurrent", 0))
        global_ask_retrieval = int(
            getattr(settings, "global_ask_retrieval_concurrency", 0)
        )
        global_ask = global_ask_jobs + global_ask_retrieval
        # 单库(含挂载参考库)内的 chunk 联邦扇出自建一个独立的有界线程池
        # (``chunk_fanout_max_workers``,默认 8),与上面全局问答的共享执行器是
        # 两条完全独立的池——即使只问一个库、从不触发全局问答也会占用它。今天
        # 从未进过这份预算,漏算它会让「其余各池刚好贴着池容量」的配置在扇出
        # 打满时被打穿而无预警。
        chunk_fanout = int(getattr(settings, "chunk_fanout_max_workers", 0))
        budget = heavy + light + kg + search + scale + delete + global_ask + chunk_fanout
        pool_max = int(settings.postgres_pool_max_size)
        if pool_max > budget:
            return None
        return (
            f"pool-budget: POSTGRES_POOL_MAX_SIZE={pool_max} <= "
            f"重活维护池({heavy})+轻活维护池({light})+KG 分析并发({kg})"
            f"+搜索并发({search})+scale 构建并发({scale})"
            f"+删除作业并发({delete})"
            f"+全局问答并发(任务{global_ask_jobs}+检索{global_ask_retrieval}"
            f"={global_ask})+chunk 联邦扇出({chunk_fanout})={budget}；"
            "高峰期后台 job、搜索与索引构建可能耗尽连接池并让前台请求排队甚至超时。"
            f"建议把 POSTGRES_POOL_MAX_SIZE 调到至少 {budget + 1}。"
        )
    except Exception:  # noqa: BLE001 — diagnostic-only; never affects startup
        return None


#: Chat workloads whose own feature flag can be ON while nothing is bound to
#: them. Each entry is ``(workload id, settings flag)``. These get their own
#: WARNING line on top of the summary one, because "the operator switched this
#: feature on and it silently does nothing" is a different — and louder —
#: condition than "this workload was never wired up". Both members were added
#: by Agentic Memory P1/P2, i.e. AFTER most deployments generated their
#: ``model-services.toml``, which is exactly how a stale file turns a feature
#: the operator believes is running into a job that only ever settles as
#: ``failed:模型未配置，无法整理``.
_FEATURE_GATED_CHAT_WORKLOADS: tuple[tuple[str, str], ...] = (
    ("agent_profile_consolidate", "agent_profile_enabled"),
    ("retrieval_experience_distill", "retrieval_experience_enabled"),
)


def _unbound_workload_warnings(settings: object, models: object) -> tuple[str, ...]:
    """Return the startup WARNING lines about this deployment's [bindings].

    Why this exists: the registry answers an unbound workload with ``None`` and
    every caller fails soft, so a ``model-services.toml`` generated before a
    workload existed produces no error anywhere — only a feature that quietly
    never runs. A binding file is generated once and then carried across
    upgrades, so every NEW workload starts life unbound in every existing
    deployment. One summary line names all of them at once rather than one line
    per workload: an operator scrolling a boot log reads a single sentence, not
    a screen of near-identical warnings.

    Since ``MODEL_BINDINGS_STRICT`` (default on) turns exactly these two
    conditions into a refusal to start, every line below is reachable only in a
    deployment that switched the gate off — plus the offline notice, which
    strict mode deliberately never covers.

    Scope matches the strict gate exactly — ALL three workload kinds, not just
    chat. An unbound embedding or rerank workload degrades retrieval just as
    silently as an unbound chat one, and a warning that is narrower than the
    refusal it replaces would tell a deployment running with the gate off that
    it is fine when it is not.

    A deployment with NO model services at all is not silently exempt any more:
    an empty ``MODEL_SERVICES_CONFIG`` is the supported offline/deterministic
    runtime (the same claim ``RuntimeModelProvider.primary_unconfigured``
    makes), but "everything that needs a model is answering deterministically"
    is worth one line before READY. Listing each unbound workload there would
    still be noise about a state the operator chose, so it stays ONE line, not
    forty.

    Workload ids and their labels are a closed, in-code vocabulary and are
    always safe to name. The stale ids on the last line are not — they are
    arbitrary TOML keys copied out of the deployment's file, so they go through
    ``model_registry.loggable_id_list``, the same sanitiser the startup refusal
    uses: anything not shaped like a workload id collapses into a counted
    placeholder rather than putting a path (or a forged extra line) in the log.

    Fail-open like ``_pool_budget_warning`` above and for the same reason: a
    diagnostic must never be able to change the shape of a startup that would
    otherwise succeed. Any settings double or provider stub that lacks these
    attributes yields no warnings rather than an ``AttributeError`` reported as
    a startup failure.
    """
    try:
        from app.services.model_registry import WORKLOADS, loggable_id_list

        if not models.registry.services():
            # 只说现象:判据是「注册表里一个可用服务都没有」,而不是读到了空的
            # MODEL_SERVICES_CONFIG——后者只是最常见的一种成因。
            return (
                "model-bindings: 当前没有可用的模型服务——"
                "所有需要模型的功能将降级为确定性回复。",
            )
        unbound = tuple(
            workload
            for workload in WORKLOADS.values()
            if not models.configured(workload.id)
        )
        lines: list[str] = []
        if unbound:
            listed = "，".join(
                f"{workload.display_label}（{workload.id}）" for workload in unbound
            )
            lines.append(
                "model-bindings: 以下工作负载在 MODEL_SERVICES_CONFIG 的 "
                f"[bindings] 里没有绑定，对应功能会静默不可用：{listed}。"
                "升级后请重新生成 model-services.toml 或补齐新增工作负载的绑定。"
            )
            unbound_ids = {workload.id: workload for workload in unbound}
            for workload_id, flag in _FEATURE_GATED_CHAT_WORKLOADS:
                workload = unbound_ids.get(workload_id)
                if workload is None or not bool(getattr(settings, flag, False)):
                    continue
                lines.append(
                    "model-bindings: 特性已开但模型未绑定："
                    f"{workload.display_label}（{workload.id}）"
                    "——请在 model-services.toml 的 [bindings] 补绑定或重新生成配置"
                )
        # Appended last so the existing lines keep their positions, and read
        # through a tolerant accessor: a provider double predating this member
        # must lose THIS line, not every line above it.
        reader = getattr(models.registry, "unknown_bindings", None)
        stale = tuple(reader()) if callable(reader) else ()
        if stale:
            lines.append(
                "model-bindings: MODEL_SERVICES_CONFIG 的 [bindings]/[thinking] 里有"
                f"未知或已退役的 id，已忽略：{loggable_id_list(stale)}。"
                "请删除这些行；MODEL_BINDINGS_STRICT 为 false 才降级为本条告警，"
                "默认会因此拒绝启动。"
            )
        return tuple(lines)
    except Exception:  # noqa: BLE001 — diagnostic-only; never affects startup
        return ()


def run_startup(lease: object | None) -> object | None:
    """Construct the repository, recover interrupted work, warm caches, and
    then mark the service ready. Any exception is captured into readiness and
    must never crash the server.

    ``lease`` must have been obtained from :func:`begin_lifecycle` before this
    function is called. Returns the exact warmed repository instance. Lifespan
    shutdown must pass both that lease and instance to ``close_repository``.
    """
    if lease is None or not _start_lifecycle(lease):
        return None
    try:
        from app.services.model_provider import (
            validate_process_local_scheduler_deployment,
        )

        validate_process_local_scheduler_deployment()
        # One-time loud pool-budget disclosure: warn, never refuse to start
        # (see _pool_budget_warning for why this stays a warning).
        from app.core.config import get_settings

        pool_warning = _pool_budget_warning(get_settings())
        if pool_warning:
            logger.warning(pool_warning)
        logger.info("startup: constructing repository (runs schema migrations)…")
        # Imported lazily so module import stays cheap and side-effect free.
        from app.api.deps import repository as repository_factory

        if not _record_repository_factory(lease, repository_factory):
            return None
        repo = repository_factory()  # construct + migrate + seed
        # Server startup is the only owner allowed to settle rows left in an
        # in-progress state by the previous process. Offline CLIs must not make
        # this claim while a live backend may still own those jobs.
        repo._recover_interrupted_jobs()
        _sweep_global_ask_jobs(repo)
        # Agentic Memory P1 (T4): the same claim, for the same reason, for the
        # understanding-consolidation chains. It is NOT part of
        # ``_recover_interrupted_jobs`` because that one is a per-backend SQL
        # script while this sweep is a backend-neutral store method — but it
        # has to run in the same place and only here: rows left ``running`` by
        # a previous process hold their notebook's chain forever otherwise, and
        # an offline CLI has no right to make the "nothing else owns these
        # rows" claim while a live backend may.
        _sweep_agent_profile_chains(repo)
        _sweep_notebook_delete_jobs_on_start(repo)
        if not _bind_repository_and_begin_warmup(lease, repo):
            # Defensive only: a valid lease cannot be detached during startup.
            # If that invariant is ever broken, do not leak the just-created
            # composition root. There is deliberately no admission-refresher
            # stop here: this cycle starts one only once the bind below
            # succeeds, so it owns none — and a detached lease means some
            # *other* cycle may be the live owner, whose refresher is not this
            # branch's to reap.
            _close_repository_instance(repo)
            _clear_repository_cache(repository_factory)
            return None
        # This repository now owns the process, so it is the one whose toggle
        # rows this process should track. Started here rather than after
        # ``mark_ready`` so the very first admitted request already sees a
        # converging snapshot, and after the bind rather than before it so a
        # lifecycle that never becomes the owner cannot leave a thread polling
        # a repository nobody will close. Every later exit stops it again.
        _start_admission_refresher(lease, repo)
        _start_notebook_delete_sweeper(lease, repo)
        logger.info("startup: migrations + recovery done; warming open-path caches…")

        def _progress(done: int, total: int) -> None:
            _set_warmup_progress(lease, done, total)

        total = repo.warm_open_path_caches(progress=_progress)
        preload_summary = {"indexes": 0, "ann_handles": 0, "ppr_cores": 0}
        preload = getattr(repo, "_preload_scale_retrieval_artifacts", None)
        preload_enabled = bool(
            getattr(
                getattr(repo, "settings", None),
                "startup_preload_scale_indexes",
                False,
            )
        )
        if preload_enabled and callable(preload):
            if not _begin_index_preload(lease):
                _stop_admission_refresher(lease)
                _stop_notebook_delete_sweeper(lease)
                return None

            def _index_progress(done: int, count: int) -> None:
                _set_index_preload_progress(lease, done, count)

            logger.info(
                "startup: notebook caches warm; preloading scale retrieval artifacts…"
            )
            preload_summary = preload(progress=_index_progress)
        if not _mark_lifecycle_ready(lease, repo):
            _stop_admission_refresher(lease)
            _stop_notebook_delete_sweeper(lease)
            return None
        source_ingestion = getattr(
            getattr(repo, "_runtime", None), "source_ingestion", None
        )
        resume_completion = getattr(
            source_ingestion, "resume_pending_relation_completions", None
        )
        if callable(resume_completion):
            try:
                resumed = int(resume_completion() or 0)
            except Exception:
                logger.exception(
                    "startup: pending relation-completion scheduling failed"
                )
            else:
                if resumed:
                    logger.info(
                        "startup: scheduled %d pending relation-completion source(s)",
                        resumed,
                    )
        # Loud before READY, not after: an operator who reads only up to the
        # readiness line must still see that a feature they switched on has no
        # model behind it. Never a refusal to start from HERE — by the time a
        # repository exists the strict gate in main._model_bindings_preflight
        # has already run, so anything left is either the offline mode or a
        # deployment that explicitly set MODEL_BINDINGS_STRICT=false.
        for binding_warning in _unbound_workload_warnings(
            getattr(repo, "settings", None),
            getattr(getattr(repo, "_runtime", None), "models", None),
        ):
            logger.warning(binding_warning)
        logger.info(
            "startup: READY — %d notebook(s) warmed; %d scale index(es), "
            "%d ANN handle(s), %d PPR core(s) preloaded",
            total,
            preload_summary["indexes"],
            preload_summary["ann_handles"],
            preload_summary["ppr_cores"],
        )
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
    strict no-ops. The sole no-repository form is the exact lease of a failed
    startup whose pool/cache were already cleaned; its owning lifespan uses
    ``(lease, None)`` to release the retained failure tombstone. There is no
    wildcard form that could close another lifespan's repository.
    """
    global _active_lifecycle
    if lease is None:
        return
    with _cleanup_lock:
        state = _active_lifecycle
        if state is None or state.lease is not lease:
            return
        failed = state.status == "failed" and repository_instance is None
        ready = (
            state.status == "ready"
            and repository_instance is not None
            and state.repository is repository_instance
        )
        if not failed and not ready:
            return
        state.status = "closing_failed" if failed else "closing"
        repository_factory = None if failed else state.repository_factory

    # The reservation remains active while the exact pool is closed and its
    # cache entry cleared, so a fresh cycle cannot race cleanup. This cycle's
    # admission refresher is stopped BEFORE the close, and that order is the
    # invariant: the thread borrows a connection every tick, so reaping it
    # after the pool went away would log a database error belonging to nothing.
    # (The ``failed`` form already stopped it in ``_fail_lifecycle``; the
    # handle was taken out of the state there, so this is then a no-op.)
    _stop_admission_refresher(lease)
    _stop_notebook_delete_sweeper(lease)
    try:
        if not failed:
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
                and state.status == ("closing_failed" if failed else "closing")
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
