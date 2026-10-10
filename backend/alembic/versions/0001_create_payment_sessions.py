"""create payment_sessions

Revision ID: 0001
Revises:
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_reference", sa.String(length=100), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="CREATED", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payment_sessions_amount_positive")),
        sa.CheckConstraint(
            "currency IN ('USD', 'EUR', 'GBP', 'INR')",
            name=op.f("ck_payment_sessions_currency_supported"),
        ),
        sa.CheckConstraint(
            "status IN ('CREATED', 'PAID', 'EXPIRED')",
            name=op.f("ck_payment_sessions_status_allowed"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_sessions")),
    )
    op.create_index(
        op.f("ix_payment_sessions_merchant_reference"),
        "payment_sessions",
        ["merchant_reference"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_payment_sessions_merchant_reference"), table_name="payment_sessions")
    op.drop_table("payment_sessions")
