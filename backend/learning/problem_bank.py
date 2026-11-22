from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class VerifiedProblem:
    problem_id: str
    topic_id: str
    grade: int
    textbook_version: str
    question_text: str
    hints: tuple[str, str, str]  # Level 1 clue, Level 2 diagram, Level 3 worked example
    review_variant_question: str
    method_card_id: str
    method_card_front: str
    method_card_back: str
    required_answer_tokens: tuple[str, ...]
    required_reason_tokens: tuple[str, ...]


@dataclass(frozen=True)
class PreviewPrompt:
    preview_id: str
    source_label: str
    question_text: str
    note_text: str


VERIFIED_PROBLEMS: dict[str, VerifiedProblem] = {
    "math_g4_unit_fraction_v1": VerifiedProblem(
        problem_id="math_g4_unit_fraction_v1",
        topic_id="unit_fraction_compare",
        grade=4,
        textbook_version="人教版",
        question_text="第 3 题：比较 1/6 和 1/8 哪个大",
        hints=(
            "先画两个一样大的整体，一个分成 6 份，一个分成 8 份，各取一份看看。",
            "示意图：两个同样长的纸条，分成 6 份的 1 份，比分 8 份的 1 份更长。",
            "类似例题：同一个披萨平均分给 2 个人和 4 个人，每人拿到的哪一块更大？",
        ),
        review_variant_question="两个一样大的蛋糕，各取 1/5 和 1/7，哪块更大？说说为什么。",
        method_card_id="card_same_whole",
        method_card_front="先看整体",
        method_card_back="比较单位分数前，先确认两个整体一样大。例：同样大的蛋糕，1/5 比 1/7 大。",
        required_answer_tokens=("1/5", "五分之一"),
        required_reason_tokens=("一样大", "同样大", "份数", "分的少", "分 5 份", "分成5份", "分成 5 份"),
    ),
    "math_g3_carry_add_v1": VerifiedProblem(
        problem_id="math_g3_carry_add_v1",
        topic_id="two_digit_carry_add",
        grade=3,
        textbook_version="北师大版",
        question_text="第 5 题：36 + 47",
        hints=(
            "先算个位 6 加 7 是多少，看看满十了没有？",
            "画一画：13 可以拆成 1 个十和 3 个一，个位写 3，向十位进 1。",
            "类似例题：28 + 15，个位 8 + 5 = 13，写 3 进 1，十位 2 + 1 + 1 = 4，结果是 43。",
        ),
        review_variant_question="45 + 38，个位满十了吗？说说你怎么算。",
        method_card_id="card_carry_one",
        method_card_front="满十进一",
        method_card_back="两位数加法先算个位，满十就向十位进 1。例：45 + 38，个位 5 + 8 = 13，写 3 进 1。",
        required_answer_tokens=("83", "满十"),
        required_reason_tokens=("进1", "进 1", "进一", "13", "5+8", "5 加 8"),
    ),
}

PREVIEW_PROMPTS: dict[str, PreviewPrompt] = {
    "math_g4_area_preview_v1": PreviewPrompt(
        preview_id="math_g4_area_preview_v1",
        source_label="王老师 · 明天第 5 单元《面积》",
        question_text="用同样大的纸片铺两本书，哪本需要更多纸片？",
        note_text="先猜一猜，明天上课看看你的猜法。",
    )
}


def get_problem(problem_id: str) -> Optional[VerifiedProblem]:
    return VERIFIED_PROBLEMS.get(problem_id)


def grade_review_explanation(problem: VerifiedProblem, utterance: str) -> str:
    """
    Deterministic explainable grader for verified elementary math variants.
    Returns 'pass' only when both the answer and the reasoning are present;
    otherwise returns 'pending_verify' (never fabricates verified mastery).
    """
    text = (utterance or "").strip()
    has_answer = any(tok in text for tok in problem.required_answer_tokens)
    has_reason = any(tok in text for tok in problem.required_reason_tokens)
    if has_answer and has_reason:
        return "pass"
    return "pending_verify"
