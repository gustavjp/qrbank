from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BehaviorEvent(Base):
    __tablename__ = "behavior_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    classroom_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    enrollment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    behavior_type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"),
    )
    financial_transaction_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action_code_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_name_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
