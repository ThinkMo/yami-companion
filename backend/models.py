from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Family(Base):
    __tablename__ = "families"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    quiet_start_hour: Mapped[int] = mapped_column(Integer, default=20)
    quiet_start_minute: Mapped[int] = mapped_column(Integer, default=30)
    dingtalk_webhook_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dingtalk_app_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    dingtalk_app_secret: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    dingtalk_class_group_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    students: Mapped[list[Student]] = relationship(back_populates="family", cascade="all, delete-orphan")
    devices: Mapped[list[Device]] = relationship(back_populates="family", cascade="all, delete-orphan")
    guardians: Mapped[list[GuardianUser]] = relationship(back_populates="family", cascade="all, delete-orphan")


class GuardianUser(Base):
    """Multi-tenant parent/guardian login account belonging to a Family."""

    __tablename__ = "guardian_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    account: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), default="家长")
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(24), default="owner")  # owner | member
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    family: Mapped[Family] = relationship(back_populates="guardians")


class Student(Base):
    __tablename__ = "students"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    grade: Mapped[int] = mapped_column(Integer, default=3)
    textbook_version: Mapped[str] = mapped_column(String(64), default="人教版")
    graphiti_group_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    voiceprint_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    camera_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    pet_jump_seq: Mapped[int] = mapped_column(Integer, default=0)
    last_desk_check_at_desk: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_desk_check_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_desk_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    family: Mapped[Family] = relationship(back_populates="students")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[Optional[str]] = mapped_column(ForeignKey("families.id", ondelete="SET NULL"), nullable=True, index=True)
    hardware_uid: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    device_token: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    auth_version: Mapped[int] = mapped_column(Integer, default=1)
    profile_epoch: Mapped[int] = mapped_column(Integer, default=1)
    active_student_id: Mapped[Optional[str]] = mapped_column(ForeignKey("students.id", ondelete="SET NULL"), nullable=True)
    has_camera: Mapped[bool] = mapped_column(Boolean, default=False)
    mic_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    firmware_version: Mapped[str] = mapped_column(String(32), default="0.1.0")
    desk_check_seq: Mapped[int] = mapped_column(Integer, default=0)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    family: Mapped[Optional[Family]] = relationship(back_populates="devices")


