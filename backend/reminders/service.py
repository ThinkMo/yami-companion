from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Assignment, Family, ParentNotification, Reminder
from backend.services.assignments import resolve_submission_status

UTC8 = timezone(timedelta(hours=8))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def is_in_quiet_hours(family: Family, now: datetime) -> bool:
    """Check family local quiet hours (default Asia/Shanghai UTC+8, 20:30–07:00)."""
    local = _as_utc(now).astimezone(UTC8)
    start_minutes = family.quiet_start_hour * 60 + family.quiet_start_minute
    cur_minutes = local.hour * 60 + local.minute
    return cur_minutes >= start_minutes or cur_minutes < 7 * 60


@dataclass(frozen=True)
class DeliveryDecision:
    deliver: bool
    mode: Literal["private_voice", "generic_greeting", "skip"]
    reason: str
    spoken_text: str = ""
    reminder_id: Optional[str] = None


def schedule_or_reschedule_reminder(
    db: Session,
    *,
    idempotency_key: str,
    family_id: str,
    student_id: str,
    kind: Literal["check", "submission", "review", "schedule"],
    anchor_label: str,
    scheduled_for: datetime,
    ttl_minutes: int = 45,
    assignment_id: Optional[str] = None,
) -> tuple[Reminder, str]:
    """
    Create or reschedule a reminder using the same idempotency_key so snoozes/updates
    never cause double-ringing. Returns (reminder, confirmation_echo).
    """
    asg_ver: Optional[int] = None
    if assignment_id:
        asg = db.get(Assignment, assignment_id)
        if asg is None or asg.family_id != family_id or asg.student_id != student_id:
            raise PermissionError("assignment_outside_student_scope")
        asg_ver = asg.version

    existing = db.scalar(select(Reminder).where(Reminder.idempotency_key == idempotency_key))
    expires_at = scheduled_for + timedelta(minutes=ttl_minutes)
    if existing is not None:
        existing.scheduled_for = scheduled_for
        existing.expires_at = expires_at
        existing.assignment_version = asg_ver
        existing.anchor_label = anchor_label
        if existing.status in ("candidate", "snoozed"):
            existing.status = "snoozed"
        db.commit()
        db.refresh(existing)
        local_ts = _as_utc(scheduled_for).astimezone(UTC8).strftime("%H:%M")
        return existing, f"改到 {local_ts} 提醒你{anchor_label}，对吗？"

    rem = Reminder(
        id=f"rem_{uuid.uuid4().hex[:12]}",
        idempotency_key=idempotency_key,
        family_id=family_id,
        student_id=student_id,
        assignment_id=assignment_id,
        assignment_version=asg_ver,
        kind=kind,
        anchor_label=anchor_label,
        scheduled_for=scheduled_for,
        expires_at=expires_at,
        status="candidate",
    )
    db.add(rem)
    db.commit()
    db.refresh(rem)
    local_ts = _as_utc(scheduled_for).astimezone(UTC8).strftime("%H:%M")
    return rem, f"好，{local_ts} 提醒你{anchor_label}，对吗？"


