"""identity and authentication

Revision ID: 0001_identity_and_auth
Revises: None
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_identity_and_auth"
down_revision: Union[str, Sequence[str], None] = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("email_normalized", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("account_status", sa.Text(), nullable=False, server_default=sa.text("'pending_verification'")),
        sa.Column("teacher_verification_status", sa.Text(), nullable=False, server_default=sa.text("'not_requested'")),
        sa.Column("teacher_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("email_normalized", name="uq_users__email_normalized"),
        sa.CheckConstraint("account_status IN ('pending_verification','active','suspended','disabled')", name="ck_users__account_status"),
        sa.CheckConstraint("teacher_verification_status IN ('not_requested','pending','verified','rejected')", name="ck_users__teacher_verification_status"),
    )

    op.create_table(
        "auth_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_type", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_auth_tokens__user_id__users"),
        sa.UniqueConstraint("token_hash", name="uq_auth_tokens__token_hash"),
        sa.CheckConstraint("token_type IN ('email_verification','password_reset')", name="ck_auth_tokens__token_type"),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_tokens__expires_after_created"),
    )
    op.create_index("ix_auth_tokens__user_id", "auth_tokens", ["user_id"])

    op.create_table(
        "user_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_hash", sa.LargeBinary(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_sessions__user_id__users"),
        sa.UniqueConstraint("session_token_hash", name="uq_user_sessions__session_token_hash"),
        sa.CheckConstraint("expires_at > created_at", name="ck_user_sessions__expires_after_created"),
    )
    op.create_index(
        "ix_user_sessions__active_user_expires",
        "user_sessions",
        ["user_id", "expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index("ix_user_sessions__expires_at", "user_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_user_sessions__expires_at", table_name="user_sessions")
    op.drop_index("ix_user_sessions__active_user_expires", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_index("ix_auth_tokens__user_id", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    op.drop_table("users")
