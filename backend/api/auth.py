from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models import Device, Family, GuardianUser, Student
from backend.services.auth import (
    get_current_family_id,
    get_current_guardian_claims,
    login_guardian,
    register_guardian,
)
from backend.services.identity import claim_pairing_code, revoke_device

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    account: str = Field(..., min_length=3, max_length=128)
    password: str = Field(..., min_length=6, max_length=128)
    family_name: str = Field(default="我的家庭", max_length=120)
    display_name: str = Field(default="家长", max_length=64)
    first_student_name: str = Field(default="小禾", max_length=64)
    first_student_grade: int = Field(default=4, ge=1, le=9)


class LoginRequest(BaseModel):
    account: str = Field(..., min_length=3, max_length=128)
    password: str = Field(..., min_length=1, max_length=128)


class ClaimDeviceRequest(BaseModel):
    pairing_code: str = Field(..., min_length=4, max_length=16)


@router.post("/register")
def api_register(payload: RegisterRequest, db: Session = Depends(get_db)):
    try:
        user, fam, stu, token = register_guardian(
            db,
            account=payload.account,
            password=payload.password,
            family_name=payload.family_name,
            display_name=payload.display_name,
            first_student_name=payload.first_student_name,
            first_student_grade=payload.first_student_grade,
        )
    except ValueError as exc:
        code = 409 if str(exc) == "account_already_exists" else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "account": user.account,
            "display_name": user.display_name,
            "role": user.role,
        },
        "family": {"id": fam.id, "name": fam.name},
        "default_student": {"id": stu.id, "name": stu.name, "grade": stu.grade},
    }


@router.post("/login")
def api_login(payload: LoginRequest, db: Session = Depends(get_db)):
    try:
        user, token = login_guardian(db, account=payload.account, password=payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    fam = db.get(Family, user.family_id)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "account": user.account,
            "display_name": user.display_name,
            "role": user.role,
        },
        "family": {"id": user.family_id, "name": fam.name if fam else ""},
    }


@router.get("/me")
def api_me(
    claims: dict = Depends(get_current_guardian_claims),
    db: Session = Depends(get_db),
):
    user = db.get(GuardianUser, claims["sub"])
    if user is None:
        raise HTTPException(status_code=401, detail="user_not_found")
    fam = db.get(Family, user.family_id)
    students = list(
        db.scalars(select(Student).where(Student.family_id == user.family_id).order_by(Student.created_at.asc()))
    )
    devices = list(
        db.scalars(
            select(Device).where(Device.family_id == user.family_id, Device.revoked.is_(False))
        )
    )
    return {
        "user": {
            "id": user.id,
            "account": user.account,
            "display_name": user.display_name,
            "role": user.role,
        },
        "family": {"id": user.family_id, "name": fam.name if fam else ""},
        "students": [
            {"id": s.id, "name": s.name, "grade": s.grade, "textbook_version": s.textbook_version}
            for s in students
        ],
        "devices": [
            {
                "id": d.id,
                "hardware_uid": d.hardware_uid,
                "has_camera": d.has_camera,
                "firmware_version": d.firmware_version,
                "active_student_id": d.active_student_id,
            }
            for d in devices
        ],
    }


@router.post("/devices/claim")
def api_authenticated_claim_device(
    payload: ClaimDeviceRequest,
    family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    """Bind a Mosaico device to the authenticated guardian's family using its 6-digit pairing code."""
    try:
        device = claim_pairing_code(db, family_id=family_id, code=payload.pairing_code.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "claimed": True,
        "device_id": device.id,
        "hardware_uid": device.hardware_uid,
        "family_id": device.family_id,
    }


@router.post("/devices/{device_id}/revoke")
def api_authenticated_revoke_device(
    device_id: str,
    family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    """Unbind and revoke a Mosaico device belonging to the authenticated guardian's family."""
    dev = db.get(Device, device_id)
    if dev is None or dev.family_id != family_id:
        raise HTTPException(status_code=404, detail="device_not_found")
    revoke_device(db, device_id=device_id)
    dev.family_id = None
    db.commit()
    return {"revoked": True, "device_id": device_id}
