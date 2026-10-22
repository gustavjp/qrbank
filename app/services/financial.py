from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.action import Action, QuickAction
from app.models.behavior import BehaviorEvent
from app.models.classroom import ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.student import Enrollment
from app.services.classrooms import ClassroomPermissionError, require_classroom_access
from app.services.scanner import ScanValidationError, parse_scan_command


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
        .execution_options(populate_existing=True)
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


# Authoritative financial service for QR action scans; caller commits atomically.
@dataclass(frozen=True)
class ScanPostingResult:
    transaction: FinancialTransaction | None
    replayed: bool
    rejection_code: str | None
    amount: int
    balance_after: int
    action_name: str


def _scan_posting_result(
    db: Session, request: FinancialRequest, *, replayed: bool,
) -> ScanPostingResult:
    response = request.response_json
    if response is None:
        raise FinancialIntegrityError("scan request has no persisted result")
    transaction_id = response.get("transaction_id")
    transaction = db.get(FinancialTransaction, uuid.UUID(transaction_id)) if transaction_id else None
    if transaction_id and (
        transaction is None or transaction.financial_request_id != request.id
        or transaction.classroom_id != request.classroom_id
    ):
        raise FinancialIntegrityError("scan request ledger result is missing or mismatched")
    return ScanPostingResult(
        transaction=transaction, replayed=replayed,
        rejection_code=request.rejection_code,
        amount=response["amount"], balance_after=response["balance_after"],
        action_name=response["action_name"],
    )


