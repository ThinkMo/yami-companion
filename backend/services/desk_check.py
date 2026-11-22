from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Device, Student


def queue_desk_check(db: Session, hardware_uid: str = "mosaico-yami-s31") -> int:
    """Stateless PostgreSQL-backed desk-check trigger sequence per device."""
    dev = db.scalar(select(Device).where(Device.hardware_uid == hardware_uid))
    if dev is None:
        dev = Device(
            id=f"dev_{uuid.uuid4().hex[:10]}",
            family_id="fam_a",
            hardware_uid=hardware_uid,
            desk_check_seq=1,
        )
        db.add(dev)
        db.commit()
        return 1
    dev.desk_check_seq = int(dev.desk_check_seq or 0) + 1
    db.commit()
    return dev.desk_check_seq


def get_desk_check_seq(db: Session, hardware_uid: str = "mosaico-yami-s31") -> int:
    dev = db.scalar(select(Device).where(Device.hardware_uid == hardware_uid))
    return int(dev.desk_check_seq or 0) if dev is not None else 0


def record_desk_check_result(
    db: Session,
    student_id: str,
    *,
    at_desk: bool,
    summary: str,
) -> dict[str, Any]:
    """Privacy-first desk-check record: zero image retention, stores ONLY 1-sentence text summary + timestamp."""
    now = datetime.now(timezone.utc)
    stu = db.get(Student, student_id)
    if stu is not None:
        stu.last_desk_check_at_desk = bool(at_desk)
        stu.last_desk_check_summary = summary
        stu.last_desk_check_at = now
        db.commit()
    return {
        "student_id": student_id,
        "at_desk": bool(at_desk),
        "summary": summary,
        "checked_at": now.isoformat(),
    }


def get_latest_desk_check(db: Session, student_id: str) -> Optional[dict[str, Any]]:
    stu = db.get(Student, student_id)
    if stu is None or not stu.last_desk_check_summary:
        return None
    return {
        "student_id": student_id,
        "at_desk": bool(stu.last_desk_check_at_desk),
        "summary": stu.last_desk_check_summary,
        "checked_at": stu.last_desk_check_at.isoformat() if stu.last_desk_check_at else "",
    }


def bump_pet_jump_seq(db: Session, student_id: str) -> int:
    """Stateless DB-backed sequence triggering Mosaico to auto-jump to Pet House page."""
    stu = db.get(Student, student_id)
    if stu is None:
        return 0
    stu.pet_jump_seq = int(stu.pet_jump_seq or 0) + 1
    db.commit()
    return stu.pet_jump_seq


def get_pet_jump_seq(db: Session, student_id: str) -> int:
    stu = db.get(Student, student_id)
    return int(stu.pet_jump_seq or 0) if stu is not None else 0
