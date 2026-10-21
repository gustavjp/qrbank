from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError


def u() -> str:
    return str(uuid.uuid4())


def create_user(conn, *, email: str | None = None) -> str:
    user_id = u()
    email = email or f"{user_id}@example.test"
    conn.execute(
        text(
            """
            INSERT INTO users (id, email, email_normalized, password_hash, account_status, teacher_verification_status)
            VALUES (:id, :email, :email_normalized, 'not-a-real-password-hash', 'active', 'not_requested')
            """
        ),
        {"id": user_id, "email": email, "email_normalized": email.lower()},
    )
    return user_id


def create_classroom(conn, *, name: str = "Period 1", school_year: str = "2026-2027") -> str:
    classroom_id = u()
    conn.execute(
        text("INSERT INTO classrooms (id, name, school_year) VALUES (:id, :name, :school_year)"),
        {"id": classroom_id, "name": name, "school_year": school_year},
    )
    conn.execute(text("INSERT INTO classroom_settings (classroom_id) VALUES (:id)"), {"id": classroom_id})
    return classroom_id


def create_student_and_enrollment(conn, classroom_id: str, student_code: str) -> tuple[str, str]:
    student_id = u()
    enrollment_id = u()
    conn.execute(text("INSERT INTO students (id) VALUES (:id)"), {"id": student_id})
    conn.execute(
        text(
            """
            INSERT INTO enrollments (id, classroom_id, student_id, student_code)
            VALUES (:id, :classroom_id, :student_id, :student_code)
            """
        ),
        {"id": enrollment_id, "classroom_id": classroom_id, "student_id": student_id, "student_code": student_code},
    )
    return student_id, enrollment_id


def create_financial_request(conn, classroom_id: str, *, key: str | None = None) -> str:
    request_id = u()
    conn.execute(
        text(
            """
            INSERT INTO financial_requests
              (id, classroom_id, idempotency_key, request_kind, request_fingerprint, status, completed_at)
            VALUES
              (:id, :classroom_id, :key, 'manual', decode('00','hex'), 'completed', CURRENT_TIMESTAMP)
            """
        ),
        {"id": request_id, "classroom_id": classroom_id, "key": key or u()},
    )
    return request_id


def create_transaction(
    conn,
    *,
    classroom_id: str,
    enrollment_id: str,
    student_code: str,
    amount: int,
    before: int,
    transaction_type: str = "manual",
    reverses_transaction_id: str | None = None,
) -> str:
    transaction_id = u()
    request_id = create_financial_request(conn, classroom_id)
    conn.execute(
        text(
            """
            INSERT INTO financial_transactions
              (id, classroom_id, enrollment_id, financial_request_id, transaction_type, source,
               amount, balance_before, balance_after, student_code_snapshot, reverses_transaction_id)
            VALUES
              (:id, :classroom_id, :enrollment_id, :request_id, :transaction_type, 'manual',
               :amount, :before, :after, :student_code, :reverses_transaction_id)
            """
        ),
        {
            "id": transaction_id,
            "classroom_id": classroom_id,
            "enrollment_id": enrollment_id,
            "request_id": request_id,
            "transaction_type": transaction_type,
            "amount": amount,
            "before": before,
            "after": before + amount,
            "student_code": student_code,
            "reverses_transaction_id": reverses_transaction_id,
        },
    )
    return transaction_id


def test_schema_is_at_head(engine) -> None:
    tables = set(inspect(engine).get_table_names())
    expected = {
        "users",
        "classrooms",
        "classroom_memberships",
        "classroom_settings",
        "students",
        "enrollments",
        "actions",
        "quick_actions",
        "behavior_reasons",
        "behavior_events",
        "jobs",
        "job_assignments",
        "financial_requests",
        "financial_transactions",
        "rewards",
        "reward_purchases",
        "reward_fulfillment_events",
        "reward_inventory_events",
        "automation_runs",
        "scheduled_payments",
        "admin_audit_events",
    }
    assert expected <= tables

    with engine.connect() as conn:
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == "0010_financial_hardening"


def test_classroom_starting_balance_default_is_zero(engine) -> None:
    with engine.begin() as conn:
        classroom_id = create_classroom(conn)
        value = conn.execute(
            text("SELECT starting_balance FROM classroom_settings WHERE classroom_id = :id"),
            {"id": classroom_id},
        ).scalar_one()
    assert value == 0


def test_owner_role_is_single_membership_and_active_owner_is_unique(engine) -> None:
    with engine.begin() as conn:
        user_a = create_user(conn)
        user_b = create_user(conn)
        classroom_id = create_classroom(conn)
        conn.execute(
            text("INSERT INTO classroom_memberships (id, classroom_id, user_id, role) VALUES (:id,:c,:u,'owner')"),
            {"id": u(), "c": classroom_id, "u": user_a},
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO classroom_memberships (id, classroom_id, user_id, role) VALUES (:id,:c,:u,'owner')"),
                {"id": u(), "c": classroom_id, "u": user_b},
            )


def test_student_code_is_unique_per_classroom_but_reusable_between_classrooms(engine) -> None:
    with engine.begin() as conn:
        c1 = create_classroom(conn, name="C1")
        c2 = create_classroom(conn, name="C2")
        create_student_and_enrollment(conn, c1, "01")
        create_student_and_enrollment(conn, c2, "01")

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            create_student_and_enrollment(conn, c1, "01")


def test_qr_facing_codes_reject_malformed_values(engine) -> None:
    with engine.begin() as conn:
        classroom_id = create_classroom(conn)

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            student_id = u()
            conn.execute(text("INSERT INTO students (id) VALUES (:id)"), {"id": student_id})
            conn.execute(
                text("INSERT INTO enrollments (id,classroom_id,student_id,student_code) VALUES (:id,:c,:s,'bad code!')"),
                {"id": u(), "c": classroom_id, "s": student_id},
            )


