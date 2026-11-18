"""SQLAlchemy 2 ORM models.

Only tables backing implemented features are defined here; the remaining tables from
the spec (§23) are tracked in docs/NEXT_STEPS.md and added with the feature that uses them.
All timestamps are timezone-aware UTC. All primary keys are UUIDs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JsonType, list[Any]: JsonType}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# --------------------------------------------------------------------------- identity


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = uuid_pk()
    # Channel-native accounts (created from a WhatsApp/Telegram identity) have no email/password.
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    profile: Mapped[UserProfile] = relationship(back_populates="user", uselist=False, lazy="selectin")


class UserProfile(TimestampMixin, Base):
    __tablename__ = "user_profiles"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)
    preferred_language: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    # Standing permissions for Level-1 internal actions (drafts, reminders, notes).
    allow_internal_actions: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="profile")


class AuthSession(TimestampMixin, Base):
    """Refresh-token session ("sessions" in spec §23). Only a hash of the token is stored."""

    __tablename__ = "auth_sessions"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(400))


class LinkedChannel(TimestampMixin, Base):
    """A messaging identity (WhatsApp wa_id, Telegram chat id) bound to a user.

    Created either when that channel identity first messages us (the channel itself verifies the
    sender) or via a one-time link code issued to an already-authenticated identity. Never by
    matching an unverified phone number or email.
    """

    __tablename__ = "linked_channels"
    __table_args__ = (UniqueConstraint("channel", "external_id"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    verified_via: Mapped[str] = mapped_column(String(40), nullable=False)  # channel_inbound | link_code
    opted_in: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MovieCatalog(TimestampMixin, Base):
    """BookMyShow movie IDs (ET codes), from BookMyShow's listing or links users sent (migration 0002)."""

    __tablename__ = "movie_catalog"
    id: Mapped[uuid.UUID] = uuid_pk()
    et_code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)


class SandboxCinemaBooking(TimestampMixin, Base):
    """Seat holds and TEST bookings in the sandbox cinema (migration 0003). Never a real ticket."""

    __tablename__ = "sandbox_cinema_bookings"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    show_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    theatre: Mapped[str] = mapped_column(String(200), nullable=False)
    movie: Mapped[str] = mapped_column(String(200), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    seats: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    seat_class: Mapped[str] = mapped_column(String(60), nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # HELD | BOOKED | RELEASED
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payment_ref: Mapped[str | None] = mapped_column(String(40))
    booking_code: Mapped[str | None] = mapped_column(String(40), unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SandboxGroceryOrder(TimestampMixin, Base):
    """Carts and TEST orders in the sandbox quick-commerce provider (migration 0004). Never a real order."""

    __tablename__ = "sandbox_grocery_orders"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    store: Mapped[str] = mapped_column(String(40), nullable=False)
    city: Mapped[str] = mapped_column(String(80), nullable=False)
    area: Mapped[str | None] = mapped_column(String(120))
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    item_total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_fee_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    handling_fee_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    eta_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # CART | PAYMENT_PENDING | PLACED | REPLACED | CANCELLED
    payment_ref: Mapped[str | None] = mapped_column(String(40))
    order_code: Mapped[str | None] = mapped_column(String(40), unique=True)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quote_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # migration 0005
    request: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)  # what the user asked for (migration 0005)


class ProviderAccount(TimestampMixin, Base):
    """A user's link to a provider account. Tokens are AES-GCM encrypted (security/crypto.py)."""

    __tablename__ = "provider_accounts"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_provider_account_user_provider"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # LINKED | REVOKED
    token_ciphertext: Mapped[str | None] = mapped_column(Text)
    token_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="oauth")
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthChallenge(TimestampMixin, Base):
    """A provider's login challenge (e.g. OTP) answered on a one-time secure page, never in chat."""

    __tablename__ = "auth_challenges"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    purpose: Mapped[str] = mapped_column(String(120), nullable=False)
    session_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # PENDING | SUBMITTED | CONSUMED | EXPIRED | LOCKED
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaymentIntent(TimestampMixin, Base):
    """One payment request through a gateway (or the TEST sandbox), verified by status API/webhook."""

    __tablename__ = "payment_intents"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    gateway: Mapped[str] = mapped_column(String(40), nullable=False)  # razorpay | sandbox
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)  # grocery_order | movie_booking
    purpose_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # CREATED|PENDING|PAID|FAILED|EXPIRED|CANCELLED
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    gateway_ref: Mapped[str | None] = mapped_column(String(80), index=True)
    payment_url: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gateway_status: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class WebhookEvent(Base):
    """Provider webhook ids already processed (duplicate deliveries are acknowledged and ignored)."""

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_event"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    event_id: Mapped[str] = mapped_column(String(120), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class OfferSet(Base):
    """A comparison shown to the user (sandbox offers engine); bookings reference its offers (0006)."""

    __tablename__ = "offer_sets"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    vertical: Mapped[str] = mapped_column(String(20), nullable=False)
    query: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    offers: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SandboxBooking(TimestampMixin, Base):
    """TEST bookings/orders from the offers engine: products, food, flights, hotels, buses, events, tables,
    rides (0006). Never real."""

    __tablename__ = "sandbox_bookings"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    vertical: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    offer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    offer: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # HELD|PAYMENT_PENDING|CONFIRMED|CANCELLED|EXPIRED
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    booking_code: Mapped[str | None] = mapped_column(String(40), unique=True)
    payment_ref: Mapped[str | None] = mapped_column(String(60))
    refund_minor: Mapped[int | None] = mapped_column(Integer)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PriceWatch(TimestampMixin, Base):
    """'Tell me when it gets cheaper': checked by PriceWatchWorkflow (0006)."""

    __tablename__ = "price_watches"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    vertical: Mapped[str] = mapped_column(String(20), nullable=False)
    query: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    item_key: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    start_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    last_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    target_price_minor: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # ACTIVE | TRIGGERED | CANCELLED | ENDED
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ShoppingListItem(TimestampMixin, Base):
    __tablename__ = "shopping_list_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    size: Mapped[str | None] = mapped_column(String(30))
    brand: Mapped[str | None] = mapped_column(String(60))


