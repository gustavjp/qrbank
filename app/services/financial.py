from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.classroom import ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.student import Enrollment
from app.services.classrooms import ClassroomPermissionError, require_classroom_access


class FinancialIdempotencyConflictError(ValueError):
    pass


class FinancialIntegrityError(RuntimeError):
    pass


class NegativeBalanceNotAllowedError(ValueError):
    pass


@dataclass(frozen=True)
class FinancialPostingResult:
    transaction: FinancialTransaction
    replayed: bool


def _fingerprint(parts: list[str]) -> bytes:
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).digest()


def _claim_request(
    db: Session,
    *,
    classroom_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    idempotency_key: str,
    request_kind: str,
    request_fingerprint: bytes,
) -> tuple[FinancialRequest, bool]:
    request_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    inserted_id = db.scalar(
        pg_insert(FinancialRequest)
        .values(
            id=request_id,
            classroom_id=classroom_id,
            idempotency_key=idempotency_key,
            request_kind=request_kind,
            request_fingerprint=request_fingerprint,
            status="completed",
            actor_user_id=actor_user_id,
            completed_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["classroom_id", "idempotency_key"],
        )
        .returning(FinancialRequest.id)
    )

    if inserted_id is not None:
        request = db.get(FinancialRequest, inserted_id)
        if request is None:
            raise FinancialIntegrityError("financial request claim disappeared")
        return request, False

    request = db.scalar(
        select(FinancialRequest).where(
            FinancialRequest.classroom_id == classroom_id,
            FinancialRequest.idempotency_key == idempotency_key,
        )
    )
    if request is None:
        raise FinancialIntegrityError("financial request conflict could not be resolved")
    if request.request_fingerprint != request_fingerprint:
        raise FinancialIdempotencyConflictError(
            "idempotency key was reused with different financial inputs"
        )
    return request, True


def _existing_transaction_for_request(
    db: Session,
    *,
    request_id: uuid.UUID,
) -> FinancialTransaction:
    transaction = db.scalar(
        select(FinancialTransaction).where(
            FinancialTransaction.financial_request_id == request_id
        )
    )
    if transaction is None:
        raise FinancialIntegrityError(
            "completed financial request has no ledger transaction"
        )
    return transaction


def post_opening_balance(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    amount: int,
    student_code_snapshot: str,
) -> FinancialPostingResult | None:
    if amount == 0:
        return None

    access = require_classroom_access(
        db,
        user_id=actor_user_id,
        classroom_id=classroom_id,
    )
    if access.membership.role not in {"owner", "teacher"}:
        raise ClassroomPermissionError(
            "owner or teacher permission is required for opening balances"
        )

    idempotency_key = f"opening-balance:{enrollment_id}"
    request_fingerprint = _fingerprint(
        [
            "opening_balance",
            str(classroom_id),
            str(enrollment_id),
            str(amount),
            student_code_snapshot,
        ]
    )

    request, replayed = _claim_request(
        db,
        classroom_id=classroom_id,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        request_kind="opening_balance",
        request_fingerprint=request_fingerprint,
    )

    if replayed:
        return FinancialPostingResult(
            transaction=_existing_transaction_for_request(
                db,
                request_id=request.id,
            ),
            replayed=True,
        )

    enrollment = db.scalar(
        select(Enrollment)
        .where(
            Enrollment.id == enrollment_id,
            Enrollment.classroom_id == classroom_id,
        )
        .with_for_update()
    )
    if enrollment is None:
        raise FinancialIntegrityError("enrollment not found for opening balance")

    settings = db.get(ClassroomSettings, classroom_id)
    if settings is None:
        raise FinancialIntegrityError("classroom settings not found")

    balance_before = enrollment.current_balance
    balance_after = balance_before + amount

    if balance_after < 0 and not settings.allow_negative_balance:
        raise NegativeBalanceNotAllowedError(
            "opening balance would create a negative balance"
        )

    transaction = FinancialTransaction(
        id=uuid.uuid4(),
        classroom_id=classroom_id,
        enrollment_id=enrollment_id,
        financial_request_id=request.id,
        transaction_type="opening_balance",
        source="system",
        amount=amount,
        balance_before=balance_before,
        balance_after=balance_after,
        actor_user_id=actor_user_id,
        note="Opening balance",
        student_code_snapshot=student_code_snapshot,
    )
    db.add(transaction)
    enrollment.current_balance = balance_after
    db.flush()

    request.response_json = {
        "transaction_id": str(transaction.id),
        "display_number": transaction.display_number,
        "balance_before": balance_before,
        "balance_after": balance_after,
    }

    return FinancialPostingResult(
        transaction=transaction,
        replayed=False,
    )
