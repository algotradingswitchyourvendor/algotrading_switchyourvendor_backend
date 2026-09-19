"""Initial schema — MarketPulse SaaS

Revision ID: 0001
Revises:
Create Date: 2026-09-15

Creates all application tables:
  users, auth_accounts, sessions,
  subscription_plans, subscriptions, payments, payment_events,
  presets, audit_logs
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── users ──────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("avatar_url", sa.Text, nullable=True),
        sa.Column("role", sa.String(10), nullable=False, server_default="USER"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_created_at", "users", ["created_at"])
    op.create_index("ix_users_status", "users", ["status"])

    # ── auth_accounts ───────────────────────────────────────────────────────
    op.create_table(
        "auth_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_account_id", sa.String(255), nullable=False),
        sa.Column("access_token_enc", sa.Text, nullable=True),
        sa.Column("refresh_token_enc", sa.Text, nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_profile", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_auth_accounts_provider", "auth_accounts", ["provider", "provider_account_id"])
    op.create_index("ix_auth_accounts_user_id", "auth_accounts", ["user_id"])
    op.create_index("ix_auth_accounts_provider", "auth_accounts", ["provider"])

    # ── sessions ────────────────────────────────────────────────────────────
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_sessions_token_hash", "sessions", ["token_hash"])
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    # ── subscription_plans ─────────────────────────────────────────────────
    op.create_table(
        "subscription_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("price_inr", sa.Integer, nullable=False),
        sa.Column("razorpay_plan_id", sa.String(255), nullable=True),
        sa.Column("features", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
    )
    op.create_unique_constraint("uq_subscription_plans_name", "subscription_plans", ["name"])

    # ── subscriptions ──────────────────────────────────────────────────────
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("subscription_plans.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("razorpay_subscription_id", sa.String(255), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index("ix_subscriptions_razorpay_id", "subscriptions", ["razorpay_subscription_id"])

    # ── payments ───────────────────────────────────────────────────────────
    op.create_table(
        "payments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("subscription_id", sa.String(36), sa.ForeignKey("subscriptions.id"), nullable=True),
        sa.Column("amount_inr", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default="INR"),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("razorpay_order_id", sa.String(255), nullable=True),
        sa.Column("razorpay_payment_id", sa.String(255), nullable=True),
        sa.Column("razorpay_signature", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_payments_user_id", "payments", ["user_id"])
    op.create_index("ix_payments_status", "payments", ["status"])
    op.create_index("ix_payments_created_at", "payments", ["created_at"])

    # ── payment_events (idempotency table) ─────────────────────────────────
    op.create_table(
        "payment_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False, server_default="razorpay"),
        sa.Column("provider_event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # CRITICAL: UNIQUE constraint for idempotency — prevents duplicate webhook processing
    op.create_unique_constraint("uq_payment_events_provider_event_id", "payment_events", ["provider_event_id"])
    op.create_index("ix_payment_events_provider_event_id", "payment_events", ["provider_event_id"])

    # ── presets ────────────────────────────────────────────────────────────
    op.create_table(
        "presets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("scanner_type", sa.String(20), nullable=False, server_default="live"),
        sa.Column("version", sa.String(20), nullable=True),
        sa.Column("request", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("ui_state", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("is_public", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("favorite", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("usage_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_presets_user_id", "presets", ["user_id"])
    op.create_index("ix_presets_updated_at", "presets", ["updated_at"])

    # ── audit_logs ─────────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("admin_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_admin_id", "audit_logs", ["admin_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("presets")
    op.drop_table("payment_events")
    op.drop_table("payments")
    op.drop_table("subscriptions")
    op.drop_table("subscription_plans")
    op.drop_table("sessions")
    op.drop_table("auth_accounts")
    op.drop_table("users")
