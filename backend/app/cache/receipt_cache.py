"""Short-lived Redis cache of committed payment receipts.

Only an accelerator for replays: PostgreSQL holds the durable receipt and is
always the fallback. Every Redis failure or malformed entry is treated as a
cache miss, and a failed write never affects a committed payment.
"""

import logging
import uuid
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator
from redis import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class CachedReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_fingerprint: Sha256Hex
    session_id: uuid.UUID
    status_code: Annotated[int, Field(ge=200, le=299)]
    body: dict[str, Any]

    @model_validator(mode="after")
    def _body_matches_session(self) -> "CachedReceipt":
        if self.body.get("id") != str(self.session_id):
            raise ValueError("cached body does not belong to session_id")
        return self


class ReceiptCache:
    def __init__(self, client: Redis, prefix: str, ttl_seconds: int) -> None:
        self._client = client
        self._prefix = prefix
        self._ttl_seconds = ttl_seconds

    def key_for(self, key_digest: str) -> str:
        return f"{self._prefix}:pay-receipt:{key_digest}"

    def get(self, key_digest: str) -> CachedReceipt | None:
        try:
            raw = self._client.get(self.key_for(key_digest))
        except RedisError as exc:
            logger.warning("Receipt cache read failed: %s", type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            return CachedReceipt.model_validate_json(raw)
        except ValidationError:
            logger.warning("Ignoring malformed receipt cache entry")
            return None

    def put(self, key_digest: str, receipt: CachedReceipt) -> None:
        """Call only after the receipt is committed in PostgreSQL."""
        try:
            self._client.set(
                self.key_for(key_digest), receipt.model_dump_json(), ex=self._ttl_seconds
            )
        except RedisError as exc:
            logger.warning("Receipt cache write failed: %s", type(exc).__name__)
