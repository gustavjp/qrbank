from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.automation import AutomationRun, ScheduledPayment
from app.models.classroom import Classroom, ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.student import Enrollment, Student
from app.services.financial import post_weekly_allowance
from app.services.weekly_allowances import (
    due_allowance_period, run_due_weekly_allowances,
)


PAYDAY = datetime(2031, 1, 6, 12, tzinfo=timezone.utc)  # Monday, 7 AM EST


def _classroom(
    db: Session, *, enabled: bool = True, amount: int = 20,
    zone: str = "America/New_York", due_time: time = time(6),
) -> uuid.UUID:
    classroom_id = uuid.uuid4()
    db.add(Classroom(
        id=classroom_id, name=f"Allowances {classroom_id}", school_year="2030-2031",
    ))
    db.flush()
    db.add(ClassroomSettings(
        classroom_id=classroom_id, timezone=zone,
        weekly_allowance_enabled=enabled, weekly_allowance_amount=amount,
        weekly_allowance_day=0, weekly_allowance_time=due_time,
    ))
    db.flush()
    return classroom_id


def _enrollment(
    db: Session, classroom_id: uuid.UUID, code: str, *,
    balance: int = 0, active: bool = True,
    started_on: date | None = date(2030, 9, 1),
    ended_on: date | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    student_id, enrollment_id = uuid.uuid4(), uuid.uuid4()
    db.add(Student(id=student_id, display_name=f"Student {code}"))
    db.flush()
    kwargs = dict(
        id=enrollment_id, classroom_id=classroom_id,
        student_id=student_id, student_code=code, active=active,
        started_on=started_on, ended_on=ended_on, current_balance=balance,
    )
    if created_at is not None:
        kwargs["created_at"] = created_at
    db.add(Enrollment(**kwargs))
    db.flush()
    return enrollment_id


def _rows(engine, model, classroom_id):
    with Session(engine) as db:
        return list(db.scalars(select(model).where(model.classroom_id == classroom_id)))


def _run(engine, classroom_id, *, now=PAYDAY):
    return run_due_weekly_allowances(
        session_factory=sessionmaker(bind=engine),
        now=now, classroom_id=classroom_id,
    )


def test_allowance_schedule_is_classroom_local_and_dst_safe():
    settings = ClassroomSettings(
        classroom_id=uuid.uuid4(), timezone="America/New_York",
        weekly_allowance_day=0, weekly_allowance_time=time(6),
    )
    assert due_allowance_period(
        settings, datetime(2031, 1, 6, 10, 59, tzinfo=timezone.utc)
    ) is None
    winter = due_allowance_period(
        settings, datetime(2031, 1, 6, 11, tzinfo=timezone.utc)
    )
    assert winter is not None
    assert winter.key == "2031-01-06"
    assert winter.due_at == datetime(2031, 1, 6, 11, tzinfo=timezone.utc)

    summer = datetime(2031, 7, 7, 6, tzinfo=ZoneInfo("America/New_York"))
    summer_utc = summer.astimezone(timezone.utc)
    assert summer_utc.hour == 10
    assert due_allowance_period(settings, summer_utc - timedelta(seconds=1)) is None
    due = due_allowance_period(settings, summer_utc)
    assert due is not None and due.key == "2031-07-07" and due.due_at == summer_utc


def test_default_disabled_and_not_due_do_not_change_financial_state(engine):
    with Session(engine) as db, db.begin():
        disabled = _classroom(db, enabled=False)
        enrolled = _enrollment(db, disabled, "01")
        enabled = _classroom(db, due_time=time(9))
        _enrollment(db, enabled, "01")

    assert _run(engine, disabled).classrooms_due == 0
    assert _run(engine, enabled).classrooms_due == 0
    assert _rows(engine, FinancialRequest, disabled) == []
    assert _rows(engine, FinancialTransaction, enabled) == []
    with Session(engine) as db:
        assert db.get(Enrollment, enrolled).current_balance == 0


def test_multiple_students_once_per_week_snapshots_and_changed_settings(engine):
    with Session(engine) as db, db.begin():
        classroom = _classroom(db)
        alice = _enrollment(db, classroom, "01", balance=4)
        bob = _enrollment(db, classroom, "02")

    first = _run(engine, classroom)
    assert first.classrooms_due == 1 and first.payments_posted == 2
    assert first.payments_failed == 0
    txs = _rows(engine, FinancialTransaction, classroom)
    assert len(txs) == 2
    assert {tx.student_code_snapshot for tx in txs} == {"01", "02"}
    assert all(
        tx.transaction_type == "allowance" and tx.source == "automatic"
        and tx.amount == 20 and tx.actor_user_id is None
        and tx.job_assignment_id is None
        for tx in txs
    )
    with Session(engine) as db:
        assert db.get(Enrollment, alice).current_balance == 24
        assert db.get(Enrollment, bob).current_balance == 20
    assert {p.period_key for p in _rows(engine, ScheduledPayment, classroom)} == {"2031-01-06"}

    with Session(engine) as db, db.begin():
        db.get(ClassroomSettings, classroom).weekly_allowance_amount = 6

    again = _run(engine, classroom)
    assert again.payments_posted == 0 and again.payments_replayed == 2
    assert len(_rows(engine, FinancialTransaction, classroom)) == 2
    assert {p.amount_snapshot for p in _rows(engine, ScheduledPayment, classroom)} == {20}
    following = _run(engine, classroom, now=PAYDAY + timedelta(days=7))
    assert following.payments_posted == 2 and following.payments_failed == 0
    assert {p.amount_snapshot for p in _rows(engine, ScheduledPayment, classroom)} == {6, 20}
    assert len(_rows(engine, FinancialRequest, classroom)) == 4
    assert len(_rows(engine, FinancialTransaction, classroom)) == 4
    with Session(engine) as db:
        assert db.get(Enrollment, alice).current_balance == 30
        assert db.get(Enrollment, bob).current_balance == 26
    assert all(run.status == "completed" for run in _rows(engine, AutomationRun, classroom))


def test_zero_allowance_records_period_but_not_zero_ledger_row(engine):
    with Session(engine) as db, db.begin():
        classroom = _classroom(db, amount=0)
        student = _enrollment(db, classroom, "01")

    assert _run(engine, classroom).payments_posted == 1
    payments = _rows(engine, ScheduledPayment, classroom)
    assert len(payments) == 1 and payments[0].amount_snapshot == 0
    assert payments[0].financial_transaction_id is None
    assert _rows(engine, FinancialTransaction, classroom) == []
    with Session(engine) as db, db.begin():
        db.get(ClassroomSettings, classroom).weekly_allowance_amount = 9
    assert _run(engine, classroom).payments_replayed == 1
    assert _rows(engine, FinancialTransaction, classroom) == []
    assert _run(engine, classroom, now=PAYDAY + timedelta(days=7)).payments_posted == 1
    assert _rows(engine, FinancialTransaction, classroom)[0].amount == 9
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 9


def test_active_enrollments_only_and_cross_classroom_isolation(engine):
    with Session(engine) as db, db.begin():
        a = _classroom(db)
        b = _classroom(db)
        eligible = _enrollment(db, a, "01")
        inactive = _enrollment(db, a, "02", active=False)
        late_created = _enrollment(
            db, a, "03", created_at=PAYDAY + timedelta(seconds=1),
        )
        later_start = _enrollment(db, a, "04", started_on=date(2031, 1, 7))
        ended = _enrollment(db, a, "05", ended_on=date(2030, 12, 20))
        foreign = _enrollment(db, b, "01")
    result = _run(engine, a)
    assert result.classrooms_due == 1 and result.payments_posted == 1
    assert len(_rows(engine, ScheduledPayment, a)) == 1
    assert _rows(engine, FinancialTransaction, b) == []
    assert _rows(engine, FinancialRequest, b) == []
    with Session(engine) as db:
        assert db.get(Enrollment, eligible).current_balance == 20
        assert all(
            db.get(Enrollment, item).current_balance == 0
            for item in (inactive, late_created, later_start, ended, foreign)
        )


def test_partial_failure_rolls_back_one_student_and_retries(engine):
    with Session(engine) as db, db.begin():
        classroom = _classroom(db, amount=10)
        overflow = _enrollment(db, classroom, "01", balance=2**31 - 5)
        normal = _enrollment(db, classroom, "02")

    first = _run(engine, classroom)
    assert first.payments_failed == 1 and first.payments_posted == 1
    assert len(_rows(engine, FinancialRequest, classroom)) == 1
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1
    assert len(_rows(engine, ScheduledPayment, classroom)) == 1
    assert _rows(engine, AutomationRun, classroom)[0].status == "partial"
    with Session(engine) as db, db.begin():
        db.get(ClassroomSettings, classroom).weekly_allowance_amount = 3

    retry = _run(engine, classroom)
    assert retry.payments_failed == 0
    assert retry.payments_posted == 1 and retry.payments_replayed == 1
    assert len(_rows(engine, ScheduledPayment, classroom)) == 2
    with Session(engine) as db:
        assert db.get(Enrollment, overflow).current_balance == 2**31 - 2
        assert db.get(Enrollment, normal).current_balance == 10


def test_concurrent_runners_cannot_double_pay(engine):
    with Session(engine) as db, db.begin():
        classroom = _classroom(db, amount=13)
        student = _enrollment(db, classroom, "01")

    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda _: _run(engine, classroom), range(2)))
    assert sum(item.payments_posted for item in runs) == 1
    assert sum(item.payments_replayed for item in runs) == 1
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1
    assert len(_rows(engine, ScheduledPayment, classroom)) == 1
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 13


