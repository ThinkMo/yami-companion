from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.memory.service import write_learner_memory
from backend.models import Assignment, AssignmentRevision, DingTalkSyncState, GrowthRecord, Reminder, Student
from backend.reminders.service import dispatch_parent_dingtalk_alert
from backend.schemas import DingTalkBatchIn, DingTalkSyncResult


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ingest_dingtalk_batch(
    db: Session,
    batch: DingTalkBatchIn,
    *,
    now: Optional[datetime] = None,
) -> DingTalkSyncResult:
    """
    Bounded incremental DingTalk sync:
    - Enforces student belongs to family.
    - Auth/rate-limit failure preserves last trusted assignments and watermark.
    - Filters strictly by authorized teachers.
    - Handles idempotent overlap windows and teacher revisions.
    - Advances watermark ONLY when page_complete=True.
    """
    now = now or utcnow()
    student = db.get(Student, batch.student_id)
    if student is None or student.family_id != batch.family_id:
        raise PermissionError("student_outside_family")

    sync_state = db.scalar(
        select(DingTalkSyncState).where(
            DingTalkSyncState.family_id == batch.family_id,
            DingTalkSyncState.student_id == batch.student_id,
            DingTalkSyncState.conversation_id == batch.conversation_id,
        )
    )
    if sync_state is None:
        sync_state = DingTalkSyncState(
            family_id=batch.family_id,
            student_id=batch.student_id,
            conversation_id=batch.conversation_id,
        )
        db.add(sync_state)
        db.flush()

    if batch.auth_status != "ok":
        sync_state.status = batch.auth_status
        sync_state.last_error = f"sync_{batch.auth_status}"
        db.commit()
        if batch.auth_status == "auth_expired":
            dispatch_parent_dingtalk_alert(
                db,
                family_id=batch.family_id,
                event_key=f"auth_expired:{batch.conversation_id}:{now.strftime('%Y-%m-%d')}",
                kind="auth_expired",
                summary="钉钉班级群读取授权已失效，原清单仍可查看，请在家长网页重新授权。",
                now=now,
            )
        return DingTalkSyncResult(
            status=batch.auth_status,
            watermark_advanced=False,
            watermark_at=sync_state.watermark_at,
            created=0,
            revised=0,
            skipped_untrusted_sender=0,
            skipped_duplicate=0,
            pending_parent_confirm=0,
        )

    created = 0
    revised = 0
    skipped_untrusted = 0
    skipped_duplicate = 0
    pending_confirm = 0

    for msg in batch.messages:
        if msg.sender_id not in batch.authorized_teachers:
            skipped_untrusted += 1
            continue

        attachment_issues = [
            f"{a.kind}:{a.status}" for a in msg.attachments if a.status != "ok"
        ]
        attachment_warning = "; ".join(attachment_issues) if attachment_issues else None
        needs_confirm = bool(
            msg.unclear_reason
            or msg.submission_mode == "unknown"
            or (msg.due_at is None and msg.submission_mode != "none")
            or attachment_warning
        )

        # Case 1: Teacher revision of an existing assignment
        if msg.revises_message_id:
            target = db.scalar(
                select(Assignment).where(
                    Assignment.family_id == batch.family_id,
                    Assignment.student_id == batch.student_id,
                    Assignment.source_conversation_id == batch.conversation_id,
                    Assignment.source_message_id == msg.revises_message_id,
                )
            )
            if target is not None:
                already_applied = db.scalar(
                    select(AssignmentRevision).where(
                        AssignmentRevision.assignment_id == target.id,
                        AssignmentRevision.source_message_id == msg.message_id,
                    )
                )
                if already_applied is not None:
                    skipped_duplicate += 1
                    continue
                target.version += 1
                target.content = msg.content
                target.page_ref = msg.page_ref
                target.item_range = msg.item_range
                target.submission_mode = msg.submission_mode
                target.due_at = msg.due_at
                target.needs_parent_confirm = needs_confirm
                target.unclear_reason = msg.unclear_reason
                target.attachment_warning = attachment_warning
                db.add(
                    AssignmentRevision(
                        assignment_id=target.id,
                        version=target.version,
                        source_message_id=msg.message_id,
                        content=msg.content,
                        page_ref=msg.page_ref,
                        item_range=msg.item_range,
                        published_at=msg.published_at,
                    )
                )
                # Invalidate stale reminders tied to older versions of this assignment
                stale_rems = list(
                    db.scalars(
                        select(Reminder).where(
                            Reminder.assignment_id == target.id,
                            Reminder.status.in_(("candidate", "snoozed")),
                        )
                    )
                )
                for r in stale_rems:
                    r.status = "cancelled"
                revised += 1
                if needs_confirm:
                    pending_confirm += 1
                continue

        # Case 2: Duplicate message in overlap window
        existing = db.scalar(
            select(Assignment).where(
                Assignment.family_id == batch.family_id,
                Assignment.student_id == batch.student_id,
                Assignment.source_conversation_id == batch.conversation_id,
                Assignment.source_message_id == msg.message_id,
            )
        )
        if existing is not None:
            skipped_duplicate += 1
            continue

        # Case 3: New assignment
        expires_at = (msg.due_at or now) + timedelta(days=7)
        assignment = Assignment(
            id=f"asg_{uuid.uuid4().hex[:12]}",
            family_id=batch.family_id,
            student_id=batch.student_id,
            source_conversation_id=batch.conversation_id,
            source_message_id=msg.message_id,
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            subject=msg.subject or batch.authorized_teachers[msg.sender_id],
            content=msg.content,
            page_ref=msg.page_ref,
            item_range=msg.item_range,
            submission_mode=msg.submission_mode,
            due_at=msg.due_at,
            published_at=msg.published_at,
            version=1,
            needs_parent_confirm=needs_confirm,
            unclear_reason=msg.unclear_reason,
            attachment_warning=attachment_warning,
            expires_at=expires_at,
        )
        db.add(assignment)
        db.flush()
        db.add(
            AssignmentRevision(
                assignment_id=assignment.id,
                version=1,
                source_message_id=msg.message_id,
                content=msg.content,
                page_ref=msg.page_ref,
                item_range=msg.item_range,
                published_at=msg.published_at,
            )
        )
        created += 1
        if needs_confirm:
            pending_confirm += 1

    watermark_advanced = False
    sync_state.status = "ok"
    sync_state.last_error = None
    if batch.page_complete:
        sync_state.watermark_at = batch.watermark_at
        sync_state.last_success_at = now
        watermark_advanced = True

    db.commit()
    return DingTalkSyncResult(
        status="ok",
        watermark_advanced=watermark_advanced,
        watermark_at=sync_state.watermark_at,
        created=created,
        revised=revised,
        skipped_untrusted_sender=skipped_untrusted,
        skipped_duplicate=skipped_duplicate,
        pending_parent_confirm=pending_confirm,
    )