class OAuthState(Base):
    """One-time OAuth state for 'connect Google' (PKCE verifier encrypted), 10-minute life (0006)."""

    __tablename__ = "oauth_states"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    verifier_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ChannelLinkCode(Base):
    __tablename__ = "channel_link_codes"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class OutboundMessage(TimestampMixin, Base):
    """Every outbound channel delivery attempt, with the provider's own outcome."""

    __tablename__ = "outbound_messages"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # session | template
    text: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # QUEUED | SENT (accepted by provider) | DELIVERED | READ | FAILED | SKIPPED_NO_CREDENTIALS | BLOCKED_POLICY
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)


# --------------------------------------------------------------------------- conversation


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str | None] = mapped_column(String(200))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="web")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    # Rich, UI-renderable payload (approval cards, links, receipts). Rendered via approved components.
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Idempotency for inbound channel messages / workflow-generated replies.
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# --------------------------------------------------------------------------- tasks


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_user_status", "user_id", "status"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="CREATED")
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16))
    planner: Mapped[str | None] = mapped_column(String(60))
    plan: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result_summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    workflow_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    error: Mapped[str | None] = mapped_column(Text)

    steps: Mapped[list[TaskStep]] = relationship(
        back_populates="task", order_by="TaskStep.position", lazy="selectin", cascade="all, delete-orphan"
    )


class TaskStep(TimestampMixin, Base):
    __tablename__ = "task_steps"
    __table_args__ = (UniqueConstraint("task_id", "step_key"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str] = mapped_column(String(40), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    tool: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(default=dict)
    depends_on: Mapped[list[Any]] = mapped_column(default=list)
    risk_level: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="PLANNED")
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    task: Mapped[Task] = relationship(back_populates="steps")


class Approval(TimestampMixin, Base):
    """Exact-action approval record (spec §21)."""

    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_steps.id", ondelete="CASCADE"), unique=True)
    tool: Mapped[str] = mapped_column(String(80), nullable=False)
    operation_summary: Mapped[str] = mapped_column(Text, nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action_snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    target_provider: Mapped[str | None] = mapped_column(String(80))
    recipient: Mapped[str | None] = mapped_column(String(320))
    amount_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(3))
    risk_level: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    policy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ToolExecution(TimestampMixin, Base):
    __tablename__ = "tool_executions"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_steps.id", ondelete="CASCADE"))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    tool: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # STARTED | SUCCEEDED | FAILED
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowEvent(Base):
    """Append-only, user-visible event stream for a task (feeds SSE + audit)."""

    __tablename__ = "workflow_events"
    __table_args__ = (Index("ix_workflow_events_task_seq", "task_id", "seq"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# --------------------------------------------------------------------------- reminders / notifications


class Reminder(TimestampMixin, Base):
    __tablename__ = "reminders"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    fire_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    local_display: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="SCHEDULED")
    workflow_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # migration 0006
    snoozes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # migration 0006


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="in_app")
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# --------------------------------------------------------------------------- email (mock/sandbox mailbox)


class EmailMessage(TimestampMixin, Base):
    """Drafts and sent messages handled by an EmailProvider. `provider` says which (mock vs gmail)."""

    __tablename__ = "email_messages"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    thread_ref: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # DRAFT | SENT | FAILED
    to_address: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(400), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- browser


class BrowserSession(TimestampMixin, Base):
    __tablename__ = "browser_sessions"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    profile_ref: Mapped[str] = mapped_column(String(400), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # ACTIVE | CLOSED | HANDOFF | FAILED
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrowserAction(Base):
    __tablename__ = "browser_actions"
    __table_args__ = (Index("ix_browser_actions_session_seq", "session_id", "seq"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("browser_sessions.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    target: Mapped[str | None] = mapped_column(Text)
    # Typed values are stored redacted; never raw secrets.
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    screenshot_ref: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# --------------------------------------------------------------------------- audit


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_user_created", "user_id", "created_at"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor: Mapped[str] = mapped_column(String(80), nullable=False)  # user | agent | system | admin:<id>
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(60))
    target_id: Mapped[str | None] = mapped_column(String(80))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=text("now()"), nullable=False
    )
