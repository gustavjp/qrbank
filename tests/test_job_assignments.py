from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.audit import AdminAuditEvent
from app.models.classroom import Classroom, ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.job import Job, JobAssignment
from app.models.student import Enrollment, Student
from app.services.auth import (
    create_email_verification_token,
    create_user_session,
    register_user,
    verify_email_token,
)
from app.services.job_assignments import (
    JobAssignmentValidationError,
    create_job_assignment,
    end_job_assignment,
)


PASSWORD = "job-assignment-test-password"


def _client(engine) -> tuple[TestClient, uuid.UUID]:
    with Session(engine) as db, db.begin():
        user = register_user(db, email=f"assignment-{uuid.uuid4()}@example.test", password=PASSWORD)
        token = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=token.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, session_token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.get("/dashboard")
    return client, user_id


def _classroom(engine, owner: uuid.UUID, *, zone: str = "America/New_York") -> uuid.UUID:
    with Session(engine) as db, db.begin():
        classroom = Classroom(
            id=uuid.uuid4(), name=f"Assignment class {uuid.uuid4()}",
            school_year="2026-2027",
        )
        db.add(classroom)
        db.flush()
        db.add_all([
            ClassroomSettings(classroom_id=classroom.id, timezone=zone),
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom.id,
                user_id=owner, role="owner", created_by_user_id=owner,
            ),
        ])
        return classroom.id


def _student(engine, classroom_id: uuid.UUID, *, code: str, active: bool = True) -> uuid.UUID:
    with Session(engine) as db, db.begin():
        student = Student(id=uuid.uuid4(), display_name=f"Student {code}")
        db.add(student)
        db.flush()
        enrollment = Enrollment(
            id=uuid.uuid4(), classroom_id=classroom_id,
            student_id=student.id, student_code=code,
            current_balance=0, started_on=date(2026, 1, 1), active=active,
        )
        db.add(enrollment)
        return enrollment.id


def _csrf(client: TestClient) -> str:
    token = client.cookies.get(settings.csrf_cookie_name)
    assert token
    return token


def _job(engine, client: TestClient, classroom_id: uuid.UUID, *, code: str = "J01") -> uuid.UUID:
    client.get(f"/classrooms/{classroom_id}/jobs")
    response = client.post(f"/classrooms/{classroom_id}/jobs/new", data={
        "code": code, "name": f"Job {code}",
        "weekly_wage": "20", "csrf_token": _csrf(client),
    })
    assert response.status_code == 303
    with Session(engine) as db:
        job_id = db.scalar(select(Job.id).where(
            Job.classroom_id == classroom_id, Job.job_code == code,
        ))
        assert job_id is not None
        return job_id


def _assign(client: TestClient, classroom_id: uuid.UUID, enrollment_id: uuid.UUID, job_id: uuid.UUID):
    return client.post(f"/classrooms/{classroom_id}/job-assignments/new", data={
        "enrollment_id": str(enrollment_id), "job_id": str(job_id),
        "csrf_token": _csrf(client),
    })


def _end(client: TestClient, classroom_id: uuid.UUID, assignment_id: uuid.UUID):
    return client.post(f"/classrooms/{classroom_id}/job-assignments/{assignment_id}/end", data={
        "csrf_token": _csrf(client),
    })


def _assignment(engine, classroom_id: uuid.UUID, enrollment_id: uuid.UUID, job_id: uuid.UUID) -> JobAssignment:
    with Session(engine) as db:
        row = db.scalar(select(JobAssignment).where(
            JobAssignment.classroom_id == classroom_id,
            JobAssignment.enrollment_id == enrollment_id,
            JobAssignment.job_id == job_id,
        ).order_by(JobAssignment.created_at.desc(), JobAssignment.id.desc()))
        assert row is not None
        db.expunge(row)
        return row


