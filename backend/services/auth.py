from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional
import uuid

from fastapi import Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Family, GuardianUser, Student


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iters = 120_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iters)
    return f"pbkdf2_sha256${iters}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt, digest_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), int(iters_s))
        return hmac.compare_digest(dk.hex(), digest_hex)
    except Exception:
        return False


def issue_guardian_jwt(*, user_id: str, family_id: str, account: str, role: str = "owner") -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "fam": family_id,
        "acc": account,
        "role": role,
        "iat": now,
        "exp": now + settings.jwt_ttl_seconds,
    }
    h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    sig = hmac.new(settings.jwt_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64url_encode(sig)}"


def verify_guardian_jwt(token: str) -> dict[str, Any]:
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise ValueError("invalid_token_format")
    h_b64, p_b64, s_b64 = parts
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    expected = hmac.new(settings.jwt_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64url_decode(s_b64)):
        raise ValueError("invalid_token_signature")
    payload = json.loads(_b64url_decode(p_b64).decode("utf-8"))
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("token_expired")
    if not payload.get("fam") or not payload.get("sub"):
        raise ValueError("invalid_token_claims")
    return payload


def register_guardian(
    db: Session,
    *,
    account: str,
    password: str,
    family_name: str,
    display_name: str = "家长",
    first_student_name: str = "小禾",
    first_student_grade: int = 4,
) -> tuple[GuardianUser, Family, Student, str]:
    norm_acc = account.strip().lower()
    if len(norm_acc) < 3:
        raise ValueError("account_too_short")
    if len(password) < 6:
        raise ValueError("password_too_short")
    existing = db.scalar(select(GuardianUser).where(GuardianUser.account == norm_acc))
    if existing is not None:
        raise ValueError("account_already_exists")

    fam = Family(
        id=f"fam_{uuid.uuid4().hex[:10]}",
        name=family_name.strip() or f"{first_student_name}一家",
    )
    stu = Student(
        id=f"stu_{uuid.uuid4().hex[:10]}",
        family_id=fam.id,
        name=first_student_name.strip() or "小禾",
        grade=max(1, min(9, int(first_student_grade))),
        textbook_version="人教版",
        graphiti_group_id=f"grp_{uuid.uuid4().hex[:12]}",
        camera_consent=True,
        voiceprint_consent=True,
    )
    user = GuardianUser(
        id=f"usr_{uuid.uuid4().hex[:10]}",
        family_id=fam.id,
        account=norm_acc,
        display_name=display_name.strip() or "家长",
        password_hash=hash_password(password),
        role="owner",
        created_at=datetime.now(timezone.utc),
    )
    db.add_all([fam, stu, user])
    db.commit()
    db.refresh(fam)
    db.refresh(stu)
    db.refresh(user)

    token = issue_guardian_jwt(
        user_id=user.id,
        family_id=fam.id,
        account=user.account,
        role=user.role,
    )
    return user, fam, stu, token


def login_guardian(db: Session, *, account: str, password: str) -> tuple[GuardianUser, str]:
    norm_acc = account.strip().lower()
    user = db.scalar(select(GuardianUser).where(GuardianUser.account == norm_acc))
    if user is None or not verify_password(password, user.password_hash):
        raise ValueError("invalid_credentials")
    token = issue_guardian_jwt(
        user_id=user.id,
        family_id=user.family_id,
        account=user.account,
        role=user.role,
    )
    return user, token


def get_current_family_id(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_family_id: Optional[str] = Header(default=None, alias="X-Family-Id"),
) -> str:
    """
    Unified multi-tenant Guardian auth dependency:
      1) Production / Multi-user: verifies `Authorization: Bearer <jwt>` and extracts `family_id`.
      2) Local dev / Legacy tests: accepts `X-Family-Id` when Bearer header is absent.
    """
    if authorization and authorization.lower().startswith("bearer "):
        raw_token = authorization.split(" ", 1)[1].strip()
        try:
            claims = verify_guardian_jwt(raw_token)
            return str(claims["fam"])
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    if x_family_id and x_family_id.strip():
        return x_family_id.strip()
    raise HTTPException(status_code=401, detail="missing_auth")


def get_current_guardian_claims(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    raw_token = authorization.split(" ", 1)[1].strip()
    try:
        return verify_guardian_jwt(raw_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
