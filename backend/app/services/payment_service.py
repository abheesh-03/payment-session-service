"""Payment session business logic.

State machine::

    CREATED --pay--> PAID       (terminal)
    CREATED --expire--> EXPIRED (terminal)

Expiration is lazy: a CREATED session past ``expires_at`` is persisted as
EXPIRED the next time it is fetched or a payment is attempted.

Paying requires an Idempotency-Key. Lock order inside one transaction:
  1. advisory lock on the key digest (serializes requests using that key)
  2. durable receipt lookup (replay or conflict; no session is touched)
  3. payment-session row lock, terminal/expiry checks
  4. provider call (only for a new, eligible payment), clock re-read,
     expiry re-check, then the CREATED -> PAID transition
The PAID update and its receipt commit together.

Limitation: this is atomic only because the mock provider has no external
side effects. A real charge cannot join this PostgreSQL transaction; see
docs/api.md ("Provider limitation").
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, Inexact
from typing import Any

from sqlalchemy.orm import Session

from app.cache.receipt_cache import CachedReceipt, ReceiptCache
from app.core.errors import (
    AppError,
    IdempotencyConflictError,
    PaymentProviderError,
    PaymentProviderUnavailableError,
    SessionAlreadyPaidError,
    SessionExpiredError,
    SessionNotFoundError,
)
from app.models.payment_receipt import PaymentReceipt
from app.models.payment_session import PaymentSession, SessionStatus
from app.providers.base import ChargeRequest, PaymentProvider, ProviderUnavailableError
from app.repositories.payment_receipt_repository import PaymentReceiptRepository
from app.repositories.payment_session_repository import PaymentSessionRepository
from app.schemas.payment_session import CreatePaymentSessionRequest, PaymentSessionResponse
from app.services.idempotency import digest_key, pay_fingerprint

logger = logging.getLogger(__name__)

_CENTS = Decimal("0.01")
# Raises instead of rounding if a value somehow has more than 2 decimals.
_EXACT = Context(traps=[Inexact])


@dataclass(frozen=True)
class _Transition:
    action: str
    old_status: str
    new_status: str


@dataclass(frozen=True)
class PaymentResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool


class PaymentService:
    def __init__(
        self,
        db: Session,
        request_id: str | None = None,
        receipt_cache: ReceiptCache | None = None,
    ) -> None:
        self._db = db
        self._sessions = PaymentSessionRepository(db)
        self._receipts = PaymentReceiptRepository(db)
        self._request_id = request_id
        self._receipt_cache = receipt_cache

    def create_session(self, request: CreatePaymentSessionRequest) -> PaymentSession:
        now = datetime.now(UTC)
        payment_session = PaymentSession(
            id=uuid.uuid4(),
            merchant_reference=request.merchant_reference,
            # Normalize scale (10 -> 10.00); validation already guarantees <= 2 places.
            amount=request.amount.quantize(_CENTS, context=_EXACT),
            currency=request.currency.value,
            description=request.description,
            status=SessionStatus.CREATED.value,
            created_at=now,
            expires_at=now + timedelta(minutes=request.expires_in_minutes),
            paid_at=None,
            updated_at=now,
        )
        # Commits on success, rolls back on any error.
        with self._db.begin():
            self._sessions.add(payment_session)
        return payment_session

    def get_session(self, session_id: uuid.UUID) -> PaymentSession:
        """Return the canonical session, persisting lazy expiration first."""
        with self._db.begin():
            payment_session = self._sessions.get_for_update(session_id)
            if payment_session is None:
                raise SessionNotFoundError()
            transition = self._expire_if_due(payment_session, self._sessions.current_time())
        # Committed: safe to report the transition.
        self._log_transition(payment_session, transition)
        return payment_session

    def pay_session(
        self,
        session_id: uuid.UUID,
        idempotency_key: str,
        provider: PaymentProvider,
        frontend_base_url: str,
    ) -> PaymentResult:
        """Simulated payment: CREATED and unexpired -> PAID, idempotent per key.

        Rejections are raised only after the transaction commits, so an
        expiration discovered here is persisted even though the caller gets 409.
        """
        key_digest = digest_key(idempotency_key)
        fingerprint = pay_fingerprint(session_id)

        # Fast path: a committed receipt for exactly this request.
        cached = self._read_cache(key_digest)
        if cached is not None and cached.request_fingerprint == fingerprint:
            return PaymentResult(cached.status_code, cached.body, replayed=True)

        error: AppError | None = None
        transition: _Transition | None = None
        payment_session: PaymentSession | None = None
        with self._db.begin():
            # 1. Serialize every request carrying this key.
            self._receipts.lock_key(key_digest)
            # 2. A durable receipt decides replay vs. conflict.
            receipt = self._receipts.get_by_key_digest(key_digest)
            if receipt is not None and receipt.request_fingerprint != fingerprint:
                raise IdempotencyConflictError()  # nothing modified; rollback is fine
            if receipt is None:
                # 3. Lock the session, then decide using the clock read under the lock.
                payment_session = self._sessions.get_for_update(session_id)
                if payment_session is None:
                    raise SessionNotFoundError()
                transition = self._expire_if_due(payment_session, self._sessions.current_time())

                if payment_session.status == SessionStatus.PAID:
                    error = SessionAlreadyPaidError()
                elif payment_session.status == SessionStatus.EXPIRED:
                    error = SessionExpiredError()
                # 4. Only a new, eligible payment reaches the provider.
                elif not self._charge(provider, payment_session, key_digest):
                    error = PaymentProviderUnavailableError()  # nothing changed
                else:
                    # Time passed during the provider call: re-read and re-check.
                    now = self._sessions.current_time()
                    transition = self._expire_if_due(payment_session, now)
                    if transition is not None:
                        error = SessionExpiredError()
                    else:
                        receipt = self._mark_paid(
                            payment_session, now, key_digest, fingerprint, frontend_base_url
                        )
                        transition = _Transition("pay", SessionStatus.CREATED, SessionStatus.PAID)
        # Committed: the PAID update and receipt, or the expiration, are durable.
        if payment_session is not None:
            self._log_transition(payment_session, transition)
        if isinstance(error, PaymentProviderUnavailableError):
            self._log_provider_failure(session_id, "provider_unavailable")
        if error is not None:
            raise error

        assert receipt is not None
        self._write_cache(key_digest, receipt)
        return PaymentResult(
            receipt.response_status,
            receipt.response_body,
            replayed=transition is None,
        )

    def _charge(
        self, provider: PaymentProvider, payment_session: PaymentSession, key_digest: str
    ) -> bool:
        """Call the provider. False on a known outage; unexpected errors abort
        the transaction (rolled back by the caller's ``begin()`` block)."""
        request = ChargeRequest(
            session_id=payment_session.id,
            amount=payment_session.amount,
            currency=payment_session.currency,
            idempotency_key_digest=key_digest,
        )
        try:
            provider.charge(request)
        except ProviderUnavailableError:
            return False
        except Exception as exc:
            # The exception text may hold provider details; never log or return it.
            self._log_provider_failure(payment_session.id, "provider_error")
            raise PaymentProviderError() from exc
        return True

    def _mark_paid(
        self,
        payment_session: PaymentSession,
        now: datetime,
        key_digest: str,
        fingerprint: str,
        frontend_base_url: str,
    ) -> PaymentReceipt:
        payment_session.status = SessionStatus.PAID.value
        payment_session.paid_at = now
        payment_session.updated_at = now
        receipt = PaymentReceipt(
            id=uuid.uuid4(),
            key_digest=key_digest,
            request_fingerprint=fingerprint,
            session_id=payment_session.id,
            response_status=200,
            response_body=PaymentSessionResponse.from_model(
                payment_session, frontend_base_url
            ).model_dump(mode="json"),
            created_at=now,
        )
        # Flushed in the same transaction as the PAID update.
        self._receipts.add(receipt)
        return receipt

    def _read_cache(self, key_digest: str) -> CachedReceipt | None:
        return self._receipt_cache.get(key_digest) if self._receipt_cache else None

    def _write_cache(self, key_digest: str, receipt: PaymentReceipt) -> None:
        if self._receipt_cache is None:
            return
        self._receipt_cache.put(
            key_digest,
            CachedReceipt(
                request_fingerprint=receipt.request_fingerprint,
                session_id=receipt.session_id,
                status_code=receipt.response_status,
                body=receipt.response_body,
            ),
        )

    @staticmethod
    def _expire_if_due(payment_session: PaymentSession, now: datetime) -> _Transition | None:
        if payment_session.status != SessionStatus.CREATED or now < payment_session.expires_at:
            return None
        payment_session.status = SessionStatus.EXPIRED.value
        payment_session.updated_at = now
        return _Transition("expire", SessionStatus.CREATED, SessionStatus.EXPIRED)

    def _log_provider_failure(self, session_id: uuid.UUID, error_category: str) -> None:
        # The session is left unchanged in both failure categories.
        logger.warning(
            "payment provider failure",
            extra={
                "request_id": self._request_id,
                "session_id": str(session_id),
                "action": "pay",
                "error_category": error_category,
                "old_status": SessionStatus.CREATED.value,
                "new_status": SessionStatus.CREATED.value,
            },
        )

    def _log_transition(
        self, payment_session: PaymentSession, transition: _Transition | None
    ) -> None:
        if transition is None:
            return
        logger.info(
            "payment session state transition",
            extra={
                "request_id": self._request_id,
                "session_id": str(payment_session.id),
                "action": transition.action,
                "old_status": str(transition.old_status),
                "new_status": str(transition.new_status),
            },
        )
