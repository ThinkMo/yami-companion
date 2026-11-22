from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Literal, Optional
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import DictationSession, DictationWord, LearningEvent, ReviewItem, Student


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CurriculumDictationPack:
    pack_id: str
    subject: Literal["语文", "英语"]
    grade: int
    textbook_version: str
    unit: int
    lesson: Optional[int]
    title: str
    words: list[tuple[str, str]]  # (word, context/meaning for voice dictation)


# Verified teaching material bank keyed by (subject, textbook_version, grade, unit, lesson)
CURRICULUM_DICTATION_BANK: list[CurriculumDictationPack] = [
    CurriculumDictationPack(
        pack_id="zh_renjiao_g4_u3_l4",
        subject="语文",
        grade=4,
        textbook_version="人教版",
        unit=3,
        lesson=4,
        title="四年级人教版 · 语文第3单元第4课组词",
        words=[
            ("均匀", "铺得很均匀的均匀"),
            ("重叠", "叶子重叠在一起的重叠"),
            ("空隙", "叶柄之间的空隙"),
            ("牢固", "攀附得很牢固的牢固"),
            ("休想", "休想拉下它的休想"),
            ("住宅", "蟋蟀的住宅"),
        ],
    ),
    CurriculumDictationPack(
        pack_id="zh_renjiao_g4_l8",
        subject="语文",
        grade=4,
        textbook_version="人教版",
        unit=2,
        lesson=8,
        title="四年级人教版 · 语文第8课生字词",
        words=[
            ("观察", "观察植物的观察"),
            ("均匀", "铺得很均匀的均匀"),
            ("重叠", "叶子重叠在一起的重叠"),
            ("空隙", "叶柄之间的空隙"),
        ],
    ),
    CurriculumDictationPack(
        pack_id="en_renjiao_g4_u3",
        subject="英语",
        grade=4,
        textbook_version="人教版",
        unit=3,
        lesson=None,
        title="四年级人教版 · 英语 Unit 3 单词",
        words=[
            ("library", "图书馆"),
            ("playground", "操场"),
            ("weather", "天气"),
            ("breakfast", "早餐"),
        ],
    ),
]


@dataclass(frozen=True)
class DictationPromptOut:
    session_id: str
    status: str  # in_progress | self_check | completed
    index: int
    total: int
    spoken_text: str
    screen_text: str  # Minimal screen status; NEVER reveals spelling during active dictation!
    reveal_words: list[dict[str, str]]


def _parse_cn_or_arabic_int(token: str) -> int:
    mapping = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if token.isdigit():
        return int(token)
    return mapping.get(token, 0)


def lookup_curriculum_dictation(
    *,
    textbook_version: str,
    grade: int,
    utterance: str,
) -> Optional[CurriculumDictationPack]:
    """
    Resolve natural-language dictation request (from DingTalk parent message or child voice)
    such as '听写第3单元第4课组词', '听写第8课生字', '听写英语 Unit 3 单词'
    against verified curriculum teaching materials for the student's grade & textbook version.
    """
    text = (utterance or "").strip()
    is_english = ("英语" in text) or bool(re.search(r"unit\s*\d+", text, re.IGNORECASE))

    unit_m = re.search(r"第\s*([0-9一二三四五六七八九十]+)\s*单元|unit\s*(\d+)", text, re.IGNORECASE)
    lesson_m = re.search(r"第\s*([0-9一二三四五六七八九十]+)\s*课", text)

    unit = _parse_cn_or_arabic_int(unit_m.group(1) or unit_m.group(2)) if unit_m else None
    lesson = _parse_cn_or_arabic_int(lesson_m.group(1)) if lesson_m else None
    target_subject = "英语" if is_english else "语文"

    from backend.services.textbook_catalog import parse_subject_textbooks

    tb_raw = (textbook_version or "").strip()
    if tb_raw.startswith("{"):
        tb_map = parse_subject_textbooks(tb_raw)
        tb_resolved = tb_map.get("english" if is_english else "chinese") or "人教版"
    else:
        tb_resolved = tb_raw or "人教版"
    # Normalize common aliases (统编版 / 人教版 PEP -> 人教版)
    tb_norm = "人教版" if any(k in tb_resolved for k in ("人教", "统编", "部编")) else tb_resolved

    for pack in CURRICULUM_DICTATION_BANK:
        if pack.subject != target_subject:
            continue
        if pack.grade != grade or (pack.textbook_version != tb_resolved and pack.textbook_version != tb_norm):
            continue
        if unit is not None and lesson is not None:
            if pack.unit == unit and pack.lesson == lesson:
                return pack
        elif unit is not None and pack.unit == unit and pack.lesson is None:
            return pack
        elif lesson is not None and pack.lesson == lesson:
            return pack
    return None


