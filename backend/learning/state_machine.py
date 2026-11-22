from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
import uuid

from sqlalchemy.orm import Session

from backend.learning.problem_bank import (
    PreviewPrompt,
    PREVIEW_PROMPTS,
    get_problem,
    grade_review_explanation,
)
from backend.middleware.safety import SafetyMiddleware
from backend.models import Assignment, LearningEvent, ReviewItem, StudySession

safety_middleware = SafetyMiddleware()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TutoringStepOut:
    session_id: str
    stage: str
    hint_level: int
    spoken_text: str
    can_advance_hint: bool
    award_eligible_event: Optional[str] = None  # task_started | stuck_explained | check_confirmed


@dataclass(frozen=True)
class ReviewOutcomeOut:
    review_id: str
    status: str  # independent_pass | hinted_pass | pending_verify | skipped
    unlock_method_card_id: Optional[str]
    verified_growth_eligible: bool
    spoken_text: str


def start_task_session(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    assignment_id: str,
    now: Optional[datetime] = None,
) -> TutoringStepOut:
    now = now or utcnow()
    asg = db.get(Assignment, assignment_id)
    if asg is None or asg.family_id != family_id or asg.student_id != student_id:
        raise PermissionError("assignment_outside_student_scope")

    sess = StudySession(
        id=f"study_{uuid.uuid4().hex[:12]}",
        family_id=family_id,
        student_id=student_id,
        assignment_id=asg.id,
        assignment_version=asg.version,
        stage="writing",
        hint_level=0,
        expires_at=now + timedelta(hours=24),
    )
    db.add(sess)
    db.add(
        LearningEvent(
            id=f"ev_{uuid.uuid4().hex[:12]}",
            family_id=family_id,
            student_id=student_id,
            assignment_id=asg.id,
            event_type="task_started",
            summary=f"孩子自主选择并开始{asg.subject}",
            expires_at=now + timedelta(days=30),
        )
    )
    db.commit()
    return TutoringStepOut(
        session_id=sess.id,
        stage=sess.stage,
        hint_level=0,
        spoken_text=f"好，我们先做{asg.subject}。你先在纸上写，卡住了就叫我。",
        can_advance_hint=False,
        award_eligible_event="task_started",
    )


def request_help_confirm_question(
    db: Session,
    *,
    study_session_id: str,
    problem_id: str,
    via_photo: bool = False,
) -> TutoringStepOut:
    sess = db.get(StudySession, study_session_id)
    if sess is None:
        raise ValueError("study_session_not_found")
    prob = get_problem(problem_id)
    if prob is None:
        return TutoringStepOut(
            session_id=sess.id,
            stage="confirm_question",
            hint_level=0,
            spoken_text="这道题的资料还不全，请把题目完整读给我听，或先问问老师。",
            can_advance_hint=False,
        )
    sess.problem_id = prob.problem_id
    sess.stage = "child_attempt"
    sess.hint_level = 0
    sess.echo_confirmed = False
    db.commit()
    prefix = "我看照片识别出这道题，确认一下：" if via_photo else ""
    return TutoringStepOut(
        session_id=sess.id,
        stage=sess.stage,
        hint_level=0,
        spoken_text=f"{prefix}{prob.question_text}。你先说说试到哪一步了？",
        can_advance_hint=False,
    )


def record_child_attempt(
    db: Session,
    *,
    study_session_id: str,
    attempt_text: str,
) -> TutoringStepOut:
    sess = db.get(StudySession, study_session_id)
    if sess is None or sess.stage not in ("child_attempt", "echo_confirm"):
        raise ValueError("invalid_tutoring_stage")
    check = safety_middleware.check_input(attempt_text)
    if not check.allowed:
        return TutoringStepOut(
            session_id=sess.id,
            stage=sess.stage,
            hint_level=sess.hint_level,
            spoken_text=check.replacement_text,
            can_advance_hint=False,
        )
    sess.child_attempt_text = attempt_text.strip()
    sess.stage = "echo_confirm"
    db.commit()
    return TutoringStepOut(
        session_id=sess.id,
        stage=sess.stage,
        hint_level=sess.hint_level,
        spoken_text=f"你是说「{sess.child_attempt_text}」，我听对了吗？",
        can_advance_hint=False,
    )