def test_students_can_hold_multiple_jobs_with_classroom_scoped_history_and_no_money(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    student_a = _student(engine, classroom_id, code="01")
    student_b = _student(engine, classroom_id, code="02")
    job_a = _job(engine, client, classroom_id)
    job_b = _job(engine, client, classroom_id, code="J02")

    assert _assign(client, classroom_id, student_a, job_a).status_code == 303
    assert _assign(client, classroom_id, student_a, job_b).status_code == 303
    assert _assign(client, classroom_id, student_b, job_a).status_code == 303

    page = client.get(f"/classrooms/{classroom_id}/job-assignments")
    assert page.status_code == 200
    assert "J01" in page.text and "J02" in page.text
    assert "01" in page.text and "02" in page.text
    filtered = client.get(f"/classrooms/{classroom_id}/job-assignments?enrollment_id={student_a}")
    assert filtered.status_code == 200
    assert "Student 01" in filtered.text and "Student 02" not in filtered.text
    assert f"enrollment_id={student_a}" in client.get(
        f"/classrooms/{classroom_id}/students/{student_a}"
    ).text
    assert "job-assignments" in client.get(f"/classrooms/{classroom_id}/jobs").text

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(JobAssignment).where(
            JobAssignment.classroom_id == classroom_id
        )) == 3
        events = list(db.scalars(select(AdminAuditEvent).where(
            AdminAuditEvent.classroom_id == classroom_id,
            AdminAuditEvent.event_type == "job_assignment_created",
        )))
        assert len(events) == 3
        assert all(event.actor_user_id == owner and
                   event.target_type == "job_assignment" and
                   event.before_json is None and
                   event.after_json["ended_on"] is None for event in events)
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom_id
        )) == 0
        assert db.scalar(select(func.count()).select_from(FinancialRequest).where(
            FinancialRequest.classroom_id == classroom_id
        )) == 0


def test_duplicate_active_pair_is_rejected_but_reassignment_after_end_keeps_history(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, classroom_id, code="01")
    job_id = _job(engine, client, classroom_id)

    assert _assign(client, classroom_id, student_id, job_id).status_code == 303
    first = _assignment(engine, classroom_id, student_id, job_id)
    assert _assign(client, classroom_id, student_id, job_id).status_code == 409
    assert _end(client, classroom_id, first.id).status_code == 303
    assert _end(client, classroom_id, first.id).status_code == 303
    assert _assign(client, classroom_id, student_id, job_id).status_code == 303

    with Session(engine) as db:
        rows = list(db.scalars(select(JobAssignment).where(
            JobAssignment.classroom_id == classroom_id,
            JobAssignment.enrollment_id == student_id,
            JobAssignment.job_id == job_id,
        )))
        assert len(rows) == 2
        old = next(row for row in rows if row.id == first.id)
        newer = next(row for row in rows if row.id != first.id)
        assert old.ended_on is not None
        assert old.ended_on >= old.started_on
        assert old.ended_by_user_id == owner
        assert newer.ended_on is None
        events = list(db.scalars(select(AdminAuditEvent).where(
            AdminAuditEvent.classroom_id == classroom_id,
            AdminAuditEvent.target_type == "job_assignment",
        )))
        assert len(events) == 3
        end = next(event for event in events if event.event_type == "job_assignment_ended")
        assert end.before_json["ended_on"] is None
        assert end.after_json["ended_on"] == old.ended_on.isoformat()
        assert end.after_json["ended_by_user_id"] == str(owner)


