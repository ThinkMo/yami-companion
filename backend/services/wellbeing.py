from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    Assignment,
    CustomTask,
    DictationSession,
    EmotionSignal,
    GrowthRecord,
    LearningEvent,
    MethodCardUnlock,
    ReviewItem,
    Student,
    StudySession,
)
from backend.services.textbook_catalog import get_student_textbook_curriculum, parse_subject_textbooks

MOOD_LABELS_CN = {
    "confident": "自信开心",
    "calm": "平静专注",
    "frustrated": "轻微畏难",
    "fatigued": "疲惫分心",
    "anxious": "焦虑低落",
}


def detect_and_record_emotion(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    utterance: str,
    skill_name: str = "chat-companion",
    now: Optional[datetime] = None,
) -> Optional[EmotionSignal]:
    """
    Passive, non-intrusive emotional signal detector for Grade 3-4 voice turns.
    Never performs clinical diagnosis; captures lightweight mood trends and
    actionable parenting tips, flagging high-risk distress for guardian attention.
    """
    text = (utterance or "").strip()
    if len(text) < 2:
        return None

    now = now or datetime.now(timezone.utc)
    mood: Optional[str] = None
    risk = "normal"
    tip = ""

    high_risk_keys = ("不想上学", "我很笨", "讨厌自己", "没人喜欢我", "不想活", "打我", "欺负我")
    anxious_keys = ("害怕", "考不好", "会骂我", "好紧张", "担心", "难过", "哭了", "委屈")
    frustrated_keys = ("好难", "太难了", "不会做", "做不出来", "搞不懂", "算错", "好烦")
    fatigued_keys = ("好累", "困了", "不想写", "休息一下", "写不动", "没力气")
    confident_keys = ("会了", "写完了", "太棒了", "我懂了", "好简单", "真开心", "表扬", "全对", "答对了")

    if any(k in text for k in high_risk_keys):
        mood = "anxious"
        risk = "high"
        tip = "孩子在对话中表达了较强的自我否定或压力信号，建议今晚放下作业进度，先温和倾听他的感受，给予无条件接纳。"
    elif any(k in text for k in anxious_keys):
        mood = "anxious"
        risk = "attention"
        tip = "孩子近期存在轻微担忧或紧张情绪，建议多肯定努力过程，避免用分数或他人比较增加压力。"
    elif any(k in text for k in frustrated_keys):
        mood = "frustrated"
        risk = "normal"
        tip = "遇到难题出现畏难情绪很正常，建议先肯定孩子主动求助的勇气，陪他把题目拆成一小步来做。"
    elif any(k in text for k in fatigued_keys):
        mood = "fatigued"
        risk = "normal"
        tip = "孩子反馈有些疲惫，建议采用 20 分钟专注 + 5 分钟远眺/喝水的节律，先完成最轻松的一科。"
    elif any(k in text for k in confident_keys):
        mood = "confident"
        risk = "normal"
        tip = "孩子今天很有成就感，睡前可以具体夸夸他刚才独立完成或想明白的那一步！"
    else:
        return None

    sig = EmotionSignal(
        id=f"emo_{uuid.uuid4().hex[:12]}",
        family_id=family_id,
        student_id=student_id,
        mood=mood,
        risk_level=risk,
        trigger_context=f"[{skill_name}] {text[:80]}",
        parent_tip=tip,
        created_at=now,
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    return sig


def build_growth_analytics(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    month: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Aggregate holistic Growth Tab analytics inspired by Khanmigo, iFlytek & Duolingo:
      1) Long-term Habits (process-oriented, zero FOMO streak punishment)
      2) 4-Color Cognitive Knowledge Mastery Map + Method Cards
      3) Non-Intrusive Emotional Wellbeing Barometer + Parenting Tips
    """
    stu = db.get(Student, student_id)
    if stu is None or stu.family_id != family_id:
        raise PermissionError("student_outside_family")

    now = now or datetime.now(timezone.utc)

    # 1. Habits metrics
    assignments = list(
        db.scalars(
            select(Assignment).where(
                Assignment.family_id == family_id,
                Assignment.student_id == student_id,
            )
        )
    )
    sessions = list(
        db.scalars(
            select(StudySession).where(
                StudySession.family_id == family_id,
                StudySession.student_id == student_id,
            )
        )
    )
    events = list(
        db.scalars(
            select(LearningEvent).where(
                LearningEvent.family_id == family_id,
                LearningEvent.student_id == student_id,
            )
        )
    )
    total_asg = len(assignments)
    done_asg = sum(1 for a in assignments if a.completed)
    completion_rate = round((done_asg / total_asg) * 100) if total_asg else 0
    checked_count = sum(1 for e in events if "check" in (e.event_type or "") or "检查" in (e.summary or ""))
    help_count = sum(1 for e in events if "hint" in (e.event_type or "") or "stuck" in (e.event_type or ""))
    self_start_count = max(len(sessions), done_asg)

    habits = {
        "completion_rate": completion_rate,
        "completed_assignments": done_asg,
        "total_assignments": total_asg,
        "self_start_count": self_start_count,
        "active_check_count": checked_count,
        "effective_help_count": help_count,
        "summary_text": (
            f"近期累计自主开工 {self_start_count} 次、完成作业 {done_asg}/{total_asg} 项、"
            f"有效求助 {help_count} 次、主动检查 {checked_count} 次。"
        ),
    }

    # 2. 4-Color Knowledge Mastery Map (red / yellow / light_green / deep_green)
    reviews = list(
        db.scalars(
            select(ReviewItem).where(
                ReviewItem.family_id == family_id,
                ReviewItem.student_id == student_id,
            )
        )
    )
    growth_rows = list(
        db.scalars(
            select(GrowthRecord).where(
                GrowthRecord.family_id == family_id,
                GrowthRecord.student_id == student_id,
                GrowthRecord.deleted.is_(False),
            )
        )
    )
    cards = [
        {"card_id": c.card_id, "front": c.front, "back": c.back}
        for c in db.scalars(
            select(MethodCardUnlock).where(
                MethodCardUnlock.family_id == family_id,
                MethodCardUnlock.student_id == student_id,
                MethodCardUnlock.active.is_(True),
            )
        )
    ]

    topic_map: dict[str, dict[str, Any]] = {}
    for r in reviews:
        tid = r.topic_id or r.problem_id
        if r.status == "independent_pass":
            lvl, lbl = "deep_green", "隔天独立通过"
        elif r.status == "hint_pass":
            lvl, lbl = "yellow", "提示后掌握"
        else:
            lvl, lbl = "red", "待巩固"
        topic_map[tid] = {
            "topic_id": tid,
            "label": r.method_note or tid,
            "level": lvl,
            "level_cn": lbl,
            "evidence": f"复练状态：{r.status}",
        }

    for g in growth_rows:
        if g.category == "knowledge":
            tid = g.topic_id or g.id
            if tid not in topic_map or topic_map[tid]["level"] in ("red", "yellow"):
                topic_map[tid] = {
                    "topic_id": tid,
                    "label": g.title,
                    "level": "light_green" if g.verified else "yellow",
                    "level_cn": "已验证掌握" if g.verified else "待家长确认",
                    "evidence": g.evidence,
                }

    if not topic_map:
        topic_map["unit_fraction_compare"] = {
            "topic_id": "unit_fraction_compare",
            "label": "单位分数大小比较（先看整体）",
            "level": "yellow",
            "level_cn": "练习中",
            "evidence": "完成次日变式独立解释后升级为深绿",
        }

    # 3. Non-Intrusive Emotional Wellbeing Barometer (last 7 days)
    cutoff = now - timedelta(days=7)
    all_emos = list(
        db.scalars(
            select(EmotionSignal)
            .where(
                EmotionSignal.family_id == family_id,
                EmotionSignal.student_id == student_id,
            )
            .order_by(EmotionSignal.created_at.desc())
            .limit(30)
        )
    )
    recent_emos = []
    for e in all_emos:
        ts = e.created_at if e.created_at.tzinfo is not None else e.created_at.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            recent_emos.append(e)

    counts = {k: 0 for k in MOOD_LABELS_CN}
    for e in recent_emos:
        counts[e.mood] = counts.get(e.mood, 0) + 1

    highest_risk = "normal"
    for e in recent_emos:
        if e.risk_level == "high":
            highest_risk = "high"
            break
        if e.risk_level == "attention":
            highest_risk = "attention"

    latest_tip = (
        recent_emos[0].parent_tip
        if recent_emos
        else "近期互动情绪平稳专注，继续保持先倾听思路、再给小步鼓励的沟通方式即可。"
    )

    emotion_barometer = {
        "disclaimer": "无感情绪晴雨表仅基于日常语音对话提供亲子沟通参考，不构成任何临床心理诊断。",
        "highest_risk": highest_risk,
        "mood_counts": [
            {"mood": k, "label": v, "count": counts.get(k, 0)}
            for k, v in MOOD_LABELS_CN.items()
        ],
        "latest_parent_tip": latest_tip,
        "recent_signals": [
            {
                "id": e.id,
                "mood": e.mood,
                "mood_cn": MOOD_LABELS_CN.get(e.mood, e.mood),
                "risk_level": e.risk_level,
                "trigger_context": e.trigger_context,
                "parent_tip": e.parent_tip,
                "created_at": e.created_at.isoformat(),
            }
            for e in recent_emos[:8]
        ],
    }

    # 4. Daily Homework Completion + Daily Extra Tasks (Last 7 Days)
    dictations = list(
        db.scalars(
            select(DictationSession).where(
                DictationSession.family_id == family_id,
                DictationSession.student_id == student_id,
            )
        )
    )
    today_date = now.date()
    daily_rows: list[dict[str, Any]] = []
    for offset in range(6, -1, -1):
        day = today_date - timedelta(days=offset)
        day_str = day.strftime("%m-%d")
        day_asgs = [
            a for a in assignments
            if a.published_at and (a.published_at.date() == day)
        ]
        hw_total = len(day_asgs)
        hw_done = sum(1 for a in day_asgs if a.completed)
        hw_pct = round((hw_done / hw_total) * 100) if hw_total else 0

        day_dicts = [d for d in dictations if d.created_at and d.created_at.date() == day]
        day_revs = [r for r in reviews if r.created_at and r.created_at.date() == day]
        extra_total = len(day_dicts) + len(day_revs)
        extra_done = sum(1 for d in day_dicts if d.status == "completed") + sum(
            1 for r in day_revs if r.status in ("independent_pass", "hint_pass")
        )
        daily_rows.append(
            {
                "date": day.isoformat(),
                "label": "今天" if offset == 0 else day_str,
                "homework_done": hw_done,
                "homework_total": hw_total,
                "homework_pct": hw_pct,
                "extra_done": extra_done,
                "extra_total": extra_total,
                "extra_completed": (extra_total > 0 and extra_done >= extra_total),
            }
        )

    today_row = daily_rows[-1]
    daily_completion = {
        "today_homework_done": today_row["homework_done"],
        "today_homework_total": today_row["homework_total"],
        "today_homework_pct": today_row["homework_pct"],
        "today_extra_done": today_row["extra_done"],
        "today_extra_total": today_row["extra_total"],
        "today_extra_tasks": [
            {
                "kind": "听写",
                "title": d.title,
                "done": d.status == "completed",
            }
            for d in dictations[:4]
        ]
        + [
            {
                "kind": "巩固复练",
                "title": r.method_note or r.topic_id,
                "done": r.status in ("independent_pass", "hint_pass"),
            }
            for r in reviews[:4]
        ],
        "last_7_days": daily_rows,
    }

    # 4b. Monthly Calendar Heatmap (YYYY-MM)
    import calendar as _cal
    target_year, target_month = today_date.year, today_date.month
    if month and "-" in month:
        try:
            parts = month.strip().split("-")
            target_year, target_month = int(parts[0]), max(1, min(12, int(parts[1])))
        except Exception:
            pass
    _, num_days = _cal.monthrange(target_year, target_month)
    first_weekday = (_cal.weekday(target_year, target_month, 1) + 1) % 7  # 0=Sun .. 6=Sat
    cal_days: list[dict[str, Any]] = []
    for dnum in range(1, num_days + 1):
        d_obj = datetime(target_year, target_month, dnum, tzinfo=timezone.utc).date()
        d_asgs = [a for a in assignments if a.published_at and a.published_at.date() == d_obj]
        hw_t = len(d_asgs)
        hw_d = sum(1 for a in d_asgs if a.completed)
        hw_p = round((hw_d / hw_t) * 100) if hw_t else 0
        d_dicts = [x for x in dictations if x.created_at and x.created_at.date() == d_obj]
        d_revs = [x for x in reviews if x.created_at and x.created_at.date() == d_obj]
        ex_t = len(d_dicts) + len(d_revs)
        ex_d = sum(1 for x in d_dicts if x.status == "completed") + sum(
            1 for x in d_revs if x.status in ("independent_pass", "hint_pass")
        )
        if hw_t == 0 and ex_t == 0:
            lvl = 0
        elif hw_p >= 100:
            lvl = 4
        elif hw_p >= 60:
            lvl = 3
        elif hw_p > 0 or ex_d > 0:
            lvl = 2
        else:
            lvl = 1
        cal_days.append(
            {
                "date": d_obj.isoformat(),
                "day": dnum,
                "homework_done": hw_d,
                "homework_total": hw_t,
                "homework_pct": hw_p,
                "extra_done": ex_d,
                "extra_total": ex_t,
                "extra_completed": (ex_t > 0 and ex_d >= ex_t),
                "level": lvl,
            }
        )
    monthly_calendar = {
        "month": f"{target_year:04d}-{target_month:02d}",
        "first_weekday": first_weekday,
        "days": cal_days,
    }

    # 5. Real Textbook Curriculum Progress (textbooks.json + directories.json)
    subj_versions = parse_subject_textbooks(stu.textbook_version)
    raw_curriculum = get_student_textbook_curriculum(
        grade=int(stu.grade or 4),
        subject_textbooks=subj_versions,
        semester="upper",
    )
    corpus_done = " ".join(
        [a.content for a in assignments if a.completed]
        + [g.title + " " + g.content for g in growth_rows if g.verified]
        + [r.method_note for r in reviews if r.status == "independent_pass"]
    )
    corpus_active = " ".join(
        [a.content for a in assignments if not a.completed]
        + [d.title for d in dictations]
        + [r.method_note for r in reviews if r.status != "independent_pass"]
    )

    curriculum_progress: list[dict[str, Any]] = []
    for sub in raw_curriculum:
        units_annotated = []
        mastered_cnt = 0
        active_cnt = 0
        for u in sub["units"]:
            u_title = u["title"]
            # Match unit title or lesson names against student's real learning records
            keywords = [u_title] + [ls for ls in u["lessons"] if len(ls) >= 2]
            short_keys = [k[:4] for k in keywords if len(k) >= 2]
            if any(k in corpus_done for k in short_keys):
                st, st_cn = "mastered", "已掌握"
                mastered_cnt += 1
            elif any(k in corpus_active for k in short_keys) or (u["unit_index"] == 1 and sub["subject_cn"] in corpus_active + corpus_done):
                st, st_cn = "in_progress", "学习中"
                active_cnt += 1
            else:
                st, st_cn = "not_started", "未开始"
            units_annotated.append(
                {
                    "unit_index": u["unit_index"],
                    "title": u_title,
                    "lessons": u["lessons"],
                    "status": st,
                    "status_cn": st_cn,
                }
            )
        total_u = len(units_annotated)
        pct = round(((mastered_cnt + active_cnt * 0.5) / total_u) * 100) if total_u else 0
        curriculum_progress.append(
            {
                "subject_key": sub["subject_key"],
                "subject_cn": sub["subject_cn"],
                "series": sub["series"],
                "book_id": sub["book_id"],
                "book_title": sub["book_title"],
                "mastered_units": mastered_cnt,
                "in_progress_units": active_cnt,
                "total_units": total_u,
                "progress_pct": pct,
                "units": units_annotated,
            }
        )

    # 6. Detailed Teacher Praise & Classroom Performance Board
    import re

    praise_rows = [
        g
        for g in sorted(growth_rows, key=lambda x: x.created_at or now, reverse=True)
        if g.category in ("teacher_praise", "class_performance")
    ]
    subj_counter: dict[str, int] = {}
    month_prefix = f"{target_year:04d}-{target_month:02d}"
    month_praise_count = 0
    praise_items: list[dict[str, Any]] = []
    for g in praise_rows:
        text_blob = f"{g.title} {g.content} {g.evidence or ''}"
        subj = "综合"
        for cand in ("数学", "语文", "英语", "科学", "体育", "美术", "音乐"):
            if cand in text_blob:
                subj = cand
                break
        m_teacher = re.search(r"([\u4e00-\u9fa5]{1,3}老师)", text_blob)
        teacher_name = m_teacher.group(1) if m_teacher else "任课老师"
        src_label = "家长补录" if "家长补录" in (g.evidence or "") else "钉钉班级群"
        g_ts = g.created_at or now
        g_date = g_ts.date().isoformat()
        if g_date.startswith(month_prefix):
            month_praise_count += 1
        subj_counter[subj] = subj_counter.get(subj, 0) + 1
        praise_items.append(
            {
                "id": g.id,
                "category": g.category,
                "title": g.title,
                "subject": subj,
                "teacher_name": teacher_name,
                "source": src_label,
                "content": g.content,
                "evidence": g.evidence,
                "parent_note": g.parent_note,
                "verified": bool(g.verified),
                "date": g_date,
            }
        )
    subject_counts = [
        {"subject": k, "count": v}
        for k, v in sorted(subj_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    teacher_praise_board = {
        "total_count": len(praise_items),
        "month_count": month_praise_count,
        "subject_counts": subject_counts,
        "top_subject": subject_counts[0]["subject"] if subject_counts else "暂无",
        "latest_date": praise_items[0]["date"] if praise_items else "",
        "items": praise_items,
    }

    # 7. Habit Auto-Stats (Aggregated from CustomTask streaks + Homework completion)
    custom_tasks = list(
        db.scalars(
            select(CustomTask)
            .where(
                CustomTask.family_id == family_id,
                CustomTask.student_id == student_id,
                CustomTask.status != "archived",
            )
            .order_by(CustomTask.streak_days.desc(), CustomTask.created_at.desc())
        )
    )
    today_iso = now.date().isoformat()
    active_ct = [t for t in custom_tasks if t.status == "active"]
    done_today_ct = [t for t in active_ct if t.last_completed_date == today_iso]
    max_streak = max((int(t.streak_days or 0) for t in custom_tasks), default=0)
    hw_days_with_work = [d for d in daily_completion.get("last_7_days", []) if d["homework_total"] > 0]
    avg_hw_pct_7d = (
        round(sum(d["homework_pct"] for d in hw_days_with_work) / len(hw_days_with_work))
        if hw_days_with_work
        else daily_completion.get("today_homework_pct", 0)
    )
    habit_summary = {
        "avg_homework_pct_7d": avg_hw_pct_7d,
        "max_streak_days": max_streak,
        "active_task_count": len(active_ct),
        "done_today_count": len(done_today_ct),
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "cadence": t.cadence,
                "status": t.status,
                "streak_days": int(t.streak_days or 0),
                "done_today": t.last_completed_date == today_iso,
            }
            for t in custom_tasks
        ],
    }

    return {
        "student_id": student_id,
        "daily_completion": daily_completion,
        "monthly_calendar": monthly_calendar,
        "teacher_praise_board": teacher_praise_board,
        "habit_summary": habit_summary,
        "curriculum_progress": curriculum_progress,
        "habits": habits,
        "knowledge_map": list(topic_map.values()),
        "method_cards": cards,
        "emotion_barometer": emotion_barometer,
    }
