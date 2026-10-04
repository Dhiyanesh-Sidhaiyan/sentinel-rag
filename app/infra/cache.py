"""Cache + distributed rate limiting. RedisCache works with Valkey (BSD) or Redis."""

import time
from typing import Protocol

from app.core.config import Settings


class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_s: int) -> None: ...
    async def hit_window(self, key: str, window_s: int) -> int: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class MemoryCache:
    def __init__(self, max_items: int = 10_000) -> None:
        self._data: dict[str, tuple[float, str]] = {}
        self._max = max_items

    async def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None or item[0] < time.monotonic():
            self._data.pop(key, None)
            return None
        return item[1]

    async def set(self, key: str, value: str, ttl_s: int) -> None:
        if len(self._data) >= self._max:
            self._data.pop(next(iter(self._data)))
        self._data[key] = (time.monotonic() + ttl_s, value)

    async def hit_window(self, key: str, window_s: int) -> int:
        bucket = f"{key}:{int(time.time() // window_s)}"
        count = int(await self.get(bucket) or 0) + 1
        await self.set(bucket, str(count), window_s)
        return count

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class RedisCache:
    def __init__(self, settings: Settings) -> None:
        import redis.asyncio as redis

        self._r = redis.from_url(
            settings.redis_url.get_secret_value(), decode_responses=True,
            socket_timeout=2, socket_connect_timeout=2, health_check_interval=30,
        )

    async def get(self, key: str) -> str | None:
        value = await self._r.get(key)
        return str(value) if value is not None else None

    async def set(self, key: str, value: str, ttl_s: int) -> None:
        await self._r.set(key, value, ex=ttl_s)

    async def hit_window(self, key: str, window_s: int) -> int:
        bucket = f"rl:{key}:{int(time.time() // window_s)}"
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.incr(bucket)
            pipe.expire(bucket, window_s + 1)
            count, _ = await pipe.execute()
        return int(count)

    async def ping(self) -> bool:
        try:
            return bool(await self._r.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._r.aclose()


def build_cache(settings: Settings) -> Cache:
    return RedisCache(settings) if settings.cache_backend == "redis" else MemoryCache()
