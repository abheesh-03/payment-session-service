"""Data access for payment sessions. Never commits; callers own transactions."""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.payment_session import PaymentSession


class PaymentSessionRepository:
    def __init__(self, db: Session) -> None:
        self._db = db

    def add(self, payment_session: PaymentSession) -> None:
        self._db.add(payment_session)
        self._db.flush()

    def get_for_update(self, session_id: uuid.UUID) -> PaymentSession | None:
        """``SELECT ... FOR UPDATE``: lock the row until the transaction ends.

        ``populate_existing`` overwrites any copy already in the identity map,
        so the caller always sees the canonical row as of acquiring the lock.
        """
        stmt = (
            select(PaymentSession)
            .where(PaymentSession.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._db.execute(stmt).scalar_one_or_none()

    def current_time(self) -> datetime:
        """PostgreSQL wall-clock time (UTC).

        ``clock_timestamp()`` advances during a transaction, unlike ``now()``,
        which is fixed at transaction start and would be stale after a lock wait.
        """
        return self._db.execute(select(func.clock_timestamp())).scalar_one()
