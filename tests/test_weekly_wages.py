from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.automation import AutomationRun, ScheduledPayment
from app.models.classroom import Classroom, ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.job import Job, JobAssignment
from app.models.student import Enrollment, Student
from app.services.financial import post_weekly_job_wage
from app.services.weekly_wages import due_wage_period, run_due_weekly_wages


# Use far-future calendar dates so fixture creation is before the due instant.
PAYDAY = datetime(2031, 1, 6, 12, 0, tzinfo=timezone.utc)  # Monday, 7am EST


def _classroom(
    db: Session, *, enabled: bool = True, zone: str = "America/New_York",
    due_time: time = time(6, 10),
) -> uuid.UUID:
    classroom_id = uuid.uuid4()
    db.add(Classroom(
        id=classroom_id, name=f"Wages {classroom_id}", school_year="2030-2031"
    ))
    db.flush()
    db.add(ClassroomSettings(
        classroom_id=classroom_id, timezone=zone,
        weekly_wages_enabled=enabled, weekly_wage_day=0,
        weekly_wage_time=due_time,
    ))
    return classroom_id


def _enrollment(
    db: Session, classroom_id: uuid.UUID, code: str, *,
    balance: int = 0, active: bool = True,
) -> uuid.UUID:
    student_id, enrollment_id = uuid.uuid4(), uuid.uuid4()
    db.add(Student(id=student_id, display_name=f"Student {code}"))
    db.flush()
    db.add(Enrollment(
        id=enrollment_id, classroom_id=classroom_id,
        student_id=student_id, student_code=code, active=active,
        started_on=date(2030, 9, 1), current_balance=balance,
    ))
    db.flush()
    return enrollment_id


def _job(db: Session, classroom_id: uuid.UUID, code: str, wage: int, *,
         active: bool = True) -> uuid.UUID:
    job_id = uuid.uuid4()
    db.add(Job(
        id=job_id, classroom_id=classroom_id, job_code=code,
        name=f"Job {code}", weekly_wage=wage, active=active,
    ))
    db.flush()
    return job_id


