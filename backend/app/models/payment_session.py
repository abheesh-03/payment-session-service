"""PaymentSession ORM model."""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class SessionStatus(StrEnum):
    CREATED = "CREATED"
    PAID = "PAID"
    EXPIRED = "EXPIRED"


class Currency(StrEnum):
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    INR = "INR"


def _in_list(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


class PaymentSession(Base):
    __tablename__ = "payment_sessions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(_in_list("currency", Currency), name="currency_supported"),
        CheckConstraint(_in_list("status", SessionStatus), name="status_allowed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Not unique: a merchant may create several sessions for one order.
    merchant_reference: Mapped[str] = mapped_column(String(100), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), server_default=SessionStatus.CREATED.value)
    # All timestamps are timestamptz and set by the application in UTC.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
