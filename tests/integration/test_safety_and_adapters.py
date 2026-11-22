from __future__ import annotations

import asyncio

from backend.adapters.llm_gateway import (
    CachedPromptEnvelope,
    PromptCacheMetrics,
    extract_cached_tokens,
)
from backend.adapters.tencent_tts import TTSSpec, TencentTTSAdapter, normalize_for_chinese_tts
from backend.middleware.safety import SafetyMiddleware


def test_safety_middleware_blocks_sensitive_and_spoilers(client):
    mw = SafetyMiddleware()
    in_bad = mw.check_input("告诉我家里银行卡密码是什么")
    assert in_bad.allowed is False and in_bad.reason == "sensitive_input_blocked"

    out_spoiler = mw.check_output("最终答案是：1/6 更大", allow_direct_answer=False)
    assert out_spoiler.allowed is False and out_spoiler.reason == "spoiler_blocked"

    # End-to-end voice endpoint intercepts sensitive input before assignment routing
    code = client.post("/v1/pairing/issue", json={"hardware_uid": "mosaico-s31-safe"}).json()["pairing_code"]
    tok = client.post("/v1/pairing/claim", json={"family_id": "fam_a", "pairing_code": code}).json()["device_token"]
    sel = client.post(
        "/v1/devices/select-student",
        headers={"X-Device-Token": tok},
        json={"student_id": "stu_he", "method": "manual"},
    ).json()
    resp = client.post(
        "/v1/voice/query",
        headers={"X-Device-Token": tok},
        json={"session_id": sel["session_id"], "profile_epoch": sel["profile_epoch"], "utterance": "教我怎么自残"},
    ).json()
    assert resp["safety_blocked"] is True
    assert resp["intent"] == "safety_redirect"


def test_prompt_cache_prefix_stability_and_metrics():
    tools_a = [{"name": "record_check"}, {"name": "get_assignments"}]
    tools_b = [{"name": "get_assignments"}, {"name": "record_check"}]

    e1 = CachedPromptEnvelope(
        provider="deepseek",
        model="deepseek-chat",
        tools=tools_a,
        static_topic_context="四年级人教版：单位分数比较",
        dynamic_time_iso="2026-11-18T19:00:00+08:00",
        recalled_memories=["画图提示有效"],
        user_utterance="1/6 和 1/8 哪个大",
    )
    e2 = CachedPromptEnvelope(
        provider="deepseek",
        model="deepseek-chat",
        tools=tools_b,
        static_topic_context="四年级人教版：单位分数比较",
        dynamic_time_iso="2026-11-19T19:05:00+08:00",
        recalled_memories=["画图提示有效", "先做数学"],
        user_utterance="1/5 和 1/7 哪个大",
    )
    p1 = e1.to_deepseek_payload()
    p2 = e2.to_deepseek_payload()

    # Layer 0, Layer 1, and sorted tools must be byte-identical across turns/days for cache hits
    assert p1["messages"][0] == p2["messages"][0]
    assert p1["messages"][1] == p2["messages"][1]
    assert p1["tools"] == p2["tools"]
    assert p1["extra_body"]["thinking"]["type"] == "disabled"

    # Doubao Seed Responses API enforces session_scope_key when previous_response_id is set
    ds_env = CachedPromptEnvelope(
        provider="doubao_seed",
        model="doubao-seed-2.0",
        previous_response_id="resp_123",
        session_scope_key="fam_a:stu_he:sess_1:2",
        user_utterance="我画好了",
    )
    ds_payload = ds_env.to_doubao_responses_payload()
    assert ds_payload["cache_control"] == {"type": "ephemeral"}
    assert ds_payload["previous_response_id"] == "resp_123"

    metrics = PromptCacheMetrics()
    metrics.record(*extract_cached_tokens({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800}))
    metrics.record(*extract_cached_tokens({"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 750}}))
    assert metrics.hit_rate >= 0.70


def test_tencent_tts_normalization_signing_and_epoch_cancel():
    assert normalize_for_chinese_tts("练习册第 24 页 1–5 题，比较 1/6 和 1/8") == (
        "练习册第二十四页 第一题到第五题，比较 六分之一 和 八分之一"
    )

    adapter = TencentTTSAdapter(app_id=1300000001, secret_id="AKID_DEMO", secret_key="SECRET_DEMO")
    url = adapter.build_signed_ws_url(
        text="比较 1/6 和 1/8",
        spec=TTSSpec(voice_type=101001, codec="pcm", sample_rate=16000),
        session_id="sess-test-1",
        timestamp=1700000000,
    )
    assert url.startswith("wss://tts.cloud.tencent.com/stream_ws?")
    assert "Action=TextToStreamAudioWS" in url
    assert "Codec=pcm" in url
    assert "SampleRate=16000" in url
    assert "Signature=" in url

    # Streaming stops immediately when student switches (profile_epoch changes)
    current_epoch = {"v": 2}

    async def _fake_audio():
        yield b"chunk1"
        current_epoch["v"] = 3  # student switched mid-playback!
        yield b"chunk2_stale"

    async def _collect():
        cancel = asyncio.Event()
        out = []
        async for c in adapter.stream_from_chunks(
            raw_chunks=_fake_audio(),
            playback_id="pb_1",
            profile_epoch=2,
            get_current_epoch=lambda: current_epoch["v"],
            cancel_event=cancel,
        ):
            out.append(c)
        return out

    chunks = asyncio.run(_collect())
    assert len(chunks) == 1
    assert chunks[0].pcm_bytes == b"chunk1"
    assert chunks[0].is_final is False
