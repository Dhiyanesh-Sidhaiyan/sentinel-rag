"""FastAPI application factory: lifespan-managed dependencies, middleware, error handling.

Run with: uvicorn --factory app.main:create_app
"""

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import health, v1
from app.container import Container
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import HTTP_LATENCY

log = get_logger("app")

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = Container.build(settings)
        await container.start()
        app.state.container = container
        log.info("startup", env=settings.environment, vector=settings.vector_backend, graph=settings.graph_backend,
                 llm=settings.llm_provider, version=__version__)
        try:
            yield
        finally:
            await container.stop()
            log.info("shutdown")

    is_prod = settings.environment == "prod"
    app = FastAPI(
        title="Sentinel RAG", version=__version__, lifespan=lifespan,
        description="Agentic GraphRAG API with hybrid retrieval and AI guardrails.",
        docs_url=None if is_prod else "/docs", redoc_url=None, openapi_url=None if is_prod else "/openapi.json",
    )
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST", "DELETE"],
                           allow_headers=["X-API-Key", "Content-Type", "X-Request-ID"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if 8 <= len(incoming) <= 64 and incoming.isascii() else uuid.uuid4().hex
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, path=request.url.path)
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            log.exception("unhandled_error")
            response = JSONResponse({"error": "internal_error", "request_id": request_id}, status_code=500)
        finally:
            elapsed = time.perf_counter() - start
            route = request.scope.get("route")
            HTTP_LATENCY.labels(request.method, getattr(route, "path", "unmatched"), str(status_code)).observe(elapsed)
        response.headers["X-Request-ID"] = request_id
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path not in ("/healthz", "/readyz", "/metrics"):
            log.info("request", method=request.method, status=status_code, ms=round(elapsed * 1000, 2))
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]  # never echo input
        return JSONResponse({"error": "validation_error", "detail": errors,
                             "request_id": getattr(request.state, "request_id", None)}, status_code=422)

    app.include_router(health.router)
    app.include_router(v1.router)
    return app

