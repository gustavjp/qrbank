from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.behavior import BehaviorEvent
from app.services.behavior_history import (
    BehaviorHistoryValidationError, select_classroom_behavior,
)


TREND_WEEK_COUNT = 8


@dataclass(frozen=True)
class BehaviorWeek:
    starts_on: date
    positive_count: int
    negative_count: int

    @property
    def total_count(self) -> int:
        return self.positive_count + self.negative_count


@dataclass(frozen=True)
class BehaviorWeeklyTrend:
    weeks: list[BehaviorWeek]
    positive_count: int
    negative_count: int
    max_count: int
    timezone_name: str
    starts_on: date
    ends_on: date

    @property
    def total_count(self) -> int:
        return self.positive_count + self.negative_count


def classroom_weekly_behavior_trend(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID | None = None,
    behavior_type: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
    today: date | None = None,
) -> BehaviorWeeklyTrend:
    """Read-only trend using the exact authorized filters from behavior history.

    Show eight consecutive classroom-local weeks, through the week of the
    selected end date (if supplied), otherwise through the current local week.
    A future-only start date anchors its own week. The chart is deliberately
    bounded even when the unpaginated summary covers years of history.
    """
    selection = select_classroom_behavior(
        db, user_id=user_id, classroom_id=classroom_id,
        enrollment_id=enrollment_id, behavior_type=behavior_type,
        date_from=date_from, date_to=date_to,
    )
    zone = ZoneInfo(selection.timezone_name)
    local_today = today if today is not None else datetime.now(zone).date()
    if date_to is not None:
        anchor = date_to
    elif date_from is not None:
        anchor = max(local_today, date_from)
    else:
        anchor = local_today
    try:
        current_week = anchor - timedelta(days=anchor.weekday())
        first_week = current_week - timedelta(weeks=TREND_WEEK_COUNT - 1)
        last_exclusive = current_week + timedelta(days=7)
        end_sunday = last_exclusive - timedelta(days=1)
    except OverflowError as exc:
        raise BehaviorHistoryValidationError(
            "weekly chart date is outside the supported range"
        ) from exc

    lower = datetime.combine(first_week, time.min, tzinfo=zone).astimezone(timezone.utc)
    upper = datetime.combine(last_exclusive, time.min, tzinfo=zone).astimezone(timezone.utc)

    # timezone(tz, timestamptz) yields a local timestamp; date_trunc('week')
    # groups by Monday in the classroom's wall time, including DST transitions.
    week_start = func.date_trunc(
        "week", func.timezone(selection.timezone_name, BehaviorEvent.occurred_at)
    ).label("week_start")
    stmt = (
        selection.statement
        .with_only_columns(
            week_start, BehaviorEvent.behavior_type, func.count(),
        )
        .where(
            BehaviorEvent.occurred_at >= lower,
            BehaviorEvent.occurred_at < upper,
        )
        .group_by(week_start, BehaviorEvent.behavior_type)
    )
    by_week: dict[date, dict[str, int]] = {}
    for local_week_start, kind, count in db.execute(stmt):
        day = local_week_start.date()
        by_week.setdefault(day, {})[kind] = count

    weeks = []
    for index in range(TREND_WEEK_COUNT):
        monday = first_week + timedelta(weeks=index)
        counts = by_week.get(monday, {})
        weeks.append(BehaviorWeek(
            starts_on=monday,
            positive_count=counts.get("positive", 0),
            negative_count=counts.get("negative", 0),
        ))
    return BehaviorWeeklyTrend(
        weeks=weeks,
        positive_count=sum(week.positive_count for week in weeks),
        negative_count=sum(week.negative_count for week in weeks),
        max_count=max(1, *(max(week.positive_count, week.negative_count) for week in weeks)),
        timezone_name=selection.timezone_name,
        starts_on=first_week,
        ends_on=end_sunday,
    )