class PairingCode(Base):
    __tablename__ = "pairing_codes"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceSession(Base):
    __tablename__ = "device_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    profile_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DingTalkSyncState(Base):
    __tablename__ = "dingtalk_sync_states"
    __table_args__ = (UniqueConstraint("family_id", "student_id", "conversation_id", name="uq_sync_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    watermark_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ok")  # ok | auth_expired | rate_limited | error
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("family_id", "student_id", "source_conversation_id", "source_message_id", name="uq_assignment_source"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    source_conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sender_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sender_name: Mapped[str] = mapped_column(String(64), default="")
    subject: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    item_range: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    submission_mode: Mapped[str] = mapped_column(String(32), default="unknown")
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    needs_parent_confirm: Mapped[bool] = mapped_column(Boolean, default=False)
    unclear_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attachment_warning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    revisions: Mapped[list[AssignmentRevision]] = relationship(back_populates="assignment", cascade="all, delete-orphan")
    submissions: Mapped[list[SubmissionEvidence]] = relationship(back_populates="assignment", cascade="all, delete-orphan")


class AssignmentRevision(Base):
    __tablename__ = "assignment_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    item_range: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    assignment: Mapped[Assignment] = relationship(back_populates="revisions")


class SubmissionEvidence(Base):
    __tablename__ = "submission_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    assignment_version: Mapped[int] = mapped_column(Integer, nullable=False)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # child_said_done | photo | needs_parent | not_yet | parent_confirmed
    source_rank: Mapped[int] = mapped_column(Integer, nullable=False)  # parent_confirmed=30, photo=20, child/not_yet/needs_parent=10
    photo_object_key: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    photo_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    assignment: Mapped[Assignment] = relationship(back_populates="submissions")


class StudySession(Base):
    __tablename__ = "study_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    assignment_version: Mapped[int] = mapped_column(Integer, nullable=False)
    problem_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    stage: Mapped[str] = mapped_column(String(32), default="writing")
    hint_level: Mapped[int] = mapped_column(Integer, default=0)
    child_attempt_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    echo_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    checks_csv: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LearningEvent(Base):
    __tablename__ = "learning_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    assignment_id: Mapped[Optional[str]] = mapped_column(ForeignKey("assignments.id", ondelete="SET NULL"), nullable=True)
    topic_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    problem_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    hint_level: Mapped[int] = mapped_column(Integer, default=0)
    source_kind: Mapped[str] = mapped_column(String(32), default="device_confirmed")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewItem(Base):
    __tablename__ = "review_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    topic_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    problem_id: Mapped[str] = mapped_column(String(64), nullable=False)
    method_note: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opened_yesterday_hint: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="scheduled")  # scheduled | independent_pass | hinted_pass | pending_verify | skipped | expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    assignment_id: Mapped[Optional[str]] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), nullable=True, index=True)
    assignment_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # check | submission | review | schedule
    anchor_label: Mapped[str] = mapped_column(String(128), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="candidate")  # candidate | snoozed | delivered | displayed | cancelled | expired
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SubmissionPromptLog(Base):
    __tablename__ = "submission_prompt_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    study_day: Mapped[str] = mapped_column(String(16), nullable=False)  # YYYY-MM-DD in family local tz
    prompted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ParentNotification(Base):
    __tablename__ = "parent_notifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # needs_parent_submit | unclear_assignment | auth_expired
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="sent")  # sent | deferred_quiet | no_webhook
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LearnerMemory(Base):
    __tablename__ = "learner_memories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # preference | strategy | agreement
    topic_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    graphiti_episode_uuid: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MemoryOutbox(Base):
    __tablename__ = "memory_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    memory_id: Mapped[str] = mapped_column(ForeignKey("learner_memories.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    op: Mapped[str] = mapped_column(String(16), nullable=False)  # upsert | delete
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GrowthRecord(Base):
    __tablename__ = "growth_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)  # habit | knowledge | teacher_praise
    topic_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), default="rubric_verified")
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    parent_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MethodCardUnlock(Base):
    __tablename__ = "method_card_unlocks"
    __table_args__ = (UniqueConstraint("student_id", "card_id", name="uq_student_method_card"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    card_id: Mapped[str] = mapped_column(String(64), nullable=False)
    front: Mapped[str] = mapped_column(String(128), nullable=False)
    back: Mapped[str] = mapped_column(Text, nullable=False)
    growth_record_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DictationSession(Base):
    __tablename__ = "dictation_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(16), nullable=False)  # 语文 | 英语
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # parent_dingtalk | student_initiated
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    current_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="ready")  # ready | in_progress | self_check | completed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    words: Mapped[list[DictationWord]] = relationship(back_populates="session", cascade="all, delete-orphan", order_by="DictationWord.order_index")


class DictationWord(Base):
    __tablename__ = "dictation_words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("dictation_sessions.id", ondelete="CASCADE"), index=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    word: Mapped[str] = mapped_column(String(128), nullable=False)
    hint_or_meaning: Mapped[str] = mapped_column(String(256), default="")
    repeat_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    self_check_correct: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    session: Mapped[DictationSession] = relationship(back_populates="words")


class ConversationTurn(Base):
    """Persistent multi-turn dialogue memory per student (survives restarts & feeds Layer 2 context)."""

    __tablename__ = "conversation_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    skill_name: Mapped[str] = mapped_column(String(64), default="chat-companion")
    user_text: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PetProfile(Base):
    """Per-student virtual pet raising state (cat / dog / beetle, 3 breeds each, habitat Lv.1-4)."""

    __tablename__ = "pet_profiles"

    student_id: Mapped[str] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), primary_key=True
    )
    family_id: Mapped[str] = mapped_column(
        ForeignKey("families.id", ondelete="CASCADE"), index=True
    )
    adopted: Mapped[bool] = mapped_column(Boolean, default=True)
    species: Mapped[str] = mapped_column(String(16), default="dog")      # cat | dog | beetle
    breed: Mapped[str] = mapped_column(String(24), default="golden")     # orange|ragdoll|calico | golden|husky|corgi | kabuto|hercules|elephas
    pet_name: Mapped[str] = mapped_column(String(32), default="小金毛")
    coins: Mapped[int] = mapped_column(Integer, default=40)
    hunger: Mapped[int] = mapped_column(Integer, default=75)             # 0..100
    happiness: Mapped[int] = mapped_column(Integer, default=80)          # 0..100
    exp: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=1)
    habitat_lv: Mapped[int] = mapped_column(Integer, default=1)          # 1..4
    is_runaway: Mapped[bool] = mapped_column(Boolean, default=False)
    last_action_kind: Mapped[str] = mapped_column(String(24), default="idle")
    last_food: Mapped[str] = mapped_column(String(24), default="")
    last_event_text: Mapped[str] = mapped_column(Text, default="刚住进温馨小窝，正摇着尾巴等你呢！")
    last_care_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_decay_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class StudentSkillConfig(Base):
    """Per-student Skill lifecycle & enable/disable state managed from Guardian Web Skills panel."""

    __tablename__ = "student_skill_configs"
    __table_args__ = (UniqueConstraint("student_id", "skill_name", name="uq_student_skill_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    skill_name: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    lifecycle_status: Mapped[str] = mapped_column(String(24), default="active")  # active | paused | beta
    invocation_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EmotionSignal(Base):
    """Non-intrusive child emotional wellbeing signal passively detected during voice turns (non-clinical)."""

    __tablename__ = "emotion_signals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    mood: Mapped[str] = mapped_column(String(24), nullable=False)  # confident | calm | frustrated | fatigued | anxious
    risk_level: Mapped[str] = mapped_column(String(16), default="normal")  # normal | attention | high
    trigger_context: Mapped[str] = mapped_column(Text, default="")
    parent_tip: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CustomTask(Base):
    """Parent-defined custom task with full lifecycle (active/paused/archived, cadence, daily check-in & streak)."""

    __tablename__ = "custom_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    cadence: Mapped[str] = mapped_column(String(24), default="daily")  # daily | weekdays | once
    status: Mapped[str] = mapped_column(String(24), default="active")  # active | paused | archived
    last_completed_date: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # YYYY-MM-DD
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