def create_dictation_session(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    subject: Literal["语文", "英语"],
    source: Literal["parent_dingtalk", "student_initiated"],
    title: str,
    words: list[tuple[str, str]],
) -> DictationSession:
    student = db.get(Student, student_id)
    if student is None or student.family_id != family_id:
        raise PermissionError("student_outside_family")
    if not words:
        raise ValueError("word_list_empty")

    # Mark any prior active dictation session for this student as completed
    for old in db.scalars(
        select(DictationSession).where(
            DictationSession.student_id == student_id,
            DictationSession.status == "in_progress",
        )
    ):
        old.status = "completed"

    sess = DictationSession(
        id=f"dict_{uuid.uuid4().hex[:12]}",
        family_id=family_id,
        student_id=student_id,
        subject=subject,
        source=source,
        title=title.strip(),
        current_index=0,
        status="in_progress",
    )
    db.add(sess)
    db.flush()
    for idx, (w, hint) in enumerate(words):
        db.add(
            DictationWord(
                session_id=sess.id,
                order_index=idx,
                word=w.strip(),
                hint_or_meaning=hint.strip(),
            )
        )
    db.commit()
    db.refresh(sess)
    return sess


def start_dictation_from_request(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    utterance: str,
    source: Literal["parent_dingtalk", "student_initiated"],
) -> tuple[Optional[DictationSession], str]:
    """
    End-to-end natural-language dictation entry:
    Looks up the student's textbook version & grade in verified teaching materials.
    Returns (None, honest_missing_message) if the lesson is not in the curriculum bank.
    """
    student = db.get(Student, student_id)
    if student is None or student.family_id != family_id:
        raise PermissionError("student_outside_family")

    pack = lookup_curriculum_dictation(
        textbook_version=student.textbook_version,
        grade=student.grade,
        utterance=utterance,
    )
    if pack is None:
        from backend.services.textbook_catalog import parse_subject_textbooks

        tb_display = student.textbook_version or "人教版"
        if tb_display.strip().startswith("{"):
            tb_map = parse_subject_textbooks(tb_display)
            is_en = bool(re.search(r"英语|英文|unit|word|单词", utterance, re.IGNORECASE))
            tb_display = tb_map.get("english" if is_en else "chinese") or "人教版"
        return (
            None,
            f"我还没找到{student.grade}年级{tb_display}这课的听写材料，请家长补充词表或先告诉我具体词语。",
        )

    sess = create_dictation_session(
        db,
        family_id=family_id,
        student_id=student_id,
        subject=pack.subject,
        source=source,
        title=pack.title,
        words=pack.words,
    )
    first = dictation_action(db, session_id=sess.id, action="current")
    return sess, f"好，开始{pack.title}，共 {len(pack.words)} 个词。准备好纸笔，{first.spoken_text}"


def get_active_dictation(db: Session, *, student_id: str) -> Optional[DictationSession]:
    return db.scalar(
        select(DictationSession)
        .where(
            DictationSession.student_id == student_id,
            DictationSession.status == "in_progress",
        )
        .order_by(DictationSession.created_at.desc())
    )


def handle_dictation_voice_command(
    db: Session,
    *,
    session_id: str,
    utterance: str,
) -> Optional[DictationPromptOut]:
    """Pure voice control during active dictation: '再读一遍', '下一个/写好了', '跳过', '上一个'."""
    text = (utterance or "").strip()
    if any(k in text for k in ("再读", "重读", "再说一遍", "没听清", "慢一点")):
        return dictation_action(db, session_id=session_id, action="repeat")
    if any(k in text for k in ("上一个", "前一个", "刚才那个")):
        return dictation_action(db, session_id=session_id, action="prev")
    if any(k in text for k in ("跳过", "不会写", "先空着")):
        return dictation_action(db, session_id=session_id, action="skip")
    if any(k in text for k in ("下一个", "下一词", "写好了", "好了", "继续")):
        return dictation_action(db, session_id=session_id, action="next")
    return None


