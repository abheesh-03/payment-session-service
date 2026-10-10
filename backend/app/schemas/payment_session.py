"""Request/response schemas for the payment session API."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, StringConstraints, field_validator

from app.models.payment_session import Currency, PaymentSession, SessionStatus

# Exactly representable in NUMERIC(12,2): at most 10 integer digits and 2
# decimal places. Values that do not fit are rejected, never rounded.
MoneyAmount = Annotated[
    Decimal,
    Field(gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False),
]

# Money leaves the API as a fixed two-decimal string, e.g. "49.90".
MoneyString = Annotated[Decimal, PlainSerializer(lambda d: f"{d:.2f}", return_type=str)]


class CreatePaymentSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_reference: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    amount: MoneyAmount
    currency: Currency
    description: Annotated[str | None, Field(max_length=500)] = None
    expires_in_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)] = 15

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class PaymentSessionResponse(BaseModel):
    id: uuid.UUID
    merchant_reference: str
    amount: MoneyString
    currency: Currency
    description: str | None
    status: SessionStatus
    created_at: datetime
    expires_at: datetime
    paid_at: datetime | None
    updated_at: datetime
    checkout_url: str

    @classmethod
    def from_model(cls, session: PaymentSession, frontend_base_url: str) -> "PaymentSessionResponse":
        return cls(
            id=session.id,
            merchant_reference=session.merchant_reference,
            amount=session.amount,
            currency=session.currency,
            description=session.description,
            status=session.status,
            created_at=session.created_at,
            expires_at=session.expires_at,
            paid_at=session.paid_at,
            updated_at=session.updated_at,
            checkout_url=f"{frontend_base_url}/pay/{session.id}",
        )
