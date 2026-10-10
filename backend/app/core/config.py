"""Application settings.

Settings are always loaded from the repository-root ``.env`` file, resolved
relative to this module rather than the current working directory. That means
the backend reads the same file whether it is started from the repository root
or from ``backend/``. Real environment variables take precedence over ``.env``.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/app/core/config.py -> parents[3] is the repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 55432

    redis_host: str = "127.0.0.1"
    redis_port: int = 56380
    redis_db: int = 0
    # Namespace for every Redis key this project writes.
    redis_key_prefix: str = "payment-session-service"
    # How long committed payment receipts stay cached in Redis.
    receipt_cache_ttl_seconds: int = Field(default=600, gt=0)

    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3020"]

    # Base URL of the checkout frontend; checkout_url is "<base>/pay/<id>".
    frontend_base_url: str = "http://localhost:3020"

    # Allow "X-Mock-Provider-Outcome: fail" to simulate a provider outage.
    enable_mock_failures: bool = False

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("frontend_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
