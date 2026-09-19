"""
SQLAlchemy ORM Models for MarketPulse SaaS.

Tables:
  users                — application users (Google/Upstox/Zerodha identity)
  auth_accounts        — OAuth provider accounts linked to users
  sessions             — opaque server-side sessions
  subscription_plans   — plan definitions (FREE/BASIC/PRO/PREMIUM)
  subscriptions        — user subscription instances (Razorpay recurring)
  payments             — individual payment records
  payment_events       — raw Razorpay webhook events (idempotency table)
  presets              — user-scoped scanner presets (migrated from JSON files)
  audit_logs           — admin action audit trail

Market data (ticks, parquet, historical) is NOT stored here.
PostgreSQL is for business/application data only.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    UUID,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

# ── Helpers ──────────────────────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ─────────────────────────────────────────────────────────────────────

class UserRole(str, Enum):
    USER = "USER"
    ADMIN = "ADMIN"


class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELETED = "DELETED"


class SubscriptionStatus(str, Enum):
    TRIALING = "TRIALING"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


# ── Models ────────────────────────────────────────────────────────────────────

class User(Base):
    """
    Core application user.

    Identity is established through OAuth providers (Google, Upstox, Zerodha).
    Multiple providers can be linked to the same User via auth_accounts.
    Role determines admin vs normal user access.
    """
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="USER",
        server_default="USER",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    auth_accounts: Mapped[list["AuthAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    payments: Mapped[list["Payment"]] = relationship(back_populates="user")
    presets: Mapped[list["Preset"]] = relationship(back_populates="user")
    support_tickets: Mapped[list["SupportTicket"]] = relationship(back_populates="user")
    preferences: Mapped[Optional["UserPreferences"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_email", "email"),
        Index("ix_users_created_at", "created_at"),
        Index("ix_users_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role}>"

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"

    @property
    def is_active(self) -> bool:
        return self.status == "ACTIVE"


class AuthAccount(Base):
    """
    OAuth provider account linked to a User.

    A single User can have multiple AuthAccounts (one per provider).
    Access tokens are encrypted at rest using Fernet encryption.

    Providers:
      google   — Google OAuth 2.0 / OIDC
      upstox   — Upstox API OAuth 2.0 (standard multi-user)
      zerodha  — Zerodha Kite Connect (requires compliance approval)
    """
    __tablename__ = "auth_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    access_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_profile: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="auth_accounts")

    __table_args__ = (
        UniqueConstraint("provider", "provider_account_id", name="uq_auth_accounts_provider"),
        Index("ix_auth_accounts_user_id", "user_id"),
        Index("ix_auth_accounts_provider", "provider"),
    )


class Session(Base):
    """
    Opaque server-side session.

    Raw session token is stored in an HttpOnly cookie.
    token_hash (SHA-256) is stored in the database.
    Never stores the raw token.
    """
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="sessions")

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_token_hash", "token_hash"),
        Index("ix_sessions_expires_at", "expires_at"),
    )


class SubscriptionPlan(Base):
    """
    Subscription plan definition.

    Seeded at application startup.
    razorpay_plan_id is populated when the plan is created via Razorpay API.
    FREE plan has no Razorpay plan (price = 0).
    """
    __tablename__ = "subscription_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    price_inr: Mapped[int] = mapped_column(Integer, nullable=False)
    razorpay_plan_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    features: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Relationships
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="plan")

    __table_args__ = (
        UniqueConstraint("name", name="uq_subscription_plans_name"),
    )


class Subscription(Base):
    """
    User subscription instance.

    Links a user to a plan via Razorpay recurring billing.
    FREE plan users have no Razorpay subscription.
    Status transitions are driven by Razorpay webhooks.
    """
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("subscription_plans.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ACTIVE"
    )
    razorpay_subscription_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    period_start: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    period_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="subscriptions")
    plan: Mapped["SubscriptionPlan"] = relationship(back_populates="subscriptions")
    payments: Mapped[list["Payment"]] = relationship(back_populates="subscription")

    __table_args__ = (
        Index("ix_subscriptions_user_id", "user_id"),
        Index("ix_subscriptions_status", "status"),
        Index("ix_subscriptions_razorpay_id", "razorpay_subscription_id"),
    )


class Payment(Base):
    """
    Individual payment record.

    Created on successful Razorpay webhook events.
    Do not create from frontend callback alone — always webhook-confirmed.
    """
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    subscription_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("subscriptions.id"), nullable=True
    )
    amount_inr: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="INR")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    razorpay_order_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    razorpay_signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="payments")
    subscription: Mapped[Optional["Subscription"]] = relationship(back_populates="payments")

    __table_args__ = (
        Index("ix_payments_user_id", "user_id"),
        Index("ix_payments_status", "status"),
        Index("ix_payments_created_at", "created_at"),
    )


class PaymentEvent(Base):
    """
    Raw Razorpay webhook event log.

    provider_event_id has a UNIQUE constraint — ensures idempotency.
    If the same Razorpay event is received twice, the second insert fails
    and the handler returns 200 without reprocessing.
    """
    __tablename__ = "payment_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="razorpay")
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    __table_args__ = (
        UniqueConstraint("provider_event_id", name="uq_payment_events_provider_event_id"),
        Index("ix_payment_events_provider_event_id", "provider_event_id"),
    )


class Preset(Base):
    """
    Scanner preset (migrated from JSON file storage).

    user_id = NULL means system/public preset (visible to all users).
    user_id = <user_id> means private user preset.
    is_public = True allows other users to view (but not modify) the preset.
    """
    __tablename__ = "presets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scanner_type: Mapped[str] = mapped_column(String(20), nullable=False, default="live")
    version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    request: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ui_state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    last_used: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user: Mapped[Optional["User"]] = relationship(back_populates="presets")

    __table_args__ = (
        Index("ix_presets_user_id", "user_id"),
        Index("ix_presets_updated_at", "updated_at"),
    )


class AuditLog(Base):
    """
    Admin action audit trail.

    Logs all admin actions: suspend user, change plan, view sensitive data, etc.
    Never stores secrets (tokens, passwords, payment secrets).
    """
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    admin_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    extra_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, name="metadata")
    ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    # Relationships
    admin: Mapped["User"] = relationship(foreign_keys=[admin_id])
    target_user: Mapped[Optional["User"]] = relationship(foreign_keys=[target_user_id])

    __table_args__ = (
        Index("ix_audit_logs_admin_id", "admin_id"),
        Index("ix_audit_logs_created_at", "created_at"),
    )


class SupportTicket(Base):
    """
    Support ticket created by a user (authenticated or unauthenticated).
    """
    __tablename__ = "support_tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    related_to: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="OPEN"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    # Relationships
    user: Mapped[Optional["User"]] = relationship(back_populates="support_tickets")

    __table_args__ = (
        Index("ix_support_tickets_user_id", "user_id"),
        Index("ix_support_tickets_email", "email"),
        Index("ix_support_tickets_status", "status"),
        Index("ix_support_tickets_created_at", "created_at"),
    )


class UserPreferences(Base):
    """
    User-specific preferences.
    """
    __tablename__ = "user_preferences"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    default_exchange: Mapped[str] = mapped_column(String(20), nullable=False, default="NSE")
    default_page_size: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="Asia/Kolkata")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="preferences")

