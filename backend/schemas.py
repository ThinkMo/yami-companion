from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PairingIssueRequest(BaseModel):
    hardware_uid: str = Field(..., min_length=3, max_length=128)
    has_camera: bool = False
    firmware_version: str = "0.1.0"


class PairingIssueResponse(BaseModel):
    device_id: str
    pairing_code: str
    expires_at: datetime


class PairingClaimRequest(BaseModel):
    family_id: str
    pairing_code: str = Field(..., min_length=6, max_length=6)


class PairingClaimResponse(BaseModel):
    device_id: str
    family_id: str
    device_token: str
    auth_version: int
    profile_epoch: int


class SelectStudentRequest(BaseModel):
    student_id: str
    method: Literal["manual", "voiceprint"] = "manual"
    confidence: Optional[float] = None
    second_best_confidence: Optional[float] = None
    overlapping_speech: bool = False


class SelectStudentResponse(BaseModel):
    confirmed: bool
    reason: str
    session_id: Optional[str] = None
    student_id: Optional[str] = None
    profile_epoch: int


class DingTalkAttachmentIn(BaseModel):
    ref_id: str
    kind: str
    status: Literal["ok", "permission_denied", "encrypted", "unavailable"] = "ok"


class DingTalkMessageIn(BaseModel):
    message_id: str
    revises_message_id: Optional[str] = None
    sender_id: str
    sender_name: str = ""
    published_at: datetime
    subject: str
    content: str
    page_ref: Optional[str] = None
    item_range: Optional[str] = None
    submission_mode: Literal["dingtalk_online", "paper_next_day", "oral", "none", "unknown"] = "unknown"
    due_at: Optional[datetime] = None
    unclear_reason: Optional[str] = None
    attachments: list[DingTalkAttachmentIn] = Field(default_factory=list)


class DingTalkBatchIn(BaseModel):
    family_id: str
    student_id: str
    conversation_id: str
    authorized_teachers: dict[str, str]  # sender_id -> subject
    page_complete: bool = True
    watermark_at: datetime
    auth_status: Literal["ok", "auth_expired", "rate_limited"] = "ok"
    messages: list[DingTalkMessageIn] = Field(default_factory=list)


class DingTalkSyncResult(BaseModel):
    status: str
    watermark_advanced: bool
    watermark_at: Optional[datetime]
    created: int
    revised: int
    skipped_untrusted_sender: int
    skipped_duplicate: int
    pending_parent_confirm: int


class AssignmentOut(BaseModel):
    id: str
    subject: str
    content: str
    page_ref: Optional[str]
    item_range: Optional[str]
    submission_mode: str
    submission_status: str
    due_at: Optional[datetime]
    published_at: datetime
    version: int
    completed: bool
    needs_parent_confirm: bool
    unclear_reason: Optional[str]
    attachment_warning: Optional[str]


class StudentAssignmentsOut(BaseModel):
    student_id: str
    student_name: str
    sync_status: str
    last_sync_at: Optional[datetime]
    stale_warning: Optional[str]
    assignments: list[AssignmentOut]


class VoiceQueryRequest(BaseModel):
    session_id: str
    profile_epoch: int
    utterance: str


class VoiceQueryResponse(BaseModel):
    accepted: bool
    rejected_reason: Optional[str] = None
    safety_blocked: bool = False
    intent: str
    spoken_text: str
    screen_title: str
    assignments: list[AssignmentOut] = Field(default_factory=list)
    stale_warning: Optional[str] = None
