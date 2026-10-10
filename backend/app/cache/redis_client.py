"""Redis access via the redis-py client."""

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import Settings

# Redis is only a cache in front of PostgreSQL, so fail fast instead of
# retrying: redis-py 8 otherwise retries a refused connection 10 times with
# backoff (several seconds per command) before the PostgreSQL fallback runs.
REDIS_RETRIES = 0
REDIS_CONNECT_TIMEOUT_SECONDS = 1.0
REDIS_SOCKET_TIMEOUT_SECONDS = 1.0


def create_redis_client(settings: Settings) -> Redis:
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), REDIS_RETRIES),  # covers connection errors and timeouts
        decode_responses=True,
    )


def check_redis(client: Redis) -> None:
    """Send ``PING``; raises if Redis is unreachable."""
    if not client.ping():
        raise ConnectionError("Redis PING failed")
