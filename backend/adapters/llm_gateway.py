from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Literal, Optional


GLOBAL_FROZEN_SYSTEM_PREFIX = (
    "你是《芽米学习伙伴》，陪伴中国大陆小学三至四年级孩子完成家庭作业的桌面伙伴。\n"
    "行为准则：\n"
    "1. 始终保持温和、简短、具体，不评价孩子本人，只描述具体学习动作。\n"
    "2. 数学答疑绝不直接给出最终答案；依次引导确认题意、孩子先说一步、给出一条最小线索、回到纸笔。\n"
    "3. 严格按指定 JSON Schema 输出，不编造未知的教材页码、题号或截止时间。"
)


@dataclass
class PromptCacheMetrics:
    total_prompt_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0

    @property
    def hit_rate(self) -> float:
        if self.total_prompt_tokens == 0:
            return 0.0
        return self.cached_tokens / self.total_prompt_tokens

    def record(self, prompt_tokens: int, cached_tokens: int) -> None:
        self.calls += 1
        self.total_prompt_tokens += max(0, prompt_tokens)
        self.cached_tokens += max(0, cached_tokens)


@dataclass
class CachedPromptEnvelope:
    """
    4-layer prefix-stable prompt builder for DeepSeek Context Caching on Disk
    and Volcengine Ark Responses API Prompt Caching:
      Layer 0: Global frozen system prefix + deterministically sorted tool schemas
      Layer 1: Static textbook/topic rules (no timestamps, names, or random IDs)
      Layer 2: Append-only session history (no reasoning_content across user turns)
      Layer 3: Dynamic suffix (current time, <=3 recalled memories, step state, child utterance)
    """

    provider: Literal["deepseek", "doubao_seed"]
    model: str
    thinking_enabled: bool = False
    reasoning_effort: Optional[Literal["low", "high", "max"]] = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    static_topic_context: str = ""
    session_history: list[dict[str, str]] = field(default_factory=list)
    dynamic_time_iso: str = ""
    recalled_memories: list[str] = field(default_factory=list)
    step_state: str = ""
    user_utterance: str = ""
    previous_response_id: Optional[str] = None
    session_scope_key: Optional[str] = None  # (family_id, student_id, session_id, profile_epoch)

    def _sorted_tools(self) -> list[dict[str, Any]]:
        return sorted(self.tools, key=lambda t: json.dumps(t, sort_keys=True, ensure_ascii=False))

    def build_messages(self) -> list[dict[str, str]]:
        # Layer 0: byte-identical global prefix
        messages: list[dict[str, str]] = [{"role": "system", "content": GLOBAL_FROZEN_SYSTEM_PREFIX}]

        # Layer 1: static topic/textbook rules (zero dynamic fields)
        if self.static_topic_context.strip():
            messages.append({"role": "system", "content": self.static_topic_context.strip()})

        # Layer 2: append-only history (strip any reasoning_content across user turns)
        for turn in self.session_history:
            messages.append({"role": turn["role"], "content": turn["content"]})

        # Layer 3: dynamic suffix placed strictly at the end
        suffix_lines: list[str] = []
        if self.dynamic_time_iso:
            suffix_lines.append(f"[当前时间] {self.dynamic_time_iso}")
        if self.step_state:
            suffix_lines.append(f"[当前步骤] {self.step_state}")
        if self.recalled_memories:
            capped = self.recalled_memories[:3]
            suffix_lines.append("[相关记忆]\n" + "\n".join(f"- {m}" for m in capped))
        suffix_lines.append(f"[孩子说] {self.user_utterance}")

        messages.append({"role": "user", "content": "\n".join(suffix_lines)})
        return messages

    def to_deepseek_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self.build_messages(),
            "extra_body": {
                "thinking": {"type": "enabled" if self.thinking_enabled else "disabled"}
            },
        }
        if self.thinking_enabled and self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        sorted_tools = self._sorted_tools()
        if sorted_tools:
            payload["tools"] = sorted_tools
        return payload

    def to_doubao_responses_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": self.build_messages(),
            "thinking": {"type": "enabled" if self.thinking_enabled else "disabled"},
            "cache_control": {"type": "ephemeral"},
        }
        if self.previous_response_id:
            if not self.session_scope_key:
                raise ValueError("previous_response_id requires explicit session_scope_key binding")
            payload["previous_response_id"] = self.previous_response_id
        sorted_tools = self._sorted_tools()
        if sorted_tools:
            payload["tools"] = sorted_tools
        return payload


def extract_cached_tokens(usage: dict[str, Any]) -> tuple[int, int]:
    """Normalize cache usage fields across DeepSeek and Volcengine Ark Responses API."""
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    if "prompt_cache_hit_tokens" in usage:
        cached = int(usage["prompt_cache_hit_tokens"])
    elif isinstance(usage.get("prompt_tokens_details"), dict):
        cached = int(usage["prompt_tokens_details"].get("cached_tokens") or 0)
    elif isinstance(usage.get("input_tokens_details"), dict):
        cached = int(usage["input_tokens_details"].get("cached_tokens") or 0)
    else:
        cached = 0
    return prompt_tokens, cached
