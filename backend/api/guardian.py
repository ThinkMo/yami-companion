from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.learning.dictation import (
    complete_dictation_self_check,
    create_dictation_session,
    dictation_action,
    start_dictation_from_request,
)
from backend.agent.skill_loader import SkillRegistry
from backend.config import settings
from backend.memory.graphiti_client import GraphitiStore, InMemoryGraphitiStore, create_graphiti_store
from backend.memory.service import (
    delete_growth_record_and_revoke_cards,
    delete_learner_memory,
    process_memory_outbox,
    revoke_camera_consent_and_purge_photos,
    write_learner_memory,
)
from backend.models import Assignment, ConversationTurn, CustomTask, Family, GrowthRecord, LearnerMemory, ParentNotification, ReviewItem, Student, StudentSkillConfig
from backend.reminders.service import dispatch_parent_dingtalk_alert
from backend.rewards.pet_service import (
    PET_BREEDS,
    adopt_pet,
    feed_pet,
    get_or_create_pet,
    pet_to_dict,
    play_with_pet,
    recall_pet,
    simulate_pet_unattended_days,
    upgrade_pet_habitat,
)
from backend.schemas import DingTalkBatchIn, DingTalkMessageIn
from backend.services.assignments import get_student_assignments
from backend.services.auth import get_current_family_id
from backend.services.dingtalk_sync import ingest_dingtalk_batch
from backend.services.textbook_catalog import (
    get_catalog_options,
    parse_subject_textbooks,
    serialize_subject_textbooks,
)
from backend.services.wellbeing import build_growth_analytics
from backend.submissions.service import record_submission_evidence

router = APIRouter(prefix="/v1/guardian", tags=["guardian"])
shared_graphiti: Optional[GraphitiStore] = None


def get_shared_graphiti() -> GraphitiStore:
    global shared_graphiti
    if shared_graphiti is None:
        prefer_neo4j = settings.database_url.startswith("postgresql")
        shared_graphiti = create_graphiti_store(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            prefer_neo4j=prefer_neo4j,
        )
    return shared_graphiti


class ParentDictationCreate(BaseModel):
    student_id: str
    subject: Literal["语文", "英语"]
    source: Literal["parent_dingtalk", "student_initiated"] = "parent_dingtalk"
    title: str
    words: list[tuple[str, str]] = Field(..., min_length=1)


class NaturalDictationCreate(BaseModel):
    student_id: str
    utterance: str = Field(..., min_length=2)
    source: Literal["parent_dingtalk", "student_initiated"] = "parent_dingtalk"


class DictationActionIn(BaseModel):
    action: Literal["current", "repeat", "next", "prev", "skip"]


class DictationCheckIn(BaseModel):
    missed_words: list[str] = Field(default_factory=list)


class GrowthFixIn(BaseModel):
    parent_note: str


class FamilyWebhookPatch(BaseModel):
    dingtalk_webhook_url: str = ""
    dingtalk_app_id: Optional[str] = None
    dingtalk_app_secret: Optional[str] = None
    dingtalk_class_group_id: Optional[str] = None


class ManualAssignmentCreate(BaseModel):
    subject: Literal["语文", "数学", "英语", "科学"]
    content: str = Field(..., min_length=2)
    page_ref: Optional[str] = None
    submission_mode: Literal["none", "paper_next_day", "dingtalk_online", "photo_upload", "unknown"] = "paper_next_day"


class GuardianPetPatch(BaseModel):
    breed: str
    pet_name: Optional[str] = None


class GuardianPetFeedIn(BaseModel):
    food: str


class GuardianPetSimulateIn(BaseModel):
    days: float = Field(default=0.0, ge=0.0, le=30.0)


class SkillConfigPatch(BaseModel):
    enabled: bool
    lifecycle_status: Optional[Literal["active", "paused", "beta"]] = None


class StudentSettingsPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=24)
    grade: Optional[int] = Field(default=None, ge=1, le=9)
    textbook_version: Optional[str] = None
    subject_textbooks: Optional[dict[str, str]] = None
    voiceprint_consent: Optional[bool] = None
    camera_consent: Optional[bool] = None


@router.get("/catalog/textbooks")
def guardian_textbook_catalog(grade: Optional[int] = None):
    return {"grade": grade, "subjects": get_catalog_options(grade=grade)}


