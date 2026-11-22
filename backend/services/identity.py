from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets
from typing import Optional
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Device, DeviceSession, Family, PairingCode, Student

VOICEPRINT_MIN_CONFIDENCE = 0.82
VOICEPRINT_MIN_MARGIN = 0.12


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def issue_pairing_code(
    db: Session,
    *,
    hardware_uid: str,
    has_camera: bool = False,
    firmware_version: str = "0.1.0",
    now: Optional[datetime] = None,
) -> tuple[Device, PairingCode]:
    now = now or utcnow()
    device = db.scalar(select(Device).where(Device.hardware_uid == hardware_uid))
    if device is None:
        device = Device(
            id=f"dev_{uuid.uuid4().hex[:12]}",
            hardware_uid=hardware_uid,
            has_camera=has_camera,
            firmware_version=firmware_version,
        )
        db.add(device)
        db.flush()
    else:
        device.has_camera = has_camera
        device.firmware_version = firmware_version

    code_str = f"{secrets.randbelow(1_000_000):06d}"
    while db.get(PairingCode, code_str) is not None:
        code_str = f"{secrets.randbelow(1_000_000):06d}"

    pairing = PairingCode(
        code=code_str,
        device_id=device.id,
        expires_at=now + timedelta(seconds=settings.pairing_code_ttl_seconds),
    )
    db.add(pairing)
    db.commit()
    db.refresh(device)
    db.refresh(pairing)
    return device, pairing


def claim_pairing_code(
    db: Session,
    *,
    family_id: str,
    code: str,
    now: Optional[datetime] = None,
) -> Device:
    now = now or utcnow()
    family = db.get(Family, family_id)
    if family is None:
        raise ValueError("family_not_found")

    pairing = db.get(PairingCode, code)
    if pairing is None or pairing.consumed_at is not None:
        raise ValueError("invalid_pairing_code")
    if _as_utc(pairing.expires_at) <= now:
        raise ValueError("pairing_code_expired")

    device = db.get(Device, pairing.device_id)
    if device is None:
        raise ValueError("device_not_found")

    pairing.consumed_at = now
    device.family_id = family.id
    device.device_token = f"dtok_{secrets.token_urlsafe(24)}"
    device.auth_version += 1
    device.profile_epoch += 1
    device.active_student_id = None
    device.revoked = False
    db.commit()
    db.refresh(device)
    return device


def authenticate_device(db: Session, token: str) -> Device:
    device = db.scalar(select(Device).where(Device.device_token == token))
    if device is None or device.revoked or not device.family_id:
        raise PermissionError("device_unauthorized")
    return device


def revoke_device(db: Session, device_id: str) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise ValueError("device_not_found")
    device.revoked = True
    device.auth_version += 1
    device.profile_epoch += 1
    device.active_student_id = None
    for sess in db.scalars(select(DeviceSession).where(DeviceSession.device_id == device.id, DeviceSession.active.is_(True))):
        sess.active = False
    db.commit()
    db.refresh(device)
    return device


def select_student(
    db: Session,
    *,
    device: Device,
    student_id: str,
    method: str = "manual",
    confidence: Optional[float] = None,
    second_best_confidence: Optional[float] = None,
    overlapping_speech: bool = False,
    now: Optional[datetime] = None,
) -> tuple[bool, str, Optional[DeviceSession], int]:
    """Bind active student to device. Low-confidence or overlapping voiceprint requires manual confirm."""
    now = now or utcnow()
    student = db.get(Student, student_id)
    if student is None or student.family_id != device.family_id:
        raise PermissionError("student_outside_family")

    if method == "voiceprint":
        if not student.voiceprint_consent:
            return False, "voiceprint_consent_required", None, device.profile_epoch
        if overlapping_speech:
            return False, "overlapping_speech_require_manual", None, device.profile_epoch
        if confidence is None or confidence < VOICEPRINT_MIN_CONFIDENCE:
            return False, "low_confidence_require_manual", None, device.profile_epoch
        if second_best_confidence is not None and (confidence - second_best_confidence) < VOICEPRINT_MIN_MARGIN:
            return False, "ambiguous_sibling_require_manual", None, device.profile_epoch

    # Deactivate any prior active sessions on this device and bump profile_epoch on switch
    prior_sessions = list(
        db.scalars(select(DeviceSession).where(DeviceSession.device_id == device.id, DeviceSession.active.is_(True)))
    )
    for old in prior_sessions:
        old.active = False

    if device.active_student_id != student.id:
        device.profile_epoch += 1
    device.active_student_id = student.id

    session = DeviceSession(
        id=f"sess_{uuid.uuid4().hex[:12]}",
        device_id=device.id,
        family_id=device.family_id,
        student_id=student.id,
        profile_epoch=device.profile_epoch,
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
    )
    db.add(session)
    db.commit()
    db.refresh(device)
    db.refresh(session)
    return True, "confirmed", session, device.profile_epoch


def validate_turn_session(
    db: Session,
    *,
    device: Device,
    session_id: str,
    profile_epoch: int,
    now: Optional[datetime] = None,
) -> tuple[bool, str, Optional[DeviceSession]]:
    """Reject stale async responses from previous students or expired sessions."""
    now = now or utcnow()
    session = db.get(DeviceSession, session_id)
    if session is None or session.device_id != device.id or session.family_id != device.family_id:
        return False, "session_not_found", None
    if not session.active:
        return False, "session_inactive", None
    if _as_utc(session.expires_at) <= now:
        session.active = False
        db.commit()
        return False, "session_expired", None
    if profile_epoch != device.profile_epoch or session.profile_epoch != device.profile_epoch:
        return False, "stale_profile_epoch", None
    if device.active_student_id != session.student_id:
        return False, "student_switched", None
    return True, "ok", session