def post_scanned_action(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    payload: str,
    request_id: uuid.UUID,
    expected_action_id: uuid.UUID | None = None,
    expected_quick_action_id: uuid.UUID | None = None,
    expected_amount: int | None = None,
) -> ScanPostingResult:
    """Post one QR action in caller-owned transaction; never trust browser amounts."""
    access = require_classroom_access(db, user_id=actor_user_id, classroom_id=classroom_id)
    if access.membership.role not in {"owner", "teacher"}:
        raise ClassroomPermissionError("owner or teacher permission is required to post points")

    command, code = parse_scan_command(payload)
    if command == "STUDENT":
        raise ScanValidationError("student scans select a student; they cannot post points")

    request, replayed = _claim_request(
        db, classroom_id=classroom_id, actor_user_id=actor_user_id,
        idempotency_key=str(request_id), request_kind="scan_action",
        request_fingerprint=_fingerprint([
            "scan_action", str(classroom_id), str(actor_user_id),
            str(enrollment_id), command, code,
        ]),
    )
    if replayed:
        if request.request_kind != "scan_action" or request.actor_user_id != actor_user_id:
            raise FinancialIdempotencyConflictError("idempotency key belongs to another request")
        return _scan_posting_result(db, request, replayed=True)

    # Serialize all writers for this enrollment, independent of request IDs.
    enrollment = db.scalar(select(Enrollment).where(
        Enrollment.id == enrollment_id, Enrollment.classroom_id == classroom_id,
        Enrollment.active.is_(True),
    ).with_for_update().execution_options(populate_existing=True))
    if enrollment is None:
        raise ScanValidationError("selected student is not active in this classroom")
    settings = db.get(ClassroomSettings, classroom_id)
    if settings is None:
        raise FinancialIntegrityError("classroom settings not found")

    quick: QuickAction | None = None
    if command == "QUICK":
        quick = db.scalar(select(QuickAction).where(
            QuickAction.classroom_id == classroom_id,
            QuickAction.quick_action_code == code, QuickAction.active.is_(True),
        ).with_for_update().execution_options(populate_existing=True))
        if quick is None:
            raise ScanValidationError("Quick Action is unknown or inactive in this classroom")
        action = db.scalar(select(Action).where(
            Action.id == quick.action_id, Action.classroom_id == classroom_id,
            Action.active.is_(True),
        ).with_for_update().execution_options(populate_existing=True))
    else:
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_id, Action.action_code == code,
            Action.active.is_(True),
        ).with_for_update().execution_options(populate_existing=True))
    if action is None:
        raise ScanValidationError("action is unknown or inactive in this classroom")

    amount = action.amount
    if type(amount) is not int or not -(2**31 - 1) <= amount <= 2**31 - 1:
        raise FinancialIntegrityError("action amount is outside the supported integer range")

    def reject(reason: str) -> ScanPostingResult:
        request.status = "rejected"
        request.rejection_code = reason
        request.response_json = {
            "transaction_id": None, "amount": amount,
            "balance_after": enrollment.current_balance, "action_name": action.name,
        }
        db.flush()
        return _scan_posting_result(db, request, replayed=False)

    # Confirmation is bound to the exact mapping and amount previously shown.
    if (
        (expected_action_id is not None and expected_action_id != action.id)
        or (expected_quick_action_id is not None and
            (quick is None or expected_quick_action_id != quick.id))
        or (expected_amount is not None and expected_amount != amount)
    ):
        return reject("configuration_changed")

    if settings.duplicate_scan_seconds > 0 and amount != 0:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.duplicate_scan_seconds)
        recent = db.scalar(select(FinancialTransaction.id).where(
            FinancialTransaction.classroom_id == classroom_id,
            FinancialTransaction.enrollment_id == enrollment_id,
            FinancialTransaction.action_id == action.id,
            FinancialTransaction.quick_action_id == (quick.id if quick else None),
            FinancialTransaction.actor_user_id == actor_user_id,
            FinancialTransaction.source == "scanner",
            FinancialTransaction.occurred_at >= cutoff,
        ).limit(1))
        if recent is not None:
            return reject("duplicate_scan")

    before = enrollment.current_balance
    after = before + amount
    if after < 0 and not settings.allow_negative_balance:
        return reject("insufficient_balance")
    if not -(2**31) <= after <= 2**31 - 1:
        return reject("balance_out_of_range")

    transaction: FinancialTransaction | None = None
    if amount != 0:
        transaction = FinancialTransaction(
            id=uuid.uuid4(), classroom_id=classroom_id,
            enrollment_id=enrollment_id, financial_request_id=request.id,
            transaction_type="action", source="scanner",
            amount=amount, balance_before=before, balance_after=after,
            actor_user_id=actor_user_id, action_id=action.id,
            quick_action_id=quick.id if quick else None,
            student_code_snapshot=enrollment.student_code,
            action_code_snapshot=action.action_code,
            action_name_snapshot=action.name,
            action_kind_snapshot=action.action_kind,
            quick_action_code_snapshot=quick.quick_action_code if quick else None,
        )
        db.add(transaction)
        enrollment.current_balance = after
        db.flush()

    behavior_event_id: uuid.UUID | None = None
    if action.action_kind in {"behavior_positive", "behavior_negative"}:
        behavior_event_id = uuid.uuid4()
        db.add(BehaviorEvent(
            id=behavior_event_id, classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            behavior_type="positive" if action.action_kind == "behavior_positive" else "negative",
            financial_transaction_id=transaction.id if transaction else None,
            action_id=action.id, action_code_snapshot=action.action_code,
            action_name_snapshot=action.name, created_by_user_id=actor_user_id,
        ))
        db.flush()

    request.response_json = {
        "transaction_id": str(transaction.id) if transaction else None,
        "behavior_event_id": str(behavior_event_id) if behavior_event_id else None,
        "amount": amount, "balance_after": after, "action_name": action.name,
    }
    db.flush()
    return _scan_posting_result(db, request, replayed=False)


class ManualTransactionValidationError(ValueError):
    pass


class ManualEnrollmentNotFoundError(LookupError):
    pass


MANUAL_CATEGORIES = frozenset({"general", "behavior_positive", "behavior_negative"})


@dataclass(frozen=True)
class ManualPostingResult:
    transaction: FinancialTransaction | None
    replayed: bool
    rejection_code: str | None
    balance_after: int


def _manual_posting_result(
    db: Session, request: FinancialRequest, *, replayed: bool,
) -> ManualPostingResult:
    response = request.response_json
    if response is None:
        raise FinancialIntegrityError("manual request has no persisted result")
    transaction_id = response.get("transaction_id")
    transaction = (
        _existing_transaction_for_request(db, request_id=request.id)
        if transaction_id else None
    )
    if transaction is not None and (
        str(transaction.id) != transaction_id
        or transaction.classroom_id != request.classroom_id
        or transaction.transaction_type != "manual"
    ):
        raise FinancialIntegrityError("manual request ledger result is mismatched")
    if (request.status == "completed") != (transaction is not None):
        raise FinancialIntegrityError("manual financial request/ledger state is inconsistent")
    return ManualPostingResult(
        transaction=transaction, replayed=replayed,
        rejection_code=request.rejection_code,
        balance_after=response["balance_after"],
    )


