import logging
import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth_routes import auth_router
from app.api.debug_logs import router as debug_logs_router
from app.api.deps import get_current_user
from app.api.routes import router
from app.core.config import env_file_diagnosis, get_settings
from app.core.event_logging import EventLogger, new_id

logger = logging.getLogger("silicon_notebook.startup")


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
    _env_file_preflight()
    settings = get_settings()

    # 启动路径日志：一眼可查 DB/storage/日志目录实际解析到哪里（uvicorn 控制台
    # 可见），根治「CLI 建索引 vs 服务启动」CWD 不一致导致数据分裂却无从察觉
    # 的问题。storage_dir/database_url 经 config.py 的 model_validator 锚定到
    # 仓库根后此处一定是绝对路径；event_log_dir 独立锚定（event_logging._ROOT_DIR）。
    db_path = settings.sqlite_path
    storage_dir = settings.storage_dir
    log_dir = settings.event_log_dir
    if not Path(log_dir).is_absolute():
        from app.core.event_logging import _ROOT_DIR as _LOG_ROOT_DIR
        log_dir = str(_LOG_ROOT_DIR / log_dir)
    logger.info("paths: db=%s storage=%s log_dir=%s", db_path, storage_dir, log_dir)

    # 启动 autotune 报告：一眼可查本次进程实际解析到的核绑定旋钮值（KG_CLUSTER_ANN_THREADS
    # 未显式设时按 min(cpu,32) 自动推导；回填进程池默认值同理）。模型端并发旋钮
    # （KG_EXTRACT_WORKERS/KG_JOB_CONCURRENCY/EMBED_CONCURRENCY）不随本机核数缩放，
    # 升级核数不会改变它们，需要更高吞吐要先扩模型/embed 服务端。
    from app.services.batch_ingest import _BACKFILL_DEFAULT_WORKERS
    logger.info(
        "autotune: kg_cluster_ann_threads=%d backfill_default_workers=%d "
        "(模型端并发旋钮 EXTRACT/JOB/EMBED 与本机核数无关,不自动缩放)",
        settings.kg_cluster_ann_threads, _BACKFILL_DEFAULT_WORKERS,
    )

    app = FastAPI(
        title="silicon-notebook API",
        version="0.1.0",
        description="Local beta API for semiconductor knowhow notebooks.",
    )

    request_log = EventLogger(settings, channel="requests")

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        request_id = new_id("req")
        start = time.perf_counter()
        client = request.client.host if request.client else ""
        try:
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
        status = "slow" if slow else ("ok" if response.status_code < 500 else "error")
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
    )

    @app.get("/")
    def root() -> dict:
        return {
            "service": "silicon-notebook",
            "status": "ok",
            "docs": "/docs",
            "api": "/api",
        }

    app.include_router(auth_router, prefix="/api")  # 公开：注册/登录/登出
    app.include_router(
        router, prefix="/api", dependencies=[Depends(get_current_user)]
    )  # 其余全部需登录（router 级依赖：零逐路由遗漏）
    app.include_router(debug_logs_router, prefix="/api")
    return app


app = create_app()