@router.get("/students")
def guardian_list_students(
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    rows = list(
        db.scalars(
            select(Student)
            .where(Student.family_id == x_family_id)
            .order_by(Student.id.asc())
        )
    )
    return [
        {
            "student_id": s.id,
            "name": s.name,
            "grade": s.grade,
            "textbook_version": s.textbook_version,
            "subject_textbooks": parse_subject_textbooks(s.textbook_version),
            "voiceprint_consent": s.voiceprint_consent,
            "camera_consent": s.camera_consent,
        }
        for s in rows
    ]


@router.get("/students/{student_id}/today")
def guardian_today(
    student_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        data = get_student_assignments(db, family_id=x_family_id, student_id=student_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    needs_action = [
        a.model_dump()
        for a in data.assignments
        if a.submission_status == "needs_parent" or a.needs_parent_confirm or a.subject == "家校通知"
    ]
    all_asgs = [a.model_dump() for a in data.assignments if a.subject != "家校通知"]
    previews = [
        a
        for a in all_asgs
        if "预习" in (a.get("content") or "") or a.get("subject") in ("预习", "每日任务")
    ]
    homework_items = [
        a
        for a in all_asgs
        if "预习" not in (a.get("content") or "") and a.get("subject") not in ("预习", "每日任务")
    ]

    review_rows = list(
        db.scalars(
            select(ReviewItem)
            .where(
                ReviewItem.family_id == x_family_id,
                ReviewItem.student_id == student_id,
            )
            .order_by(ReviewItem.due_at.asc())
            .limit(8)
        )
    )
    review_items = [
        {
            "id": r.id,
            "topic_id": r.topic_id,
            "problem_id": r.problem_id,
            "method_note": r.method_note,
            "status": r.status,
            "due_at": r.due_at,
        }
        for r in review_rows
    ]

    class_perf_rows = list(
        db.scalars(
            select(GrowthRecord)
            .where(
                GrowthRecord.family_id == x_family_id,
                GrowthRecord.student_id == student_id,
                GrowthRecord.deleted.is_(False),
                GrowthRecord.category.in_(("teacher_praise", "habit", "knowledge")),
            )
            .order_by(GrowthRecord.created_at.desc())
            .limit(6)
        )
    )
    class_performances = [
        {
            "id": r.id,
            "category": r.category,
            "title": r.title,
            "content": r.content,
            "evidence": r.evidence,
            "verified": r.verified,
            "created_at": r.created_at,
        }
        for r in class_perf_rows
    ]

    return {
        "student_id": data.student_id,
        "student_name": data.student_name,
        "sync_status": data.sync_status,
        "last_sync_at": data.last_sync_at,
        "stale_warning": data.stale_warning,
        "needs_action": needs_action,
        "assignments": all_asgs,
        "stages": {
            "review": review_items,
            "homework": homework_items,
            "preview": previews,
        },
        "class_performances": class_performances,
    }


def _serialize_custom_task(t: CustomTask, today_iso: str) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "cadence": t.cadence,
        "status": t.status,
        "done_today": t.last_completed_date == today_iso,
        "last_completed_date": t.last_completed_date,
        "streak_days": t.streak_days,
        "created_at": t.created_at,
    }


class CustomTaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    cadence: Literal["daily", "weekdays", "once"] = "daily"


class CustomTaskPatch(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=160)
    cadence: Optional[Literal["daily", "weekdays", "once"]] = None
    status: Optional[Literal["active", "paused", "archived"]] = None
    done_today: Optional[bool] = None


@router.get("/students/{student_id}/custom-tasks")
def list_custom_tasks(
    student_id: str,
    status: Optional[str] = None,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    stu = db.get(Student, student_id)
    if stu is None or stu.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    stmt = select(CustomTask).where(
        CustomTask.family_id == x_family_id,
        CustomTask.student_id == student_id,
    )
    if status in ("active", "paused", "archived"):
        stmt = stmt.where(CustomTask.status == status)
    else:
        stmt = stmt.where(CustomTask.status != "archived")
    rows = list(db.scalars(stmt.order_by(CustomTask.created_at.desc())))
    today_iso = datetime.now(timezone.utc).date().isoformat()
    return [_serialize_custom_task(t, today_iso) for t in rows]


@router.post("/students/{student_id}/custom-tasks")
def create_custom_task(
    student_id: str,
    body: CustomTaskCreate,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    stu = db.get(Student, student_id)
    if stu is None or stu.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    t = CustomTask(
        id=f"ct_{uuid.uuid4().hex[:12]}",
        family_id=x_family_id,
        student_id=student_id,
        title=body.title.strip(),
        cadence=body.cadence,
        status="active",
        streak_days=0,
    )
    db.add(t)
    db.commit()
    today_iso = datetime.now(timezone.utc).date().isoformat()
    return _serialize_custom_task(t, today_iso)


@router.patch("/students/{student_id}/custom-tasks/{task_id}")
def update_custom_task(
    student_id: str,
    task_id: str,
    body: CustomTaskPatch,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    t = db.get(CustomTask, task_id)
    if t is None or t.family_id != x_family_id or t.student_id != student_id:
        raise HTTPException(status_code=404, detail="custom_task_not_found")
    today_iso = datetime.now(timezone.utc).date().isoformat()
    if body.title is not None:
        t.title = body.title.strip()
    if body.cadence is not None:
        t.cadence = body.cadence
    if body.status is not None:
        t.status = body.status
    if body.done_today is not None:
        if body.done_today and t.last_completed_date != today_iso:
            t.last_completed_date = today_iso
            t.streak_days = (t.streak_days or 0) + 1
        elif not body.done_today and t.last_completed_date == today_iso:
            t.last_completed_date = None
            t.streak_days = max(0, (t.streak_days or 0) - 1)
    db.commit()
    return _serialize_custom_task(t, today_iso)


@router.delete("/students/{student_id}/custom-tasks/{task_id}")
def delete_custom_task(
    student_id: str,
    task_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    t = db.get(CustomTask, task_id)
    if t is None or t.family_id != x_family_id or t.student_id != student_id:
        raise HTTPException(status_code=404, detail="custom_task_not_found")
    db.delete(t)
    db.commit()
    return {"id": task_id, "deleted": True}


@router.get("/students/{student_id}/growth/analytics")
def guardian_growth_analytics(
    student_id: str,
    month: Optional[str] = None,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        return build_growth_analytics(db, family_id=x_family_id, student_id=student_id, month=month)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/students/{student_id}/growth/{growth_id}/verify")
def guardian_verify_growth(
    student_id: str,
    growth_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    rec = db.get(GrowthRecord, growth_id)
    if rec is None or rec.family_id != x_family_id or rec.student_id != student_id or rec.deleted:
        raise HTTPException(status_code=404, detail="growth_not_found")
    rec.verified = True
    db.commit()
    return {"id": rec.id, "verified": True}


class TeacherPraiseCreate(BaseModel):
    subject: str = Field(default="综合", max_length=24)
    teacher_name: str = Field(default="任课老师", max_length=32)
    title: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=500)


@router.post("/students/{student_id}/praise")
def create_teacher_praise(
    student_id: str,
    body: TeacherPraiseCreate,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    stu = db.get(Student, student_id)
    if stu is None or stu.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    teacher = body.teacher_name.strip() or "任课老师"
    if not teacher.endswith("老师"):
        teacher = f"{teacher}老师"
    subj = body.subject.strip() or "综合"
    rec = GrowthRecord(
        id=f"gr_praise_{uuid.uuid4().hex[:10]}",
        family_id=x_family_id,
        student_id=student_id,
        category="teacher_praise",
        title=body.title.strip(),
        content=body.content.strip(),
        evidence=f"家长补录 · {subj} · {teacher}",
        verified=True,
        deleted=False,
    )
    db.add(rec)
    db.commit()
    return {"id": rec.id, "title": rec.title, "subject": subj, "teacher_name": teacher, "verified": True}


_SKILL_META_CN = {
    "homework-companion": ("作业陪伴与分级答疑", True, "复习 → 写作业 → 预习全流程，苏格拉底式线索引导"),
    "voice-dictation": ("语文/英语纯语音听写", False, "按年级教材逐词报读、不露字形、错词自动进次日巩固"),
    "english-speaking": ("双语英语口语陪练", False, "日常场景双语对话与温和纠音鼓励"),
    "pet-companion-house": ("芽米宠物小屋激励", False, "完成任务赚宠粮币、喂食陪伴、7天出走与100币召回"),
    "chat-companion": ("温暖日常陪伴", True, "问候、情绪倾听、百科好奇心解答与学习偏好记忆"),
}


@router.get("/students/{student_id}/skills")
def guardian_list_student_skills(
    student_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    stu = db.get(Student, student_id)
    if stu is None or stu.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    reg = SkillRegistry()
    cfg_map = {
        c.skill_name: c
        for c in db.scalars(
            select(StudentSkillConfig).where(
                StudentSkillConfig.family_id == x_family_id,
                StudentSkillConfig.student_id == student_id,
            )
        )
    }
    items = []
    for sk in reg.list_skills():
        cfg = cfg_map.get(sk.name)
        title_cn, is_core, summary_cn = _SKILL_META_CN.get(sk.name, (sk.name, False, sk.description))
        enabled = cfg.enabled if cfg is not None else True
        status = cfg.lifecycle_status if cfg is not None else ("active" if enabled else "paused")
        items.append(
            {
                "name": sk.name,
                "title_cn": title_cn,
                "summary_cn": summary_cn,
                "description": sk.description,
                "is_core": is_core,
                "enabled": enabled,
                "lifecycle_status": status,
                "invocation_count": int(cfg.invocation_count or 0) if cfg is not None else 0,
                "last_used_at": cfg.last_used_at.isoformat() if (cfg and cfg.last_used_at) else None,
            }
        )
    return {"student_id": student_id, "skills": items}


@router.patch("/students/{student_id}/skills/{skill_name}")
def guardian_update_student_skill(
    student_id: str,
    skill_name: str,
    payload: SkillConfigPatch,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    stu = db.get(Student, student_id)
    if stu is None or stu.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    reg = SkillRegistry()
    if reg.get(skill_name) is None:
        raise HTTPException(status_code=404, detail="skill_not_found")
    cfg = db.scalar(
        select(StudentSkillConfig).where(
            StudentSkillConfig.student_id == student_id,
            StudentSkillConfig.skill_name == skill_name,
        )
    )
    new_status = payload.lifecycle_status or ("active" if payload.enabled else "paused")
    if cfg is None:
        cfg = StudentSkillConfig(
            id=f"skc_{uuid.uuid4().hex[:10]}",
            family_id=x_family_id,
            student_id=student_id,
            skill_name=skill_name,
            enabled=payload.enabled,
            lifecycle_status=new_status,
            invocation_count=0,
        )
        db.add(cfg)
    else:
        cfg.enabled = payload.enabled
        cfg.lifecycle_status = new_status
    db.commit()
    db.refresh(cfg)
    return {
        "student_id": student_id,
        "skill_name": skill_name,
        "enabled": cfg.enabled,
        "lifecycle_status": cfg.lifecycle_status,
        "invocation_count": cfg.invocation_count,
    }


@router.get("/students/{student_id}/growth")
def guardian_growth(
    student_id: str,
    category: Optional[str] = None,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    student = db.get(Student, student_id)
    if student is None or student.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    stmt = (
        select(GrowthRecord)
        .where(
            GrowthRecord.family_id == x_family_id,
            GrowthRecord.student_id == student_id,
            GrowthRecord.deleted.is_(False),
        )
        .order_by(GrowthRecord.created_at.desc())
    )
    if category:
        stmt = stmt.where(GrowthRecord.category == category)
    rows = list(db.scalars(stmt))
    return [
        {
            "id": r.id,
            "category": r.category,
            "title": r.title,
            "content": r.content,
            "evidence": r.evidence,
            "verified": r.verified,
            "parent_note": r.parent_note,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.patch("/students/{student_id}/growth/{growth_id}")
def guardian_fix_growth(
    student_id: str,
    growth_id: str,
    payload: GrowthFixIn,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    rec = db.get(GrowthRecord, growth_id)
    if rec is None or rec.family_id != x_family_id or rec.student_id != student_id or rec.deleted:
        raise HTTPException(status_code=404, detail="growth_not_found")
    rec.parent_note = payload.parent_note.strip()
    db.commit()
    return {"id": rec.id, "parent_note": rec.parent_note}


@router.delete("/students/{student_id}/growth/{growth_id}")
def guardian_delete_growth(
    student_id: str,
    growth_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        revoked_cards = delete_growth_record_and_revoke_cards(
            db, family_id=x_family_id, student_id=student_id, growth_id=growth_id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"deleted": True, "revoked_cards": revoked_cards}


@router.get("/students/{student_id}/pet")
def guardian_get_pet(
    student_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        pet = get_or_create_pet(db, family_id=x_family_id, student_id=student_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "pet": pet_to_dict(pet),
        "catalog": [
            {"key": k, "species": v["species"], "species_cn": v["species_cn"], "breed_cn": v["breed_cn"], "default_name": v["default_name"]}
            for k, v in PET_BREEDS.items()
        ],
    }


@router.patch("/students/{student_id}/pet")
def guardian_update_pet(
    student_id: str,
    payload: GuardianPetPatch,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    student = db.get(Student, student_id)
    if student is None or student.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    res = adopt_pet(
        db,
        family_id=x_family_id,
        student_id=student_id,
        breed_or_species=payload.breed,
        nickname=payload.pet_name,
    )
    if not res.get("adopted"):
        raise HTTPException(status_code=400, detail="invalid_pet_breed")
    return res


@router.post("/students/{student_id}/pet/feed")
def guardian_feed_pet(
    student_id: str,
    payload: GuardianPetFeedIn,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        return feed_pet(db, family_id=x_family_id, student_id=student_id, food_name=payload.food)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/students/{student_id}/pet/play")
def guardian_play_pet(
    student_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        return play_with_pet(db, family_id=x_family_id, student_id=student_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/students/{student_id}/pet/upgrade")
def guardian_upgrade_pet(
    student_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        return upgrade_pet_habitat(db, family_id=x_family_id, student_id=student_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/students/{student_id}/pet/recall")
def guardian_recall_pet(
    student_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        return recall_pet(db, family_id=x_family_id, student_id=student_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/students/{student_id}/pet/simulate-time")
def guardian_simulate_pet_time(
    student_id: str,
    payload: GuardianPetSimulateIn,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        return simulate_pet_unattended_days(
            db, family_id=x_family_id, student_id=student_id, days=payload.days
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/students/{student_id}/conversations")
def guardian_conversations(
    student_id: str,
    limit: int = 30,
    day: Optional[str] = None,
    tz_offset_min: int = 0,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    student = db.get(Student, student_id)
    if student is None or student.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    stmt = (
        select(ConversationTurn)
        .where(
            ConversationTurn.family_id == x_family_id,
            ConversationTurn.student_id == student_id,
        )
        .order_by(ConversationTurn.id.desc())
    )
    cap = max(1, min(limit, 100))
    utc_start: Optional[datetime] = None
    utc_end: Optional[datetime] = None
    if day and len(day.strip()) == 10:
        try:
            local_start = datetime.strptime(day.strip(), "%Y-%m-%d")
            # JS Date.getTimezoneOffset() returns (UTC - Local) in minutes, so Local + offset = UTC
            offset = timedelta(minutes=max(-840, min(840, int(tz_offset_min or 0))))
            utc_start = (local_start + offset).replace(tzinfo=timezone.utc)
            utc_end = utc_start + timedelta(days=1)
        except ValueError:
            pass

    # Normalize naive (SQLite) & tz-aware (PostgreSQL) timestamps consistently in UTC
    raw_rows = list(db.scalars(stmt.limit(500 if utc_start is not None else cap)))
    matched: list[dict] = []
    for r in raw_rows:
        ts = r.created_at
        if ts is not None:
            ts_utc = ts.astimezone(timezone.utc) if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        else:
            ts_utc = None
        if utc_start is not None and utc_end is not None:
            if ts_utc is None or not (utc_start <= ts_utc < utc_end):
                continue
        matched.append(
            {
                "id": r.id,
                "skill_name": r.skill_name,
                "user_text": r.user_text,
                "assistant_text": r.assistant_text,
                "created_at": ts_utc.isoformat().replace("+00:00", "Z") if ts_utc is not None else None,
            }
        )
        if len(matched) >= cap:
            break
    return matched


@router.delete("/students/{student_id}/conversations")
def guardian_clear_conversations(
    student_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    student = db.get(Student, student_id)
    if student is None or student.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    rows = list(
        db.scalars(
            select(ConversationTurn).where(
                ConversationTurn.family_id == x_family_id,
                ConversationTurn.student_id == student_id,
            )
        )
    )
    count = len(rows)
    for r in rows:
        db.delete(r)
    db.commit()
    return {"deleted_turns": count}


@router.post("/students/{student_id}/assignments")
def guardian_add_manual_assignment(
    student_id: str,
    payload: ManualAssignmentCreate,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    student = db.get(Student, student_id)
    if student is None or student.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    import uuid
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    asg = Assignment(
        id=f"asg_man_{uuid.uuid4().hex[:10]}",
        family_id=x_family_id,
        student_id=student_id,
        source_conversation_id="guardian_manual",
        source_message_id=f"manual_{uuid.uuid4().hex[:10]}",
        sender_id="guardian",
        sender_name="家长手动录入",
        subject=payload.subject,
        content=payload.content.strip(),
        page_ref=(payload.page_ref or "").strip() or None,
        submission_mode=payload.submission_mode,
        published_at=now,
        expires_at=now + timedelta(days=7),
        completed=False,
    )
    db.add(asg)
    db.commit()
    db.refresh(asg)
    return {"id": asg.id, "subject": asg.subject, "content": asg.content, "page_ref": asg.page_ref}


class AssignmentCompletePatch(BaseModel):
    completed: bool = True


@router.patch("/students/{student_id}/assignments/{assignment_id}/complete")
def guardian_toggle_assignment_complete(
    student_id: str,
    assignment_id: str,
    body: AssignmentCompletePatch,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    asg = db.get(Assignment, assignment_id)
    if asg is None or asg.family_id != x_family_id or asg.student_id != student_id:
        raise HTTPException(status_code=404, detail="assignment_not_found")
    asg.completed = bool(body.completed)
    asg.completed_at = datetime.now(timezone.utc) if asg.completed else None
    db.commit()
    return {"assignment_id": asg.id, "completed": asg.completed, "completed_at": asg.completed_at}


@router.delete("/students/{student_id}/assignments/{assignment_id}")
def guardian_delete_assignment(
    student_id: str,
    assignment_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    asg = db.get(Assignment, assignment_id)
    if asg is None or asg.family_id != x_family_id or asg.student_id != student_id:
        raise HTTPException(status_code=404, detail="assignment_not_found")
    db.delete(asg)
    db.commit()
    return {"deleted": True, "assignment_id": assignment_id}


@router.get("/family/webhook")
def guardian_get_webhook(
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    fam = db.get(Family, x_family_id)
    if fam is None:
        raise HTTPException(status_code=404, detail="family_not_found")
    alerts = list(
        db.scalars(
            select(ParentNotification)
            .where(ParentNotification.family_id == x_family_id)
            .order_by(ParentNotification.created_at.desc())
            .limit(10)
        )
    )
    return {
        "family_id": fam.id,
        "dingtalk_webhook_url": fam.dingtalk_webhook_url or "",
        "dingtalk_app_id": fam.dingtalk_app_id or "",
        "dingtalk_app_secret_configured": bool(fam.dingtalk_app_secret),
        "dingtalk_class_group_id": fam.dingtalk_class_group_id or "",
        "recent_alerts": [
            {"id": a.id, "kind": a.kind, "summary": a.summary, "status": a.status, "created_at": a.created_at}
            for a in alerts
        ],
    }


@router.patch("/family/webhook")
def guardian_update_webhook(
    payload: FamilyWebhookPatch,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    fam = db.get(Family, x_family_id)
    if fam is None:
        raise HTTPException(status_code=404, detail="family_not_found")
    fam.dingtalk_webhook_url = payload.dingtalk_webhook_url.strip() or None
    if payload.dingtalk_app_id is not None:
        fam.dingtalk_app_id = payload.dingtalk_app_id.strip() or None
    if payload.dingtalk_app_secret is not None and payload.dingtalk_app_secret.strip():
        fam.dingtalk_app_secret = payload.dingtalk_app_secret.strip()
    if payload.dingtalk_class_group_id is not None:
        fam.dingtalk_class_group_id = payload.dingtalk_class_group_id.strip() or None
    db.commit()
    return {
        "family_id": fam.id,
        "dingtalk_webhook_url": fam.dingtalk_webhook_url or "",
        "dingtalk_app_id": fam.dingtalk_app_id or "",
        "dingtalk_app_secret_configured": bool(fam.dingtalk_app_secret),
        "dingtalk_class_group_id": fam.dingtalk_class_group_id or "",
    }


@router.post("/family/webhook/test")
def guardian_test_webhook(
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    import uuid

    notif = dispatch_parent_dingtalk_alert(
        db,
        family_id=x_family_id,
        event_key=f"test_alert_{uuid.uuid4().hex[:10]}",
        kind="needs_parent_submit",
        summary="芽米家长端测试提醒：家庭专属钉钉群 Webhook 通道已连通。",
    )
    return {"id": notif.id, "status": notif.status, "summary": notif.summary}


@router.post("/family/dingtalk/dws-sync")
def guardian_trigger_dws_sync(
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    """Pull latest messages from configured DingTalk conversations via local `dws` CLI + Agent Harness."""
    from backend.adapters.dws_poller import sync_family_via_dws

    try:
        return sync_family_via_dws(db, family_id=x_family_id, limit_per_conv=8)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"dws_sync_failed: {exc}") from exc


@router.get("/students/{student_id}/memories")
def guardian_list_memories(
    student_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    student = db.get(Student, student_id)
    if student is None or student.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    rows = list(
        db.scalars(
            select(LearnerMemory).where(
                LearnerMemory.family_id == x_family_id,
                LearnerMemory.student_id == student_id,
                LearnerMemory.active.is_(True),
            )
        )
    )
    return [{"id": m.id, "kind": m.kind, "topic_id": m.topic_id, "summary": m.summary} for m in rows]


@router.delete("/students/{student_id}/memories/{memory_id}")
def guardian_delete_memory(
    student_id: str,
    memory_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        delete_learner_memory(
            db,
            family_id=x_family_id,
            student_id=student_id,
            memory_id=memory_id,
            graphiti=get_shared_graphiti(),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"deleted": True}


@router.patch("/students/{student_id}/settings")
def guardian_update_settings(
    student_id: str,
    payload: StudentSettingsPatch,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    student = db.get(Student, student_id)
    if student is None or student.family_id != x_family_id:
        raise HTTPException(status_code=403, detail="student_outside_family")
    purged_photos = 0
    if payload.name is not None and payload.name.strip():
        student.name = payload.name.strip()
    if payload.grade is not None:
        student.grade = payload.grade
    if payload.subject_textbooks is not None:
        student.textbook_version = serialize_subject_textbooks(payload.subject_textbooks)
    elif payload.textbook_version is not None:
        student.textbook_version = payload.textbook_version.strip()
    if payload.voiceprint_consent is not None:
        student.voiceprint_consent = payload.voiceprint_consent
    if payload.camera_consent is False:
        purged_photos = revoke_camera_consent_and_purge_photos(db, family_id=x_family_id, student_id=student_id)
    elif payload.camera_consent is True:
        student.camera_consent = True
    db.commit()
    return {
        "student_id": student.id,
        "name": student.name,
        "grade": student.grade,
        "textbook_version": student.textbook_version,
        "subject_textbooks": parse_subject_textbooks(student.textbook_version),
        "voiceprint_consent": student.voiceprint_consent,
        "camera_consent": student.camera_consent,
        "purged_photos": purged_photos,
    }


@router.post("/dictation")
def api_create_dictation(
    payload: ParentDictationCreate,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        sess = create_dictation_session(
            db,
            family_id=x_family_id,
            student_id=payload.student_id,
            subject=payload.subject,
            source=payload.source,
            title=payload.title,
            words=payload.words,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    prompt = dictation_action(db, session_id=sess.id, action="current")
    return {"session_id": sess.id, "title": sess.title, "prompt": prompt.__dict__}


@router.post("/dictation/natural")
def api_create_natural_dictation(
    payload: NaturalDictationCreate,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        sess, spoken = start_dictation_from_request(
            db,
            family_id=x_family_id,
            student_id=payload.student_id,
            utterance=payload.utterance,
            source=payload.source,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if sess is None:
        return {
            "found": False,
            "session_id": None,
            "title": None,
            "spoken_text": spoken,
            "word_count": 0,
        }
    return {
        "found": True,
        "session_id": sess.id,
        "title": sess.title,
        "spoken_text": spoken,
        "word_count": len(sess.words),
    }


@router.post("/dictation/{session_id}/step")
def api_dictation_step(session_id: str, payload: DictationActionIn, db: Session = Depends(get_db)):
    try:
        prompt = dictation_action(db, session_id=session_id, action=payload.action)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return prompt.__dict__


@router.post("/dictation/{session_id}/self-check")
def api_dictation_self_check(session_id: str, payload: DictationCheckIn, db: Session = Depends(get_db)):
    try:
        sess, queued = complete_dictation_self_check(db, session_id=session_id, missed_words=payload.missed_words)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "session_id": sess.id,
        "status": sess.status,
        "queued_review_words": [q.problem_id for q in queued],
    }


@router.post("/students/{student_id}/assignments/{assignment_id}/confirm-submission")
def guardian_confirm_submission(
    student_id: str,
    assignment_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    try:
        _, resolved = record_submission_evidence(
            db,
            family_id=x_family_id,
            student_id=student_id,
            assignment_id=assignment_id,
            status="parent_confirmed",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"assignment_id": assignment_id, "submission_status": resolved}


@router.post("/students/{student_id}/assignments/{assignment_id}/confirm-unclear")
def guardian_confirm_unclear(
    student_id: str,
    assignment_id: str,
    x_family_id: str = Depends(get_current_family_id),
    db: Session = Depends(get_db),
):
    asg = db.get(Assignment, assignment_id)
    if asg is None or asg.family_id != x_family_id or asg.student_id != student_id:
        raise HTTPException(status_code=403, detail="assignment_outside_student_scope")
    asg.needs_parent_confirm = False
    asg.unclear_reason = None
    db.commit()
    return {"assignment_id": asg.id, "needs_parent_confirm": False}


@router.post("/dev/seed-demo")
def seed_demo_data(db: Session = Depends(get_db)):
    """Idempotently seeds demo family, two students, assignments, growth, and memories for local preview."""
    now = datetime.now(timezone.utc)
    if db.get(Family, "fam_a") is None:
        db.add(Family(id="fam_a", name="小禾乐乐家", dingtalk_webhook_url="https://oapi.dingtalk.com/robot/send?access_token=demo"))
        db.flush()
    if db.get(Student, "stu_he") is None:
        db.add(Student(id="stu_he", family_id="fam_a", name="小禾", grade=4, textbook_version="人教版", graphiti_group_id="stu_grp_he_01", voiceprint_consent=True, camera_consent=True))
    if db.get(Student, "stu_le") is None:
        db.add(Student(id="stu_le", family_id="fam_a", name="乐乐", grade=3, textbook_version="北师大版", graphiti_group_id="stu_grp_le_02", voiceprint_consent=False, camera_consent=False))
    db.commit()

    ingest_dingtalk_batch(
        db,
        DingTalkBatchIn(
            family_id="fam_a",
            student_id="stu_he",
            conversation_id="conv_class_3_2_demo",
            authorized_teachers={"teacher_wang": "数学", "teacher_li": "语文", "teacher_chen": "英语"},
            page_complete=True,
            watermark_at=now,
            messages=[
                DingTalkMessageIn(
                    message_id="demo_msg_math",
                    sender_id="teacher_wang",
                    sender_name="王老师",
                    published_at=now - timedelta(hours=2),
                    subject="数学",
                    content="练习册第 24 页 1–5 题；第 6 题选做，明天交纸质。",
                    page_ref="第 24 页",
                    item_range="1–5 题；第 6 题选做",
                    submission_mode="paper_next_day",
                    due_at=now + timedelta(hours=14),
                ),
                DingTalkMessageIn(
                    message_id="demo_msg_zh",
                    sender_id="teacher_li",
                    sender_name="李老师",
                    published_at=now - timedelta(hours=2),
                    subject="语文",
                    content="读第 8 课，背诵第 2 段。",
                    page_ref="第 8 课",
                    item_range="背诵第 2 段",
                    submission_mode="unknown",
                    unclear_reason="背诵没写截止时间",
                ),
                DingTalkMessageIn(
                    message_id="demo_msg_en",
                    sender_id="teacher_chen",
                    sender_name="陈老师",
                    published_at=now - timedelta(hours=2),
                    subject="英语",
                    content="朗读 Unit 3 课文，钉钉家校本提交。",
                    page_ref="Unit 3",
                    item_range="课文朗读",
                    submission_mode="dingtalk_online",
                    due_at=now + timedelta(hours=2),
                ),
            ],
        ),
        now=now,
    )

    eng = db.scalar(select(Assignment).where(Assignment.source_message_id == "demo_msg_en"))
    if eng and not eng.completed:
        eng.completed = True
        db.commit()
        record_submission_evidence(db, family_id="fam_a", student_id="stu_he", assignment_id=eng.id, status="needs_parent", now=now)

    if db.get(GrowthRecord, "demo_gr_1") is None:
        db.add_all([
            GrowthRecord(id="demo_gr_1", family_id="fam_a", student_id="stu_he", category="knowledge", title="从画图提示到隔天独立解释", content="独立答出单位分数比较题并说明理由。", evidence="第一晚画图提示后解释，次日无提示独立解释，题库评分通过。", verified=True),
            GrowthRecord(id="demo_gr_2", family_id="fam_a", student_id="stu_he", category="habit", title="自己决定先做哪科", content="连续 3 个上学日由孩子自主开工。", evidence="来自设备开工记录，非家长提醒。", verified=True),
            GrowthRecord(id="demo_gr_3", family_id="fam_a", student_id="stu_he", category="teacher_praise", title="朗读流利", content="陈老师在班级群点名表扬英语朗读。", evidence="来源：班级群消息（原文已按 7 天规则清理），家长已确认。", verified=True),
        ])
        db.commit()

    existing_mem = list(db.scalars(select(LearnerMemory).where(LearnerMemory.student_id == "stu_he", LearnerMemory.active.is_(True))))
    if not existing_mem:
        write_learner_memory(db, family_id="fam_a", student_id="stu_he", kind="preference", summary="以后有数学就先做数学")
        write_learner_memory(db, family_id="fam_a", student_id="stu_he", kind="strategy", topic_id="unit_fraction_compare", summary="比较单位分数时，画两个一样大的整体有帮助")
        process_memory_outbox(db, graphiti=get_shared_graphiti(), now=now)

    return {"seeded": True, "family_id": "fam_a", "students": ["stu_he", "stu_le"]}
