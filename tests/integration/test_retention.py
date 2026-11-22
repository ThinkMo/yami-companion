from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.memory.service import (
    delete_growth_record_and_revoke_cards,
    revoke_camera_consent_and_purge_photos,
    run_retention_cleanup,
)
from backend.models import Assignment, GrowthRecord, LearningEvent, MethodCardUnlock, SubmissionEvidence
from backend.rewards.service import list_method_cards, unlock_method_card


def test_retention_expiry_preserves_growth_and_revokes_photos_and_cards(client, db_session):
    day1 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

    asg = Assignment(
        id="asg_ret_1",
        family_id="fam_a",
        student_id="stu_he",
        source_conversation_id="conv_1",
        source_message_id="msg_ret_1",
        sender_id="teacher_wang",
        subject="数学",
        content="第 24 页 1–5 题",
        submission_mode="paper_next_day",
        published_at=day1,
        expires_at=day1 + timedelta(days=7),
    )
    db_session.add(asg)
    db_session.flush()

    ev = SubmissionEvidence(
        assignment_id=asg.id,
        assignment_version=1,
        student_id="stu_he",
        status="photo",
        source_rank=20,
        photo_object_key="photos/fam_a/stu_he/submission/proof.jpg",
        photo_expires_at=day1 + timedelta(days=7),
        created_at=day1,
    )
    le = LearningEvent(
        id="ev_ret_1",
        family_id="fam_a",
        student_id="stu_he",
        event_type="stuck_explained",
        summary="画图提示有效",
        expires_at=day1 + timedelta(days=30),
        created_at=day1,
    )
    gr = GrowthRecord(
        id="gr_ret_1",
        family_id="fam_a",
        student_id="stu_he",
        category="knowledge",
        topic_id="unit_fraction_compare",
        title="从画图提示到隔天独立解释",
        content="独立答出单位分数比较题并说明理由。",
        evidence="第一晚画图提示后解释，次日无提示独立解释，题库评分通过。",
        verified=True,
        created_at=day1 + timedelta(days=1),
    )
    db_session.add_all([ev, le, gr])
    db_session.commit()

    unlock_method_card(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        card_id="card_same_whole",
        front="先看整体",
        back="先确认两个整体一样大。",
        growth_record_id=gr.id,
    )

    # Advance clock +35 days: assignment, 7d photo, and 30d learning event all expire; GrowthRecord survives!
    stats = run_retention_cleanup(db_session, now=day1 + timedelta(days=35))
    assert stats["expired_assignments"] == 1
    assert stats["expired_learning_events"] == 1

    # Guardian growth API still explains growth after raw sources expired
    growth_list = client.get("/v1/guardian/students/stu_he/growth", headers={"X-Family-Id": "fam_a"}).json()
    assert len(growth_list) == 1 and growth_list[0]["id"] == "gr_ret_1"

    # Deleting growth record revokes dependent method card
    del_resp = client.delete("/v1/guardian/students/stu_he/growth/gr_ret_1", headers={"X-Family-Id": "fam_a"}).json()
    assert del_resp["deleted"] is True and del_resp["revoked_cards"] == 1
    assert list_method_cards(db_session, family_id="fam_a", student_id="stu_he") == []

    # Revoking camera consent immediately purges any stored photo references
    asg2 = Assignment(
        id="asg_ret_2",
        family_id="fam_a",
        student_id="stu_he",
        source_conversation_id="conv_1",
        source_message_id="msg_ret_2",
        sender_id="teacher_chen",
        subject="英语",
        content="朗读",
        submission_mode="dingtalk_online",
        published_at=day1,
        expires_at=day1 + timedelta(days=60),
    )
    db_session.add(asg2)
    db_session.flush()
    db_session.add(
        SubmissionEvidence(
            assignment_id=asg2.id,
            assignment_version=1,
            student_id="stu_he",
            status="photo",
            source_rank=20,
            photo_object_key="photos/fam_a/stu_he/submission/proof2.jpg",
            photo_expires_at=day1 + timedelta(days=42),
            created_at=day1,
        )
    )
    db_session.commit()

    purged = revoke_camera_consent_and_purge_photos(db_session, family_id="fam_a", student_id="stu_he")
    assert purged == 1
