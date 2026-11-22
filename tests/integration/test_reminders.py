from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.models import Assignment, Family
from backend.reminders.service import (
    ack_reminder_delivered,
    dispatch_parent_dingtalk_alert,
    evaluate_reminder_delivery,
    schedule_or_reschedule_reminder,
)
from backend.schemas import DingTalkBatchIn, DingTalkMessageIn
from backend.services.dingtalk_sync import ingest_dingtalk_batch


def test_reminder_lifecycle_reschedule_quiet_hours_mismatch_expiry_and_revision(db_session):
    base = datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc)  # 19:00 UTC+8
    asg = Assignment(
        id="asg_rem_1",
        family_id="fam_a",
        student_id="stu_he",
        source_conversation_id="conv_1",
        source_message_id="msg_rem_1",
        sender_id="teacher_wang",
        sender_name="王老师",
        subject="数学",
        content="第 24 页 1–6 题",
        submission_mode="paper_next_day",
        published_at=base,
        version=1,
        expires_at=base + timedelta(days=7),
    )
    db_session.add(asg)
    db_session.commit()

    # 1. Schedule + reschedule same idempotency_key -> single reminder, no double ring
    rem1, echo1 = schedule_or_reschedule_reminder(
        db_session,
        idempotency_key="rem:stu_he:check:asg_rem_1",
        family_id="fam_a",
        student_id="stu_he",
        kind="check",
        anchor_label="检查数学有没有漏题",
        scheduled_for=base + timedelta(minutes=10),
        ttl_minutes=30,
        assignment_id=asg.id,
    )
    assert "19:10" in echo1

    rem2, echo2 = schedule_or_reschedule_reminder(
        db_session,
        idempotency_key="rem:stu_he:check:asg_rem_1",
        family_id="fam_a",
        student_id="stu_he",
        kind="check",
        anchor_label="检查数学有没有漏题",
        scheduled_for=base + timedelta(minutes=20),
        ttl_minutes=30,
        assignment_id=asg.id,
    )
    assert rem2.id == rem1.id and rem2.status == "snoozed" and "19:20" in echo2

    due_time = base + timedelta(minutes=21)  # 19:21 UTC+8

    # 2. Wrong student (乐乐) on device -> NEVER leaks private task, only generic greeting
    wrong = evaluate_reminder_delivery(db_session, reminder_id=rem1.id, current_student_id="stu_le", now=due_time)
    assert wrong.deliver is True and wrong.mode == "generic_greeting"
    assert "数学" not in wrong.spoken_text

    # 3. Device quiet or interacting -> skip
    busy = evaluate_reminder_delivery(
        db_session, reminder_id=rem1.id, current_student_id="stu_he", is_interacting=True, now=due_time
    )
    assert busy.deliver is False and busy.reason == "device_busy_or_quiet"

    # 4. Quiet hours (20:45 UTC+8 = 12:45 UTC) -> skip
    quiet_rem, _ = schedule_or_reschedule_reminder(
        db_session,
        idempotency_key="rem:stu_he:quiet_test",
        family_id="fam_a",
        student_id="stu_he",
        kind="review",
        anchor_label="试一道分数题",
        scheduled_for=datetime(2026, 11, 18, 12, 40, tzinfo=timezone.utc),
        ttl_minutes=30,
    )
    q_res = evaluate_reminder_delivery(
        db_session,
        reminder_id=quiet_rem.id,
        current_student_id="stu_he",
        now=datetime(2026, 11, 18, 12, 45, tzinfo=timezone.utc),
    )
    assert q_res.deliver is False and q_res.reason == "family_quiet_hours"

    # 5. Correct student & valid window -> deliver private voice, ack is idempotent, second check skips
    ok = evaluate_reminder_delivery(db_session, reminder_id=rem1.id, current_student_id="stu_he", now=due_time)
    assert ok.deliver is True and ok.mode == "private_voice" and "检查数学有没有漏题" in ok.spoken_text

    ack_reminder_delivered(db_session, reminder_id=rem1.id, now=due_time)
    ack_reminder_delivered(db_session, reminder_id=rem1.id, now=due_time + timedelta(seconds=5))
    again = evaluate_reminder_delivery(db_session, reminder_id=rem1.id, current_student_id="stu_he", now=due_time + timedelta(seconds=10))
    assert again.deliver is False and again.reason == "already_delivered"

    # 6. Offline reconnect after expiry -> expired, NEVER catch-up plays
    exp_rem, _ = schedule_or_reschedule_reminder(
        db_session,
        idempotency_key="rem:stu_he:exp_test",
        family_id="fam_a",
        student_id="stu_he",
        kind="review",
        anchor_label="过期复练",
        scheduled_for=base + timedelta(minutes=5),
        ttl_minutes=10,
    )
    exp_res = evaluate_reminder_delivery(
        db_session, reminder_id=exp_rem.id, current_student_id="stu_he", now=base + timedelta(minutes=25)
    )
    assert exp_res.deliver is False and exp_res.reason == "expired_no_catchup"

    # 7. Teacher revises assignment -> pending reminder on old version is automatically cancelled
    rev_rem, _ = schedule_or_reschedule_reminder(
        db_session,
        idempotency_key="rem:stu_he:rev_test",
        family_id="fam_a",
        student_id="stu_he",
        kind="check",
        anchor_label="检查旧版数学",
        scheduled_for=base + timedelta(minutes=30),
        ttl_minutes=30,
        assignment_id=asg.id,
    )
    ingest_dingtalk_batch(
        db_session,
        DingTalkBatchIn(
            family_id="fam_a",
            student_id="stu_he",
            conversation_id="conv_1",
            authorized_teachers={"teacher_wang": "数学"},
            page_complete=True,
            watermark_at=base + timedelta(minutes=15),
            messages=[
                DingTalkMessageIn(
                    message_id="msg_rem_2",
                    revises_message_id="msg_rem_1",
                    sender_id="teacher_wang",
                    sender_name="王老师",
                    published_at=base + timedelta(minutes=15),
                    subject="数学",
                    content="更正：第 6 题选做",
                    submission_mode="paper_next_day",
                )
            ],
        ),
        now=base + timedelta(minutes=15),
    )
    rev_res = evaluate_reminder_delivery(
        db_session, reminder_id=rev_rem.id, current_student_id="stu_he", now=base + timedelta(minutes=31)
    )
    assert rev_res.deliver is False and rev_res.reason == "already_cancelled"


def test_parent_dingtalk_alert_defers_in_quiet_hours(db_session):
    fam = db_session.get(Family, "fam_a")
    fam.dingtalk_webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=demo_family_bot"
    db_session.commit()

    quiet_now = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)  # 21:00 UTC+8
    notif = dispatch_parent_dingtalk_alert(
        db_session,
        family_id="fam_a",
        event_key="unclear:asg_1",
        kind="unclear_assignment",
        summary="语文背诵未写截止时间，请确认。",
        now=quiet_now,
    )
    assert notif.status == "deferred_quiet"