def post_manual_transaction(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    direction: str,
    points: int,
    category: str,
    note: str,
    request_id: uuid.UUID,
) -> ManualPostingResult:
    """Post an auditable manual entry in the caller's transaction.

    Amount/sign, category and note are validated server-side. The enrollment
    row lock serializes this writer with QR and opening-balance writers.
    """
    if direction not in {"credit", "debit"}:
        raise ManualTransactionValidationError("choose add or subtract")
    if type(points) is not int or not 1 <= points <= 2**31 - 1:
        raise ManualTransactionValidationError("points must be a positive whole number within the supported range")
    if category not in MANUAL_CATEGORIES:
        raise ManualTransactionValidationError("choose a valid category")
    if category == "behavior_positive" and direction != "credit":
        raise ManualTransactionValidationError("positive behavior must add points")
    if category == "behavior_negative" and direction != "debit":
        raise ManualTransactionValidationError("negative behavior must subtract points")
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
        raise ManualTransactionValidationError("enter a reason of 1–500 characters")
    note = note.strip()

    access = require_classroom_access(
        db, user_id=actor_user_id, classroom_id=classroom_id,
    )
    if access.membership.role not in {"owner", "teacher"}:
        raise ClassroomPermissionError("owner or teacher permission is required to post points")

    amount = points if direction == "credit" else -points
    request, replayed = _claim_request(
        db, classroom_id=classroom_id, actor_user_id=actor_user_id,
        idempotency_key=str(request_id), request_kind="manual",
        request_fingerprint=_fingerprint([
            "manual", str(classroom_id), str(actor_user_id),
            str(enrollment_id), str(amount), category, note,
        ]),
    )
    if replayed:
        if request.request_kind != "manual" or request.actor_user_id != actor_user_id:
            raise FinancialIdempotencyConflictError("request ID belongs to another operation")
        return _manual_posting_result(db, request, replayed=True)

    enrollment = db.scalar(
        select(Enrollment).where(
            Enrollment.id == enrollment_id,
            Enrollment.classroom_id == classroom_id,
            Enrollment.active.is_(True),
        ).with_for_update().execution_options(populate_existing=True)
    )
    if enrollment is None:
        raise ManualEnrollmentNotFoundError("active student enrollment not found")

    settings = db.get(ClassroomSettings, classroom_id)
    if settings is None:
        raise FinancialIntegrityError("classroom settings not found")

    before = enrollment.current_balance
    after = before + amount

    def reject(code: str) -> ManualPostingResult:
        request.status = "rejected"
        request.rejection_code = code
        request.response_json = {"transaction_id": None, "balance_after": before}
        db.flush()
        return _manual_posting_result(db, request, replayed=False)

    if after < 0 and not settings.allow_negative_balance:
        return reject("insufficient_balance")
    if not -(2**31) <= after <= 2**31 - 1:
        return reject("balance_out_of_range")

    transaction = FinancialTransaction(
        id=uuid.uuid4(),
        classroom_id=classroom_id,
        enrollment_id=enrollment_id,
        financial_request_id=request.id,
        transaction_type="manual",
        source="manual",
        amount=amount,
        balance_before=before,
        balance_after=after,
        actor_user_id=actor_user_id,
        note=note,
        manual_category=category,
        student_code_snapshot=enrollment.student_code,
    )
    db.add(transaction)
    enrollment.current_balance = after
    db.flush()

    if category in {"behavior_positive", "behavior_negative"}:
        db.add(BehaviorEvent(
            id=uuid.uuid4(), classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            behavior_type="positive" if category == "behavior_positive" else "negative",
            financial_transaction_id=transaction.id,
            created_by_user_id=actor_user_id,
        ))
        db.flush()

    request.response_json = {
        "transaction_id": str(transaction.id),
        "balance_after": after,
    }
    db.flush()
    return _manual_posting_result(db, request, replayed=False)


