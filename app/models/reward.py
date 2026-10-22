from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Reward(Base):
    """Classroom reward definition, independent of a financial purchase."""

    __tablename__ = "rewards"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    classroom_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reward_code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reward_type: Mapped[str] = mapped_column(Text, nullable=False)
    price_points: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"),
    )
    inventory_tracking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
    )
    quantity_available: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    available_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    image_storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
