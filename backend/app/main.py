"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.sessions import router as sessions_router
from app.cache.redis_client import check_redis, create_redis_client
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.request_id import REQUEST_ID_HEADER, RequestIdMiddleware
from app.db.database import check_database, create_db_engine, create_session_factory

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Connections are opened lazily, so the app starts even if a dependency
    # is down; /health reports the actual state.
    app.state.db_engine = create_db_engine(settings)
    app.state.session_factory = create_session_factory(app.state.db_engine)
    app.state.redis = create_redis_client(settings)
    try:
        yield
    finally:
        app.state.redis.close()
        app.state.db_engine.dispose()


app = FastAPI(title="Payment Session Service", lifespan=lifespan)

register_error_handlers(app)

# Middleware added last runs first: CORS wraps the request-ID middleware.
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[REQUEST_ID_HEADER],
)

app.include_router(sessions_router)


@app.get("/health")
def health(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}

    try:
        check_database(request.app.state.db_engine)
        checks["postgres"] = "ok"
    except Exception as exc:
        # Log the exception type only; messages can contain connection details.
        logger.warning("PostgreSQL health check failed: %s", type(exc).__name__)
        checks["postgres"] = "unavailable"

    try:
        check_redis(request.app.state.redis)
        checks["redis"] = "ok"
    except Exception as exc:
        logger.warning("Redis health check failed: %s", type(exc).__name__)
        checks["redis"] = "unavailable"

    healthy = all(status == "ok" for status in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
