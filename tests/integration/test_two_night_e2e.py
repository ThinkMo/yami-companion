from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.learning.dictation import complete_dictation_self_check, create_dictation_session, dictation_action
from backend.learning.state_machine import (
    complete_assignment_with_checks,
    confirm_echo_and_get_hint,
    record_child_attempt,
    request_help_confirm_question,
    return_to_paper,
    schedule_next_day_review,
    start_task_session,
    submit_review_attempt,
)
from backend.memory.graphiti_client import InMemoryGraphitiStore
from backend.memory.service import process_memory_outbox, recall_for_turn, write_learner_memory
from backend.models import GrowthRecord
from backend.reminders.service import ack_reminder_delivered, evaluate_reminder_delivery, schedule_or_reschedule_reminder
from backend.rewards.pet_service import TASK_COIN_REWARDS, get_or_create_pet, grant_pet_coins
from backend.rewards.service import list_method_cards, unlock_method_card
from backend.submissions.service import issue_photo_upload_slot, record_submission_evidence


def test_two_night_end_to_end_story_and_guardian_web(client, db_session):
    """Full Two-Night Demo (product.md) + Guardian Web HTML & live API endpoints."""
    night1 = datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc)  # 19:00 UTC+8
    night2 = datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc)  # next evening
    graphiti = InMemoryGraphitiStore()

    # Guardian web page is served cleanly
    web_resp = client.get("/guardian")
    assert web_resp.status_code == 200 and "芽米学习伙伴" in web_resp.text

    # Seed & sync via DingTalk batch (with teacher correction)
    seed_resp = client.post("/v1/guardian/dev/seed-demo")
    assert seed_resp.status_code == 200

    # Pair Mosaico device with camera to fam_a
    code = client.post("/v1/pairing/issue", json={"hardware_uid": "mosaico-e2e-01", "has_camera": True}).json()["pairing_code"]
    tok = client.post("/v1/pairing/claim", json={"family_id": "fam_a", "pairing_code": code}).json()["device_token"]

    # Night 1: 小禾 wakes & selects math
    sel_he = client.post("/v1/devices/select-student", headers={"X-Device-Token": tok}, json={"student_id": "stu_he", "method": "manual"}).json()
    assert sel_he["confirmed"] is True

    today_he = client.get("/v1/guardian/students/stu_he/today", headers={"X-Family-Id": "fam_a"}).json()
    math_asg = next(a for a in today_he["assignments"] if a["subject"] == "数学")
    eng_asg = next(a for a in today_he["assignments"] if a["subject"] == "英语")

    # Start math -> tutoring -> complete with checks -> earn Pet House coins
    st = start_task_session(db_session, family_id="fam_a", student_id="stu_he", assignment_id=math_asg["id"], now=night1)
    request_help_confirm_question(db_session, study_session_id=st.session_id, problem_id="math_g4_unit_fraction_v1")
    record_child_attempt(db_session, study_session_id=st.session_id, attempt_text="不知道 1/6 和 1/8 哪个大")
    confirm_echo_and_get_hint(db_session, study_session_id=st.session_id, confirmed=True, now=night1)
    return_to_paper(db_session, study_session_id=st.session_id)
    complete_assignment_with_checks(db_session, study_session_id=st.session_id, checks=["miss"], now=night1)
    pet_before = get_or_create_pet(db_session, family_id="fam_a", student_id="stu_he").coins
    pet_after = grant_pet_coins(db_session, family_id="fam_a", student_id="stu_he", amount=TASK_COIN_REWARDS["homework_complete"], reason="完成数学作业")
    assert pet_after.coins == pet_before + 20

    # Save long-term preference + strategy to PG + Graphiti & schedule next-day review
    write_learner_memory(db_session, family_id="fam_a", student_id="stu_he", kind="preference", summary="以后有数学就先做数学")
    write_learner_memory(db_session, family_id="fam_a", student_id="stu_he", kind="strategy", topic_id="unit_fraction_compare", summary="画两个一样大的整体")
    process_memory_outbox(db_session, graphiti=graphiti, now=night1)
    rev = schedule_next_day_review(db_session, family_id="fam_a", student_id="stu_he", problem_id="math_g4_unit_fraction_v1", method_note="先看整体一样大", due_at=night2)

    # Parent initiates Chinese dictation, child finishes & misses 1 word
    ds = create_dictation_session(db_session, family_id="fam_a", student_id="stu_he", subject="语文", source="parent_dingtalk", title="第 8 课听写", words=[("观察", "观察植物"), ("空隙", "叶柄空隙")])
    assert "观察" not in dictation_action(db_session, session_id=ds.id, action="current").screen_text
    dictation_action(db_session, session_id=ds.id, action="next")
    dictation_action(db_session, session_id=ds.id, action="next")
    _, queued = complete_dictation_self_check(db_session, session_id=ds.id, missed_words=["空隙"], now=night1)
    assert len(queued) == 1

    # English submission reminder + photo proof
    rem, _ = schedule_or_reschedule_reminder(db_session, idempotency_key="e2e:eng:sub", family_id="fam_a", student_id="stu_he", kind="submission", anchor_label="检查英语朗读有没有交", scheduled_for=night1 + timedelta(minutes=10), assignment_id=eng_asg["id"])
    dec = evaluate_reminder_delivery(db_session, reminder_id=rem.id, current_student_id="stu_he", now=night1 + timedelta(minutes=11))
    assert dec.deliver is True and dec.mode == "private_voice"
    ack_reminder_delivered(db_session, reminder_id=rem.id, now=night1 + timedelta(minutes=11))

    from backend.services.identity import authenticate_device
    dev = authenticate_device(db_session, tok)
    slot = issue_photo_upload_slot(db_session, device=dev, student_id="stu_he", purpose="submission", now=night1 + timedelta(minutes=12))
    _, sub_status = record_submission_evidence(db_session, family_id="fam_a", student_id="stu_he", assignment_id=eng_asg["id"], status="photo", photo_object_key=slot.object_key, now=night1 + timedelta(minutes=12))
    assert sub_status == "photo"

    # Night 2: 乐乐 wakes first (isolated), then 小禾 returns and passes independent review
    client.post("/v1/devices/select-student", headers={"X-Device-Token": tok}, json={"student_id": "stu_le", "method": "manual"})
    assert recall_for_turn(db_session, family_id="fam_a", student_id="stu_le", intent="start", graphiti=graphiti) == []

    client.post("/v1/devices/select-student", headers={"X-Device-Token": tok}, json={"student_id": "stu_he", "method": "manual"})
    recalled_review = recall_for_turn(db_session, family_id="fam_a", student_id="stu_he", intent="review", topic_id="unit_fraction_compare", graphiti=graphiti, is_independent_review_first_attempt=True)
    assert all(m.kind != "strategy" for m in recalled_review)

    outcome = submit_review_attempt(db_session, review_id=rev.id, utterance="1/5 更大，因为两个蛋糕一样大，分成 5 份每份更大。", now=night2)
    assert outcome.status == "independent_pass" and outcome.verified_growth_eligible is True
    gr = GrowthRecord(id="gr_e2e_n2", family_id="fam_a", student_id="stu_he", category="knowledge", title="次日独立解释单位分数", content="无提示说明理由", evidence="题库评分通过", verified=True)
    db_session.add(gr)
    db_session.commit()
    unlock_method_card(db_session, family_id="fam_a", student_id="stu_he", card_id="card_same_whole", front="先看整体", back="先确认两个整体一样大。", growth_record_id=gr.id)
    assert len(list_method_cards(db_session, family_id="fam_a", student_id="stu_he")) == 1
