"""provider accounts, auth challenges, payment intents, webhook dedupe, cart quote expiry

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = (sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
       sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def upgrade() -> None:
    op.add_column("sandbox_grocery_orders", sa.Column("quote_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sandbox_grocery_orders", sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_table(
        "provider_accounts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=True),
        sa.Column("token_kind", sa.String(20), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *(c.copy() for c in _TS),
        sa.UniqueConstraint("user_id", "provider", name="uq_provider_account_user_provider"),
    )
    op.create_index("ix_provider_accounts_user_id", "provider_accounts", ["user_id"])
    op.create_table(
        "auth_challenges",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("purpose", sa.String(120), nullable=False),
        sa.Column("session_ref", sa.String(120), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *(c.copy() for c in _TS),
    )
    op.create_index("ix_auth_challenges_user_id", "auth_challenges", ["user_id"])
    op.create_table(
        "payment_intents",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gateway", sa.String(40), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("purpose_ref", sa.String(64), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("gateway_ref", sa.String(80), nullable=True),
        sa.Column("payment_url", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gateway_status", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *(c.copy() for c in _TS),
    )
    op.create_index("ix_payment_intents_user_id", "payment_intents", ["user_id"])
    op.create_index("ix_payment_intents_gateway_ref", "payment_intents", ["gateway_ref"])
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("event_id", sa.String(120), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "event_id", name="uq_webhook_event"),
    )


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_index("ix_payment_intents_gateway_ref", table_name="payment_intents")
    op.drop_index("ix_payment_intents_user_id", table_name="payment_intents")
    op.drop_table("payment_intents")
    op.drop_index("ix_auth_challenges_user_id", table_name="auth_challenges")
    op.drop_table("auth_challenges")
    op.drop_index("ix_provider_accounts_user_id", table_name="provider_accounts")
    op.drop_table("provider_accounts")
    op.drop_column("sandbox_grocery_orders", "request")
    op.drop_column("sandbox_grocery_orders", "quote_expires_at")