class FinancialCorrectionValidationError(ValueError):
    pass


class FinancialCorrectionNotFoundError(LookupError):
    pass


# Rewards, wages, allowances and batches require coordinated domain-specific undo.
REVERSIBLE_TRANSACTION_TYPES = frozenset({
    "opening_balance", "action", "manual", "adjustment", "reversal",
})


@dataclass(frozen=True)
class CorrectionPostingResult:
    transaction: FinancialTransaction | None
    replayed: bool
    rejection_code: str | None
    balance_after: int


def _correction_note(note: str) -> str:
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
        raise FinancialCorrectionValidationError("enter a reason of 1–500 characters")
    return note.strip()


def _correction_result(
    db: Session, request: FinancialRequest, *, kind: str, replayed: bool,
) -> CorrectionPostingResult:
    response = request.response_json
    if response is None:
        raise FinancialIntegrityError("correction request has no persisted result")
    transaction_id = response.get("transaction_id")
    transaction = (
        _existing_transaction_for_request(db, request_id=request.id)
        if transaction_id else None
    )
    if transaction is not None and (
        str(transaction.id) != transaction_id
        or transaction.classroom_id != request.classroom_id
        or transaction.transaction_type != kind
    ):
        raise FinancialIntegrityError("correction ledger result is mismatched")
    if (request.status == "completed") != (transaction is not None):
        raise FinancialIntegrityError("correction request/ledger state is inconsistent")
    return CorrectionPostingResult(
        transaction=transaction, replayed=replayed,
        rejection_code=request.rejection_code,
        balance_after=response["balance_after"],
    )


def _require_correction_permission(
    db: Session, *, actor_user_id: uuid.UUID, classroom_id: uuid.UUID,
) -> None:
    access = require_classroom_access(
        db, user_id=actor_user_id, classroom_id=classroom_id,
    )
    if access.membership.role not in {"owner", "teacher"}:
        raise ClassroomPermissionError("owner or teacher permission is required")


def post_balance_adjustment(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    target_balance: int,
    note: str,
    request_id: uuid.UUID,
) -> CorrectionPostingResult:
    """Set an active enrollment to a target by posting the locked difference."""
    if type(target_balance) is not int or not -(2**31) <= target_balance <= 2**31 - 1:
        raise FinancialCorrectionValidationError("target must be a supported whole-number balance")
    note = _correction_note(note)
    _require_correction_permission(
        db, actor_user_id=actor_user_id, classroom_id=classroom_id,
    )
    request, replayed = _claim_request(
        db, classroom_id=classroom_id, actor_user_id=actor_user_id,
        idempotency_key=str(request_id), request_kind="balance_adjustment",
        request_fingerprint=_fingerprint([
            "balance_adjustment", str(classroom_id), str(actor_user_id),
            str(enrollment_id), str(target_balance), note,
        ]),
    )
    if replayed:
        if request.request_kind != "balance_adjustment" or request.actor_user_id != actor_user_id:
            raise FinancialIdempotencyConflictError("request ID belongs to another operation")
        return _correction_result(db, request, kind="adjustment", replayed=True)

    enrollment = db.scalar(
        select(Enrollment).where(
            Enrollment.id == enrollment_id,
            Enrollment.classroom_id == classroom_id,
            Enrollment.active.is_(True),
        ).with_for_update().execution_options(populate_existing=True)
    )
    if enrollment is None:
        raise FinancialCorrectionNotFoundError("active enrollment not found")
    settings = db.get(ClassroomSettings, classroom_id)
    if settings is None:
        raise FinancialIntegrityError("classroom settings not found")

    before = enrollment.current_balance
    amount = target_balance - before

    def reject(code: str) -> CorrectionPostingResult:
        request.status = "rejected"
        request.rejection_code = code
        request.response_json = {"transaction_id": None, "balance_after": before}
        db.flush()
        return _correction_result(db, request, kind="adjustment", replayed=False)

    if amount == 0:
        return reject("already_at_target")
    if target_balance < 0 and not settings.allow_negative_balance:
        return reject("negative_balance_disabled")
    if not -(2**31 - 1) <= amount <= 2**31 - 1:
        return reject("amount_out_of_range")

    transaction = FinancialTransaction(
        id=uuid.uuid4(), classroom_id=classroom_id,
        enrollment_id=enrollment_id, financial_request_id=request.id,
        transaction_type="adjustment", source="manual",
        amount=amount, balance_before=before, balance_after=target_balance,
        actor_user_id=actor_user_id, note=note,
        adjustment_target_balance=target_balance,
        student_code_snapshot=enrollment.student_code,
    )
    db.add(transaction)
    enrollment.current_balance = target_balance
    db.flush()
    request.response_json = {
        "transaction_id": str(transaction.id), "balance_after": target_balance,
    }
    db.flush()
    return _correction_result(db, request, kind="adjustment", replayed=False)


