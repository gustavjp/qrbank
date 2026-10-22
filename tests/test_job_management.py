from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.audit import AdminAuditEvent
from app.models.classroom import Classroom, ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.job import Job
from app.services.auth import (
    create_email_verification_token,
    create_user_session,
    register_user,
    verify_email_token,
)


PASSWORD = "job-management-test-password"


def _client(engine) -> tuple[TestClient, uuid.UUID]:
    with Session(engine) as db, db.begin():
        user = register_user(db, email=f"job-{uuid.uuid4()}@example.test", password=PASSWORD)
        token = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=token.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, session_token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.get("/dashboard")
    return client, user_id


def _classroom(engine, owner_id: uuid.UUID) -> uuid.UUID:
    with Session(engine) as db, db.begin():
        classroom = Classroom(
            id=uuid.uuid4(), name=f"Jobs classroom {uuid.uuid4()}",
            school_year="2026-2027",
        )
        db.add(classroom)
        db.flush()
        db.add_all([
            ClassroomSettings(classroom_id=classroom.id),
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom.id,
                user_id=owner_id, role="owner", created_by_user_id=owner_id,
            ),
        ])
        return classroom.id


def _csrf(client: TestClient) -> str:
    token = client.cookies.get(settings.csrf_cookie_name)
    assert token
    return token


def _create(
    client: TestClient, classroom_id: uuid.UUID, *,
    code: str = "J01", name: str = "Class Librarian",
    wage: int = 10, notes: str = "Organizes books",
):
    client.get(f"/classrooms/{classroom_id}/jobs")
    return client.post(f"/classrooms/{classroom_id}/jobs/new", data={
        "code": code, "name": name, "weekly_wage": str(wage),
        "notes": notes, "csrf_token": _csrf(client),
    })


def _job(engine, classroom_id: uuid.UUID, code: str = "J01") -> Job:
    with Session(engine) as db:
        job = db.scalar(select(Job).where(
            Job.classroom_id == classroom_id, Job.job_code == code,
        ))
        assert job is not None
        db.expunge(job)
        return job


def test_create_jobs_in_separate_classrooms_and_record_audit_without_money(engine) -> None:
    client, owner = _client(engine)
    a = _classroom(engine, owner)
    b = _classroom(engine, owner)
    assert _create(client, a, code=" j01 ", wage=0).status_code == 303
    assert _create(client, b, code="j01", wage=25).status_code == 303

    job_a, job_b = _job(engine, a), _job(engine, b)
    assert job_a.job_code == job_b.job_code == "J01"
    assert job_a.id != job_b.id
    assert job_a.weekly_wage == 0 and job_a.active is True
    assert job_b.weekly_wage == 25

    page = client.get(f"/classrooms/{a}/jobs")
    assert page.status_code == 200
    assert "J01" in page.text and "Class Librarian" in page.text
    assert "Create Job" in page.text
    assert f"/classrooms/{a}/jobs" in client.get(f"/classrooms/{a}").text

    with Session(engine) as db:
        event = db.scalar(select(AdminAuditEvent).where(
            AdminAuditEvent.classroom_id == a,
            AdminAuditEvent.event_type == "job_created",
        ))
        assert event is not None
        assert event.actor_user_id == owner
        assert event.target_type == "job" and event.target_id == job_a.id
        assert event.before_json is None
        assert event.after_json["weekly_wage"] == 0
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id.in_([a, b])
        )) == 0
        assert db.scalar(select(func.count()).select_from(FinancialRequest).where(
            FinancialRequest.classroom_id.in_([a, b])
        )) == 0