def test_inactive_jobs_and_students_block_new_assignments_but_can_be_ended(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, classroom_id, code="01")
    inactive_student_id = _student(engine, classroom_id, code="02", active=False)
    job_id = _job(engine, client, classroom_id)
    inactive_job_id = _job(engine, client, classroom_id, code="J02")

    assert _assign(client, classroom_id, student_id, job_id).status_code == 303
    assigned = _assignment(engine, classroom_id, student_id, job_id)
    assert client.post(f"/classrooms/{classroom_id}/jobs/{inactive_job_id}/active", data={
        "active": "false", "csrf_token": _csrf(client),
    }).status_code == 303
    assert _assign(client, classroom_id, student_id, inactive_job_id).status_code == 400
    assert _assign(client, classroom_id, inactive_student_id, job_id).status_code == 400

    # Ending remains possible even after a previously assigned student and job are disabled.
    assert client.post(f"/classrooms/{classroom_id}/jobs/{job_id}/active", data={
        "active": "false", "csrf_token": _csrf(client),
    }).status_code == 303
    with Session(engine) as db, db.begin():
        enrollment = db.get(Enrollment, student_id)
        assert enrollment is not None
        enrollment.active = False
    assert _end(client, classroom_id, assigned.id).status_code == 303
    assert client.get(f"/classrooms/{classroom_id}/job-assignments").status_code == 200
    assert _assignment(engine, classroom_id, student_id, job_id).ended_on is not None


def test_cross_tenant_job_enrollment_and_assignment_ids_cannot_be_used(engine) -> None:
    first_client, first_owner = _client(engine)
    second_client, second_owner = _client(engine)
    a = _classroom(engine, first_owner)
    b = _classroom(engine, second_owner)
    a_student = _student(engine, a, code="01")
    b_student = _student(engine, b, code="01")
    a_job = _job(engine, first_client, a)
    b_job = _job(engine, second_client, b)

    assert _assign(first_client, a, a_student, a_job).status_code == 303
    assigned = _assignment(engine, a, a_student, a_job)
    assert second_client.get(f"/classrooms/{a}/job-assignments").status_code == 404
    assert second_client.get(
        f"/classrooms/{a}/job-assignments?enrollment_id={a_student}"
    ).status_code == 404
    assert _assign(second_client, a, a_student, a_job).status_code == 404

    assert _assign(first_client, a, b_student, a_job).status_code == 404
    assert _assign(first_client, a, a_student, b_job).status_code == 404
    assert first_client.get(
        f"/classrooms/{a}/job-assignments?enrollment_id={b_student}"
    ).status_code == 404
    assert _end(second_client, b, assigned.id).status_code == 404
    with Session(engine) as db:
        row = db.get(JobAssignment, assigned.id)
        assert row is not None and row.ended_on is None
        assert db.scalar(select(func.count()).select_from(JobAssignment).where(
            JobAssignment.classroom_id == b
        )) == 0


def test_teacher_can_manage_assistant_is_read_only_and_ended_membership_denied(engine) -> None:
    owner_client, owner = _client(engine)
    teacher_client, teacher = _client(engine)
    assistant_client, assistant = _client(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, classroom_id, code="01")
    job_id = _job(engine, owner_client, classroom_id)
    with Session(engine) as db, db.begin():
        db.add_all([
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom_id,
                user_id=teacher, role="teacher", created_by_user_id=owner,
            ),
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom_id,
                user_id=assistant, role="assistant", created_by_user_id=owner,
            ),
        ])

    assert teacher_client.get(f"/classrooms/{classroom_id}/job-assignments").status_code == 200
    assert _assign(teacher_client, classroom_id, student_id, job_id).status_code == 303
    assigned = _assignment(engine, classroom_id, student_id, job_id)
    assert assistant_client.get(f"/classrooms/{classroom_id}/job-assignments").status_code == 200
    assert "Assign job" not in assistant_client.get(
        f"/classrooms/{classroom_id}/job-assignments"
    ).text
    assert _assign(assistant_client, classroom_id, student_id, job_id).status_code == 403
    assert _end(assistant_client, classroom_id, assigned.id).status_code == 403

    with Session(engine) as db, db.begin():
        member = db.scalar(select(ClassroomMembership).where(
            ClassroomMembership.classroom_id == classroom_id,
            ClassroomMembership.user_id == teacher,
            ClassroomMembership.ended_at.is_(None),
        ))
        assert member is not None
        member.ended_at = datetime.now(timezone.utc)

    assert teacher_client.get(f"/classrooms/{classroom_id}/job-assignments").status_code == 404
    assert _end(teacher_client, classroom_id, assigned.id).status_code == 404
    assert _assignment(engine, classroom_id, student_id, job_id).ended_on is None


