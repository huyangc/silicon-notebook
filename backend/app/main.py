import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import get_settings
from app.core.event_logging import EventLogger, new_id


def create_app() -> FastAPI:
    settings = get_settings()
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

    app.include_router(router, prefix="/api")
    return app


app = create_app()

