from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Assignment, DingTalkSyncState, Student, SubmissionEvidence
from backend.schemas import AssignmentOut, StudentAssignmentsOut


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_submission_status(assignment: Assignment) -> str:
    if assignment.submission_mode == "none":
        return "none"
    if not assignment.submissions:
        return "pending"
    # Highest source rank wins (parent_confirmed=30 > photo=20 > child=10); tie-break by latest created_at
    best = max(assignment.submissions, key=lambda e: (e.source_rank, e.created_at))
    return best.status


def to_assignment_out(a: Assignment) -> AssignmentOut:
    return AssignmentOut(
        id=a.id,
        subject=a.subject,
        content=a.content,
        page_ref=a.page_ref,
        item_range=a.item_range,
        submission_mode=a.submission_mode,
        submission_status=resolve_submission_status(a),
        due_at=a.due_at,
        published_at=a.published_at,
        version=a.version,
        completed=a.completed,
        needs_parent_confirm=a.needs_parent_confirm,
        unclear_reason=a.unclear_reason,
        attachment_warning=a.attachment_warning,
    )


def get_student_assignments(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    subject: Optional[str] = None,
    now: Optional[datetime] = None,
) -> StudentAssignmentsOut:
    now = now or utcnow()
    student = db.get(Student, student_id)
    if student is None or student.family_id != family_id:
        raise PermissionError("student_outside_family")

    stmt = (
        select(Assignment)
        .where(
            Assignment.family_id == family_id,
            Assignment.student_id == student_id,
            Assignment.expires_at > now,
        )
        .order_by(Assignment.published_at.asc())
    )
    if subject:
        stmt = stmt.where(Assignment.subject == subject)
    rows = list(db.scalars(stmt))

    sync_states = list(
        db.scalars(
            select(DingTalkSyncState).where(
                DingTalkSyncState.family_id == family_id,
                DingTalkSyncState.student_id == student_id,
            )
        )
    )
    sync_status = "ok"
    last_sync_at: Optional[datetime] = None
    stale_warning: Optional[str] = None
    if sync_states:
        last_sync_at = max((s.last_success_at for s in sync_states if s.last_success_at), default=None)
        bad = [s for s in sync_states if s.status != "ok"]
        if bad:
            sync_status = bad[0].status
            ts = last_sync_at.strftime("%H:%M") if last_sync_at else "之前"
            stale_warning = f"清单更新到 {ts}，今天可能还没同步齐"

    return StudentAssignmentsOut(
        student_id=student.id,
        student_name=student.name,
        sync_status=sync_status,
        last_sync_at=last_sync_at,
        stale_warning=stale_warning,
        assignments=[to_assignment_out(a) for a in rows],
    )


def route_voice_assignment_query(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    utterance: str,
    now: Optional[datetime] = None,
) -> tuple[str, str, str, list[AssignmentOut], Optional[str]]:
    """Structured voice routing for '今天有什么作业', '数学做哪一页', '接下来做什么'."""
    text = utterance.strip()
    target_subject: Optional[str] = None
    for subj in ("数学", "语文", "英语"):
        if subj in text:
            target_subject = subj
            break

    if "接下来" in text or "先做" in text:
        intent = "query_next"
        data = get_student_assignments(db, family_id=family_id, student_id=student_id, now=now)
        pending = [a for a in data.assignments if not a.completed]
        if not pending:
            spoken = "今天的作业都已经标记写完啦，看看有没有待交的事项。"
        else:
            subjects = "、".join(a.subject for a in pending)
            spoken = f"现在还有{subjects}没写完，你想先做哪一科？"
        if data.stale_warning:
            spoken = f"{data.stale_warning}。{spoken}"
        return intent, spoken, "接下来做什么", pending, data.stale_warning

    if target_subject:
        intent = "query_subject"
        data = get_student_assignments(
            db, family_id=family_id, student_id=student_id, subject=target_subject, now=now
        )
        if not data.assignments:
            spoken = f"今天还没有同步到{target_subject}作业。"
        else:
            parts = []
            for a in data.assignments:
                detail = " ".join(x for x in (a.page_ref, a.item_range) if x) or a.content
                if a.needs_parent_confirm:
                    detail += "（有待确认项）"
                parts.append(detail)
            spoken = f"{target_subject}是：{'；'.join(parts)}。"
        if data.stale_warning:
            spoken = f"{data.stale_warning}。{spoken}"
        return intent, spoken, f"{target_subject}作业", data.assignments, data.stale_warning

    intent = "query_today"
    data = get_student_assignments(db, family_id=family_id, student_id=student_id, now=now)
    if not data.assignments:
        spoken = "今天还没有同步到作业。"
    else:
        items = []
        for a in data.assignments:
            detail = " ".join(x for x in (a.page_ref, a.item_range) if x) or a.content
            items.append(f"{a.subject}{detail}")
        spoken = f"今天有{len(data.assignments)}项作业：{'；'.join(items)}。你想先做哪一科？"
    if data.stale_warning:
        spoken = f"{data.stale_warning}。{spoken}"
    return intent, spoken, "今天的作业", data.assignments, data.stale_warning
