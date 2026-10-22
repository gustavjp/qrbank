from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.audit import AdminAuditEvent
from app.models.classroom import ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.student import Enrollment, Student
from app.services.auth import (
    create_email_verification_token,
    create_user_session,
    register_user,
    verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.financial import post_opening_balance


PASSWORD = "student-enrollment-password"


def _logged_in_client(engine) -> tuple[TestClient, uuid.UUID]:
    email = f"student-manager-{uuid.uuid4()}@example.test"

    with Session(engine) as db, db.begin():
        user = register_user(db, email=email, password=PASSWORD)
        verification = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=verification.token)
        created = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id = user.id
        session_token = created.token

    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.get("/dashboard")
    return client, user_id


def _csrf(client: TestClient) -> str:
    token = client.cookies.get(settings.csrf_cookie_name)
    assert token
    return token


def _create_classroom(
    engine,
    owner_id: uuid.UUID,
    *,
    starting_balance: int = 0,
    allow_negative_balance: bool = False,
) -> uuid.UUID:
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db,
            owner_user_id=owner_id,
            name=f"Class {uuid.uuid4()}",
            school_year="2026-2027",
        )
        settings_row = db.get(ClassroomSettings, classroom.id)
        assert settings_row is not None
        settings_row.starting_balance = starting_balance
        settings_row.allow_negative_balance = allow_negative_balance
        return classroom.id


def _post_new_student(
    client: TestClient,
    classroom_id: uuid.UUID,
    *,
    code: str,
    name: str = "Alex",
):
    page = client.get(f"/classrooms/{classroom_id}/students/new")
    assert page.status_code == 200
    return client.post(
        f"/classrooms/{classroom_id}/students/new",
        data={
            "student_code": code,
            "display_name": name,
            "notes": "Classroom note",
            "csrf_token": _csrf(client),
        },
    )


def test_zero_starting_balance_creates_no_fake_ledger_transaction(engine) -> None:
    client, owner_id = _logged_in_client(engine)
    classroom_id = _create_classroom(engine, owner_id, starting_balance=0)

    response = _post_new_student(client, classroom_id, code="01")

    assert response.status_code == 303

    with Session(engine) as db:
        enrollment = db.scalar(
            select(Enrollment).where(
                Enrollment.classroom_id == classroom_id,
                Enrollment.student_code == "01",
            )
        )
        assert enrollment is not None
        assert enrollment.current_balance == 0

        tx_count = db.scalar(
            select(func.count())
            .select_from(FinancialTransaction)
            .where(FinancialTransaction.enrollment_id == enrollment.id)
        )
        request_count = db.scalar(
            select(func.count())
            .select_from(FinancialRequest)
            .where(FinancialRequest.classroom_id == classroom_id)
        )

        assert tx_count == 0
        assert request_count == 0


def test_nonzero_starting_balance_posts_one_opening_transaction(engine) -> None:
    client, owner_id = _logged_in_client(engine)
    classroom_id = _create_classroom(engine, owner_id, starting_balance=100)

    response = _post_new_student(client, classroom_id, code="02", name="Jordan")

    assert response.status_code == 303

    with Session(engine) as db:
        enrollment = db.scalar(
            select(Enrollment).where(
                Enrollment.classroom_id == classroom_id,
                Enrollment.student_code == "02",
            )
        )
        assert enrollment is not None
        assert enrollment.current_balance == 100

        transactions = list(
            db.scalars(
                select(FinancialTransaction).where(
                    FinancialTransaction.enrollment_id == enrollment.id
                )
            )
        )
        assert len(transactions) == 1
        tx = transactions[0]
        assert tx.transaction_type == "opening_balance"
        assert tx.source == "system"
        assert tx.amount == 100
        assert tx.balance_before == 0
        assert tx.balance_after == 100
        assert tx.student_code_snapshot == "02"

        request = db.scalar(
            select(FinancialRequest).where(
                FinancialRequest.id == tx.financial_request_id
            )
        )
        assert request is not None
        assert request.idempotency_key == f"opening-balance:{enrollment.id}"
        assert request.status == "completed"

        audit = db.scalar(
            select(AdminAuditEvent).where(
                AdminAuditEvent.classroom_id == classroom_id,
                AdminAuditEvent.event_type == "student_enrollment_created",
                AdminAuditEvent.target_id == enrollment.id,
            )
        )
        assert audit is not None
        assert audit.after_json["starting_balance"] == 100
        assert audit.after_json["opening_transaction_id"] == str(tx.id)

    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "opening_balance" in detail.text
    assert "+100" in detail.text