def parse_and_ingest_raw_class_message(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    raw_text: str,
    sender_name: str = "班主任",
    conversation_id: str = "conv_class_live",
    now: Optional[datetime] = None,
) -> tuple[DingTalkSyncResult, list[dict[str, str]]]:
    """
    Parse raw Chinese teacher homework message from a DingTalk class group
    (e.g. '1. 数学练习册P42第1-3题，明早交纸质；2. 语文背诵第12课；3. 英语朗读Unit4，钉钉打卡')
    into structured assignments and ingest idempotently via ingest_dingtalk_batch.
    """
    import hashlib
    import re
    from backend.schemas import DingTalkMessageIn

    now = now or utcnow()
    # Split on newlines, semicolons, or numbered items (1. / 2、 / （1）)
    cleaned = raw_text.strip()
    segments = re.split(r"[\n；;]+|(?=(?:^|\s)[1-9][\.、\)）])", cleaned)
    parsed_msgs: list[DingTalkMessageIn] = []
    preview: list[dict[str, str]] = []

    subject_patterns = [
        ("数学", ("数学", "口算", "计算", "练习册", "应用题")),
        ("语文", ("语文", "古诗", "背诵", "听写", "生字", "作文", "阅读")),
        ("英语", ("英语", "Unit", "unit", "单词", "课文朗读", "对话", "英语打卡")),
        ("科学", ("科学", "实验", "观察记录")),
    ]

    for seg in segments:
        line = re.sub(r"^[0-9]+[\.、\)）\s]+", "", seg.strip()).strip()
        if len(line) < 4:
            continue
        if any(skip in line for skip in ("各位家长", "大家好", "今日作业：", "今天作业：")) and len(line) < 16:
            continue
        # Strip leading announcement prefix if inline
        line = re.sub(r"^(?:各位家长好[，,：:\s]*|今天作业[：:\s]*|今日作业[：:\s]*)", "", line).strip()
        if len(line) < 3:
            continue

        subj = None
        for s_name, keys in subject_patterns:
            if any(k in line for k in keys):
                subj = s_name
                break
        if subj is None:
            continue

        page_m = re.search(r"([Pp页]\s*\d+(?:\s*[-–~]\s*\d+)?|第\s*\d+\s*页)", line)
        page_ref = page_m.group(1).replace(" ", "") if page_m else None

        if any(k in line for k in ("钉钉", "打卡", "线上", "群里交")):
            sub_mode = "dingtalk_online"
        elif any(k in line for k in ("拍照", "上传照片", "发图")):
            sub_mode = "photo_upload"
        elif any(k in line for k in ("不用交", "无需提交")):
            sub_mode = "none"
        else:
            sub_mode = "paper_next_day"

        due_at = now.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
        if "今晚" in line or "今天" in line:
            due_at = now.replace(hour=12, minute=30, second=0, microsecond=0)

        msg_hash = hashlib.sha1(f"{conversation_id}:{subj}:{line}".encode("utf-8")).hexdigest()[:12]
        parsed_msgs.append(
            DingTalkMessageIn(
                message_id=f"dt_raw_{msg_hash}",
                sender_id="teacher_class_auto",
                sender_name=sender_name,
                subject=subj,
                content=line,
                page_ref=page_ref,
                submission_mode=sub_mode,  # type: ignore[arg-type]
                due_at=due_at,
                published_at=now,
            )
        )
        preview.append({"subject": subj, "content": line, "page_ref": page_ref or "", "submission_mode": sub_mode})

    batch = DingTalkBatchIn(
        family_id=family_id,
        student_id=student_id,
        conversation_id=conversation_id,
        authorized_teachers={"teacher_class_auto": "综合"},
        messages=parsed_msgs,
        watermark_at=now,
        page_complete=True,
    )
    res = ingest_dingtalk_batch(db, batch, now=now)
    return res, preview


