"""sandbox quick-commerce carts and TEST orders

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_grocery_orders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("store", sa.String(length=40), nullable=False),
        sa.Column("city", sa.String(length=80), nullable=False),
        sa.Column("area", sa.String(length=120), nullable=True),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("item_total_minor", sa.Integer(), nullable=False),
        sa.Column("delivery_fee_minor", sa.Integer(), nullable=False),
        sa.Column("handling_fee_minor", sa.Integer(), nullable=False),
        sa.Column("total_minor", sa.Integer(), nullable=False),
        sa.Column("eta_minutes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("payment_ref", sa.String(length=40), nullable=True),
        sa.Column("order_code", sa.String(length=40), nullable=True),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("order_code"),
    )
    op.create_index("ix_sandbox_grocery_orders_user_id", "sandbox_grocery_orders", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sandbox_grocery_orders_user_id", table_name="sandbox_grocery_orders")
    op.drop_table("sandbox_grocery_orders")