def post_transaction_reversal(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    target_transaction_id: uuid.UUID,
    note: str,
    request_id: uuid.UUID,
) -> CorrectionPostingResult:
    """Append an exact opposite ledger entry; never edit the target transaction."""
    note = _correction_note(note)
    _require_correction_permission(
        db, actor_user_id=actor_user_id, classroom_id=classroom_id,
    )
    request, replayed = _claim_request(
        db, classroom_id=classroom_id, actor_user_id=actor_user_id,
        idempotency_key=str(request_id), request_kind="reversal",
        request_fingerprint=_fingerprint([
            "reversal", str(classroom_id), str(actor_user_id),
            str(enrollment_id), str(target_transaction_id), note,
        ]),
    )
    if replayed:
        if request.request_kind != "reversal" or request.actor_user_id != actor_user_id:
            raise FinancialIdempotencyConflictError("request ID belongs to another operation")
        return _correction_result(db, request, kind="reversal", replayed=True)

    # Also permit authorized historical corrections for inactive enrollments.
    enrollment = db.scalar(
        select(Enrollment).where(
            Enrollment.id == enrollment_id,
            Enrollment.classroom_id == classroom_id,
        ).with_for_update().execution_options(populate_existing=True)
    )
    if enrollment is None:
        raise FinancialCorrectionNotFoundError("enrollment not found")

    target = db.scalar(select(FinancialTransaction).where(
        FinancialTransaction.id == target_transaction_id,
        FinancialTransaction.classroom_id == classroom_id,
        FinancialTransaction.enrollment_id == enrollment_id,
    ))
    if target is None:
        raise FinancialCorrectionNotFoundError("transaction not found for this enrollment")
    settings = db.get(ClassroomSettings, classroom_id)
    if settings is None:
        raise FinancialIntegrityError("classroom settings not found")

    before = enrollment.current_balance
    amount = -target.amount
    after = before + amount

    def reject(code: str) -> CorrectionPostingResult:
        request.status = "rejected"
        request.rejection_code = code
        request.response_json = {"transaction_id": None, "balance_after": before}
        db.flush()
        return _correction_result(db, request, kind="reversal", replayed=False)

    if target.transaction_type not in REVERSIBLE_TRANSACTION_TYPES:
        return reject("unsupported_transaction_type")
    reversed_already = db.scalar(select(FinancialTransaction.id).where(
        FinancialTransaction.classroom_id == classroom_id,
        FinancialTransaction.enrollment_id == enrollment_id,
        FinancialTransaction.reverses_transaction_id == target.id,
    ).limit(1))
    if reversed_already is not None:
        return reject("already_reversed")
    if not -(2**31 - 1) <= amount <= 2**31 - 1:
        return reject("amount_out_of_range")
    if after < 0 and not settings.allow_negative_balance:
        return reject("insufficient_balance")
    if not -(2**31) <= after <= 2**31 - 1:
        return reject("balance_out_of_range")

    transaction = FinancialTransaction(
        id=uuid.uuid4(), classroom_id=classroom_id,
        enrollment_id=enrollment_id, financial_request_id=request.id,
        transaction_type="reversal", source="manual",
        amount=amount, balance_before=before, balance_after=after,
        actor_user_id=actor_user_id, note=note,
        reverses_transaction_id=target.id,
        student_code_snapshot=enrollment.student_code,
    )
    db.add(transaction)
    enrollment.current_balance = after
    db.flush()
    request.response_json = {
        "transaction_id": str(transaction.id), "balance_after": after,
    }
    db.flush()
    return _correction_result(db, request, kind="reversal", replayed=False)
