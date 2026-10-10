"""PostgreSQL access via SQLAlchemy + psycopg 3."""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import URL, Engine, MetaData, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import Settings

# Deterministic constraint names so Alembic migrations stay reproducible.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_database_url(settings: Settings) -> URL:
    return URL.create(
        drivername="postgresql+psycopg",
        username=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


def create_db_engine(settings: Settings) -> Engine:
    return create_engine(
        build_database_url(settings),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        # Pin the session time zone so timestamptz values come back in UTC.
        connect_args={"connect_timeout": 3, "options": "-c timezone=UTC"},
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: committed objects stay readable for the response
    # without an extra SELECT.
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one Session per request, closed afterwards.

    Sessions do not auto-commit; writes must commit explicitly. Anything left
    uncommitted is rolled back when the session closes.
    """
    with request.app.state.session_factory() as session:
        yield session


def check_database(engine: Engine) -> None:
    """Run ``SELECT 1``; raises if PostgreSQL is unreachable."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1")).scalar_one()
