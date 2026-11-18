"""sandbox cinema holds and TEST bookings

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_cinema_bookings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("show_id", sa.String(length=120), nullable=False),
        sa.Column("theatre", sa.String(length=200), nullable=False),
        sa.Column("movie", sa.String(length=200), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("seat_class", sa.String(length=60), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payment_ref", sa.String(length=40), nullable=True),
        sa.Column("booking_code", sa.String(length=40), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("booking_code"),
    )
    op.create_index("ix_sandbox_cinema_bookings_user_id", "sandbox_cinema_bookings", ["user_id"])
    op.create_index("ix_sandbox_cinema_bookings_show_id", "sandbox_cinema_bookings", ["show_id"])


def downgrade() -> None:
    op.drop_index("ix_sandbox_cinema_bookings_show_id", table_name="sandbox_cinema_bookings")
    op.drop_index("ix_sandbox_cinema_bookings_user_id", table_name="sandbox_cinema_bookings")
    op.drop_table("sandbox_cinema_bookings")