def test_edit_and_toggle_are_audited_without_changing_financial_data(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    assert _create(client, classroom_id).status_code == 303
    job_id = _job(engine, classroom_id).id

    response = client.get(f"/classrooms/{classroom_id}/jobs/{job_id}/edit")
    assert response.status_code == 200
    assert "Class Librarian" in response.text
    edit_data = {
        "code": "j15", "name": "Library Manager",
        "weekly_wage": "75", "notes": "New duties", "csrf_token": _csrf(client),
    }
    assert client.post(f"/classrooms/{classroom_id}/jobs/{job_id}/edit",
                       data=edit_data).status_code == 303
    assert client.post(f"/classrooms/{classroom_id}/jobs/{job_id}/active", data={
        "active": "false", "csrf_token": _csrf(client),
    }).status_code == 303
    assert client.post(f"/classrooms/{classroom_id}/jobs/{job_id}/active", data={
        "active": "true", "csrf_token": _csrf(client),
    }).status_code == 303

    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.job_code == "J15" and job.name == "Library Manager"
        assert job.weekly_wage == 75 and job.active is True
        events = list(db.scalars(select(AdminAuditEvent).where(
            AdminAuditEvent.classroom_id == classroom_id,
            AdminAuditEvent.target_id == job_id,
        ).order_by(AdminAuditEvent.occurred_at, AdminAuditEvent.id)))
        assert len(events) == 4
        updates = [event for event in events if event.event_type == "job_updated"]
        assert len(updates) == 1
        assert updates[0].before_json["weekly_wage"] == 10
        assert updates[0].after_json["weekly_wage"] == 75
        assert {event.event_type for event in events} == {
            "job_created", "job_updated", "job_deactivated", "job_reactivated",
        }
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom_id
        )) == 0

    # Unchanged edits and redundant status requests must not create fake audit entries.
    assert client.post(f"/classrooms/{classroom_id}/jobs/{job_id}/edit",
                       data=edit_data).status_code == 303
    assert client.post(f"/classrooms/{classroom_id}/jobs/{job_id}/active", data={
        "active": "true", "csrf_token": _csrf(client),
    }).status_code == 303
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AdminAuditEvent).where(
            AdminAuditEvent.target_id == job_id
        )) == 4


def test_duplicate_codes_bad_values_and_csrf_fail_without_partial_writes(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    assert _create(client, classroom_id).status_code == 303
    assert _create(client, classroom_id, code="j01").status_code == 409
    assert _create(client, classroom_id, code="bad code").status_code == 400
    assert _create(client, classroom_id, code="J02", name="  ").status_code == 400
    assert _create(client, classroom_id, code="J02", wage=-1).status_code == 400
    assert _create(client, classroom_id, code="J02", wage=2**31).status_code == 400
    assert _create(client, classroom_id, code="J02", notes="x" * 1001).status_code == 400
    assert _create(client, classroom_id, code="J02", name="x" * 121).status_code == 400
    assert client.post(f"/classrooms/{classroom_id}/jobs/new", data={
        "code": "J02", "name": "Bad CSRF", "weekly_wage": "10",
        "csrf_token": "invalid",
    }).status_code == 403
    assert client.post(f"/classrooms/{classroom_id}/jobs/new", data={
        "code": "J02", "name": "Not integer", "weekly_wage": "1.5",
        "csrf_token": _csrf(client),
    }).status_code == 422

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(
            Job.classroom_id == classroom_id,
        )) == 1
        assert db.scalar(select(func.count()).select_from(AdminAuditEvent).where(
            AdminAuditEvent.classroom_id == classroom_id,
            AdminAuditEvent.event_type == "job_created",
        )) == 1


