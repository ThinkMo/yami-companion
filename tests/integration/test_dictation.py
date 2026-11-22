from __future__ import annotations

from backend.learning.dictation import (
    complete_dictation_self_check,
    dictation_action,
    start_dictation_from_request,
)


def test_natural_language_curriculum_dictation_by_voice_and_dingtalk(client, db_session):
    # 1. Parent sends natural-language DingTalk request: "听写第3单元第4课组词"
    sess, intro = start_dictation_from_request(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        utterance="听写第3单元第4课组词",
        source="parent_dingtalk",
    )
    assert sess is not None
    assert "第3单元第4课组词" in sess.title
    assert "均匀" in intro

    # 2. Missing curriculum lesson returns honest fallback (never hallucinates words)
    missing_sess, missing_msg = start_dictation_from_request(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        utterance="听写第9单元第1课组词",
        source="parent_dingtalk",
    )
    assert missing_sess is None
    assert "还没找到" in missing_msg

    # 3. Pure voice-driven dictation via /v1/voice/query (no screen button taps needed!)
    code = client.post("/v1/pairing/issue", json={"hardware_uid": "mosaico-dict-voice"}).json()["pairing_code"]
    tok = client.post("/v1/pairing/claim", json={"family_id": "fam_a", "pairing_code": code}).json()["device_token"]
    sel = client.post(
        "/v1/devices/select-student",
        headers={"X-Device-Token": tok},
        json={"student_id": "stu_he", "method": "manual"},
    ).json()

    # Child says: "听写第3单元第4课组词"
    start = client.post(
        "/v1/voice/query",
        headers={"X-Device-Token": tok},
        json={"session_id": sel["session_id"], "profile_epoch": sel["profile_epoch"], "utterance": "听写第3单元第4课组词"},
    ).json()
    assert start["intent"] == "dictation_start"
    assert "均匀" in start["spoken_text"]
    assert "均匀" not in start["screen_title"]  # Screen NEVER shows characters

    # Child says: "再读一遍"
    rep = client.post(
        "/v1/voice/query",
        headers={"X-Device-Token": tok},
        json={"session_id": sel["session_id"], "profile_epoch": sel["profile_epoch"], "utterance": "再读一遍"},
    ).json()
    assert rep["intent"] == "dictation_step" and "均匀" in rep["spoken_text"]

    # Child says: "写好了" -> advances to word 2 (重叠)
    w2 = client.post(
        "/v1/voice/query",
        headers={"X-Device-Token": tok},
        json={"session_id": sel["session_id"], "profile_epoch": sel["profile_epoch"], "utterance": "写好了"},
    ).json()
    assert w2["intent"] == "dictation_step" and "重叠" in w2["spoken_text"] and "重叠" not in w2["screen_title"]

    # Child says: "跳过" -> skips word 2, advances to word 3 (空隙)
    w3 = client.post(
        "/v1/voice/query",
        headers={"X-Device-Token": tok},
        json={"session_id": sel["session_id"], "profile_epoch": sel["profile_epoch"], "utterance": "跳过"},
    ).json()
    assert w3["intent"] == "dictation_step" and "空隙" in w3["spoken_text"]

    # Complete self-check: missed + skipped words queued for next-day review
    from backend.learning.dictation import get_active_dictation
    active = get_active_dictation(db_session, student_id="stu_he")
    assert active is not None
    done_sess, queued = complete_dictation_self_check(db_session, session_id=active.id, missed_words=["住宅"])
    assert done_sess.status == "completed"
    assert set(q.problem_id for q in queued) == {"dictation_word:重叠", "dictation_word:住宅"}


def test_guardian_natural_dictation_and_hardware_audio_turn(client):
    # Guardian Web natural-language dictation trigger
    nat = client.post(
        "/v1/guardian/dictation/natural",
        headers={"X-Family-Id": "fam_a"},
        json={"student_id": "stu_he", "utterance": "听写第3单元第4课组词"},
    )
    assert nat.status_code == 200
    body = nat.json()
    assert body["found"] is True
    assert body["word_count"] >= 4
    assert "均匀" in body["spoken_text"]

    # Hardware voice audio-turn endpoint returns 16kHz PCM + headers
    fake_pcm = b"\x00\x00" * 1600  # 100ms silence
    audio_resp = client.post(
        "/v1/voice/audio-turn",
        params={"utterance": "写好了"},
        headers={"X-Hardware-Uid": "mosaico-hw-voice-1"},
        content=fake_pcm,
    )
    assert audio_resp.status_code == 200
    assert audio_resp.headers["content-type"].startswith("audio/pcm")
    assert len(audio_resp.content) > 1000
    assert audio_resp.headers["x-yami-intent"] == "dictation_step"
    assert audio_resp.headers["x-yami-audio-in-bytes"] == str(len(fake_pcm))
