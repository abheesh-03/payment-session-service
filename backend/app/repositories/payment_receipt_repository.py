"""Data access for payment receipts. Never commits; callers own transactions."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.payment_receipt import PaymentReceipt
from app.services.idempotency import advisory_lock_id


class PaymentReceiptRepository:
    def __init__(self, db: Session) -> None:
        self._db = db

    def lock_key(self, key_digest: str) -> None:
        """Transaction-scoped advisory lock: released on commit/rollback."""
        self._db.execute(select(func.pg_advisory_xact_lock(advisory_lock_id(key_digest))))

    def get_by_key_digest(self, key_digest: str) -> PaymentReceipt | None:
        stmt = (
            select(PaymentReceipt)
            .where(PaymentReceipt.key_digest == key_digest)
            .execution_options(populate_existing=True)
        )
        return self._db.execute(stmt).scalar_one_or_none()

    def add(self, receipt: PaymentReceipt) -> None:
        self._db.add(receipt)
        self._db.flush()
