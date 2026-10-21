"""students and enrollments

Revision ID: 0003_students_and_enrollments
Revises: 0002_classrooms_and_memberships
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003_students_and_enrollments"
down_revision: Union[str, Sequence[str], None] = "0002_classrooms_and_memberships"
branch_labels = None
depends_on = None

CODE_RE = "^[A-Z0-9_-]{1,32}$"


def upgrade() -> None:
    op.create_table(
        "students",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], name="fk_students__created_by_user_id__users"),
    )

    op.create_table(
        "enrollments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("student_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("student_code", sa.Text(), nullable=False),
        sa.Column("current_balance", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("started_on", sa.Date(), nullable=True),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_enrollments__classroom_id__classrooms"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], name="fk_enrollments__student_id__students"),
        sa.UniqueConstraint("classroom_id", "student_code", name="uq_enrollments__classroom_student_code"),
        sa.UniqueConstraint("classroom_id", "student_id", name="uq_enrollments__classroom_student"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_enrollments__id_classroom"),
        sa.CheckConstraint(f"student_code ~ '{CODE_RE}'", name="ck_enrollments__student_code_format"),
        sa.CheckConstraint("ended_on IS NULL OR started_on IS NULL OR ended_on >= started_on", name="ck_enrollments__end_after_start"),
    )
    op.create_index("ix_enrollments__classroom_active_student_code", "enrollments", ["classroom_id", "active", "student_code"])
    op.create_index("ix_enrollments__student_id", "enrollments", ["student_id"])


def downgrade() -> None:
    op.drop_index("ix_enrollments__student_id", table_name="enrollments")
    op.drop_index("ix_enrollments__classroom_active_student_code", table_name="enrollments")
    op.drop_table("enrollments")
    op.drop_table("students")
