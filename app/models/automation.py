from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AutomationRun(Base):
    __tablename__ = "automation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    classroom_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_type: Mapped[str] = mapped_column(Text, nullable=False)
    period_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempted_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScheduledPayment(Base):
    __tablename__ = "scheduled_payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    classroom_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payment_type: Mapped[str] = mapped_column(Text, nullable=False)
    enrollment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    period_key: Mapped[str] = mapped_column(Text, nullable=False)
    amount_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    automation_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    financial_transaction_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
