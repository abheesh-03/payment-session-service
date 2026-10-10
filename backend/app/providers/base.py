"""Payment provider interface.

The service calls a provider only for a new, eligible payment (never for
replays, conflicts, or unknown/PAID/EXPIRED sessions).
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class ChargeRequest:
    session_id: uuid.UUID
    amount: Decimal
    currency: str
    # SHA-256 of the client's Idempotency-Key; never the raw key.
    idempotency_key_digest: str


@dataclass(frozen=True)
class ChargeResult:
    provider_reference: str


class ProviderUnavailableError(Exception):
    """Known, retryable provider failure: no charge was made."""


class PaymentProvider(Protocol):
    def charge(self, request: ChargeRequest) -> ChargeResult:
        """Charge the payment, or raise ProviderUnavailableError.

        Any other exception is treated as an unexpected provider error.
        """
        ...
