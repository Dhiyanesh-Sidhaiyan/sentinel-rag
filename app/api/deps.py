"""Request dependencies: API-key auth -> tenant resolution, and per-tenant distributed rate limiting."""

import hashlib
from dataclasses import dataclass

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from app.container import Container

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_container(request: Request) -> Container:
    return request.app.state.container


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    key_fingerprint: str


async def authenticate(
    api_key: str | None = Depends(api_key_header), c: Container = Depends(get_container)
) -> Principal:
    if not c.settings.auth_enabled:
        return Principal(tenant_id="default", key_fingerprint="anonymous")
    if not api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing API key", headers={"WWW-Authenticate": "ApiKey"})
    digest = hashlib.sha256(api_key.encode()).hexdigest()
    tenant = c.settings.api_keys_sha256.get(digest)
    if tenant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key", headers={"WWW-Authenticate": "ApiKey"})
    structlog.contextvars.bind_contextvars(tenant_id=tenant)
    return Principal(tenant_id=tenant, key_fingerprint=digest[:12])


async def rate_limited(
    principal: Principal = Depends(authenticate), c: Container = Depends(get_container)
) -> Principal:
    limit = c.settings.rate_limit_per_minute
    try:
        count = await c.cache.hit_window(f"{principal.tenant_id}:{principal.key_fingerprint}", 60)
    except Exception:
        return principal  # fail-open on cache outage; availability > strict limiting
    if count > limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded", headers={"Retry-After": "60"})
    return principal
