"""Durable receipt of a successful payment, keyed by Idempotency-Key digest."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, SmallInteger, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class PaymentReceipt(Base):
    """One row per successful pay. Failed attempts never create a receipt.

    The raw Idempotency-Key is never stored; ``key_digest`` is its SHA-256 hex
    digest and is globally unique for the pay operation.
    """

    __tablename__ = "payment_receipts"
    __table_args__ = (
        CheckConstraint("key_digest ~ '^[0-9a-f]{64}$'", name="key_digest_sha256_hex"),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'", name="request_fingerprint_sha256_hex"
        ),
        CheckConstraint("response_status BETWEEN 200 AND 299", name="response_status_success"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    key_digest: Mapped[str] = mapped_column(String(64), unique=True)
    # SHA-256 of the operation and canonical session UUID (see services/idempotency.py).
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    # Unique: a session can be paid successfully at most once.
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payment_sessions.id"), unique=True)
    response_status: Mapped[int] = mapped_column(SmallInteger)
    # JSON (not JSONB) keeps the key order, so replays are byte-identical.
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
