"""DingTalk Guardian Agent Harness — powered by Doubao Seed 2.1 (multimodal) + DeepSeek fallback.

Routes parent text messages, forwarded school notices (e.g. 需签字家长会通知),
and blackboard homework photos through structured LLM tool planning, then executes
each action deterministically against the family's DB.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import json
import logging
import re
import urllib.request
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.learning.dictation import start_dictation_from_request
from backend.memory.service import write_learner_memory
from backend.models import Assignment, GrowthRecord, Student
from backend.reminders.service import schedule_or_reschedule_reminder
from backend.schemas import DingTalkBatchIn, DingTalkMessageIn
from backend.services.assignments import get_student_assignments
from backend.services.dingtalk_sync import ingest_dingtalk_batch

logger = logging.getLogger("yami.dingtalk_agent")

SYSTEM_PROMPT = """你是「芽米伴学」家长端智能管家，正在钉钉中与家长对话。
请根据家长发来的文字（和/或黑板作业图片、学校工作通知截图），理解家长的真实意图，并严格输出一个 JSON 对象（不要输出多余解释）：
{
  "intent": "sync_content | query_status | dictation | reminder | clear_assignments | chat",
  "reply_title": "简短标题（6-12字）",
  "reply_markdown": "面向家长的简洁清晰回复（Markdown，不要编造未入库的数据）",
  "actions": [
    {"type": "add_assignment", "subject": "数学/语文/英语/科学/其他", "content": "具体作业内容", "page_ref": "如P44或空", "submission_mode": "paper_next_day|dingtalk_online|none"},
    {"type": "add_todo", "subject": "家校通知", "content": "需家长签字/确认/回执的事项详情"},
    {"type": "add_praise", "subject": "数学/语文/英语/综合", "teacher_name": "如王老师", "title": "表扬主题", "content": "老师表扬或课堂表现原文"},
    {"type": "start_dictation", "utterance": "如：听写第3单元第4课组词"},
    {"type": "schedule_reminder", "minutes_from_now": 30, "label": "提醒内容"},
    {"type": "clear_assignments"}
  ]
}
规则：
1. 若图片或文字包含黑板各科作业（如语文/数学/英语），每个学科拆成独立的 add_assignment；空白栏不要生成。
2. 若包含【需签字】家长会通知、回执、缴费、带物品等需要家长处理的家校通知，生成 add_todo（会进入家长端「待办」）。
3. 若包含老师点名表扬或课堂表现反馈，生成 add_praise（会进入「教师表扬看板」）。
4. 若家长只是查询作业/表扬/学习情况，actions 置为空数组 []，直接基于提供的学生实时上下文在 reply_markdown 中回答。
5. 若家长明确要求清空旧作业/演示作业，生成 clear_assignments。
"""


def _norm_text(s: str) -> str:
    # Strip punctuation, spaces, timestamps, and boilerplate suffixes for semantic dedup
    s = re.sub(r"20\d{2}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", "", s or "")
    s = re.sub(r"[\s，。！？：；、,.!?:;（）()【】\[\]《》\"'·\-—]+", "", s)
    return s.lower()


def _is_duplicate_assignment(existing: list[Assignment], subject: str, content: str, page_ref: Optional[str] = None) -> bool:
    n_new = _norm_text(content)
    if not n_new:
        return True
    # Extract key bracketed title or exercise token if present (e.g. 【需签字】家长会通知 / 小练习十二 / P44)
    key_m = re.search(r"【[^】]+】[\u4e00-\u9fa5A-Za-z0-9]{2,14}", content)
    new_key = _norm_text(key_m.group(0)) if key_m else ""

    for ex in existing:
        if ex.subject != subject:
            continue
        n_ex = _norm_text(ex.content)
        if not n_ex:
            continue
        if n_new == n_ex or n_new in n_ex or n_ex in n_new:
            return True
        if new_key and len(new_key) >= 6 and new_key in n_ex:
            return True
        if page_ref and ex.page_ref and _norm_text(page_ref) == _norm_text(ex.page_ref):
            if SequenceMatcher(None, n_new, n_ex).ratio() >= 0.45:
                return True
        if SequenceMatcher(None, n_new, n_ex).ratio() >= 0.58:
            return True
    return False


def _is_duplicate_praise(existing: list[GrowthRecord], title: str, content: str) -> bool:
    n_new = _norm_text(f"{title} {content}")
    for g in existing:
        n_ex = _norm_text(f"{g.title} {g.content}")
        if n_new == n_ex or n_new in n_ex or n_ex in n_new:
            return True
        if SequenceMatcher(None, n_new, n_ex).ratio() >= 0.62:
            return True
    return False


def _extract_json_object(raw: str) -> Optional[dict[str, Any]]:
    s = (raw or "").strip()
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(s[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


def _call_multimodal_doubao_seed(
    user_text: str,
    context_text: str,
    image_bytes: Optional[bytes] = None,
    mime_type: str = "image/jpeg",
) -> tuple[Optional[dict[str, Any]], str]:
    """Call Doubao Seed 2.1 (volc_plan /chat/completions OR Ark /responses) with optional image."""
    # Priority 1: Volcengine Plan (doubao-seed-2.1-pro, OpenAI-compatible multimodal)
    if settings.volc_plan_api_key:
        content_parts: list[dict[str, Any]] = []
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            content_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}})
        content_parts.append({"type": "text", "text": f"【当前孩子上下文】\n{context_text}\n\n【家长消息】\n{user_text or '请识别图片中的作业或家校通知'}"})
        payload = {
            "model": settings.volc_plan_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content_parts},
            ],
            "temperature": 0.2,
            "max_tokens": 700,
        }
        req = urllib.request.Request(
            f"{settings.volc_plan_base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {settings.volc_plan_api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return _extract_json_object(str(raw)), f"volc_plan:{settings.volc_plan_model}"

    # Priority 2: Volcengine Ark Responses API (doubao-seed-2.1 / ark_seed_model)
    if settings.ark_api_key:
        input_parts: list[dict[str, Any]] = []
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            input_parts.append({"type": "input_image", "image_url": f"data:{mime_type};base64,{b64}"})
        input_parts.append({"type": "input_text", "text": f"【当前孩子上下文】\n{context_text}\n\n【家长消息】\n{user_text or '请识别图片中的作业或家校通知'}"})
        payload = {
            "model": settings.ark_seed_model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": input_parts},
            ],
            "thinking": {"type": "disabled"},
            "max_output_tokens": 700,
        }
        req = urllib.request.Request(
            f"{settings.ark_base_url.rstrip('/')}/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {settings.ark_api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = ""
        for item in data.get("output") or []:
            if item.get("type") == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text" and c.get("text"):
                        raw += str(c["text"])
        return _extract_json_object(raw), f"doubao_seed:{settings.ark_seed_model}"

    return None, "none"


def _call_deepseek_text(user_text: str, context_text: str) -> tuple[Optional[dict[str, Any]], str]:
    """DeepSeek text fallback for DingTalk parent messages."""
    if not settings.deepseek_api_key:
        return None, "none"
    payload = {
        "model": settings.deepseek_chat_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"【当前孩子上下文】\n{context_text}\n\n【家长消息】\n{user_text}"},
        ],
        "temperature": 0.2,
        "max_tokens": 700,
    }
    req = urllib.request.Request(
        f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return _extract_json_object(str(raw)), f"deepseek:{settings.deepseek_chat_model}"


def run_dingtalk_agent_turn(
    db: Session,
    *,
    family_id: str,
    student: Student,
    user_text: str,
    sender_nick: str = "家长",
    image_bytes: Optional[bytes] = None,
    mime_type: str = "image/jpeg",
) -> Optional[dict[str, Any]]:
    """Run Doubao Seed 2.1 (multimodal) + DeepSeek Harness on a DingTalk message and execute actions.

    Returns a DingTalk markdown response dict (with `agent_provider` & `executed_actions`),
    or None if no LLM provider succeeded (caller falls back to deterministic rules).
    """
    asg_out = get_student_assignments(db, family_id=family_id, student_id=student.id)
    asg_lines = [
        f"- {a.subject}：{a.page_ref or ''} {a.content}（{'已完成' if a.completed else '进行中'} / 提交:{a.submission_status}）"
        for a in asg_out.assignments[:10]
    ]
    praise_rows = list(
        db.scalars(
            select(GrowthRecord)
            .where(
                GrowthRecord.family_id == family_id,
                GrowthRecord.student_id == student.id,
                GrowthRecord.deleted.is_(False),
                GrowthRecord.category.in_(("teacher_praise", "class_performance")),
            )
            .order_by(GrowthRecord.created_at.desc())
            .limit(5)
        )
    )
    praise_lines = [f"- {g.title}：{g.content}" for g in praise_rows]
    context_text = (
        f"学生：{student.name}（{student.grade}年级）\n"
        f"当前作业（{len(asg_out.assignments)}项）：\n" + ("\n".join(asg_lines) or "暂无") + "\n"
        f"近期老师表扬：\n" + ("\n".join(praise_lines) or "暂无")
    )

    plan: Optional[dict[str, Any]] = None
    used_provider = "none"
    try:
        plan, used_provider = _call_multimodal_doubao_seed(
            user_text=user_text,
            context_text=context_text,
            image_bytes=image_bytes,
            mime_type=mime_type,
        )
    except Exception as exc:
        logger.warning("Doubao Seed DingTalk agent failed (%s), trying DeepSeek fallback", exc)

    if plan is None and not image_bytes:
        try:
            plan, used_provider = _call_deepseek_text(user_text=user_text, context_text=context_text)
        except Exception as exc:
            logger.warning("DeepSeek DingTalk agent failed (%s)", exc)

    if plan is None:
        return None

    now = datetime.now(timezone.utc)
    actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    executed: list[str] = []
    batch_msgs: list[DingTalkMessageIn] = []
    existing_asgs = list(
        db.scalars(
            select(Assignment).where(
                Assignment.family_id == family_id,
                Assignment.student_id == student.id,
            )
        )
    )

    for idx, act in enumerate(actions):
        if not isinstance(act, dict):
            continue
        atype = act.get("type")
        if atype == "clear_assignments":
            for a in existing_asgs:
                db.delete(a)
            db.commit()
            executed.append(f"已清空 {len(existing_asgs)} 条旧作业")
            existing_asgs = []
        elif atype == "add_assignment":
            content = str(act.get("content") or "").strip()
            if not content:
                continue
            subj = str(act.get("subject") or "其他").strip()[:16]
            pref = str(act.get("page_ref") or "").strip()[:32] or None
            if _is_duplicate_assignment(existing_asgs, subj, content, pref):
                executed.append(f"已存在相似作业，自动去重跳过：{subj} · {content[:24]}")
                continue
            smode = str(act.get("submission_mode") or "paper_next_day").strip()
            if smode not in ("paper_next_day", "dingtalk_online", "photo_upload", "none"):
                smode = "paper_next_day"
            batch_msgs.append(
                DingTalkMessageIn(
                    message_id=f"ag_{uuid.uuid4().hex[:10]}_{idx}",
                    sender_id="dingtalk_agent",
                    sender_name=sender_nick,
                    published_at=now,
                    subject=subj,
                    content=content,
                    page_ref=pref,
                    submission_mode=smode,
                    due_at=now + timedelta(hours=16),
                )
            )
            # Track in-batch dedup as well
            existing_asgs.append(
                Assignment(
                    id="tmp", family_id=family_id, student_id=student.id,
                    source_conversation_id="", source_message_id="", sender_id="",
                    subject=subj, content=content, page_ref=pref,
                    published_at=now, expires_at=now,
                )
            )
            executed.append(f"录入作业：{subj} · {content}")
        elif atype == "add_todo":
            content = str(act.get("content") or "").strip()
            if not content:
                continue
            subj = str(act.get("subject") or "家校通知").strip()[:16]
            if _is_duplicate_assignment(existing_asgs, subj, content):
                executed.append(f"已存在相同待办，自动去重跳过：{content[:26]}")
                continue
            asg = Assignment(
                id=f"todo_{uuid.uuid4().hex[:10]}",
                family_id=family_id,
                student_id=student.id,
                source_conversation_id="dingtalk_agent_todo",
                source_message_id=f"todo_{uuid.uuid4().hex[:10]}",
                sender_id=f"parent:{sender_nick}",
                sender_name=sender_nick,
                subject=subj,
                content=content,
                submission_mode="unknown",
                needs_parent_confirm=True,
                unclear_reason="需家长确认或签字的家校通知",
                published_at=now,
                expires_at=now + timedelta(days=7),
            )
            db.add(asg)
            db.commit()
            existing_asgs.append(asg)
            executed.append(f"加入待办：{subj} · {content}")
        elif atype == "add_praise":
            content = str(act.get("content") or "").strip()
            if not content:
                continue
            subj = str(act.get("subject") or "综合").strip()[:16]
            teacher = str(act.get("teacher_name") or "任课老师").strip()[:24]
            title = str(act.get("title") or f"{subj}课堂表扬").strip()[:60]
            if _is_duplicate_praise(praise_rows, title, content):
                executed.append(f"已存在相同表扬记录，自动去重跳过：{title}")
                continue
            rec = GrowthRecord(
                id=f"gr_dt_{uuid.uuid4().hex[:10]}",
                family_id=family_id,
                student_id=student.id,
                category="teacher_praise",
                title=title,
                content=content,
                evidence=f"钉钉同步 · {subj} · {teacher}",
                verified=True,
                deleted=False,
            )
            db.add(rec)
            db.commit()
            praise_rows.append(rec)
            write_learner_memory(
                db,
                family_id=family_id,
                student_id=student.id,
                kind="teacher_praise",
                summary=f"{teacher}表扬{student.name}：{content}"[:200],
                source_kind="dingtalk_agent",
            )
            executed.append(f"记入教师表扬：{teacher} · {title}")
        elif atype == "start_dictation":
            utt = str(act.get("utterance") or user_text).strip()
            sess, spoken = start_dictation_from_request(
                db,
                family_id=family_id,
                student_id=student.id,
                utterance=utt,
                source="parent_dingtalk",
            )
            executed.append(f"听写：{'已下发' if sess else '待补充词表'}（{spoken[:40]}）")
        elif atype == "schedule_reminder":
            mins = max(1, min(720, int(act.get("minutes_from_now") or 15)))
            label = str(act.get("label") or user_text)[:120]
            schedule_or_reschedule_reminder(
                db,
                idempotency_key=f"dtag:{family_id}:{student.id}:{uuid.uuid4().hex[:8]}",
                family_id=family_id,
                student_id=student.id,
                kind="check",
                anchor_label=label,
                scheduled_for=now + timedelta(minutes=mins),
            )
            executed.append(f"预约提醒：{mins}分钟后 · {label}")

    if batch_msgs:
        ingest_dingtalk_batch(
            db,
            DingTalkBatchIn(
                family_id=family_id,
                student_id=student.id,
                conversation_id="dingtalk_agent",
                authorized_teachers={"dingtalk_agent": "综合"},
                page_complete=True,
                watermark_at=now,
                messages=batch_msgs,
            ),
            now=now,
        )

    title = str(plan.get("reply_title") or "芽米已处理").strip()
    md = str(plan.get("reply_markdown") or "").strip()
    if executed:
        md += "\n\n**已执行同步：**\n" + "\n".join(f"- {e}" for e in executed)
    if not md:
        md = f"已收到您的消息（{used_provider}）。"

    return {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": md},
        "agent_provider": used_provider,
        "executed_actions": executed,
        "intent": plan.get("intent", "chat"),
    }