def test_update_collision_and_invalid_edit_rollback_existing_values(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    assert _create(client, classroom_id).status_code == 303
    assert _create(client, classroom_id, code="J02").status_code == 303
    first_id = _job(engine, classroom_id).id
    url = f"/classrooms/{classroom_id}/jobs/{first_id}/edit"
    assert client.post(url, data={
        "code": "J02", "name": "Collision", "weekly_wage": "100",
        "csrf_token": _csrf(client),
    }).status_code == 409
    assert client.post(url, data={
        "code": "J03", "name": "Invalid", "weekly_wage": "-2",
        "csrf_token": _csrf(client),
    }).status_code == 400
    with Session(engine) as db:
        job = db.get(Job, first_id)
        assert job is not None
        assert job.job_code == "J01" and job.name == "Class Librarian"
        assert job.weekly_wage == 10
        assert db.scalar(select(func.count()).select_from(AdminAuditEvent).where(
            AdminAuditEvent.target_id == first_id,
        )) == 1


def test_cross_classroom_id_guessing_cannot_read_or_modify_jobs(engine) -> None:
    first_client, first_owner = _client(engine)
    second_client, second_owner = _client(engine)
    first_classroom = _classroom(engine, first_owner)
    second_classroom = _classroom(engine, second_owner)
    assert _create(first_client, first_classroom).status_code == 303
    assert _create(second_client, second_classroom, code="J02").status_code == 303
    first_job_id = _job(engine, first_classroom).id
    second_job_id = _job(engine, second_classroom, "J02").id

    assert second_client.get(f"/classrooms/{first_classroom}/jobs").status_code == 404
    assert second_client.get(f"/classrooms/{first_classroom}/jobs/{first_job_id}/edit").status_code == 404
    assert _create(second_client, first_classroom, code="J03").status_code == 404

    # Even an owner of one classroom cannot target a job from another classroom.
    assert first_client.get(f"/classrooms/{first_classroom}/jobs/{second_job_id}/edit").status_code == 404
    assert first_client.post(f"/classrooms/{first_classroom}/jobs/{second_job_id}/edit", data={
        "code": "HACK", "name": "Foreign", "weekly_wage": "999",
        "csrf_token": _csrf(first_client),
    }).status_code == 404
    assert first_client.post(f"/classrooms/{first_classroom}/jobs/{second_job_id}/active", data={
        "active": "false", "csrf_token": _csrf(first_client),
    }).status_code == 404
    assert _job(engine, second_classroom, "J02").weekly_wage == 10


def test_teacher_can_manage_assistant_read_only_and_ended_membership_denied(engine) -> None:
    owner_client, owner = _client(engine)
    teacher_client, teacher = _client(engine)
    assistant_client, assistant = _client(engine)
    classroom_id = _classroom(engine, owner)
    with Session(engine) as db, db.begin():
        db.add_all([
            ClassroomMembership(id=uuid.uuid4(), classroom_id=classroom_id,
                                user_id=teacher, role="teacher", created_by_user_id=owner),
            ClassroomMembership(id=uuid.uuid4(), classroom_id=classroom_id,
                                user_id=assistant, role="assistant", created_by_user_id=owner),
        ])

    assert _create(teacher_client, classroom_id, code="T01").status_code == 303
    job_id = _job(engine, classroom_id, "T01").id
    assert assistant_client.get(f"/classrooms/{classroom_id}/jobs").status_code == 200
    assert "T01" in assistant_client.get(f"/classrooms/{classroom_id}/jobs").text
    assert "Create Job" not in assistant_client.get(f"/classrooms/{classroom_id}/jobs").text
    assert assistant_client.get(f"/classrooms/{classroom_id}/jobs/{job_id}/edit").status_code == 403
    assert _create(assistant_client, classroom_id, code="A01").status_code == 403
    assert assistant_client.post(f"/classrooms/{classroom_id}/jobs/{job_id}/active", data={
        "active": "false", "csrf_token": _csrf(assistant_client),
    }).status_code == 403

    with Session(engine) as db, db.begin():
        member = db.scalar(select(ClassroomMembership).where(
            ClassroomMembership.classroom_id == classroom_id,
            ClassroomMembership.user_id == teacher,
            ClassroomMembership.ended_at.is_(None),
        ))
        assert member is not None
        member.ended_at = datetime.now(timezone.utc)

    assert teacher_client.get(f"/classrooms/{classroom_id}/jobs").status_code == 404
    assert teacher_client.post(f"/classrooms/{classroom_id}/jobs/{job_id}/active", data={
        "active": "false", "csrf_token": _csrf(teacher_client),
    }).status_code == 404
    assert _job(engine, classroom_id, "T01").active is True


def test_anonymous_job_routes_require_login(engine) -> None:
    owner_client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    assert _create(owner_client, classroom_id).status_code == 303
    job_id = _job(engine, classroom_id).id

    stranger = TestClient(app, follow_redirects=False)
    assert stranger.get(f"/classrooms/{classroom_id}/jobs").status_code == 401
    assert stranger.get(f"/classrooms/{classroom_id}/jobs/{job_id}/edit").status_code == 401
    assert stranger.post(f"/classrooms/{classroom_id}/jobs/new", data={
        "code": "J99", "name": "No session", "weekly_wage": "0",
        "csrf_token": "missing",
    }).status_code == 401