def test_direct_writer_rejects_foreign_enrollment_and_stale_schedule(engine):
    with Session(engine) as db, db.begin():
        a = _classroom(db)
        b = _classroom(db)
        student = _enrollment(db, a, "01")

    with Session(engine) as db, db.begin():
        foreign = post_weekly_allowance(
            db, classroom_id=b, enrollment_id=student,
            period_key="2031-01-06", due_at=PAYDAY - timedelta(hours=1),
            local_due_date=date(2031, 1, 6),
        )
        assert foreign is None
        stale = post_weekly_allowance(
            db, classroom_id=a, enrollment_id=student,
            period_key="2031-01-06", due_at=PAYDAY,
            local_due_date=date(2031, 1, 6),
        )
        assert stale is None
    assert _rows(engine, FinancialRequest, a) == []
    assert _rows(engine, FinancialRequest, b) == []
    assert _rows(engine, ScheduledPayment, a) == []
    assert _rows(engine, ScheduledPayment, b) == []


def test_classroom_schedule_changes_do_not_pay_obsolete_deadline(engine):
    with Session(engine) as db, db.begin():
        classroom = _classroom(db)
        student = _enrollment(db, classroom, "01")
        db.get(ClassroomSettings, classroom).weekly_allowance_time = time(8)

    # The formerly configured 6 AM schedule must not trigger an 8 AM payday.
    assert _run(engine, classroom).classrooms_due == 0
    assert _rows(engine, FinancialTransaction, classroom) == []
    result = _run(engine, classroom, now=PAYDAY + timedelta(hours=2))
    assert result.payments_posted == 1
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 20
