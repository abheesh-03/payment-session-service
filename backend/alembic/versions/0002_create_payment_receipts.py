"""create payment_receipts

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("response_status", sa.SmallInteger(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "key_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_payment_receipts_key_digest_sha256_hex")
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_payment_receipts_request_fingerprint_sha256_hex"),
        ),
        sa.CheckConstraint(
            "response_status BETWEEN 200 AND 299",
            name=op.f("ck_payment_receipts_response_status_success"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["payment_sessions.id"],
            name=op.f("fk_payment_receipts_session_id_payment_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_receipts")),
        sa.UniqueConstraint("key_digest", name=op.f("uq_payment_receipts_key_digest")),
        sa.UniqueConstraint("session_id", name=op.f("uq_payment_receipts_session_id")),
    )


def downgrade() -> None:
    op.drop_table("payment_receipts")