def test_cross_classroom_quick_action_mapping_is_rejected(engine) -> None:
    with engine.begin() as conn:
        c1 = create_classroom(conn, name="C1")
        c2 = create_classroom(conn, name="C2")
        action_id = u()
        conn.execute(
            text("INSERT INTO actions (id,classroom_id,action_code,name,action_kind,amount) VALUES (:id,:c,'A01','Add 5','general',5)"),
            {"id": action_id, "c": c1},
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO quick_actions (id,classroom_id,quick_action_code,action_id) VALUES (:id,:c,'QA1',:a)"),
                {"id": u(), "c": c2, "a": action_id},
            )


def test_reward_inventory_constraints(engine) -> None:
    with engine.begin() as conn:
        classroom_id = create_classroom(conn)
        conn.execute(
            text(
                """
                INSERT INTO rewards
                  (id,classroom_id,reward_code,name,reward_type,price_points,inventory_tracking,quantity_available)
                VALUES
                  (:id,:c,'R01','Toy','physical',25,true,1)
                """
            ),
            {"id": u(), "c": classroom_id},
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO rewards
                      (id,classroom_id,reward_code,name,reward_type,price_points,inventory_tracking,quantity_available)
                    VALUES
                      (:id,:c,'R02','Bad Stock','physical',25,true,-1)
                    """
                ),
                {"id": u(), "c": classroom_id},
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO rewards
                      (id,classroom_id,reward_code,name,reward_type,price_points,inventory_tracking,quantity_available)
                    VALUES
                      (:id,:c,'R03','Unlimited','privilege',20,false,5)
                    """
                ),
                {"id": u(), "c": classroom_id},
            )


def test_financial_balance_arithmetic_is_enforced(engine) -> None:
    with engine.begin() as conn:
        classroom_id = create_classroom(conn)
        _, enrollment_id = create_student_and_enrollment(conn, classroom_id, "01")
        request_id = create_financial_request(conn, classroom_id)

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO financial_transactions
                      (id,classroom_id,enrollment_id,financial_request_id,transaction_type,source,
                       amount,balance_before,balance_after,student_code_snapshot)
                    VALUES
                      (:id,:c,:e,:r,'manual','manual',5,0,99,'01')
                    """
                ),
                {"id": u(), "c": classroom_id, "e": enrollment_id, "r": request_id},
            )


def test_reversal_must_be_exact_negative_and_reversal_can_be_reversed(engine) -> None:
    with engine.begin() as conn:
        classroom_id = create_classroom(conn)
        _, enrollment_id = create_student_and_enrollment(conn, classroom_id, "01")
        original = create_transaction(
            conn,
            classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            student_code="01",
            amount=10,
            before=0,
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            create_transaction(
                conn,
                classroom_id=classroom_id,
                enrollment_id=enrollment_id,
                student_code="01",
                amount=-5,
                before=10,
                transaction_type="reversal",
                reverses_transaction_id=original,
            )

    with engine.begin() as conn:
        reversal = create_transaction(
            conn,
            classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            student_code="01",
            amount=-10,
            before=10,
            transaction_type="reversal",
            reverses_transaction_id=original,
        )
        second_reversal = create_transaction(
            conn,
            classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            student_code="01",
            amount=10,
            before=0,
            transaction_type="reversal",
            reverses_transaction_id=reversal,
        )
        assert second_reversal


def test_same_transaction_cannot_be_directly_reversed_twice(engine) -> None:
    with engine.begin() as conn:
        classroom_id = create_classroom(conn)
        _, enrollment_id = create_student_and_enrollment(conn, classroom_id, "01")
        original = create_transaction(
            conn,
            classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            student_code="01",
            amount=5,
            before=0,
        )
        create_transaction(
            conn,
            classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            student_code="01",
            amount=-5,
            before=5,
            transaction_type="reversal",
            reverses_transaction_id=original,
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            create_transaction(
                conn,
                classroom_id=classroom_id,
                enrollment_id=enrollment_id,
                student_code="01",
                amount=-5,
                before=5,
                transaction_type="reversal",
                reverses_transaction_id=original,
            )


def test_scheduled_payment_period_uniqueness(engine) -> None:
    with engine.begin() as conn:
        classroom_id = create_classroom(conn)
        _, enrollment_id = create_student_and_enrollment(conn, classroom_id, "01")
        conn.execute(
            text(
                """
                INSERT INTO scheduled_payments
                  (id,classroom_id,payment_type,enrollment_id,period_key,amount_snapshot)
                VALUES
                  (:id,:c,'allowance',:e,'2026-W43',20)
                """
            ),
            {"id": u(), "c": classroom_id, "e": enrollment_id},
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO scheduled_payments
                      (id,classroom_id,payment_type,enrollment_id,period_key,amount_snapshot)
                    VALUES
                      (:id,:c,'allowance',:e,'2026-W43',20)
                    """
                ),
                {"id": u(), "c": classroom_id, "e": enrollment_id},
            )


def test_alembic_revision_ids_fit_version_table() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    for revision in script.walk_revisions():
        assert len(revision.revision) <= 32, revision.revision


def test_named_migration_identifiers_fit_postgresql_limit() -> None:
    prefixes = ("fk_", "uq_", "ux_", "ix_", "ck_", "pk_")
    for path in Path("migrations/versions").glob("*.py"):
        for identifier in re.findall(r'"([A-Za-z0-9_]+)"', path.read_text()):
            if identifier.startswith(prefixes):
                assert len(identifier) <= 63, f"{path}: {identifier}"