def _assign(
    db: Session, classroom_id: uuid.UUID, enrollment_id: uuid.UUID,
    job_id: uuid.UUID, *, started_on: date = date(2030, 9, 1),
    ended_on: date | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    assignment_id = uuid.uuid4()
    args = dict(
        id=assignment_id, classroom_id=classroom_id,
        enrollment_id=enrollment_id, job_id=job_id,
        started_on=started_on, ended_on=ended_on,
    )
    if created_at is not None:
        args["created_at"] = created_at
    db.add(JobAssignment(**args))
    db.flush()
    return assignment_id


def _rows(engine, model, classroom_id):
    with Session(engine) as db:
        return list(db.scalars(select(model).where(model.classroom_id == classroom_id)))


def _wages(engine, now=PAYDAY):
    return run_due_weekly_wages(session_factory=sessionmaker(bind=engine), now=now)


def test_schedule_respects_classroom_local_time_and_dst() -> None:
    settings = ClassroomSettings(
        classroom_id=uuid.uuid4(), timezone="America/New_York",
        weekly_wage_day=0, weekly_wage_time=time(6, 10),
    )
    assert due_wage_period(settings, datetime(2031, 1, 6, 11, 9, tzinfo=timezone.utc)) is None
    winter = due_wage_period(settings, datetime(2031, 1, 6, 11, 10, tzinfo=timezone.utc))
    assert winter is not None
    assert winter.key == "2031-01-06"
    assert winter.due_at == datetime(2031, 1, 6, 11, 10, tzinfo=timezone.utc)

    # After the DST change the same local schedule is one hour earlier in UTC.
    summer_day = date(2031, 7, 7)
    summer_utc = datetime.combine(summer_day, time(6, 10), ZoneInfo("America/New_York")).astimezone(timezone.utc)
    assert summer_utc.hour == 10
    assert due_wage_period(settings, summer_utc - timedelta(seconds=1)) is None
    summer = due_wage_period(settings, summer_utc)
    assert summer is not None
    assert summer.key == summer_day.isoformat()
    assert summer.due_at == summer_utc


def test_multiple_jobs_pay_separately_once_per_week_and_keep_snapshots(engine) -> None:
    with Session(engine) as db, db.begin():
        classroom = _classroom(db)
        alice = _enrollment(db, classroom, "01")
        bob = _enrollment(db, classroom, "02")
        first = _job(db, classroom, "J01", 12)
        second = _job(db, classroom, "J02", 5)
        a1 = _assign(db, classroom, alice, first)
        a2 = _assign(db, classroom, alice, second)
        _assign(db, classroom, bob, first)

    before = _wages(engine, PAYDAY - timedelta(hours=1))
    assert before.classrooms_due == 0
    assert _rows(engine, FinancialTransaction, classroom) == []

    first_run = _wages(engine)
    assert first_run.payments_posted == 3
    assert first_run.payments_failed == 0
    txs = _rows(engine, FinancialTransaction, classroom)
    assert len(txs) == 3
    assert len({tx.job_assignment_id for tx in txs}) == 3
    assert {a1, a2} <= {tx.job_assignment_id for tx in txs}
    assert all(tx.transaction_type == "job_wage" and tx.source == "automatic" for tx in txs)
    assert all(tx.actor_user_id is None and tx.job_wage_snapshot == tx.amount for tx in txs)
    with Session(engine) as db:
        assert db.get(Enrollment, alice).current_balance == 17
        assert db.get(Enrollment, bob).current_balance == 12
        assert {p.period_key for p in _rows(engine, ScheduledPayment, classroom)} == {"2031-01-06"}

    again = _wages(engine)
    assert again.payments_posted == 0 and again.payments_replayed == 3
    assert len(_rows(engine, FinancialTransaction, classroom)) == 3
    with Session(engine) as db, db.begin():
        job = db.get(Job, first)
        job.name = "Renamed"
        job.weekly_wage = 20

    assert _wages(engine).payments_replayed == 3
    assert all(tx.job_name_snapshot == "Job J01" for tx in _rows(engine, FinancialTransaction, classroom)
               if tx.job_assignment_id == a1)
    future = _wages(engine, PAYDAY + timedelta(days=7))
    assert future.payments_posted == 3
    assert len(_rows(engine, FinancialTransaction, classroom)) == 6
    with Session(engine) as db:
        assert db.get(Enrollment, alice).current_balance == 42
        assert db.get(Enrollment, bob).current_balance == 32
    assert len(_rows(engine, FinancialRequest, classroom)) == 6
    assert all(run.status == "completed" for run in _rows(engine, AutomationRun, classroom))


def test_zero_wage_reserves_period_without_fake_ledger_entry(engine) -> None:
    with Session(engine) as db, db.begin():
        classroom = _classroom(db)
        student = _enrollment(db, classroom, "01")
        job_id = _job(db, classroom, "ZERO", 0)
        _assign(db, classroom, student, job_id)
    first = _wages(engine)
    assert first.payments_posted == 1
    payments = _rows(engine, ScheduledPayment, classroom)
    assert len(payments) == 1
    assert payments[0].amount_snapshot == 0
    assert payments[0].financial_transaction_id is None
    assert _rows(engine, FinancialTransaction, classroom) == []

    with Session(engine) as db, db.begin():
        db.get(Job, job_id).weekly_wage = 9
    assert _wages(engine).payments_replayed == 1
    assert _rows(engine, FinancialTransaction, classroom) == []
    assert _wages(engine, PAYDAY + timedelta(days=7)).payments_posted == 1
    assert _rows(engine, FinancialTransaction, classroom)[0].amount == 9


def test_inactive_cross_tenant_and_late_assignments_are_excluded(engine) -> None:
    with Session(engine) as db, db.begin():
        a = _classroom(db)
        b = _classroom(db, enabled=False)
        active_student = _enrollment(db, a, "01")
        inactive_student = _enrollment(db, a, "02", active=False)
        normal = _job(db, a, "ACTIVE", 7)
        inactive_job = _job(db, a, "INACTIVE", 10, active=False)
        _assign(db, a, active_student, normal)
        _assign(db, a, inactive_student, normal)
        _assign(db, a, active_student, inactive_job)
        _assign(db, a, active_student, normal, ended_on=date(2030, 12, 31))
        late_job = _job(db, a, "LATE", 10)
        future_job = _job(db, a, "FUTURE", 10)
        _assign(db, a, active_student, late_job, created_at=PAYDAY + timedelta(minutes=5))
        _assign(db, a, active_student, future_job, started_on=date(2031, 1, 7))
        other_student = _enrollment(db, b, "01")
        other_job = _job(db, b, "FOREIGN", 25)
        _assign(db, b, other_student, other_job)

    summary = _wages(engine)
    assert summary.classrooms_due == 1 and summary.payments_posted == 1
    assert len(_rows(engine, FinancialTransaction, a)) == 1
    assert _rows(engine, FinancialTransaction, b) == []
    with Session(engine) as db:
        assert db.get(Enrollment, active_student).current_balance == 7
        assert db.get(Enrollment, inactive_student).current_balance == 0
        assert db.get(Enrollment, other_student).current_balance == 0


def test_partial_failure_rolls_back_and_next_run_retries_missing_payment(engine) -> None:
    with Session(engine) as db, db.begin():
        classroom = _classroom(db)
        overflow = _enrollment(db, classroom, "01", balance=2**31 - 2)
        regular = _enrollment(db, classroom, "02")
        big_job = _job(db, classroom, "BIG", 10)
        normal_job = _job(db, classroom, "SMALL", 2)
        _assign(db, classroom, overflow, big_job)
        _assign(db, classroom, regular, normal_job)

    first = _wages(engine)
    assert first.payments_failed == 1 and first.payments_posted == 1
    assert len(_rows(engine, FinancialRequest, classroom)) == 1
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1
    assert len(_rows(engine, ScheduledPayment, classroom)) == 1
    assert _rows(engine, AutomationRun, classroom)[0].status == "partial"
    with Session(engine) as db, db.begin():
        db.get(Job, big_job).weekly_wage = 1

    retry = _wages(engine)
    assert retry.payments_failed == 0
    assert retry.payments_posted == 1 and retry.payments_replayed == 1
    assert len(_rows(engine, ScheduledPayment, classroom)) == 2
    with Session(engine) as db:
        assert db.get(Enrollment, overflow).current_balance == 2**31 - 1
        assert db.get(Enrollment, regular).current_balance == 2


def test_simultaneous_scheduler_invocations_cannot_double_pay(engine) -> None:
    with Session(engine) as db, db.begin():
        classroom = _classroom(db)
        student = _enrollment(db, classroom, "01")
        job_id = _job(db, classroom, "J01", 13)
        _assign(db, classroom, student, job_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda _: _wages(engine), range(2)))
    assert sum(r.payments_posted for r in runs) == 1
    assert sum(r.payments_replayed for r in runs) == 1
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1
    assert len(_rows(engine, ScheduledPayment, classroom)) == 1
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 13

def test_financial_writer_rejects_foreign_classroom_assignment(engine) -> None:
    with Session(engine) as db, db.begin():
        a = _classroom(db)
        b = _classroom(db)
        student = _enrollment(db, a, "01")
        job_id = _job(db, a, "J01", 10)
        assignment = _assign(db, a, student, job_id)
    with Session(engine) as db, db.begin():
        result = post_weekly_job_wage(
            db, classroom_id=b, assignment_id=assignment,
            period_key="2031-01-06", due_at=PAYDAY - timedelta(minutes=50),
            local_due_date=date(2031, 1, 6),
        )
        assert result is None
    assert _rows(engine, FinancialRequest, a) == []
    assert _rows(engine, FinancialRequest, b) == []
    assert _rows(engine, ScheduledPayment, a) == []
    assert _rows(engine, ScheduledPayment, b) == []
