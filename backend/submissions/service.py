from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import Assignment, Device, Student, SubmissionEvidence, SubmissionPromptLog
from backend.reminders.service import dispatch_parent_dingtalk_alert
from backend.services.assignments import resolve_submission_status

SUBMISSION_SOURCE_RANK = {
    "parent_confirmed": 30,
    "photo": 20,
    "child_said_done": 10,
    "needs_parent": 10,
    "not_yet": 10,
}

MAX_DAILY_SUBMISSION_PROMPTS = 2
MAX_PHOTO_BYTES = 2 * 1024 * 1024  # 2 MB JPEG


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PhotoUploadSlot:
    object_key: str
    presigned_url: str
    max_bytes: int
    allowed_mime: str
    expires_at: datetime
    retention_hours: int


def can_prompt_submission_today(
    db: Session,
    *,
    student_id: str,
    assignment_id: str,
    study_day: str,
) -> bool:
    asg = db.get(Assignment, assignment_id)
    if asg is None or asg.submission_mode == "none":
        return False
    current_status = resolve_submission_status(asg)
    if current_status in ("parent_confirmed", "photo", "child_said_done", "needs_parent"):
        return False

    count = db.scalar(
        select(func.count(SubmissionPromptLog.id)).where(
            SubmissionPromptLog.student_id == student_id,
            SubmissionPromptLog.assignment_id == assignment_id,
            SubmissionPromptLog.study_day == study_day,
        )
    )
    return (count or 0) < MAX_DAILY_SUBMISSION_PROMPTS


def log_submission_prompt(
    db: Session,
    *,
    student_id: str,
    assignment_id: str,
    study_day: str,
    now: Optional[datetime] = None,
) -> None:
    db.add(
        SubmissionPromptLog(
            student_id=student_id,
            assignment_id=assignment_id,
            study_day=study_day,
            prompted_at=now or utcnow(),
        )
    )
    db.commit()


def issue_photo_upload_slot(
    db: Session,
    *,
    device: Device,
    student_id: str,
    purpose: Literal["submission", "question"],
    now: Optional[datetime] = None,
) -> PhotoUploadSlot:
    now = now or utcnow()
    if not device.has_camera:
        raise PermissionError("camera_hardware_unavailable")
    student = db.get(Student, student_id)
    if student is None or student.family_id != device.family_id:
        raise PermissionError("student_outside_family")
    if not student.camera_consent:
        raise PermissionError("camera_consent_required")

    retention_hours = 24 * 7 if purpose == "submission" else 24
    token = secrets.token_urlsafe(16)
    object_key = f"photos/{device.family_id}/{student.id}/{purpose}/{token}.jpg"
    return PhotoUploadSlot(
        object_key=object_key,
        presigned_url=f"https://storage.local/upload/{object_key}?sig={secrets.token_hex(12)}",
        max_bytes=MAX_PHOTO_BYTES,
        allowed_mime="image/jpeg",
        expires_at=now + timedelta(minutes=5),
        retention_hours=retention_hours,
    )


def record_submission_evidence(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    assignment_id: str,
    status: Literal["child_said_done", "photo", "needs_parent", "not_yet", "parent_confirmed"],
    photo_object_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> tuple[SubmissionEvidence, str]:
    """
    Record submission evidence with explicit provenance rank (parent > photo > child).
    Never grants island materials (by product definition P10/P11).
    """
    now = now or utcnow()
    asg = db.get(Assignment, assignment_id)
    if asg is None or asg.family_id != family_id or asg.student_id != student_id:
        raise PermissionError("assignment_outside_student_scope")
    if status == "photo" and not photo_object_key:
        raise ValueError("photo_object_key_required")

    ev = SubmissionEvidence(
        assignment_id=asg.id,
        assignment_version=asg.version,
        student_id=student_id,
        status=status,
        source_rank=SUBMISSION_SOURCE_RANK[status],
        photo_object_key=photo_object_key,
        photo_expires_at=(now + timedelta(days=7)) if status == "photo" else None,
        created_at=now,
    )
    db.add(ev)
    db.commit()
    db.refresh(asg)
    db.refresh(ev)

    if status == "needs_parent":
        student = db.get(Student, student_id)
        sname = student.name if student else "孩子"
        dispatch_parent_dingtalk_alert(
            db,
            family_id=family_id,
            event_key=f"needs_parent:{asg.id}:v{asg.version}",
            kind="needs_parent_submit",
            summary=f"{sname}的{asg.subject}需要你帮忙提交，请在家长网页「今天」查看。",
            now=now,
        )

    return ev, resolve_submission_status(asg)
