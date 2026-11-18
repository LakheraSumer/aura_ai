"""offers engine, sandbox bookings, price watches, shopping lists, OAuth state, reminder follow-ups

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _j() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _user() -> sa.Column:
    return sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)


def _ts() -> list[sa.Column]:
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.add_column("reminders", sa.Column("done_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("reminders", sa.Column("snoozes", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("offer_sets", sa.Column("id", sa.UUID(), primary_key=True), _user(),
                    sa.Column("vertical", sa.String(20), nullable=False), sa.Column("query", _j(), nullable=False),
                    sa.Column("offers", _j(), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("sandbox_bookings", sa.Column("id", sa.UUID(), primary_key=True), _user(),
                    sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
                    sa.Column("vertical", sa.String(20), nullable=False),
                    sa.Column("source", sa.String(40), nullable=False),
                    sa.Column("offer_id", sa.String(20), nullable=False), sa.Column("offer", _j(), nullable=False),
                    sa.Column("amount_minor", sa.Integer(), nullable=False),
                    sa.Column("status", sa.String(20), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("booking_code", sa.String(40), nullable=True, unique=True),
                    sa.Column("payment_ref", sa.String(60), nullable=True),
                    sa.Column("refund_minor", sa.Integer(), nullable=True),
                    sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True), *_ts())
    op.create_table("price_watches", sa.Column("id", sa.UUID(), primary_key=True), _user(),
                    sa.Column("vertical", sa.String(20), nullable=False), sa.Column("query", _j(), nullable=False),
                    sa.Column("item_key", sa.String(200), nullable=True),
                    sa.Column("title", sa.String(200), nullable=False),
                    sa.Column("start_price_minor", sa.Integer(), nullable=False),
                    sa.Column("last_price_minor", sa.Integer(), nullable=False),
                    sa.Column("target_price_minor", sa.Integer(), nullable=True),
                    sa.Column("status", sa.String(20), nullable=False),
                    sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
                    sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False), *_ts())
    op.create_table("shopping_list_items", sa.Column("id", sa.UUID(), primary_key=True), _user(),
                    sa.Column("name", sa.String(80), nullable=False), sa.Column("qty", sa.Integer(), nullable=False),
                    sa.Column("size", sa.String(30), nullable=True), sa.Column("brand", sa.String(60), nullable=True),
                    *_ts())
    op.create_table("oauth_states", sa.Column("id", sa.UUID(), primary_key=True), _user(),
                    sa.Column("provider", sa.String(40), nullable=False),
                    sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
                    sa.Column("verifier_ciphertext", sa.Text(), nullable=False),
                    sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    for t in ("offer_sets", "sandbox_bookings", "price_watches", "shopping_list_items", "oauth_states"):
        op.create_index(f"ix_{t}_user_id", t, ["user_id"])


def downgrade() -> None:
    for t in ("oauth_states", "shopping_list_items", "price_watches", "sandbox_bookings", "offer_sets"):
        op.drop_index(f"ix_{t}_user_id", table_name=t)
        op.drop_table(t)
    op.drop_column("reminders", "snoozes")
    op.drop_column("reminders", "done_at")