def _format_word_prompt(sess: DictationSession, word_row: DictationWord, total: int) -> DictationPromptOut:
    idx = word_row.order_index
    if sess.subject == "语文":
        spoken = f"第 {idx + 1} 个词：{word_row.word}。"
        if word_row.hint_or_meaning:
            spoken += f"{word_row.hint_or_meaning}。"
    else:
        spoken = f"第 {idx + 1} 个单词：{word_row.word}。"
        if word_row.hint_or_meaning:
            spoken += f"意思是{word_row.hint_or_meaning}。"
    return DictationPromptOut(
        session_id=sess.id,
        status="in_progress",
        index=idx,
        total=total,
        spoken_text=spoken,
        screen_text=f"正在听写 · 第 {idx + 1} / {total} 词",
        reveal_words=[],
    )


def dictation_action(
    db: Session,
    *,
    session_id: str,
    action: Literal["current", "repeat", "next", "prev", "skip"],
) -> DictationPromptOut:
    sess = db.get(DictationSession, session_id)
    if sess is None:
        raise ValueError("dictation_session_not_found")
    words = sess.words
    total = len(words)

    if action == "repeat":
        cur = words[sess.current_index]
        cur.repeat_count += 1
        db.commit()
        return _format_word_prompt(sess, cur, total)

    if action == "prev":
        sess.current_index = max(0, sess.current_index - 1)
        sess.status = "in_progress"
        db.commit()
        return _format_word_prompt(sess, words[sess.current_index], total)

    if action == "skip":
        words[sess.current_index].skipped = True

    if action in ("next", "skip"):
        if sess.current_index + 1 >= total:
            sess.status = "self_check"
            db.commit()
            return DictationPromptOut(
                session_id=sess.id,
                status="self_check",
                index=total - 1,
                total=total,
                spoken_text=f"全部 {total} 个词报完啦！你可以对照课本或拍一张听写本核对，错词明天再巩固。",
                screen_text="听写完成 · 核对错词",
                reveal_words=[
                    {"word": w.word, "hint": w.hint_or_meaning, "skipped": str(w.skipped)}
                    for w in words
                ],
            )
        sess.current_index += 1
        db.commit()

    return _format_word_prompt(sess, words[sess.current_index], total)


def complete_dictation_self_check(
    db: Session,
    *,
    session_id: str,
    missed_words: list[str],
    now: Optional[datetime] = None,
) -> tuple[DictationSession, list[ReviewItem]]:
    now = now or utcnow()
    sess = db.get(DictationSession, session_id)
    if sess is None:
        raise ValueError("dictation_session_not_found")

    missed_set = {w.strip() for w in missed_words if w.strip()}
    to_review: list[str] = []
    for row in sess.words:
        if row.word in missed_set or row.skipped:
            row.self_check_correct = False
            to_review.append(row.word)
        else:
            row.self_check_correct = True

    sess.status = "completed"
    db.add(
        LearningEvent(
            id=f"ev_{uuid.uuid4().hex[:12]}",
            family_id=sess.family_id,
            student_id=sess.student_id,
            topic_id=f"dictation_{sess.subject}",
            event_type="dictation_completed",
            summary=f"完成{sess.title}（共 {len(sess.words)} 词，待巩固 {len(to_review)} 词）",
            expires_at=now + timedelta(days=30),
        )
    )

    queued: list[ReviewItem] = []
    for w in to_review:
        item = ReviewItem(
            id=f"rev_{uuid.uuid4().hex[:12]}",
            family_id=sess.family_id,
            student_id=sess.student_id,
            topic_id=f"dictation_{sess.subject}",
            problem_id=f"dictation_word:{w}",
            method_note=f"听写巩固：{w}",
            due_at=now + timedelta(days=1),
            status="scheduled",
        )
        db.add(item)
        queued.append(item)

    db.commit()
    db.refresh(sess)
    return sess, queued