def test_csrf_and_anonymous_requests_cannot_mutate_assignments(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, classroom_id, code="01")
    job_id = _job(engine, client, classroom_id)

    assert client.post(f"/classrooms/{classroom_id}/job-assignments/new", data={
        "enrollment_id": str(student_id), "job_id": str(job_id),
        "csrf_token": "wrong",
    }).status_code == 403
    stranger = TestClient(app, follow_redirects=False)
    assert stranger.get(f"/classrooms/{classroom_id}/job-assignments").status_code == 401
    assert stranger.post(f"/classrooms/{classroom_id}/job-assignments/new", data={
        "enrollment_id": str(student_id), "job_id": str(job_id),
        "csrf_token": "missing",
    }).status_code == 401
    assert _assign(client, classroom_id, student_id, job_id).status_code == 303
    assigned = _assignment(engine, classroom_id, student_id, job_id)
    assert client.post(f"/classrooms/{classroom_id}/job-assignments/{assigned.id}/end", data={
        "csrf_token": "wrong",
    }).status_code == 403
    assert stranger.post(f"/classrooms/{classroom_id}/job-assignments/{assigned.id}/end", data={
        "csrf_token": "missing",
    }).status_code == 401
    assert _assignment(engine, classroom_id, student_id, job_id).ended_on is None


def test_local_dates_respect_classroom_timezone_across_dst(engine) -> None:
    _, owner = _client(engine)
    classroom_id = _classroom(engine, owner, zone="America/New_York")
    student_id = _student(engine, classroom_id, code="01")
    with Session(engine) as db, db.begin():
        job = Job(id=uuid.uuid4(), classroom_id=classroom_id,
                  job_code="J01", name="Morning duties", weekly_wage=5, active=True)
        db.add(job)
        job_id = job.id

    with Session(engine) as db, db.begin():
        # 04:30 UTC is 23:30 Saturday in New York, before the DST jump.
        assignment = create_job_assignment(
            db, user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_id, job_id=job_id,
            now=datetime(2026, 3, 8, 4, 30, tzinfo=timezone.utc),
        )
        assignment_id = assignment.id
        assert assignment.started_on == date(2026, 3, 7)

    with Session(engine) as db, db.begin():
        ended = end_job_assignment(
            db, user_id=owner, classroom_id=classroom_id,
            assignment_id=assignment_id,
            now=datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc),
        )
        assert ended.ended_on == date(2026, 3, 8)

    row = _assignment(engine, classroom_id, student_id, job_id)
    assert row.started_on == date(2026, 3, 7)
    assert row.ended_on == date(2026, 3, 8)


def test_ending_before_start_is_rejected_without_changing_history(engine) -> None:
    _, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, classroom_id, code="01")
    with Session(engine) as db, db.begin():
        job = Job(id=uuid.uuid4(), classroom_id=classroom_id,
                  job_code="J01", name="Duties", weekly_wage=0, active=True)
        db.add(job)
        job_id = job.id

    with Session(engine) as db, db.begin():
        assignment = create_job_assignment(
            db, user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_id, job_id=job_id,
            now=datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc),
        )
        assignment_id = assignment.id

    with Session(engine) as db:
        with pytest.raises(JobAssignmentValidationError):
            end_job_assignment(
                db, user_id=owner, classroom_id=classroom_id,
                assignment_id=assignment_id,
                now=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
            )
        db.rollback()

    row = _assignment(engine, classroom_id, student_id, job_id)
    assert row.ended_on is None
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AdminAuditEvent).where(
            AdminAuditEvent.target_id == assignment_id,
        )) == 1