def confirm_echo_and_get_hint(
    db: Session,
    *,
    study_session_id: str,
    confirmed: bool,
    now: Optional[datetime] = None,
) -> TutoringStepOut:
    now = now or utcnow()
    sess = db.get(StudySession, study_session_id)
    if sess is None or sess.stage != "echo_confirm":
        raise ValueError("invalid_tutoring_stage")
    if not confirmed:
        sess.stage = "child_attempt"
        db.commit()
        return TutoringStepOut(
            session_id=sess.id,
            stage=sess.stage,
            hint_level=0,
            spoken_text="那你再说一次，你卡在哪一步？",
            can_advance_hint=False,
        )

    prob = get_problem(sess.problem_id or "")
    if prob is None:
        raise ValueError("problem_not_bound")

    sess.echo_confirmed = True
    sess.hint_level = 1
    sess.stage = "hint_1"
    db.add(
        LearningEvent(
            id=f"ev_{uuid.uuid4().hex[:12]}",
            family_id=sess.family_id,
            student_id=sess.student_id,
            assignment_id=sess.assignment_id,
            topic_id=prob.topic_id,
            problem_version=prob.problem_id,
            event_type="stuck_explained",
            hint_level=1,
            summary=f"说清卡点：{sess.child_attempt_text}",
            expires_at=now + timedelta(days=30),
        )
    )
    db.commit()

    hint_text = prob.hints[0]
    safe = safety_middleware.check_output(hint_text, allow_direct_answer=False)
    return TutoringStepOut(
        session_id=sess.id,
        stage=sess.stage,
        hint_level=1,
        spoken_text=hint_text if safe.allowed else safe.replacement_text,
        can_advance_hint=True,
        award_eligible_event="stuck_explained",
    )


def request_next_hint(db: Session, *, study_session_id: str) -> TutoringStepOut:
    sess = db.get(StudySession, study_session_id)
    if sess is None or not sess.echo_confirmed:
        raise ValueError("must_confirm_attempt_before_hints")
    prob = get_problem(sess.problem_id or "")
    if prob is None:
        raise ValueError("problem_not_bound")
    if sess.hint_level >= 3:
        return TutoringStepOut(
            session_id=sess.id,
            stage="hint_3",
            hint_level=3,
            spoken_text="三条提示都看过了，先在纸上试着写一步吧。",
            can_advance_hint=False,
        )
    sess.hint_level += 1
    sess.stage = f"hint_{sess.hint_level}"
    db.commit()
    hint_text = prob.hints[sess.hint_level - 1]
    safe = safety_middleware.check_output(hint_text, allow_direct_answer=False)
    return TutoringStepOut(
        session_id=sess.id,
        stage=sess.stage,
        hint_level=sess.hint_level,
        spoken_text=hint_text if safe.allowed else safe.replacement_text,
        can_advance_hint=sess.hint_level < 3,
    )


def return_to_paper(db: Session, *, study_session_id: str) -> TutoringStepOut:
    sess = db.get(StudySession, study_session_id)
    if sess is None:
        raise ValueError("study_session_not_found")
    sess.stage = "writing"
    db.commit()
    return TutoringStepOut(
        session_id=sess.id,
        stage=sess.stage,
        hint_level=sess.hint_level,
        spoken_text="好，你先在纸上试一试，写完再叫我。",
        can_advance_hint=False,
    )