def _student_name_aliases(name: str) -> list[str]:
    clean = (name or "").strip()
    if not clean:
        return []
    aliases = [clean]
    if len(clean) >= 3:
        aliases.append(clean[-2:])
    return aliases


def extract_and_store_class_performance(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    raw_text: str,
    sender_name: str = "班主任",
    conversation_id: str = "conv_class_live",
    now: Optional[datetime] = None,
) -> list[dict[str, str]]:
    """
    Extract teacher praise and classroom performance feedback for `student_id` from raw
    DingTalk class-group messages, storing:
      1) GrowthRecord (verified=False for parent review in Guardian Growth timeline)
      2) LearnerMemory + MemoryOutbox (projected by yami-worker into Neo4j 5 Graphiti)
    Strictly avoids attributing a sibling's praise/feedback to the wrong student and
    deduplicates idempotently on deterministic SHA1 IDs.
    """
    import hashlib
    import re

    now = now or utcnow()
    student = db.get(Student, student_id)
    if student is None or student.family_id != family_id:
        raise PermissionError("student_outside_family")

    siblings = list(
        db.scalars(select(Student).where(Student.family_id == family_id, Student.id != student_id))
    )
    my_aliases = _student_name_aliases(student.name)
    other_aliases: list[str] = []
    for sib in siblings:
        other_aliases.extend(_student_name_aliases(sib.name))

    praise_keywords = ("表扬", "点赞", "优秀", "全对", "满分", "进步很大", "小红花", "发言积极", "画图清晰", "工整", "棒")
    feedback_keywords = ("粗心", "漏写", "写单位", "注意", "加强", "巩固", "不熟", "错题", "字迹", "大声", "速度慢", "走神")

    segments = re.split(r"[\n；;。！？!?]+", raw_text.strip())
    created_items: list[dict[str, str]] = []

    for seg in segments:
        line = re.sub(r"^[0-9]+[\.、\)）\s]+", "", seg.strip()).strip()
        if len(line) < 4:
            continue

        is_praise = any(k in line for k in praise_keywords)
        is_feedback = any(k in line for k in feedback_keywords)
        if not (is_praise or is_feedback):
            continue

        mentions_me = any(alias in line for alias in my_aliases)
        mentions_other = any(alias in line for alias in other_aliases)
        # Skip if explicitly about another sibling and not this student
        if mentions_other and not mentions_me:
            continue
        # For praise lists in a class group, require the student's name to be mentioned
        if is_praise and not mentions_me:
            continue

        category = "teacher_praise" if is_praise else ("knowledge" if any(k in line for k in ("错题", "分数", "计算", "单位", "公式", "朗读", "背诵")) else "habit")
        title = f"老师表扬：{line[:24]}" if is_praise else f"课堂反馈：{line[:24]}"
        gr_hash = hashlib.sha1(f"{family_id}:{student_id}:{conversation_id}:{category}:{line}".encode("utf-8")).hexdigest()[:12]
        gr_id = f"gr_dt_{gr_hash}"

        if db.get(GrowthRecord, gr_id) is not None:
            continue

        gr = GrowthRecord(
            id=gr_id,
            family_id=family_id,
            student_id=student_id,
            category=category,
            topic_id="class_performance",
            title=title,
            content=line,
            evidence=f"钉钉班级群（{sender_name}）",
            source_kind="dingtalk_class_group",
            verified=False,  # Awaits parent confirmation per P09
            created_at=now,
        )
        db.add(gr)
        db.commit()

        mem_prefix = "老师表扬" if is_praise else "老师课堂反馈"
        mem = write_learner_memory(
            db,
            family_id=family_id,
            student_id=student_id,
            kind="strategy",
            topic_id="class_performance",
            summary=f"{mem_prefix}：{line}",
            source_event_id=gr_id,
        )
        created_items.append(
            {
                "growth_record_id": gr_id,
                "memory_id": mem.id,
                "category": category,
                "summary": f"{mem_prefix}：{line}",
            }
        )

    return created_items