def test_opening_balance_retry_replays_without_double_credit(engine) -> None:
    client, owner_id = _logged_in_client(engine)
    classroom_id = _create_classroom(engine, owner_id, starting_balance=50)

    response = _post_new_student(client, classroom_id, code="03")
    assert response.status_code == 303

    with Session(engine) as db:
        enrollment = db.scalar(
            select(Enrollment).where(
                Enrollment.classroom_id == classroom_id,
                Enrollment.student_code == "03",
            )
        )
        assert enrollment is not None
        enrollment_id = enrollment.id

    with Session(engine) as db, db.begin():
        replay = post_opening_balance(
            db,
            actor_user_id=owner_id,
            classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            amount=50,
            student_code_snapshot="03",
        )
        assert replay is not None
        assert replay.replayed is True

    with Session(engine) as db:
        enrollment = db.get(Enrollment, enrollment_id)
        assert enrollment is not None
        assert enrollment.current_balance == 50

        tx_count = db.scalar(
            select(func.count())
            .select_from(FinancialTransaction)
            .where(FinancialTransaction.enrollment_id == enrollment_id)
        )
        assert tx_count == 1


def test_concurrent_opening_balance_requests_post_only_once(engine) -> None:
    _, owner_id = _logged_in_client(engine)
    classroom_id = _create_classroom(engine, owner_id, starting_balance=80)

    # Leave a newly created enrollment at zero before racing its opening post.
    with Session(engine) as db, db.begin():
        student = Student(
            id=uuid.uuid4(),
            created_by_user_id=owner_id,
        )
        db.add(student)
        db.flush()
        enrollment = Enrollment(
            id=uuid.uuid4(),
            classroom_id=classroom_id,
            student_id=student.id,
            student_code="RACE",
            current_balance=0,
            active=True,
        )
        db.add(enrollment)
        db.flush()
        enrollment_id = enrollment.id

    barrier = Barrier(2)

    def post_once() -> bool:
        with Session(engine) as db, db.begin():
            barrier.wait(timeout=10)
            result = post_opening_balance(
                db,
                actor_user_id=owner_id,
                classroom_id=classroom_id,
                enrollment_id=enrollment_id,
                amount=80,
                student_code_snapshot="RACE",
            )
            assert result is not None
            return result.replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(post_once) for _ in range(2)]
        replay_flags = [future.result(timeout=20) for future in futures]

    assert sorted(replay_flags) == [False, True]

    with Session(engine) as db:
        enrollment = db.get(Enrollment, enrollment_id)
        assert enrollment is not None
        assert enrollment.current_balance == 80
        transactions = list(
            db.scalars(
                select(FinancialTransaction).where(
                    FinancialTransaction.enrollment_id == enrollment_id
                )
            )
        )
        assert len(transactions) == 1
        assert transactions[0].amount == 80
        request_count = db.scalar(
            select(func.count())
            .select_from(FinancialRequest)
            .where(FinancialRequest.classroom_id == classroom_id)
        )
        assert request_count == 1


def test_duplicate_student_code_rolls_back_without_orphan_student(engine) -> None:
    client, owner_id = _logged_in_client(engine)
    classroom_id = _create_classroom(engine, owner_id)

    first = _post_new_student(client, classroom_id, code="04", name="First")
    assert first.status_code == 303

    with Session(engine) as db:
        student_count_before = db.scalar(select(func.count()).select_from(Student))

    duplicate = _post_new_student(client, classroom_id, code="04", name="Duplicate")
    assert duplicate.status_code == 409
    assert "already in use" in duplicate.text

    with Session(engine) as db:
        student_count_after = db.scalar(select(func.count()).select_from(Student))
        enrollment_count = db.scalar(
            select(func.count())
            .select_from(Enrollment)
            .where(
                Enrollment.classroom_id == classroom_id,
                Enrollment.student_code == "04",
            )
        )

    assert student_count_after == student_count_before
    assert enrollment_count == 1


def test_student_code_edit_is_audited_and_history_snapshot_is_immutable(engine) -> None:
    client, owner_id = _logged_in_client(engine)
    classroom_id = _create_classroom(engine, owner_id, starting_balance=25)

    created = _post_new_student(client, classroom_id, code="A01", name="Taylor")
    assert created.status_code == 303

    with Session(engine) as db:
        enrollment = db.scalar(
            select(Enrollment).where(
                Enrollment.classroom_id == classroom_id,
                Enrollment.student_code == "A01",
            )
        )
        assert enrollment is not None
        enrollment_id = enrollment.id

    detail = client.get(f"/classrooms/{classroom_id}/students/{enrollment_id}")
    assert detail.status_code == 200

    edited = client.post(
        f"/classrooms/{classroom_id}/students/{enrollment_id}/edit",
        data={
            "student_code": "A15",
            "display_name": "Taylor",
            "notes": "Updated note",
            "csrf_token": _csrf(client),
        },
    )
    assert edited.status_code == 303

    with Session(engine) as db:
        enrollment = db.get(Enrollment, enrollment_id)
        assert enrollment is not None
        assert enrollment.student_code == "A15"

        opening = db.scalar(
            select(FinancialTransaction).where(
                FinancialTransaction.enrollment_id == enrollment_id,
                FinancialTransaction.transaction_type == "opening_balance",
            )
        )
        assert opening is not None
        assert opening.student_code_snapshot == "A01"

        audit = db.scalar(
            select(AdminAuditEvent)
            .where(
                AdminAuditEvent.target_id == enrollment_id,
                AdminAuditEvent.event_type == "student_enrollment_updated",
            )
            .order_by(AdminAuditEvent.occurred_at.desc())
        )
        assert audit is not None
        assert audit.before_json["student_code"] == "A01"
        assert audit.after_json["student_code"] == "A15"


