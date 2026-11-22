from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from typing import Any, Iterator, Literal, Optional
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.adapters.llm_gateway import (
    CachedPromptEnvelope,
    PromptCacheMetrics,
    extract_cached_tokens,
)
from backend.agent.skill_loader import SkillRegistry
from backend.config import settings
from backend.learning.dictation import (
    get_active_dictation,
    handle_dictation_voice_command,
    start_dictation_from_request,
)
from backend.memory.service import write_learner_memory
from backend.middleware.safety import SafetyMiddleware
from backend.models import Assignment, ConversationTurn, Device, LearnerMemory, ReviewItem, Student, StudentSkillConfig
from backend.rewards.pet_service import (
    TASK_COIN_REWARDS,
    adopt_pet,
    feed_pet,
    get_or_create_pet,
    grant_pet_coins,
    pet_to_dict,
    play_with_pet,
    recall_pet,
    upgrade_pet_habitat,
)
from backend.services.assignments import get_student_assignments, route_voice_assignment_query
from backend.services.desk_check import get_latest_desk_check, queue_desk_check
from backend.services.wellbeing import detect_and_record_emotion
from backend.submissions.service import record_submission_evidence


STATIC_COMPANION_RULES = (
    "语音陪伴规则：\n"
    "1. 你正在通过桌面音箱和孩子语音对话，每次回复控制在 1–2 句话（不超过 45 个汉字），口语化、自然温暖，不使用 Markdown、星号、编号列表或 Emoji。\n"
    "2. 涉及今日作业、教材听写、学习记忆时，必须优先调用对应工具获取真实数据，禁止编造。\n"
    "3. 听写过程中只能报词语和语境提示，绝不拼写拆字；遇到数学题只给最小启发线索，绝不直接说出最终答案。\n"
    "4. 严格隔离不同学生的身份与记忆：只使用当前学生档案与历史对话；如果孩子要求切换学生、换人或报出家里另一个孩子的名字，立即调用 switch_student 工具。"
)


