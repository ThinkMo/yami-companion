from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.learning.state_machine import (
    complete_assignment_with_checks,
    confirm_echo_and_get_hint,
    get_preview_prompt,
    open_yesterday_hint,
    record_child_attempt,
    request_help_confirm_question,
    request_next_hint,
    return_to_paper,
    schedule_next_day_review,
    start_task_session,
    submit_review_attempt,
)
from backend.models import Assignment


def _seed_math_assignment(db_session, student_id: str = "stu_he") -> Assignment:
    now = datetime(2026, 11, 18, 19, 0, tzinfo=timezone.utc)
    asg = Assignment(
        id=f"asg_math_{student_id}",
        family_id="fam_a",
        student_id=student_id,
        source_conversation_id="conv_1",
        source_message_id=f"msg_math_{student_id}",
        sender_id="teacher_wang",
        sender_name="王老师",
        subject="数学",
        content="练习册第 24 页 1–5 题；第 6 题选做",
        page_ref="第 24 页",
        item_range="1–5 题；第 6 题选做",
        submission_mode="paper_next_day",
        published_at=now,
        expires_at=now + timedelta(days=7),
    )
    db_session.add(asg)
    db_session.commit()
    return asg


def test_full_tutoring_state_machine_and_check_guard(db_session):
    asg = _seed_math_assignment(db_session)

    # 1. Start task -> eligible for seed
    step = start_task_session(db_session, family_id="fam_a", student_id="stu_he", assignment_id=asg.id)
    assert step.stage == "writing" and step.award_eligible_event == "task_started"

    # 2. Confirm question (unknown problem returns honest fallback)
    unk = request_help_confirm_question(db_session, study_session_id=step.session_id, problem_id="unknown_p")
    assert unk.stage == "confirm_question" and "资料还不全" in unk.spoken_text

    # 3. Valid problem -> ask child attempt first; cannot jump straight to hint
    q = request_help_confirm_question(
        db_session, study_session_id=step.session_id, problem_id="math_g4_unit_fraction_v1", via_photo=True
    )
    assert q.stage == "child_attempt" and "照片" in q.spoken_text

    with pytest.raises(ValueError, match="must_confirm_attempt_before_hints"):
        request_next_hint(db_session, study_session_id=step.session_id)

    # 4. Child says attempt -> Yami echoes -> child rejects -> retries -> confirms -> gets hint 1 & wood event
    echo = record_child_attempt(db_session, study_session_id=step.session_id, attempt_text="我不知道 1/6 和 1/8 哪个大")
    assert echo.stage == "echo_confirm"

    retry = confirm_echo_and_get_hint(db_session, study_session_id=step.session_id, confirmed=False)
    assert retry.stage == "child_attempt"

    record_child_attempt(db_session, study_session_id=step.session_id, attempt_text="分子都是 1，不知道哪个大")
    h1 = confirm_echo_and_get_hint(db_session, study_session_id=step.session_id, confirmed=True)
    assert h1.stage == "hint_1" and h1.hint_level == 1 and h1.award_eligible_event == "stuck_explained"

    # 5. Progressive hints 2 and 3, capped at 3
    h2 = request_next_hint(db_session, study_session_id=step.session_id)
    assert h2.stage == "hint_2" and h2.hint_level == 2 and h2.can_advance_hint is True
    h3 = request_next_hint(db_session, study_session_id=step.session_id)
    assert h3.stage == "hint_3" and h3.hint_level == 3 and h3.can_advance_hint is False

    # 6. Return to paper & complete requires at least one check item
    back = return_to_paper(db_session, study_session_id=step.session_id)
    assert back.stage == "writing"

    with pytest.raises(ValueError, match="at_least_one_check_required"):
        complete_assignment_with_checks(db_session, study_session_id=step.session_id, checks=[])

    done = complete_assignment_with_checks(db_session, study_session_id=step.session_id, checks=["miss"])
    assert done.stage == "done" and done.award_eligible_event == "check_confirmed"
    db_session.refresh(asg)
    assert asg.completed is True

    # 7. Preview prompt carries explicit teacher source
    prev = get_preview_prompt("math_g4_area_preview_v1")
    assert prev is not None and "王老师" in prev.source_label


def test_next_day_review_three_branches_and_skip(db_session):
    due = datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc)

    # Branch 1: Independent pass (no yesterday hint opened) -> unlocks method card + verified growth
    r1 = schedule_next_day_review(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        problem_id="math_g4_unit_fraction_v1",
        method_note="先看整体一样大",
        due_at=due,
    )
    out1 = submit_review_attempt(
        db_session,
        review_id=r1.id,
        utterance="1/5 更大，因为两个蛋糕一样大，分成 5 份每份比分成 7 份大。",
    )
    assert out1.status == "independent_pass"
    assert out1.unlock_method_card_id == "card_same_whole"
    assert out1.verified_growth_eligible is True

    # Branch 2: Opened yesterday hint before answering -> hinted_pass, no method card, not verified independent
    r2 = schedule_next_day_review(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        problem_id="math_g4_unit_fraction_v1",
        method_note="先看整体一样大",
        due_at=due,
    )
    note = open_yesterday_hint(db_session, review_id=r2.id)
    assert note == "先看整体一样大"
    out2 = submit_review_attempt(
        db_session,
        review_id=r2.id,
        utterance="1/5 大，蛋糕一样大，分成 5 份更大。",
    )
    assert out2.status == "hinted_pass"
    assert out2.unlock_method_card_id is None
    assert out2.verified_growth_eligible is False

    # Branch 3: Answer without reasoning -> pending_verify
    r3 = schedule_next_day_review(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        problem_id="math_g4_unit_fraction_v1",
        method_note="先看整体一样大",
        due_at=due,
    )
    out3 = submit_review_attempt(db_session, review_id=r3.id, utterance="1/5 大")
    assert out3.status == "pending_verify"
    assert out3.unlock_method_card_id is None
    assert out3.verified_growth_eligible is False

    # Branch 4: Child can skip optional review
    r4 = schedule_next_day_review(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        problem_id="math_g4_unit_fraction_v1",
        method_note="先看整体一样大",
        due_at=due,
    )
    out4 = submit_review_attempt(db_session, review_id=r4.id, utterance="", skip=True)
    assert out4.status == "skipped"
