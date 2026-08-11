import time
from collections import defaultdict

from loguru import logger
from upstash_redis import Redis

from app.config import settings

_redis_client: Redis | None = None
_memory_windows: dict[str, list[float]] = defaultdict(list)


def get_redis_client() -> Redis | None:
    global _redis_client
    if not settings.upstash_redis_url or not settings.upstash_redis_token:
        return None

    if _redis_client is None:
        _redis_client = Redis(
            url=settings.upstash_redis_url,
            token=settings.upstash_redis_token,
        )
    return _redis_client



class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    def is_allowed(self, key: str) -> tuple[bool, int, int]:
        client = get_redis_client()
        now = time.time()
        window_start = now - self.window_seconds

        if client is not None:
            try:
                pipe = client.pipeline()
                pipe.zremrangebyscore(key, 0, window_start)
                pipe.zadd(key, {str(now): now})
                pipe.zcard(key)
                pipe.expire(key, self.window_seconds)
                results = pipe.exec()
                request_count: int = results[2]  # type: ignore[assignment]
                remaining = max(0, self.max_requests - request_count)
                allowed = request_count <= self.max_requests
                return allowed, remaining, request_count
            except Exception as exc:
                logger.warning("Redis rate limit failed; using memory fallback: {}", exc)

        timestamps = [ts for ts in _memory_windows[key] if ts >= window_start]
        timestamps.append(now)
        _memory_windows[key] = timestamps
        request_count = len(timestamps)
        remaining = max(0, self.max_requests - request_count)
        allowed = request_count <= self.max_requests

        return allowed, remaining, request_count


def is_allowed_ip(ip: str, route: str, limit: int, window_seconds: int) -> tuple[bool, int, int]:
    limiter = RateLimiter(max_requests=limit, window_seconds=window_seconds)
    key = f"rate_limit:ip:{ip}:{route}"
    return limiter.is_allowed(key)


def is_allowed_user(
    user_id: str, limit: int = 20, window_seconds: int = 60
) -> tuple[bool, int, int]:
    limiter = RateLimiter(max_requests=limit, window_seconds=window_seconds)
    key = f"rate_limit:user:{user_id}"
    return limiter.is_allowed(key)
