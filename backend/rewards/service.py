from __future__ import annotations

from typing import Optional
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import MethodCardUnlock


def unlock_method_card(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    card_id: str,
    front: str,
    back: str,
    growth_record_id: Optional[str] = None,
) -> tuple[bool, MethodCardUnlock]:
    existing = db.scalar(
        select(MethodCardUnlock).where(
            MethodCardUnlock.student_id == student_id,
            MethodCardUnlock.card_id == card_id,
        )
    )
    if existing is not None:
        return False, existing
    card = MethodCardUnlock(
        id=f"card_{uuid.uuid4().hex[:12]}",
        family_id=family_id,
        student_id=student_id,
        card_id=card_id,
        front=front,
        back=back,
        growth_record_id=growth_record_id,
        active=True,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return True, card


def list_method_cards(db: Session, *, family_id: str, student_id: str) -> list[dict[str, str]]:
    return [
        {"card_id": c.card_id, "front": c.front, "back": c.back}
        for c in db.scalars(
            select(MethodCardUnlock).where(
                MethodCardUnlock.family_id == family_id,
                MethodCardUnlock.student_id == student_id,
                MethodCardUnlock.active.is_(True),
            )
        )
    ]