def test_deactivate_and_reactivate_do_not_change_balance_or_ledger(engine) -> None:
    client, owner_id = _logged_in_client(engine)
    classroom_id = _create_classroom(engine, owner_id, starting_balance=30)

    created = _post_new_student(client, classroom_id, code="05")
    assert created.status_code == 303

    with Session(engine) as db:
        enrollment = db.scalar(
            select(Enrollment).where(
                Enrollment.classroom_id == classroom_id,
                Enrollment.student_code == "05",
            )
        )
        assert enrollment is not None
        enrollment_id = enrollment.id

    client.get(f"/classrooms/{classroom_id}/students/{enrollment_id}")
    deactivated = client.post(
        f"/classrooms/{classroom_id}/students/{enrollment_id}/deactivate",
        data={"csrf_token": _csrf(client)},
    )
    assert deactivated.status_code == 303

    with Session(engine) as db:
        enrollment = db.get(Enrollment, enrollment_id)
        assert enrollment is not None
        assert enrollment.active is False
        assert enrollment.ended_on is not None
        assert enrollment.current_balance == 30
        tx_count_after_deactivate = db.scalar(
            select(func.count())
            .select_from(FinancialTransaction)
            .where(FinancialTransaction.enrollment_id == enrollment_id)
        )
        assert tx_count_after_deactivate == 1

    client.get(f"/classrooms/{classroom_id}/students/{enrollment_id}")
    reactivated = client.post(
        f"/classrooms/{classroom_id}/students/{enrollment_id}/reactivate",
        data={"csrf_token": _csrf(client)},
    )
    assert reactivated.status_code == 303

    with Session(engine) as db:
        enrollment = db.get(Enrollment, enrollment_id)
        assert enrollment is not None
        assert enrollment.active is True
        assert enrollment.ended_on is None
        assert enrollment.current_balance == 30
        tx_count_after_reactivate = db.scalar(
            select(func.count())
            .select_from(FinancialTransaction)
            .where(FinancialTransaction.enrollment_id == enrollment_id)
        )
        assert tx_count_after_reactivate == 1

        events = list(
            db.scalars(
                select(AdminAuditEvent).where(
                    AdminAuditEvent.target_id == enrollment_id,
                    AdminAuditEvent.event_type.in_(
                        [
                            "student_enrollment_deactivated",
                            "student_enrollment_reactivated",
                        ]
                    ),
                )
            )
        )
        assert len(events) == 2


def test_unrelated_user_cannot_view_student_list_or_detail(engine) -> None:
    owner_client, owner_id = _logged_in_client(engine)
    other_client, _ = _logged_in_client(engine)
    classroom_id = _create_classroom(engine, owner_id)

    created = _post_new_student(owner_client, classroom_id, code="06")
    assert created.status_code == 303

    list_response = other_client.get(f"/classrooms/{classroom_id}/students")
    assert list_response.status_code == 404

    detail_response = other_client.get(created.headers["location"])
    assert detail_response.status_code == 404


def test_teacher_role_can_manage_students_but_assistant_cannot_create(engine) -> None:
    owner_client, owner_id = _logged_in_client(engine)
    teacher_client, teacher_id = _logged_in_client(engine)
    assistant_client, assistant_id = _logged_in_client(engine)
    classroom_id = _create_classroom(engine, owner_id)

    with Session(engine) as db, db.begin():
        db.add_all(
            [
                ClassroomMembership(
                    id=uuid.uuid4(),
                    classroom_id=classroom_id,
                    user_id=teacher_id,
                    role="teacher",
                    created_by_user_id=owner_id,
                ),
                ClassroomMembership(
                    id=uuid.uuid4(),
                    classroom_id=classroom_id,
                    user_id=assistant_id,
                    role="assistant",
                    created_by_user_id=owner_id,
                ),
            ]
        )

    teacher_created = _post_new_student(
        teacher_client,
        classroom_id,
        code="T01",
        name="Teacher Managed",
    )
    assert teacher_created.status_code == 303

    assistant_page = assistant_client.get(
        f"/classrooms/{classroom_id}/students/new"
    )
    assert assistant_page.status_code == 403

    assistant_list = assistant_client.get(
        f"/classrooms/{classroom_id}/students"
    )
    assert assistant_list.status_code == 200
    assert "Teacher Managed" in assistant_list.text

    owner_list = owner_client.get(f"/classrooms/{classroom_id}/students")
    assert owner_list.status_code == 200
    assert "Teacher Managed" in owner_list.text
