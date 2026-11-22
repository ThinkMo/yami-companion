from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.models import Assignment, Family, ParentNotification
from backend.services.identity import claim_pairing_code, issue_pairing_code
from backend.submissions.service import (
    can_prompt_submission_today,
    issue_photo_upload_slot,
    log_submission_prompt,
    record_submission_evidence,
)


def _seed_english_assignment(db_session) -> Assignment:
    now = datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc)  # 19:00 UTC+8 (not quiet hours)
    fam = db_session.get(Family, "fam_a")
    fam.dingtalk_webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=demo_family_bot"
    asg = Assignment(
        id="asg_eng_he",
        family_id="fam_a",
        student_id="stu_he",
        source_conversation_id="conv_1",
        source_message_id="msg_eng_1",
        sender_id="teacher_chen",
        sender_name="陈老师",
        subject="英语",
        content="朗读 Unit 3 课文，在钉钉家校本提交",
        submission_mode="dingtalk_online",
        published_at=now,
        completed=True,
        expires_at=now + timedelta(days=7),
    )
    db_session.add(asg)
    db_session.commit()
    return asg


def test_submission_provenance_priority_daily_cap_and_parent_alert(db_session):
    asg = _seed_english_assignment(db_session)
    now = datetime(2026, 11, 18, 11, 15, tzinfo=timezone.utc)  # 19:15 UTC+8

    # Daily prompt cap: max 2 prompts per study day
    assert can_prompt_submission_today(db_session, student_id="stu_he", assignment_id=asg.id, study_day="2026-11-18") is True
    log_submission_prompt(db_session, student_id="stu_he", assignment_id=asg.id, study_day="2026-11-18", now=now)
    assert can_prompt_submission_today(db_session, student_id="stu_he", assignment_id=asg.id, study_day="2026-11-18") is True
    log_submission_prompt(db_session, student_id="stu_he", assignment_id=asg.id, study_day="2026-11-18", now=now)
    assert can_prompt_submission_today(db_session, student_id="stu_he", assignment_id=asg.id, study_day="2026-11-18") is False

    # 1. Child asks parent to submit -> triggers idempotent family DingTalk alert
    _, st1 = record_submission_evidence(
        db_session, family_id="fam_a", student_id="stu_he", assignment_id=asg.id, status="needs_parent", now=now
    )
    assert st1 == "needs_parent"
    # Second needs_parent on same version does NOT duplicate parent alert
    record_submission_evidence(
        db_session, family_id="fam_a", student_id="stu_he", assignment_id=asg.id, status="needs_parent", now=now + timedelta(minutes=1)
    )
    notifs = list(db_session.scalars(select(ParentNotification).where(ParentNotification.family_id == "fam_a")))
    assert len(notifs) == 1
    assert notifs[0].kind == "needs_parent_submit" and notifs[0].status == "sent"

    # 2. Photo evidence overrides child self-report (rank 20 > rank 10)
    ev_photo, st2 = record_submission_evidence(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        assignment_id=asg.id,
        status="photo",
        photo_object_key="photos/fam_a/stu_he/submission/abc.jpg",
        now=now + timedelta(minutes=10),
    )
    assert st2 == "photo"
    assert ev_photo.photo_expires_at is not None

    # 3. Parent confirmed (rank 30) overrides photo, and later child 'not_yet' (rank 10) cannot downgrade parent_confirmed
    _, st3 = record_submission_evidence(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        assignment_id=asg.id,
        status="parent_confirmed",
        now=now + timedelta(minutes=12),
    )
    assert st3 == "parent_confirmed"

    _, st4 = record_submission_evidence(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        assignment_id=asg.id,
        status="not_yet",
        now=now + timedelta(minutes=15),
    )
    assert st4 == "parent_confirmed"


def test_camera_slot_requires_hardware_and_consent(db_session):
    # Device without camera -> blocked
    dev_no_cam, p1 = issue_pairing_code(db_session, hardware_uid="mosaico-nocam", has_camera=False)
    dev_no_cam = claim_pairing_code(db_session, family_id="fam_a", code=p1.code)
    with pytest.raises(PermissionError, match="camera_hardware_unavailable"):
        issue_photo_upload_slot(db_session, device=dev_no_cam, student_id="stu_he", purpose="submission")

    # Device with camera, student without consent (stu_le) -> blocked
    dev_cam, p2 = issue_pairing_code(db_session, hardware_uid="mosaico-cam", has_camera=True)
    dev_cam = claim_pairing_code(db_session, family_id="fam_a", code=p2.code)
    with pytest.raises(PermissionError, match="camera_consent_required"):
        issue_photo_upload_slot(db_session, device=dev_cam, student_id="stu_le", purpose="submission")

    # Device with camera + consented student (stu_he) -> succeeds with 7d submission / 24h question retention
    sub_slot = issue_photo_upload_slot(db_session, device=dev_cam, student_id="stu_he", purpose="submission")
    assert sub_slot.allowed_mime == "image/jpeg" and sub_slot.retention_hours == 24 * 7

    q_slot = issue_photo_upload_slot(db_session, device=dev_cam, student_id="stu_he", purpose="question")
    assert q_slot.retention_hours == 24