def _build_tool_schemas_chat() -> list[dict[str, Any]]:
    """OpenAI / DeepSeek Chat Completions tool definitions (deterministically sorted by envelope)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "check_student_at_desk",
                "description": "在已获家长摄像头授权的前提下，触发 Mosaico 摄像头（伴随提示音、不保存照片）查看学生当前是否在写字台前学习，并返回最新学习状态摘要。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "control_dictation",
                "description": "控制正在进行中的语音听写，如再读一遍、写好了/下一个、跳过、上一个。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "孩子说的听写控制口令，例如：再读一遍、写好了、下一个、跳过、上一个",
                        }
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_today_assignments",
                "description": "查询当前学生从钉钉班级群同步的今日作业列表与提交状态。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subject": {
                            "type": "string",
                            "description": "可选学科筛选：数学、语文、英语；留空表示全部学科",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "adopt_pet",
                "description": "领养或更换一只动态像素/潮玩宠物。支持3系列×3种共9只：奇幻星灵(星云小龙/棉花糖云/麻薯灵狐)、萌趣伙伴(泡芙水豚/宇航企鹅/呆萌恐龙)、星际潮玩(雷电机甲/极光星龙/樱花灵狐)。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "breed": {
                            "type": "string",
                            "description": "宠物名称或代号，如：星云小龙、棉花糖云、麻薯灵狐、泡芙水豚、宇航企鹅、呆萌恐龙、雷电机甲、极光星龙、樱花灵狐",
                        },
                        "nickname": {
                            "type": "string",
                            "description": "可选昵称",
                        },
                    },
                    "required": ["breed"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "feed_pet",
                "description": "消耗宠粮币给宠物喂食不同食物：能量星果(10)、星云大餐(20)、鲜脆小食(10)、豪华大餐(20)、电池糖(10)、超充能量(20)、彩虹曲奇(15)。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "food": {
                            "type": "string",
                            "description": "食物名称，如：能量星果、星云大餐、鲜脆小食、豪华大餐、电池糖、超充能量、彩虹曲奇",
                        }
                    },
                    "required": ["food"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_pet_status",
                "description": "查询当前孩子的宠物状态：品种、名字、宠粮币、饱食度、开心度、等级、住宿环境等级与升级费用。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "play_with_pet",
                "description": "陪宠物玩耍、摸摸它，触发不同种类（猫/狗/甲壳虫）专属可爱互动并增加开心度。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "upgrade_pet_habitat",
                "description": "消耗宠粮币升级宠物的住宿环境（Lv.1→Lv.4：温馨小窝/观察盒→原木小筑/朽木箱→阳光庭院/雨林缸→星空庄园/星空生态缸）。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recall_pet",
                "description": "当宠物因为连续一周未喂食或未陪伴而离家出走时，花费100枚宠粮币（约5项学习任务）将宠物温情召回小窝。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_review_queue",
                "description": "查询当前孩子待巩固的复习题和听写错词队列。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_family_students",
                "description": "查询当前家庭下所有已绑定的学生名单和年级，并标明当前正在陪伴哪位学生。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "switch_student",
                "description": "将当前设备切换到家庭里的另一位学生，并隔离切换对应的对话记忆与今日作业。当孩子说'我是乐乐'、'切换到元宝'、'换个小朋友'时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "student_name": {
                            "type": "string",
                            "description": "目标学生姓名，如：乐乐、元宝；留空或传'下一个'表示切换到家里另一位学生",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mark_assignment_completed",
                "description": "当孩子说某一科作业写完了（如：数学写完了），标记该科作业完成并自动发放今日小岛成长材料。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subject": {
                            "type": "string",
                            "description": "学科名称：数学、语文 或 英语",
                        }
                    },
                    "required": ["subject"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "report_submission_by_voice",
                "description": "孩子语音自主上报某科作业已提交（child_said_done）或需要家长协助提交（needs_parent）。仅记录提交状态，不替代家长核对也不发小岛材料。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subject": {
                            "type": "string",
                            "description": "学科名称：数学、语文 或 英语",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["child_said_done", "needs_parent"],
                            "description": "child_said_done 表示孩子口述已交，needs_parent 表示需要家长帮忙上传/核对",
                        },
                    },
                    "required": ["subject", "status"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_student_preference",
                "description": "保存孩子明确表达的学习偏好或有效方法（如：以后有数学就先做数学）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "简短客观的一句话记忆摘要",
                        }
                    },
                    "required": ["summary"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "start_curriculum_dictation",
                "description": "按孩子的年级和教材版本检索词库，启动语音听写（如：听写第3单元第4课组词）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "utterance": {
                            "type": "string",
                            "description": "听写请求原文，例如：听写第3单元第4课组词",
                        }
                    },
                    "required": ["utterance"],
                },
            },
        },
    ]


def _build_tool_schemas_responses() -> list[dict[str, Any]]:
    """Volcengine Ark Responses API flat function tool definitions."""
    out: list[dict[str, Any]] = []
    for item in _build_tool_schemas_chat():
        fn = item["function"]
        out.append(
            {
                "type": "function",
                "name": fn["name"],
                "description": fn["description"],
                "parameters": fn["parameters"],
            }
        )
    return out


def _clean_for_voice(text: str) -> str:
    cleaned = re.sub(r"[*#`>_~]+", "", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


@dataclass
class AgentTurnResult:
    intent: str
    spoken_text: str
    screen_title: str
    provider: str
    model: str
    skill_name: str = "chat-companion"
    tool_calls: list[str] = field(default_factory=list)
    safety_blocked: bool = False
    prompt_tokens: int = 0
    cached_tokens: int = 0
    active_student_id: Optional[str] = None


class YamiAgentHarness:
    """
    Skill-extensible, resilient Agentic Harness with:
      - Pre/Post SafetyMiddleware guardrails
      - Dynamic SkillRegistry routing (homework-companion, voice-dictation, chat-companion, ...)
      - 4-layer prefix-stable Prompt Cache envelope
      - Retry + automatic provider failover (Doubao Seed / DeepSeek / Skill-aware warm fallback)
      - Per-student append-only conversation history & stability metrics
    """

    def __init__(
        self,
        safety: Optional[SafetyMiddleware] = None,
        skills: Optional[SkillRegistry] = None,
    ) -> None:
        self.safety = safety or SafetyMiddleware()
        self.skills = skills or SkillRegistry()
        self.metrics = PromptCacheMetrics()
        self.success_turns: int = 0
        self.fallback_turns: int = 0
        self._histories: dict[tuple[str, str], list[dict[str, str]]] = {}

    def _load_db_history(self, db: Session, family_id: str, student_id: str, limit: int = 8) -> tuple[list[dict[str, str]], Optional[str]]:
        """Load the last N persistent conversation turns from DB so Yami remembers prior sentences across restarts."""
        try:
            rows = list(
                db.scalars(
                    select(ConversationTurn)
                    .where(
                        ConversationTurn.family_id == family_id,
                        ConversationTurn.student_id == student_id,
                    )
                    .order_by(ConversationTurn.id.desc())
                    .limit(limit)
                )
            )
        except Exception:
            rows = []
        rows.reverse()
        messages: list[dict[str, str]] = []
        for r in rows:
            messages.append({"role": "user", "content": r.user_text})
            messages.append({"role": "assistant", "content": r.assistant_text})
        last_skill = rows[-1].skill_name if rows else None
        return messages, last_skill

    def _save_turn(
        self,
        db: Session,
        *,
        family_id: str,
        student_id: str,
        skill_name: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        self._append_history(family_id, student_id, user_text, assistant_text)
        try:
            db.add(
                ConversationTurn(
                    family_id=family_id,
                    student_id=student_id,
                    skill_name=skill_name,
                    user_text=user_text,
                    assistant_text=assistant_text,
                )
            )
            db.commit()
        except Exception:
            db.rollback()

    def _get_history(self, family_id: str, student_id: str) -> list[dict[str, str]]:
        return self._histories.setdefault((family_id, student_id), [])

    def _append_history(self, family_id: str, student_id: str, user_text: str, assistant_text: str) -> None:
        hist = self._get_history(family_id, student_id)
        hist.append({"role": "user", "content": user_text})
        hist.append({"role": "assistant", "content": assistant_text})
        # Keep bounded append-only window (last 6 turns = 12 messages)
        if len(hist) > 12:
            del hist[:-12]

    def _switch_active_student(
        self,
        db: Session,
        *,
        family_id: str,
        current_student_id: str,
        target_name: str = "",
    ) -> Optional[Student]:
        rows = list(
            db.scalars(
                select(Student)
                .where(Student.family_id == family_id)
                .order_by(Student.id.asc())
            )
        )
        if not rows:
            return None
        target: Optional[Student] = None
        cleaned = target_name.strip().lower()
        if cleaned and cleaned not in ("下一个", "另一个", "换一个", "换人", "切换", "切换学生"):
            for s in rows:
                if s.name.lower() in cleaned or cleaned in s.name.lower():
                    target = s
                    break
        if target is None and len(rows) >= 2:
            # Cycle to the next student in the family
            cur_idx = next((i for i, s in enumerate(rows) if s.id == current_student_id), 0)
            target = rows[(cur_idx + 1) % len(rows)]
        if target is None:
            target = rows[0]

        devs = list(db.scalars(select(Device).where(Device.family_id == family_id)))
        for d in devs:
            d.active_student_id = target.id
            d.profile_epoch = (d.profile_epoch or 1) + 1
        db.commit()
        return target

    def _detect_voice_student_switch(
        self,
        db: Session,
        *,
        family_id: str,
        current_student_id: str,
        utterance: str,
    ) -> Optional[Student]:
        text = (utterance or "").strip()
        if not text:
            return None
        rows = list(
            db.scalars(
                select(Student)
                .where(Student.family_id == family_id)
                .order_by(Student.id.asc())
            )
        )
        if len(rows) < 2:
            return None

        # Explicit switch command with or without a specific name
        explicit_triggers = ("切换学生", "切换小朋友", "换个学生", "换个小朋友", "换人", "切换到", "换到", "切到", "切换成", "换成")
        if any(k in text for k in explicit_triggers):
            for s in rows:
                if s.name and s.name in text:
                    return self._switch_active_student(
                        db, family_id=family_id, current_student_id=current_student_id, target_name=s.name
                    )
            return self._switch_active_student(
                db, family_id=family_id, current_student_id=current_student_id, target_name=""
            )

        # Self-introduction as ANOTHER student in the family (e.g., "我是乐乐", "现在是乐乐")
        for s in rows:
            if s.id != current_student_id and s.name:
                if f"我是{s.name}" in text or f"现在是{s.name}" in text or f"我叫{s.name}" in text:
                    return self._switch_active_student(
                        db, family_id=family_id, current_student_id=current_student_id, target_name=s.name
                    )
        return None

    def _execute_tool(
        self,
        db: Session,
        *,
        family_id: str,
        student_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "check_student_at_desk":
            stu = db.get(Student, student_id)
            if stu is None or not stu.camera_consent:
                return {
                    "allowed": False,
                    "error": "camera_consent_required",
                    "message": "摄像头授权尚未开启，请先在家长端设置中打开摄像头授权。",
                }
            seq = queue_desk_check("mosaico-yami-s31")
            latest = get_latest_desk_check(student_id)
            return {
                "allowed": True,
                "queued_seq": seq,
                "latest_result": latest,
                "message": (
                    latest["summary"]
                    if latest
                    else f"已向 Mosaico 发送书桌查看请求（#{seq}），设备会响提示音并由 AI 确认{stu.name}是否在写字台前学习，不留存照片。"
                ),
            }

        if name == "get_today_assignments":
            subject = (arguments.get("subject") or "").strip() or None
            data = get_student_assignments(db, family_id=family_id, student_id=student_id, subject=subject)
            return {
                "student_name": data.student_name,
                "stale_warning": data.stale_warning,
                "assignments": [
                    {
                        "subject": a.subject,
                        "page_ref": a.page_ref,
                        "content": a.content,
                        "completed": a.completed,
                        "submission_status": a.submission_status,
                    }
                    for a in data.assignments
                ],
            }

        if name == "start_curriculum_dictation":
            req_text = str(arguments.get("utterance") or "")
            sess, spoken = start_dictation_from_request(
                db,
                family_id=family_id,
                student_id=student_id,
                utterance=req_text,
                source="student_initiated",
            )
            return {
                "found": sess is not None,
                "title": sess.title if sess else None,
                "word_count": len(sess.words) if sess else 0,
                "spoken_prompt": spoken,
            }

        if name == "control_dictation":
            cmd = str(arguments.get("command") or "")
            active = get_active_dictation(db, student_id=student_id)
            if active is None:
                return {"active": False, "message": "当前没有进行中的听写任务"}
            prompt = handle_dictation_voice_command(db, session_id=active.id, utterance=cmd)
            if prompt is None:
                return {"active": True, "recognized": False, "message": "未识别该听写口令"}
            return {
                "active": True,
                "recognized": True,
                "spoken_prompt": prompt.spoken_text,
                "screen_text": prompt.screen_text,
                "is_finished": prompt.is_finished,
            }

        if name == "get_pet_status":
            pet = get_or_create_pet(db, family_id=family_id, student_id=student_id)
            return pet_to_dict(pet)

        if name == "adopt_pet":
            breed = str(arguments.get("breed") or "").strip()
            nick = str(arguments.get("nickname") or "").strip() or None
            return adopt_pet(db, family_id=family_id, student_id=student_id, breed_or_species=breed, nickname=nick)

        if name == "feed_pet":
            food = str(arguments.get("food") or "").strip()
            return feed_pet(db, family_id=family_id, student_id=student_id, food_name=food)

        if name == "upgrade_pet_habitat":
            return upgrade_pet_habitat(db, family_id=family_id, student_id=student_id)

        if name == "play_with_pet":
            return play_with_pet(db, family_id=family_id, student_id=student_id)

        if name == "recall_pet":
            return recall_pet(db, family_id=family_id, student_id=student_id)

        if name == "get_review_queue":
            rows = list(
                db.scalars(
                    select(ReviewItem)
                    .where(
                        ReviewItem.family_id == family_id,
                        ReviewItem.student_id == student_id,
                        ReviewItem.status == "pending",
                    )
                    .limit(6)
                )
            )
            return {
                "count": len(rows),
                "items": [{"topic_id": r.topic_id, "problem_id": r.problem_id} for r in rows],
            }

        if name == "mark_assignment_completed":
            subject = str(arguments.get("subject") or "").strip()
            asg = db.scalar(
                select(Assignment).where(
                    Assignment.family_id == family_id,
                    Assignment.student_id == student_id,
                    Assignment.subject == subject,
                )
            )
            if asg is None:
                return {"found": False, "message": f"没有找到今天的{subject}作业"}
            asg.completed = True
            db.commit()
            coin_reward = TASK_COIN_REWARDS["homework_complete"]
            pet = grant_pet_coins(
                db,
                family_id=family_id,
                student_id=student_id,
                amount=coin_reward,
                reason=f"完成今日{subject}作业",
            )
            return {
                "found": True,
                "subject": subject,
                "completed": True,
                "pet_coins_gained": coin_reward,
                "pet_coins_total": pet.coins,
            }

        if name == "report_submission_by_voice":
            subject = str(arguments.get("subject") or "").strip()
            status = str(arguments.get("status") or "child_said_done").strip()
            if status not in ("child_said_done", "needs_parent"):
                status = "child_said_done"
            asg = db.scalar(
                select(Assignment).where(
                    Assignment.family_id == family_id,
                    Assignment.student_id == student_id,
                    Assignment.subject == subject,
                )
            )
            if asg is None:
                return {"found": False, "message": f"没有找到今天的{subject}作业"}
            _, resolved = record_submission_evidence(
                db,
                family_id=family_id,
                student_id=student_id,
                assignment_id=asg.id,
                status=status,  # type: ignore[arg-type]
            )
            return {
                "found": True,
                "subject": subject,
                "recorded_status": status,
                "resolved_submission_status": resolved,
            }

        if name == "list_family_students":
            rows = list(
                db.scalars(
                    select(Student)
                    .where(Student.family_id == family_id)
                    .order_by(Student.id.asc())
                )
            )
            return {
                "students": [
                    {
                        "student_id": s.id,
                        "name": s.name,
                        "grade": s.grade,
                        "is_current": s.id == student_id,
                    }
                    for s in rows
                ]
            }

        if name == "switch_student":
            target_name = str(arguments.get("student_name") or "").strip()
            target = self._switch_active_student(
                db, family_id=family_id, current_student_id=student_id, target_name=target_name
            )
            if target is None:
                return {"switched": False, "message": "没有找到要切换的学生"}
            return {
                "switched": True,
                "student_id": target.id,
                "student_name": target.name,
                "grade": target.grade,
                "message": f"已切换到{target.name}，对话记忆和今日作业已隔离切换。",
            }

        if name == "save_student_preference":
            summary = str(arguments.get("summary") or "").strip()
            if not summary:
                return {"saved": False}
            mem = write_learner_memory(
                db,
                family_id=family_id,
                student_id=student_id,
                kind="preference",
                summary=summary,
            )
            return {"saved": True, "memory_id": mem.id, "summary": mem.summary}

        return {"error": f"unknown_tool:{name}"}

    def _run_doubao_responses_loop(
        self,
        db: Session,
        *,
        envelope: CachedPromptEnvelope,
        family_id: str,
        student_id: str,
    ) -> tuple[str, list[str], int, int]:
        url = f"{settings.ark_base_url.rstrip('/')}/responses"
        headers = {
            "Authorization": f"Bearer {settings.ark_api_key}",
            "Content-Type": "application/json",
        }
        tools = _build_tool_schemas_responses()
        input_items: list[dict[str, Any]] = envelope.build_messages()
        called_tools: list[str] = []
        total_pt = 0
        total_ct = 0

        for _hop in range(3):
            payload = {
                "model": settings.ark_seed_model,
                "input": input_items,
                "tools": tools,
                "thinking": {"type": "disabled"},
                "max_output_tokens": 96,
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=14) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            pt, ct = extract_cached_tokens(data.get("usage") or {})
            total_pt += pt
            total_ct += ct
            self.metrics.record(pt, ct)

            output_items = data.get("output") or []
            fn_calls = [item for item in output_items if item.get("type") == "function_call"]

            if not fn_calls:
                texts: list[str] = []
                for item in output_items:
                    for c in item.get("content") or []:
                        if c.get("type") == "output_text" and c.get("text"):
                            texts.append(str(c["text"]))
                return _clean_for_voice(" ".join(texts)), called_tools, total_pt, total_ct

            # Append model's function_call items and execute each tool
            for fc in fn_calls:
                input_items.append(fc)
                fn_name = str(fc.get("name") or "")
                call_id = str(fc.get("call_id") or "")
                try:
                    args = json.loads(fc.get("arguments") or "{}")
                except Exception:
                    args = {}
                called_tools.append(fn_name)
                result = self._execute_tool(
                    db,
                    family_id=family_id,
                    student_id=student_id,
                    name=fn_name,
                    arguments=args,
                )
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )

        return "", called_tools, total_pt, total_ct

    def _run_openai_chat_loop(
        self,
        db: Session,
        *,
        base_url: str,
        api_key: str,
        model: str,
        envelope: CachedPromptEnvelope,
        family_id: str,
        student_id: str,
    ) -> tuple[str, list[str], int, int]:
        """Shared OpenAI-compatible tool-calling loop for Volcengine Plan, Tencent Plan, and DeepSeek."""
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        messages: list[dict[str, Any]] = envelope.build_messages()
        tools = _build_tool_schemas_chat()
        called_tools: list[str] = []
        total_pt = 0
        total_ct = 0

        for _hop in range(3):
            payload = {
                "model": model,
                "messages": messages,
                "tools": tools,
                "temperature": 0.4,
                "max_tokens": 96,
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=14) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            pt, ct = extract_cached_tokens(data.get("usage") or {})
            total_pt += pt
            total_ct += ct
            self.metrics.record(pt, ct)

            msg = ((data.get("choices") or [{}])[0]).get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return _clean_for_voice(str(msg.get("content") or "")), called_tools, total_pt, total_ct

            messages.append(msg)
            for tc in tool_calls:
                fn = tc.get("function") or {}
                fn_name = str(fn.get("name") or "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                called_tools.append(fn_name)
                result = self._execute_tool(
                    db,
                    family_id=family_id,
                    student_id=student_id,
                    name=fn_name,
                    arguments=args,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(tc.get("id") or ""),
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        return "", called_tools, total_pt, total_ct

    def run_turn(
        self,
        db: Session,
        *,
        family_id: str,
        student_id: str,
        utterance: str,
        preferred_provider: Literal["auto", "volc_plan", "tencent_plan", "doubao_seed", "deepseek"] = "auto",
    ) -> AgentTurnResult:
        # 1. Pre-hook: Child safety input filter
        safety_in = self.safety.check_input(utterance)
        if not safety_in.allowed:
            return AgentTurnResult(
                intent="safety_redirect",
                spoken_text=safety_in.replacement_text,
                screen_title="回到学习",
                provider="safety_middleware",
                model="local",
                skill_name="safety",
                safety_blocked=True,
            )

        active_dict = get_active_dictation(db, student_id=student_id)
        db_history, last_skill = self._load_db_history(db, family_id, student_id, limit=8)
        active_skill = self.skills.route(
            utterance,
            has_active_dictation=(active_dict is not None),
            last_skill_name=last_skill,
        )

        # Enforce per-student Skill Lifecycle (enable/disable) & record usage + passive emotion signal
        try:
            sk_cfg = db.scalar(
                select(StudentSkillConfig).where(
                    StudentSkillConfig.student_id == student_id,
                    StudentSkillConfig.skill_name == active_skill.name,
                )
            )
            if sk_cfg is not None and not sk_cfg.enabled:
                fallback_sk = self.skills.get("chat-companion")
                if fallback_sk is not None:
                    active_skill = fallback_sk
            elif sk_cfg is not None:
                sk_cfg.invocation_count = int(sk_cfg.invocation_count or 0) + 1
                sk_cfg.last_used_at = datetime.now(timezone.utc)
                db.commit()
            else:
                import uuid as _uuid
                db.add(
                    StudentSkillConfig(
                        id=f"skc_{_uuid.uuid4().hex[:10]}",
                        family_id=family_id,
                        student_id=student_id,
                        skill_name=active_skill.name,
                        enabled=True,
                        lifecycle_status="active",
                        invocation_count=1,
                        last_used_at=datetime.now(timezone.utc),
                    )
                )
                db.commit()
        except Exception:
            db.rollback()

        try:
            detect_and_record_emotion(
                db,
                family_id=family_id,
                student_id=student_id,
                utterance=utterance,
                skill_name=active_skill.name,
            )
        except Exception:
            db.rollback()

        # 2. Fast deterministic paths (zero LLM latency, zero hallucination, 100% reliable)
        switched_stu = self._detect_voice_student_switch(
            db, family_id=family_id, current_student_id=student_id, utterance=utterance
        )
        if switched_stu is not None:
            spoken = f"好呀，已经切换到{switched_stu.name}了！{switched_stu.name}你好，想先看今天的作业还是聊聊天呀？"
            self._save_turn(
                db,
                family_id=family_id,
                student_id=switched_stu.id,
                skill_name="chat-companion",
                user_text=utterance,
                assistant_text=spoken,
            )
            self.success_turns += 1
            return AgentTurnResult(
                intent="switch_student",
                spoken_text=spoken,
                screen_title=f"你好，{switched_stu.name}",
                provider="harness_tool_direct",
                model="deterministic",
                skill_name="chat-companion",
                tool_calls=["switch_student"],
                active_student_id=switched_stu.id,
            )

        if active_skill.name == "voice-dictation":
            if "听写" in utterance:
                sess, spoken = start_dictation_from_request(
                    db,
                    family_id=family_id,
                    student_id=student_id,
                    utterance=utterance,
                    source="student_initiated",
                )
                self._save_turn(db, family_id=family_id, student_id=student_id, skill_name=active_skill.name, user_text=utterance, assistant_text=spoken)
                self.success_turns += 1
                return AgentTurnResult(
                    intent="dictation_start" if sess else "dictation_missing_material",
                    spoken_text=spoken,
                    screen_title="正在听写" if sess else "资料不足",
                    provider="harness_tool_direct",
                    model="deterministic",
                    skill_name=active_skill.name,
                    tool_calls=["start_curriculum_dictation"],
                )
            if active_dict is not None:
                prompt = handle_dictation_voice_command(db, session_id=active_dict.id, utterance=utterance)
                if prompt is not None:
                    spoken_txt = prompt.spoken_text
                    if getattr(prompt, "is_finished", False) or getattr(prompt, "finished", False) or ("全部听写" in spoken_txt) or ("听写完成" in spoken_txt):
                        try:
                            grant_pet_coins(
                                db,
                                family_id=family_id,
                                student_id=student_id,
                                amount=TASK_COIN_REWARDS["dictation_complete"],
                                reason="完成语音听写",
                            )
                            spoken_txt = f"{spoken_txt} 奖励你{TASK_COIN_REWARDS['dictation_complete']}枚宠粮币！"
                        except Exception:
                            pass
                    self._save_turn(db, family_id=family_id, student_id=student_id, skill_name=active_skill.name, user_text=utterance, assistant_text=spoken_txt)
                    self.success_turns += 1
                    return AgentTurnResult(
                        intent="dictation_step",
                        spoken_text=spoken_txt,
                        screen_title=prompt.screen_text,
                        provider="harness_tool_direct",
                        model="deterministic",
                        skill_name=active_skill.name,
                        tool_calls=["control_dictation"],
                    )

        if any(k in utterance for k in ("今天有什么作业", "有什么作业", "作业是什么", "数学作业", "语文作业", "英语作业", "交了没", "要交")):
            intent, spoken, title, _, _ = route_voice_assignment_query(
                db, family_id=family_id, student_id=student_id, utterance=utterance
            )
            self._save_turn(db, family_id=family_id, student_id=student_id, skill_name="homework-companion", user_text=utterance, assistant_text=spoken)
            self.success_turns += 1
            return AgentTurnResult(
                intent=intent,
                spoken_text=spoken,
                screen_title=title,
                provider="harness_tool_direct",
                model="deterministic",
                skill_name="homework-companion",
                tool_calls=["get_today_assignments"],
            )

        # 3. Build 4-layer Prompt Cache envelope with active Skill injected in Layer 1
        stu = db.get(Student, student_id)
        stu_name = stu.name if stu else "小禾"
        memories = list(
            db.scalars(
                select(LearnerMemory).where(
                    LearnerMemory.family_id == family_id,
                    LearnerMemory.student_id == student_id,
                    LearnerMemory.active.is_(True),
                )
            )
        )
        recalled = [m.summary for m in memories[:3]]

        today_summary = ""
        if active_skill.name == "homework-companion":
            try:
                asg_data = get_student_assignments(db, family_id=family_id, student_id=student_id)
                if asg_data.assignments:
                    brief = [
                        f"{a.subject}({a.page_ref or ''}{a.content}, {'已写完' if a.completed else '未写完'}, 提交:{a.submission_status})"
                        for a in asg_data.assignments[:5]
                    ]
                    today_summary = "；今日作业：" + "；".join(brief)
            except Exception:
                today_summary = ""

        if active_skill.name == "pet-companion-house":
            try:
                pet = get_or_create_pet(db, family_id=family_id, student_id=student_id)
                pd = pet_to_dict(pet)
                today_summary += (
                    f"；当前宠物：{pd['breed_cn']}「{pd['pet_name']}」Lv.{pd['level']}，"
                    f"宠粮币{pd['coins']}枚，饱食{pd['hunger']}，开心{pd['happiness']}，"
                    f"住宿Lv.{pd['habitat_lv']}「{pd['habitat_name']}」"
                )
            except Exception:
                pass

        skill_context = f"{STATIC_COMPANION_RULES}\n\n[当前激活技能: {active_skill.name}]\n{active_skill.body}"

        envelope = CachedPromptEnvelope(
            provider="doubao_seed",
            model=settings.ark_seed_model,
            static_topic_context=skill_context,
            session_history=db_history or list(self._get_history(family_id, student_id)),
            dynamic_time_iso=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            recalled_memories=recalled,
            step_state=f"当前学生：{stu_name}（{stu.grade if stu else 4}年级，{stu.textbook_version if stu else '人教版'}）{today_summary}",
            user_utterance=utterance,
        )

        # 4. Resilient Agentic Loop: 1 fast retry per provider + automatic failover (never silent!)
        effective_pref = settings.agent_provider if preferred_provider == "auto" else preferred_provider
        all_candidates = ["volc_plan", "tencent_plan", "doubao_seed", "deepseek"]
        if effective_pref in all_candidates:
            provider_order = [effective_pref] + [p for p in all_candidates if p != effective_pref]
        else:
            provider_order = all_candidates

        reply_text = ""
        used_provider = "local_fallback"
        used_model = "deterministic"
        called_tools: list[str] = []
        pt = 0
        ct = 0

        for prov in provider_order:
            for attempt in range(2):
                try:
                    if prov == "volc_plan" and settings.volc_plan_api_key:
                        reply_text, called_tools, pt, ct = self._run_openai_chat_loop(
                            db,
                            base_url=settings.volc_plan_base_url,
                            api_key=settings.volc_plan_api_key,
                            model=settings.volc_plan_model,
                            envelope=envelope,
                            family_id=family_id,
                            student_id=student_id,
                        )
                        if reply_text:
                            used_provider = "volc_plan"
                            used_model = settings.volc_plan_model
                            break
                    elif prov == "tencent_plan" and settings.tencent_plan_api_key:
                        reply_text, called_tools, pt, ct = self._run_openai_chat_loop(
                            db,
                            base_url=settings.tencent_plan_base_url,
                            api_key=settings.tencent_plan_api_key,
                            model=settings.tencent_plan_model,
                            envelope=envelope,
                            family_id=family_id,
                            student_id=student_id,
                        )
                        if reply_text:
                            used_provider = "tencent_plan"
                            used_model = settings.tencent_plan_model
                            break
                    elif prov == "doubao_seed" and settings.ark_api_key:
                        reply_text, called_tools, pt, ct = self._run_doubao_responses_loop(
                            db, envelope=envelope, family_id=family_id, student_id=student_id
                        )
                        if reply_text:
                            used_provider = "doubao_seed"
                            used_model = settings.ark_seed_model
                            break
                    elif prov == "deepseek" and settings.deepseek_api_key:
                        reply_text, called_tools, pt, ct = self._run_openai_chat_loop(
                            db,
                            base_url=settings.deepseek_base_url,
                            api_key=settings.deepseek_api_key,
                            model=settings.deepseek_chat_model,
                            envelope=envelope,
                            family_id=family_id,
                            student_id=student_id,
                        )
                        if reply_text:
                            used_provider = "deepseek"
                            used_model = settings.deepseek_chat_model
                            break
                    else:
                        break  # Provider unconfigured, skip retry
                except Exception as exc:
                    print(f"[YAMI_LLM_ERR] provider={prov} attempt={attempt + 1} err={type(exc).__name__}: {exc}", flush=True)
                    if attempt == 0:
                        continue
            if reply_text:
                break

        # 5. Skill-aware warm fallback: guarantees 0% empty/hanging replies even if all networks fail
        if not reply_text:
            self.fallback_turns += 1
            if active_skill.name == "homework-companion":
                intent, spoken, title, _, _ = route_voice_assignment_query(
                    db, family_id=family_id, student_id=student_id, utterance=utterance
                )
                return AgentTurnResult(
                    intent=intent,
                    spoken_text=spoken,
                    screen_title=title,
                    provider="local_fallback",
                    model="deterministic",
                    skill_name=active_skill.name,
                    tool_calls=["get_today_assignments"],
                )
            warm = f"你好呀{stu_name}，我在呢！刚刚网络有点小卡顿，你可以再跟我说一遍，或者问我今天有什么作业。"
            return AgentTurnResult(
                intent="skill_warm_fallback",
                spoken_text=warm,
                screen_title="芽米回答",
                provider="local_fallback",
                model="deterministic",
                skill_name=active_skill.name,
            )

        # 6. Post-hook: Anti-spoiler output guard
        allow_direct = any(t in ("get_today_assignments", "start_curriculum_dictation", "control_dictation") for t in called_tools)
        safety_out = self.safety.check_output(reply_text, allow_direct_answer=allow_direct)
        final_spoken = reply_text if safety_out.allowed else safety_out.replacement_text

        self._save_turn(
            db,
            family_id=family_id,
            student_id=student_id,
            skill_name=active_skill.name,
            user_text=utterance,
            assistant_text=final_spoken,
        )
        if active_skill.name == "english-speaking":
            try:
                grant_pet_coins(
                    db,
                    family_id=family_id,
                    student_id=student_id,
                    amount=15,
                    reason="完成英语口语练习",
                )
            except Exception:
                pass
        self.success_turns += 1

        intent_name = "harness_tool_turn" if called_tools else f"harness_{used_provider}"
        if "start_curriculum_dictation" in called_tools:
            screen_title = "正在听写"
        elif "get_today_assignments" in called_tools:
            screen_title = "今日作业"
        else:
            screen_title = "芽米回答"

        return AgentTurnResult(
            intent=intent_name,
            spoken_text=final_spoken,
            screen_title=screen_title,
            provider=used_provider,
            model=used_model,
            skill_name=active_skill.name,
            tool_calls=called_tools,
            safety_blocked=not safety_out.allowed,
            prompt_tokens=pt,
            cached_tokens=ct,
        )

    def stream_doubao_clauses(
        self,
        db: Session,
        *,
        family_id: str,
        student_id: str,
        utterance: str,
    ) -> Iterator[str]:
        """
        Low-latency streaming generator using Doubao Seed Responses API (stream=True).
        Yields natural Chinese clauses on punctuation boundaries (，。？！；) so TTS can
        start speaking the first clause immediately while the rest is still generating.
        """
        safety_in = self.safety.check_input(utterance)
        if not safety_in.allowed:
            yield safety_in.replacement_text
            return

        # Fast deterministic paths for dictation, homework queries, and voice self-reporting
        if "听写" in utterance or get_active_dictation(db, student_id=student_id) is not None:
            turn = self.run_turn(db, family_id=family_id, student_id=student_id, utterance=utterance)
            for seg in re.split(r"(?<=[。！？；])", turn.spoken_text):
                if seg.strip():
                    yield seg.strip()
            return

        if any(k in utterance for k in ("交好了", "交完了", "已提交", "帮我交", "需要妈妈", "需要爸爸")):
            subject = next((s for s in ("数学", "语文", "英语") if s in utterance), "英语")
            st = "needs_parent" if any(k in utterance for k in ("帮我", "需要", "妈妈", "爸爸")) else "child_said_done"
            res = self._execute_tool(
                db,
                family_id=family_id,
                student_id=student_id,
                name="report_submission_by_voice",
                arguments={"subject": subject, "status": st},
            )
            if res.get("found"):
                if st == "needs_parent":
                    yield f"好，已经记下{subject}需要家长帮忙提交，我会提醒爸爸妈妈。"
                else:
                    yield f"好，已经记下你说{subject}提交好了，等家长确认就行。"
                return

        if any(k in utterance for k in ("今天有什么作业", "有什么作业", "作业是什么", "数学作业", "语文作业", "英语作业")):
            turn = self.run_turn(db, family_id=family_id, student_id=student_id, utterance=utterance)
            for seg in re.split(r"(?<=[。！？；])", turn.spoken_text):
                if seg.strip():
                    yield seg.strip()
            return

        db_history, last_skill = self._load_db_history(db, family_id, student_id, limit=8)
        active_skill = self.skills.route(utterance, last_skill_name=last_skill)

        # Tool-heavy skills (Pet House actions, student switching, homework completion, preferences)
        # run through the full Agentic tool loop and stream clause-by-clause.
        needs_tools = (
            active_skill.name == "pet-companion-house"
            or any(k in utterance for k in ("切换", "换人", "换个小朋友", "写完了", "做完了", "以后有", "记住"))
        )
        if needs_tools or not settings.ark_api_key or not settings.ark_seed_model:
            turn = self.run_turn(db, family_id=family_id, student_id=student_id, utterance=utterance)
            for seg in re.split(r"(?<=[。！？；])", turn.spoken_text):
                if seg.strip():
                    yield seg.strip()
            return

        stu = db.get(Student, student_id)
        stu_name = stu.name if stu else "小禾"
        memories = list(
            db.scalars(
                select(LearnerMemory).where(
                    LearnerMemory.family_id == family_id,
                    LearnerMemory.student_id == student_id,
                    LearnerMemory.active.is_(True),
                )
            )
        )
        envelope = CachedPromptEnvelope(
            provider="doubao_seed",
            model=settings.ark_seed_model,
            static_topic_context=f"{STATIC_COMPANION_RULES}\n\n[当前激活技能: {active_skill.name}]\n{active_skill.body}",
            session_history=db_history or list(self._get_history(family_id, student_id)),
            dynamic_time_iso=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            recalled_memories=[m.summary for m in memories[:3]],
            step_state=f"当前学生：{stu_name}（{stu.grade if stu else 4}年级，{stu.textbook_version if stu else '人教版'}）",
            user_utterance=utterance,
        )

        payload = {
            "model": settings.ark_seed_model,
            "input": envelope.build_messages(),
            "thinking": {"type": "disabled"},
            "max_output_tokens": 96,
            "stream": True,
        }
        req = urllib.request.Request(
            f"{settings.ark_base_url.rstrip('/')}/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {settings.ark_api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )

        buf = ""
        full_parts: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=18) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str or data_str == "[DONE]":
                        break
                    try:
                        ev = json.loads(data_str)
                    except Exception:
                        continue
                    if ev.get("type") == "response.output_text.delta":
                        delta = str(ev.get("delta") or "")
                        buf += delta
                        # Flush at natural Chinese clause boundary once >= 6 chars
                        while len(buf) >= 6:
                            m = re.search(r"[，。？！；]", buf[4:])
                            if not m:
                                break
                            cut = 4 + m.end()
                            clause = _clean_for_voice(buf[:cut])
                            buf = buf[cut:]
                            if clause:
                                safe = self.safety.check_output(clause, allow_direct_answer=False)
                                out_clause = clause if safe.allowed else safe.replacement_text
                                full_parts.append(out_clause)
                                yield out_clause
        except Exception:
            if not full_parts:
                turn = self.run_turn(db, family_id=family_id, student_id=student_id, utterance=utterance)
                yield turn.spoken_text
                return

        tail = _clean_for_voice(buf)
        if tail:
            safe = self.safety.check_output(tail, allow_direct_answer=False)
            out_tail = tail if safe.allowed else safe.replacement_text
            full_parts.append(out_tail)
            yield out_tail

        if full_parts:
            self._save_turn(
                db,
                family_id=family_id,
                student_id=student_id,
                skill_name=active_skill.name,
                user_text=utterance,
                assistant_text="".join(full_parts),
            )