def complete_assignment_with_checks(
    db: Session,
    *,
    study_session_id: str,
    checks: list[str],
    now: Optional[datetime] = None,
) -> TutoringStepOut:
    now = now or utcnow()
    sess = db.get(StudySession, study_session_id)
    if sess is None:
        raise ValueError("study_session_not_found")
    valid_checks = [c for c in checks if c in ("miss", "unit")]
    if not valid_checks:
        raise ValueError("at_least_one_check_required")

    asg = db.get(Assignment, sess.assignment_id)
    if asg is None:
        raise ValueError("assignment_not_found")

    asg.completed = True
    asg.completed_at = now
    sess.checks_csv = ",".join(sorted(set(valid_checks)))
    sess.stage = "done"

    labels = "和".join("漏题" if c == "miss" else "单位" for c in sorted(set(valid_checks)))
    db.add(
        LearningEvent(
            id=f"ev_{uuid.uuid4().hex[:12]}",
            family_id=sess.family_id,
            student_id=sess.student_id,
            assignment_id=asg.id,
            event_type="check_confirmed",
            summary=f"完成{asg.subject}并确认检查了{labels}",
            expires_at=now + timedelta(days=30),
        )
    )
    db.commit()
    return TutoringStepOut(
        session_id=sess.id,
        stage="done",
        hint_level=sess.hint_level,
        spoken_text=f"{asg.subject}写完了，你刚才确认检查了{labels}。",
        can_advance_hint=False,
        award_eligible_event="check_confirmed",
    )


def get_preview_prompt(preview_id: str) -> Optional[PreviewPrompt]:
    return PREVIEW_PROMPTS.get(preview_id)


def schedule_next_day_review(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    problem_id: str,
    method_note: str,
    due_at: datetime,
) -> ReviewItem:
    prob = get_problem(problem_id)
    if prob is None:
        raise ValueError("problem_not_found")
    item = ReviewItem(
        id=f"rev_{uuid.uuid4().hex[:12]}",
        family_id=family_id,
        student_id=student_id,
        topic_id=prob.topic_id,
        problem_id=prob.problem_id,
        method_note=method_note.strip(),
        due_at=due_at,
        status="scheduled",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def open_yesterday_hint(db: Session, *, review_id: str) -> str:
    item = db.get(ReviewItem, review_id)
    if item is None:
        raise ValueError("review_not_found")
    item.opened_yesterday_hint = True
    db.commit()
    return item.method_note


def submit_review_attempt(
    db: Session,
    *,
    review_id: str,
    utterance: str,
    skip: bool = False,
    now: Optional[datetime] = None,
) -> ReviewOutcomeOut:
    now = now or utcnow()
    item = db.get(ReviewItem, review_id)
    if item is None:
        raise ValueError("review_not_found")
    prob = get_problem(item.problem_id)
    if prob is None:
        raise ValueError("problem_not_found")

    if skip:
        item.status = "skipped"
        db.commit()
        return ReviewOutcomeOut(
            review_id=item.id,
            status="skipped",
            unlock_method_card_id=None,
            verified_growth_eligible=False,
            spoken_text="没关系，这次先跳过，下次再试。",
        )

    verdict = grade_review_explanation(prob, utterance)
    if verdict == "pending_verify":
        item.status = "pending_verify"
        event_type = "review_pending"
        spoken = "这次理由还不够清楚，我先记为待验证，下次再试一次。"
        unlock_card = None
        verified = False
    elif item.opened_yesterday_hint:
        item.status = "hinted_pass"
        event_type = "review_hinted"
        spoken = "看了昨天的方法后答出来了，我记下了，过几天再独立试一道。"
        unlock_card = None
        verified = False
    else:
        item.status = "independent_pass"
        event_type = "review_independent"
        spoken = f"隔了一天，你没看提示也自己讲清了！收藏方法卡「{prob.method_card_front}」。"
        unlock_card = prob.method_card_id
        verified = True

    db.add(
        LearningEvent(
            id=f"ev_{uuid.uuid4().hex[:12]}",
            family_id=item.family_id,
            student_id=item.student_id,
            topic_id=item.topic_id,
            problem_version=prob.problem_id,
            event_type=event_type,
            hint_level=1 if item.opened_yesterday_hint else 0,
            source_kind="rubric_verified" if verified else "review_attempt",
            summary=f"次日复练({item.status})：{utterance.strip()[:80]}",
            expires_at=now + timedelta(days=30),
        )
    )
    db.commit()
    return ReviewOutcomeOut(
        review_id=item.id,
        status=item.status,
        unlock_method_card_id=unlock_card,
        verified_growth_eligible=verified,
        spoken_text=spoken,
    )
