from __future__ import annotations

import struct

from backend.agent.harness import YamiAgentHarness
from backend.agent.skill_loader import SkillRegistry
from backend.api.main import _trim_pcm_silence


def test_claude_writing_skills_compliance_and_routing(client):
    reg = SkillRegistry()
    skills = {s.name: s for s in reg.list_skills()}
    assert {"homework-companion", "voice-dictation", "chat-companion", "english-speaking"}.issubset(skills.keys())

    # Verify Claude writing-skills frontmatter rules: hyphenated name + "Use when" trigger-only description
    for name, sk in skills.items():
        assert " " not in name
        assert sk.description.startswith("Use when "), f"{name} description must start with 'Use when '"
        assert len(sk.description) <= 500

    # Verify routing across all 4 skills
    assert reg.route("今天有什么作业").name == "homework-companion"
    assert reg.route("我想复习错题").name == "homework-companion"
    assert reg.route("帮我预习第5课").name == "homework-companion"
    assert reg.route("听写第3单元第4课组词").name == "voice-dictation"
    assert reg.route("再读一遍", has_active_dictation=True).name == "voice-dictation"
    assert reg.route("我们来练英语口语吧").name == "english-speaking"
    assert reg.route("Hello how are you").name == "english-speaking"
    assert reg.route("什么意思", last_skill_name="english-speaking").name == "english-speaking"
    assert reg.route("你好你好").name == "chat-companion"

    # Verify API lists loaded skills
    resp = client.get("/v1/agent/skills")
    assert resp.status_code == 200
    names = {item["name"] for item in resp.json()}
    assert {"homework-companion", "voice-dictation", "chat-companion"}.issubset(names)


def test_pcm_silence_trim_and_zero_silent_failure_resilience(db_session, monkeypatch):
    # 1. Pure silence trims to empty
    silence = b"\x00\x00" * 16000
    assert _trim_pcm_silence(silence) == b""

    # 2. Leading + trailing silence around 0.5s speech trims dramatically
    speech = struct.pack("<h", 2000) * 8000  # 0.5s speech
    padded = (b"\x00\x00" * 32000) + speech + (b"\x00\x00" * 32000)  # 4.5s total
    trimmed = _trim_pcm_silence(padded)
    assert 0 < len(trimmed) < len(padded) // 2

    # 3. Even if all cloud LLM calls raise exceptions, Harness ALWAYS returns a warm non-empty reply
    harness = YamiAgentHarness()

    def _always_fail(*args, **kwargs):
        raise TimeoutError("simulated upstream timeout")

    monkeypatch.setattr(harness, "_run_doubao_responses_loop", _always_fail)
    monkeypatch.setattr(harness, "_run_openai_chat_loop", _always_fail)

    res = harness.run_turn(db_session, family_id="fam_a", student_id="stu_he", utterance="你好你好")
    assert res.spoken_text.strip() != ""
    assert res.provider == "local_fallback"
    assert res.skill_name == "chat-companion"
    assert harness.fallback_turns == 1


def test_persistent_conversation_memory_survives_new_harness_instance(db_session):
    h1 = YamiAgentHarness()
    h1._save_turn(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        skill_name="english-speaking",
        user_text="I like apples",
        assistant_text="Great! Do you like bananas too?",
    )

    # Brand new harness instance (simulating server restart) loads prior turns from DB
    h2 = YamiAgentHarness()
    hist, last_skill = h2._load_db_history(db_session, "fam_a", "stu_he", limit=8)
    assert last_skill == "english-speaking"
    assert any("I like apples" in m["content"] for m in hist)
    assert any("bananas" in m["content"] for m in hist)
