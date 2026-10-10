"""Data helpers for the frontend browser tests (frontend/e2e).

Usage (from anywhere; uses the repository-root .env):
    backend/.venv/bin/python backend/scripts/e2e_support.py receipts <session-id>
    backend/.venv/bin/python backend/scripts/e2e_support.py set-expiry <session-id> <seconds>
    backend/.venv/bin/python backend/scripts/e2e_support.py cleanup <session-id> [...]

Safety: refuses anything but this project's loopback PostgreSQL (55432) and
Redis (56380). ``cleanup`` deletes only the given sessions, their receipts,
and the exact Redis receipt keys derived from those receipts. It never drops,
truncates or flushes anything.
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Connection, text  # noqa: E402

from app.cache.redis_client import create_redis_client  # noqa: E402
from app.core.config import Settings, get_settings  # noqa: E402
from app.db.database import create_db_engine  # noqa: E402

PROJECT_DB_PORT = 55432
PROJECT_REDIS_PORT = 56380
LOOPBACK = {"127.0.0.1", "localhost"}


def guarded_settings() -> Settings:
    settings = get_settings()
    if settings.postgres_host not in LOOPBACK or settings.postgres_port != PROJECT_DB_PORT:
        sys.exit("Refusing: not this project's PostgreSQL.")
    if settings.redis_host not in LOOPBACK or settings.redis_port != PROJECT_REDIS_PORT:
        sys.exit("Refusing: not this project's Redis.")
    return settings


def check_database(conn: Connection, settings: Settings) -> None:
    if conn.execute(text("SELECT current_database()")).scalar_one() != settings.postgres_db:
        sys.exit("Refusing: unexpected database.")


def parse_ids(values: list[str]) -> list[str]:
    return [str(uuid.UUID(v)) for v in values]  # rejects anything but UUIDs


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        sys.exit(__doc__)
    command, args = argv[1], argv[2:]
    settings = guarded_settings()
    engine = create_db_engine(settings)
    try:
        if command == "receipts":
            [session_id] = parse_ids(args)
            with engine.connect() as conn:
                check_database(conn, settings)
                print(
                    conn.execute(
                        text("SELECT count(*) FROM payment_receipts WHERE session_id = :id"),
                        {"id": session_id},
                    ).scalar_one()
                )
        elif command == "set-expiry":
            [session_id] = parse_ids(args[:1])
            seconds = float(args[1])
            with engine.begin() as conn:
                check_database(conn, settings)
                conn.execute(
                    text(
                        "UPDATE payment_sessions SET expires_at = clock_timestamp() "
                        "+ make_interval(secs => :secs) WHERE id = :id"
                    ),
                    {"id": session_id, "secs": seconds},
                )
        elif command == "cleanup":
            ids = parse_ids(args)
            if not ids:
                print("deleted sessions=0 receipts=0 cache_keys=0")
                return
            with engine.begin() as conn:
                check_database(conn, settings)
                params = {"ids": ids}
                digests = conn.execute(
                    text(
                        "SELECT key_digest FROM payment_receipts "
                        "WHERE session_id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    params,
                ).scalars().all()
                receipts = conn.execute(
                    text("DELETE FROM payment_receipts WHERE session_id = ANY(CAST(:ids AS uuid[]))"),
                    params,
                ).rowcount
                sessions = conn.execute(
                    text("DELETE FROM payment_sessions WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    params,
                ).rowcount
            redis = create_redis_client(settings)
            try:
                keys = [f"{settings.redis_key_prefix}:pay-receipt:{d}" for d in digests]
                cache_keys = redis.delete(*keys) if keys else 0
            finally:
                redis.close()
            print(f"deleted sessions={sessions} receipts={receipts} cache_keys={cache_keys}")
        else:
            sys.exit(f"Unknown command: {command}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main(sys.argv)
