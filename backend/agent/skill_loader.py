from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Optional


SKILLS_DIR = Path(__file__).resolve().parent / "skills"


@dataclass(frozen=True)
class AgentSkill:
    name: str
    description: str
    body: str
    path: Path


def _parse_skill_md(path: Path) -> Optional[AgentSkill]:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, flags=re.DOTALL)
    if not m:
        return None
    frontmatter, body = m.group(1), m.group(2).strip()
    name = ""
    desc = ""
    for line in frontmatter.splitlines():
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("description:"):
            desc = line.split(":", 1)[1].strip().strip('"').strip("'")
    if not name or not desc:
        return None
    return AgentSkill(name=name, description=desc, body=body, path=path)


class SkillRegistry:
    """Discovers and routes modular Yami Agent Skills from backend/agent/skills/*/SKILL.md."""

    def __init__(self, skills_dir: Path = SKILLS_DIR) -> None:
        self.skills_dir = skills_dir
        self._skills: dict[str, AgentSkill] = {}
        self.reload()

    def reload(self) -> None:
        self._skills.clear()
        if not self.skills_dir.exists():
            return
        for skill_md in sorted(self.skills_dir.glob("*/SKILL.md")):
            parsed = _parse_skill_md(skill_md)
            if parsed is not None:
                self._skills[parsed.name] = parsed

    def list_skills(self) -> list[AgentSkill]:
        return list(self._skills.values())

    def get(self, name: str) -> Optional[AgentSkill]:
        return self._skills.get(name)

    def route(
        self,
        utterance: str,
        *,
        has_active_dictation: bool = False,
        last_skill_name: Optional[str] = None,
    ) -> AgentSkill:
        """Select the best matching skill for the child's utterance."""
        text = (utterance or "").strip()

        if "听写" in text or (
            has_active_dictation
            and any(k in text for k in ("再读", "重读", "写好", "下一个", "下一词", "跳过", "上一个"))
        ):
            if "voice-dictation" in self._skills:
                return self._skills["voice-dictation"]

        # English speaking skill: explicit request, English utterance, or continuing active English practice
        english_words = re.findall(r"[A-Za-z]{2,}", text)
        exit_english = any(k in text for k in ("不练了", "回中文", "说中文", "看作业", "写作业"))
        if "english-speaking" in self._skills and not exit_english:
            if any(k in text.lower() for k in ("口语", "说英语", "练英语", "英语对话", "english")) or len(english_words) >= 2:
                return self._skills["english-speaking"]
            if last_skill_name == "english-speaking" and (len(english_words) >= 1 or any(k in text for k in ("什么意思", "再说一遍", "听不懂", "继续"))):
                return self._skills["english-speaking"]

        pet_keywords = (
            "玩游戏",
            "开始游戏",
            "打开游戏",
            "宠物",
            "领养",
            "养猫",
            "养狗",
            "甲壳虫",
            "甲虫",
            "橘猫",
            "布偶",
            "三花",
            "金毛",
            "哈士奇",
            "二哈",
            "柯基",
            "独角仙",
            "长戟",
            "大兜",
            "毛象",
            "喂食",
            "喂它",
            "喂猫",
            "喂狗",
            "喂虫",
            "小鱼干",
            "猫罐头",
            "肉骨头",
            "鲜肉大餐",
            "果冻",
            "彩虹曲奇",
            "宠粮币",
            "升级小窝",
            "升级住宿",
            "升级环境",
            "升级房间",
            "宠物屋",
            "小屋",
            "摸摸",
            "陪它玩",
        )
        if any(k in text for k in pet_keywords):
            if "pet-companion-house" in self._skills:
                return self._skills["pet-companion-house"]

        homework_keywords = (
            "作业",
            "复习",
            "预习",
            "错题",
            "数学",
            "语文",
            "英语",
            "练习册",
            "背诵",
            "朗读",
            "写完",
            "交好",
            "交完",
            "提交",
            "分之",
            "怎么算",
            "怎么做",
            "不会做",
            "这道题",
            "第几课",
        )
        if any(k in text for k in homework_keywords):
            if "homework-companion" in self._skills:
                return self._skills["homework-companion"]

        if "chat-companion" in self._skills:
            return self._skills["chat-companion"]

        # Fallback to first loaded skill or empty stub
        if self._skills:
            return next(iter(self._skills.values()))
        return AgentSkill(name="default", description="default", body="", path=self.skills_dir)
