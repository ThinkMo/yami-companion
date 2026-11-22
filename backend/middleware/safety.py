from __future__ import annotations

from dataclasses import dataclass
import re

SENSITIVE_PATTERNS = [
    re.compile(r"(自杀|自残|伤害自己|跳楼)"),
    re.compile(r"(色情|黄片|裸体|暴力血腥)"),
    re.compile(r"(银行卡密码|身份证号|家庭住址在哪|帮我黑进)"),
]

SPOILER_PATTERNS = [
    re.compile(r"(最终答案是|直接告诉你答案|正确答案就是)\s*[:：]?\s*\S+"),
]

SAFE_REDIRECT_TEXT = "这个问题超出了芽米的作业范围，我们回到今天的任务好不好？"
SAFE_HINT_FALLBACK = "先别急着看结果，说说你试到哪一步了，我给你一条小线索。"


@dataclass(frozen=True)
class SafetyCheckResult:
    allowed: bool
    reason: str = "ok"
    replacement_text: str = ""


class SafetyMiddleware:
    """Stateless child-safety & anti-spoiler guard for gateway input and output."""

    def check_input(self, utterance: str) -> SafetyCheckResult:
        text = (utterance or "").strip()
        for pat in SENSITIVE_PATTERNS:
            if pat.search(text):
                return SafetyCheckResult(
                    allowed=False,
                    reason="sensitive_input_blocked",
                    replacement_text=SAFE_REDIRECT_TEXT,
                )
        return SafetyCheckResult(allowed=True)

    def check_output(self, reply: str, *, allow_direct_answer: bool = False) -> SafetyCheckResult:
        text = (reply or "").strip()
        for pat in SENSITIVE_PATTERNS:
            if pat.search(text):
                return SafetyCheckResult(
                    allowed=False,
                    reason="sensitive_output_blocked",
                    replacement_text=SAFE_REDIRECT_TEXT,
                )
        if not allow_direct_answer:
            for pat in SPOILER_PATTERNS:
                if pat.search(text):
                    return SafetyCheckResult(
                        allowed=False,
                        reason="spoiler_blocked",
                        replacement_text=SAFE_HINT_FALLBACK,
                    )
        return SafetyCheckResult(allowed=True)
