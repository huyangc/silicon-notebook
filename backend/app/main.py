import logging
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth_routes import auth_router
from app.api.debug_logs import router as debug_logs_router
from app.api.deps import get_current_user
from app.api.routes import router
from app.core.config import get_settings
from app.core.event_logging import EventLogger, new_id

logger = logging.getLogger("silicon_notebook.startup")


def create_app() -> FastAPI:
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

