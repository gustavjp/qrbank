from __future__ import annotations

import uuid
from datetime import datetime, time

from sqlalchemy import Boolean, DateTime, Integer, SmallInteger, Text, Time, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Classroom(Base):
    __tablename__ = "classrooms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    school_year: Mapped[str] = mapped_column(Text, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class ClassroomMembership(Base):
    __tablename__ = "classroom_memberships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    classroom_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class ClassroomSettings(Base):
    __tablename__ = "classroom_settings"

    classroom_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    timezone: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'America/New_York'"),
    )
    starting_balance: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    weekly_allowance_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    weekly_allowance_amount: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("20"))
    weekly_allowance_day: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    weekly_allowance_time: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'06:00:00'"))
    weekly_wages_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    weekly_wage_day: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    weekly_wage_time: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'06:10:00'"))
    sound_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    duplicate_scan_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    allow_negative_balance: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    student_selection_timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("120"),
    )
    confirm_normal_transactions: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    confirm_balance_reset: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