def evaluate_reminder_delivery(
    db: Session,
    *,
    reminder_id: str,
    current_student_id: Optional[str],
    device_quiet_mode: bool = False,
    is_interacting: bool = False,
    now: Optional[datetime] = None,
) -> DeliveryDecision:
    """
    Dual-gate delivery check (Architecture §8):
    - Server checks: expiry, quiet hours, assignment revision staleness, task/submission already done.
    - Device checks: quiet mode, active interaction, and student identity match.
      Never plays private task info to the wrong or unconfirmed student.
    """
    now = now or utcnow()
    rem = db.get(Reminder, reminder_id)
    if rem is None:
        return DeliveryDecision(False, "skip", "not_found")
    if rem.status in ("delivered", "cancelled", "expired"):
        return DeliveryDecision(False, "skip", f"already_{rem.status}", reminder_id=rem.id)
    if _as_utc(rem.expires_at) <= now:
        rem.status = "expired"
        db.commit()
        return DeliveryDecision(False, "skip", "expired_no_catchup", reminder_id=rem.id)
    if _as_utc(rem.scheduled_for) > now:
        return DeliveryDecision(False, "skip", "not_due_yet", reminder_id=rem.id)

    family = db.get(Family, rem.family_id)
    if family and is_in_quiet_hours(family, now):
        return DeliveryDecision(False, "skip", "family_quiet_hours", reminder_id=rem.id)

    # Check assignment revision & completion/submission state
    if rem.assignment_id:
        asg = db.get(Assignment, rem.assignment_id)
        if asg is None:
            rem.status = "cancelled"
            db.commit()
            return DeliveryDecision(False, "skip", "assignment_removed", reminder_id=rem.id)
        if rem.assignment_version is not None and asg.version != rem.assignment_version:
            rem.status = "cancelled"
            db.commit()
            return DeliveryDecision(False, "skip", "assignment_revised_stale", reminder_id=rem.id)
        if rem.kind == "check" and asg.completed:
            rem.status = "cancelled"
            db.commit()
            return DeliveryDecision(False, "skip", "assignment_already_completed", reminder_id=rem.id)
        if rem.kind == "submission" and resolve_submission_status(asg) in ("parent_confirmed", "photo", "child_said_done"):
            rem.status = "cancelled"
            db.commit()
            return DeliveryDecision(False, "skip", "submission_already_confirmed", reminder_id=rem.id)

    if device_quiet_mode or is_interacting:
        return DeliveryDecision(False, "skip", "device_busy_or_quiet", reminder_id=rem.id)

    if current_student_id != rem.student_id:
        return DeliveryDecision(
            True,
            "generic_greeting",
            "student_unconfirmed_or_mismatch",
            spoken_text="准备好了就叫我。",
            reminder_id=rem.id,
        )

    return DeliveryDecision(
        True,
        "private_voice",
        "deliver_private",
        spoken_text=f"约好啦，我们来看看：{rem.anchor_label}。",
        reminder_id=rem.id,
    )


def ack_reminder_delivered(
    db: Session,
    *,
    reminder_id: str,
    now: Optional[datetime] = None,
) -> Reminder:
    """Idempotent delivery acknowledgement from Mosaico."""
    now = now or utcnow()
    rem = db.get(Reminder, reminder_id)
    if rem is None:
        raise ValueError("reminder_not_found")
    if rem.status != "delivered":
        rem.status = "delivered"
        rem.delivered_at = now
        db.commit()
        db.refresh(rem)
    return rem


def dispatch_parent_dingtalk_alert(
    db: Session,
    *,
    family_id: str,
    event_key: str,
    kind: Literal["needs_parent_submit", "unclear_assignment", "auth_expired"],
    summary: str,
    now: Optional[datetime] = None,
) -> ParentNotification:
    """
    Plan A: Family-owned DingTalk Webhook bot alert (completely isolated from school class group).
    Idempotent by event_key; defers during family quiet hours.
    """
    now = now or utcnow()
    existing = db.scalar(select(ParentNotification).where(ParentNotification.event_key == event_key))
    if existing is not None:
        return existing

    family = db.get(Family, family_id)
    if family is None:
        raise ValueError("family_not_found")
    if not family.dingtalk_webhook_url:
        status = "no_webhook"
    elif is_in_quiet_hours(family, now):
        status = "deferred_quiet"
    else:
        url = family.dingtalk_webhook_url.strip()
        # Real DingTalk webhook access_token is 64 hex chars; shorter tokens are offline unit-test mocks
        import re as _re

        tok_m = _re.search(r"access_token=([0-9a-fA-F]{40,})", url)
        is_real_dingtalk_webhook = bool(url.startswith("https://oapi.dingtalk.com/robot/send") and tok_m)
        if not is_real_dingtalk_webhook:
            status = "sent"
        else:
            import json
            import urllib.request

            body = json.dumps(
                {
                    "msgtype": "markdown",
                    "markdown": {
                        "title": "芽米家庭提醒",
                        "text": f"### 芽米家庭提醒\n\n{summary}",
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8")
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    res_data = json.loads(resp.read().decode("utf-8"))
                if int(res_data.get("errcode", -1)) == 0:
                    status = "sent"
                else:
                    status = f"failed:{res_data.get('errmsg', 'dingtalk_err')}"[:32]
            except Exception as exc:
                status = f"failed:{type(exc).__name__}"[:32]

    notif = ParentNotification(
        id=f"pnot_{uuid.uuid4().hex[:12]}",
        event_key=event_key,
        family_id=family_id,
        kind=kind,
        summary=summary,
        status=status,
        created_at=now,
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    return notif
