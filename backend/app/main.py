import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.auth_routes import auth_router
from app.api.agent_mcp_onboarding import (
    AGENT_MCP_ONBOARDING_PATH,
    agent_mcp_onboarding_router,
)
from app.api.debug_logs import router as debug_logs_router
from app.api.deps import (
    USER_MESSAGE_HEADER,
    get_current_user,
    mcp_memory_repository,
    model_provider_if_initialized,
    repository,
    shutdown_repository_if_initialized,
)
from app.api.ask_routes import public_router as public_conversation_router
from app.api.knowhow_agent_routes import agent_router as knowhow_agent_router
from app.api.report_routes import public_router as public_report_router
from app.api.mcp_server import (
    create_memory_mcp,
    mcp_public_tools,
    validate_mcp_deployment,
)
from app.api.routes import router
from app.core import diagnostics_runtime as diagnostics
from app.core import readiness
from app.core.config import env_file_diagnosis, get_settings
from app.core.event_logging import EventLogger, new_id
from app.bootstrap import application_extension_runtime
from app.services.model_provider import validate_process_local_scheduler_deployment
from app.services.pending_bus import pending_bus

logger = logging.getLogger("silicon_notebook.startup")

# Routes reachable BEFORE warm-up finishes (everything else 503s until ready):
# the anonymous liveness/readiness probes + API docs. CORS preflight (OPTIONS)
# is always allowed so the browser can even learn about the 503.
_READINESS_OPEN_PATHS = frozenset(
    {
        "/",
        "/api/ready",
        AGENT_MCP_ONBOARDING_PATH,
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
    }
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Run migration + cache warm-up OFF the event loop (daemon thread) so uvicorn
    # binds and serves /api/ready immediately; the readiness gate keeps app routes
    # at 503 until run_startup() flips readiness. See app/services/startup_warmup.
    from app.services.startup_warmup import (
        LIFESPAN_PASSTHROUGH,
        LifecycleAlreadyActiveError,
        begin_lifecycle_or_passthrough,
        close_repository,
        run_startup,
    )

    # Classify this lifespan atomically under the ONE lifecycle lock (see
    # begin_lifecycle_or_passthrough). Reserve ownership before resetting
    # readiness or touching the composition factory; an overlapping lifespan
    # fails before yield so it cannot become an unowned server context that
    # outlives the actual repository owner. Tests may mark readiness ready
    # up-front and drive a preconstructed repository — that context passes
    # through and must not reset/replace the fixture-owned state.
    #
    # This MUST be one decision, not readiness.is_ready() composed with
    # is_lifecycle_active(): those took two separate locks, so the ready/owner
    # verdict could straddle two moments (TOCTOU) — a pass-through entered after
    # readiness stopped, or another lifecycle reserved before the yield and this
    # branch's shutdown then closed the winner's cached repository.
    decision = begin_lifecycle_or_passthrough()
    if decision is None:
        raise LifecycleAlreadyActiveError(
            "cannot enter ASGI lifespan while the repository lifecycle is active"
        )
    if decision is LIFESPAN_PASSTHROUGH:
        try:
            yield
        finally:
            shutdown_repository_if_initialized()
        return

    lease = decision
    started = {"repository": None}

    def _start() -> None:
        started["repository"] = run_startup(lease)

    startup_thread = threading.Thread(
        target=_start, name="startup-warmup", daemon=True
    )
    startup_thread.start()
    try:
        yield
    finally:
        # Do not close a PostgreSQL pool while the migration/warmup thread may
        # still be borrowing it. Production shutdown is rare and correctness
        # is more important than a short shutdown timeout.
        await asyncio.to_thread(startup_thread.join)
        close_repository(lease, started["repository"])


def _diagnostic_concurrency_snapshot(*, models=None) -> dict:
    from app.services.kg import scheduler

    result = {"kg": scheduler.stats()}
    if models is None:
        models = model_provider_if_initialized()
        if models is None:
            return result
    registry = getattr(models, "registry", None)
    if registry is None:
        return result

    # runtime.json 保持有界、无凭据的类别级视图；按物理服务的
    # 调度细节和故障定位由管理员模型服务状态接口提供。
    groups: dict[str, dict[str, int]] = {}
    group_for_kind = {"chat": "llm", "embedding": "embedding", "rerank": "rerank"}
    for service in registry.services():
        group = group_for_kind.get(service.kind)
        if group is None:
            continue
        snapshot = models.scheduler_snapshot(service.id)
        aggregate = groups.setdefault(
            group, {"active": 0, "maximum": 0, "waiting": 0}
        )
        aggregate["active"] += snapshot.active
        aggregate["maximum"] += snapshot.maximum
        aggregate["waiting"] += snapshot.queued
    result.update(groups)
    return result


def _env_file_preflight() -> None:
    """缺 .env 时提前出声,不让整站静默跑在默认空配置上(embed/LLM 未配置 →
    chunk 检索静默落进全库暴力路径,大库表现为半小时级「思考中」假死)。

    - 缺 .env 且根目录有改名残骸(如 .env.local)→ 硬报错并点名(Next.js 启动
      打印的「Environments: .env.local」只代表前端自己读到了,后端只读 .env);
      纯环境变量部署可设 ALLOW_NO_ENV_FILE=1 显式跳过(降级必须 opt-in)。
    - 单纯缺失(全新 checkout / 容器注入环境变量)→ 仅告警,可启动。"""
    env_path, exists, lookalikes = env_file_diagnosis()
    if exists:
        return
    if lookalikes and os.environ.get("ALLOW_NO_ENV_FILE") != "1":
        raise RuntimeError(
            f"缺 {env_path},但发现疑似改名残骸: {', '.join(lookalikes)} — "
            f"后端只读仓库根 .env(Next.js 的「Environments: .env.local」只代表前端)。"
            f"请改回,例如: mv {env_path.parent / lookalikes[0]} {env_path};"
            f"若确要仅用系统环境变量启动,设 ALLOW_NO_ENV_FILE=1。")
    logger.warning(
        "缺 %s — 仅用系统环境变量+默认值启动(模型未配置时检索/问答会退化);"
        "参照 .env.example 创建,或忽略本警告(容器纯环境变量部署)。", env_path)


def create_app() -> FastAPI:
    validate_process_local_scheduler_deployment()
    _env_file_preflight()
    settings = get_settings()

    bind_host = os.environ.get("BACKEND_HOST", os.environ.get("HOST", "127.0.0.1"))
    mcp_public_url = os.environ.get(
        "MCP_PUBLIC_URL", f"http://{bind_host}:8000/mcp"
    )
    # Product default: DO NOT require HTTPS for MCP. This is an intranet-friendly
    # default — remote plain HTTP is allowed and Host/Origin (DNS-rebinding)
    # checks are relaxed — accompanied by a loud startup warning. Set
    # MCP_REQUIRE_HTTPS=1 on any public deployment to restore the fail-closed
    # guard (HTTPS enforced + Host/Origin validated).
    require_https = os.environ.get("MCP_REQUIRE_HTTPS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    validate_mcp_deployment(bind_host, mcp_public_url, require_https=require_https)
    extension_runtime = application_extension_runtime()
    agent_tool_log = EventLogger(settings, channel="events", per_user=True)
    mcp_server, mcp_app = create_memory_mcp(
        mcp_memory_repository,
        allowed_origins=settings.cors_origins,
        public_url=mcp_public_url,
        require_https=require_https,
        agent_tool_provider_host=extension_runtime.agent_tools,
        agent_tool_audit_sink=agent_tool_log.emit,
    )
    public_agent_tools = mcp_public_tools(mcp_server)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Mounted Starlette applications do not have their lifespan entered by
        # FastAPI automatically.  Explicit composition is required by the MCP
        # SDK's stateful Streamable HTTP session manager.  Compose that manager
        # with the repository migration/cache warm-up readiness lifecycle.
        inner = mcp_server.streamable_http_app()
        async with inner.router.lifespan_context(inner):
            root_dir = Path(__file__).resolve().parents[2]
            with diagnostics.activate_runtime(
                root_dir / ".local" / "diagnostics",
                readiness_provider=readiness.snapshot,
                concurrency_provider=_diagnostic_concurrency_snapshot,
            ):
                async with _lifespan(_app):
                    yield

    # 日志归档：启动时后台扫一遍「非今天」的天文件并 gzip，best-effort、不阻塞启动。
    from app.core.event_logging import archive_stale_days, _archive_pool
    try:
        _archive_pool.submit(archive_stale_days, settings)
    except Exception:  # pragma: no cover - 归档接线绝不阻断启动
        logger.warning("log archive sweep 提交失败（不影响启动）", exc_info=False)

    # 启动路径日志：一眼可查 DB/storage/日志目录实际解析到哪里（uvicorn 控制台
    # 可见），根治「CLI 建索引 vs 服务启动」CWD 不一致导致数据分裂却无从察觉
    # 的问题。storage_dir/database_url 经 config.py 的 model_validator 锚定到
    # 仓库根后此处一定是绝对路径；event_log_dir 独立锚定（event_logging._ROOT_DIR）。
    from app.core.database_url import database_status

    database = database_status(settings.database_url)
    storage_dir = settings.storage_dir
    log_dir = settings.event_log_dir
    if not Path(log_dir).is_absolute():
        from app.core.event_logging import _ROOT_DIR as _LOG_ROOT_DIR
        log_dir = str(_LOG_ROOT_DIR / log_dir)
    logger.info("paths: %s storage=%s log_dir=%s", database, storage_dir, log_dir)

    # 启动 autotune 报告：一眼可查本次进程实际解析到的核绑定旋钮值（KG_CLUSTER_ANN_THREADS
    # 未显式设时按 min(cpu,32) 自动推导；回填进程池默认值同理）。模型服务容量
    # 由 MODEL_SERVICES_CONFIG 的 max_concurrency 管理，不随本机核数缩放。
    from app.services.batch_ingest import _BACKFILL_DEFAULT_WORKERS
    logger.info(
        "autotune: kg_cluster_ann_threads=%d backfill_default_workers=%d "
        "(模型服务 max_concurrency 与本机核数无关,不自动缩放)",
        settings.kg_cluster_ann_threads, _BACKFILL_DEFAULT_WORKERS,
    )

    app = FastAPI(
        title="silicon-notebook API",
        version="0.1.0",
        description="Local beta API for semiconductor knowhow notebooks.",
        lifespan=lifespan,
    )
    app.state.extension_registry = extension_runtime.registry

    request_log = EventLogger(settings, channel="requests")

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        request_id = new_id("req")
        start = time.perf_counter()
        client = request.client.host if request.client else ""
        with diagnostics.request_scope(
            request_id, request.method, request.url.path
        ):
            try:
                with diagnostics.diagnostic_phase("http.dispatch"):
                    response = await call_next(request)
            except Exception as exc:
                latency_ms = round((time.perf_counter() - start) * 1000)
                request_log.emit(
                    {
                        "id": request_id,
                        "kind": "http",
                        "method": request.method,
                        "path": request.url.path,
                        "status": "error",
                        "latency_ms": latency_ms,
                        "client": client,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                raise
            latency_ms = round((time.perf_counter() - start) * 1000)
            slow = latency_ms >= settings.slow_request_ms
            status = "slow" if slow else (
                "ok" if response.status_code < 500 else "error"
            )
            request_log.emit(
                {
                    "id": request_id,
                    "kind": "http",
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "status": status,
                    "latency_ms": latency_ms,
                    "client": client,
                }
            )
            response.headers["X-Request-Id"] = request_id
            return response

    @app.middleware("http")
    async def readiness_gate(request: Request, call_next):
        # Until startup warm-up finishes, every app route (incl. login) returns
        # 503 so the frontend shows a "服务启动中" screen instead of hanging on a
        # cold path — and no request constructs the repo mid-migration. Fast path
        # once ready (the overwhelmingly common case) is a single flag read.
        if (
            readiness.is_ready()
            or request.method == "OPTIONS"          # CORS preflight
            or request.url.path in _READINESS_OPEN_PATHS
        ):
            return await call_next(request)
        snap = readiness.snapshot()
        return JSONResponse(
            status_code=503,
            content={
                "detail": "service starting",
                "ready": False,
                "phase": snap["phase"],
                "progress": snap.get("detail", ""),
                "warmed_notebooks": snap.get("warmed_notebooks", 0),
                "total_notebooks": snap.get("total_notebooks", 0),
                "error": snap.get("error"),
            },
            headers={"Retry-After": "2"},
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # X-User-Message 是错误层的信任标记（见 api/deps.py 的 user_error）：
        # 不 expose 的话跨源部署时 JS 根本读不到它，后端所有中文用户文案都会被
        # 前端当成「没标记」而压平成通用文案。同源开发和单测都察觉不到这个坑，
        # 故在此显式登记（test_user_error.py 有守卫）。
        expose_headers=["X-Request-Id", USER_MESSAGE_HEADER],
    )

    @app.get("/")
    def root() -> dict:
        return {
            "service": "silicon-notebook",
            "status": "ok",
            "docs": "/docs",
            "api": "/api",
        }

    @app.get("/api/ready")
    def ready() -> dict:
        """Anonymous readiness probe the frontend polls before showing the app.
        Reports migration / warm-up progress; ``{"ready": true}`` once fully
        warm. Never constructs the repository (reads the process readiness flag),
        so it answers instantly even while migration is still running."""
        return readiness.snapshot()

    app.include_router(auth_router, prefix="/api")  # 公开：注册/登录/登出
    # 机器可读的接入说明不含 token，也不要求先有浏览器 session。用户把这条
    # URL 与另行签发的一次性明文 token 一起交给 Agent，Agent 才能在尚未接通
    # MCP 的前提下先读取配置步骤。MCP_PUBLIC_URL 是说明里唯一的服务地址真源。
    app.include_router(
        agent_mcp_onboarding_router(mcp_public_url, public_agent_tools),
        prefix="/api",
    )
    app.include_router(
        router, prefix="/api", dependencies=[Depends(get_current_user)]
    )  # 其余全部需登录（router 级依赖：零逐路由遗漏）
    # 公开分享的深度报告：唯一不需要 session 的读取，所以不能挂在上面那个带
    # router 级 get_current_user 的 router 上。token 本身就是全部授权。
    app.include_router(public_report_router, prefix="/api")
    # 公开分享的问答会话:同理,唯一不需要 session 的会话读取,token 即全部授权,
    # 不能挂在带 router 级 get_current_user 的主 router 上(T3)。
    app.include_router(public_conversation_router, prefix="/api")
    app.include_router(debug_logs_router, prefix="/api")
    # Knowhow-tables agent surface (PR-2+3 Task 10): session OR Agent Bearer
    # token, NOT the blanket get_current_user router dependency above — an
    # Agent token is not a valid session token, so it must never reach that
    # guard first. Every route resolves its own auth (app.api.deps.
    # require_user_or_agent / user_or_agent_scope).
    app.include_router(knowhow_agent_router, prefix="/api")
    app.mount("/mcp", mcp_app, name="memory-mcp")

    # 待确认中心事件总线：注入 recompute，供后台 job（mark_dirty）与流式端点
    # 复用同一份快照计算口径（app.api.system_routes 里初始 snapshot 直接调
    # repository().pending_actions，不走这个私有 _recompute）。
    pending_bus.set_recompute(lambda uid: repository().pending_actions(uid))

    return app


app = create_app()
