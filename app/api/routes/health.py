from fastapi import APIRouter, Depends, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app import __version__
from app.api.deps import get_container
from app.container import Container
from app.schemas.api import HealthResponse

router = APIRouter(tags=["ops"])


@router.get("/healthz", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    """Liveness: process is up. Never checks dependencies (avoids cascading restarts)."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/readyz", response_model=HealthResponse)
async def readiness(response: Response, c: Container = Depends(get_container)) -> HealthResponse:
    """Readiness: required dependencies reachable. Cache is optional (degraded, still ready)."""
    checks = await c.health()
    critical_down = checks["vector_store"] != "ok" or checks["graph_store"] != "ok"
    if critical_down:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="degraded" if critical_down or "down" in checks.values() else "ok",
                          version=__version__, checks=checks)


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
