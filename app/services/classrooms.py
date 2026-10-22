from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.classroom import Classroom, ClassroomMembership, ClassroomSettings


def create_classroom_for_owner(
    db: Session,
    *,
    owner_user_id: uuid.UUID,
    name: str,
    school_year: str,
) -> Classroom:
    classroom = Classroom(
        id=uuid.uuid4(),
        name=name.strip(),
        school_year=school_year.strip(),
    )
    db.add(classroom)
    db.flush()

    db.add_all(
        [
            ClassroomMembership(
                id=uuid.uuid4(),
                classroom_id=classroom.id,
                user_id=owner_user_id,
                role="owner",
                created_by_user_id=owner_user_id,
            ),
            ClassroomSettings(classroom_id=classroom.id),
        ]
    )
    db.flush()
    return classroom
