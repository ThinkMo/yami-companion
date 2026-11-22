from __future__ import annotations

import json
from pathlib import Path


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "dingtalk" / "sample_messages.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_incremental_sync_incomplete_page_revision_auth_failure_and_voice_query(client):
    fx = _load_fixture()
    init_batch = fx["initial_batch"]

    # 1. Incomplete pagination must NOT advance watermark
    incomplete_resp = client.post(
        "/v1/sync/dingtalk/ingest",
        json={
            "family_id": "fam_a",
            "student_id": "stu_he",
            "conversation_id": fx["conversation_id"],
            "authorized_teachers": fx["authorized_teachers"],
            "page_complete": False,
            "watermark_at": init_batch["watermark_iso"],
            "messages": init_batch["messages"][:1],
        },
    )
    assert incomplete_resp.status_code == 200
    body = incomplete_resp.json()
    assert body["watermark_advanced"] is False
    assert body["watermark_at"] is None
    assert body["created"] == 1

    # 2. Full overlapping batch is idempotent on msg_001, filters untrusted parent_other, flags unclear & attachment
    full_resp = client.post(
        "/v1/sync/dingtalk/ingest",
        json={
            "family_id": "fam_a",
            "student_id": "stu_he",
            "conversation_id": fx["conversation_id"],
            "authorized_teachers": fx["authorized_teachers"],
            "page_complete": True,
            "watermark_at": init_batch["watermark_iso"],
            "messages": init_batch["messages"],
        },
    )
    assert full_resp.status_code == 200
    body = full_resp.json()
    assert body["watermark_advanced"] is True
    assert body["created"] == 2  # msg_002, msg_003
    assert body["skipped_duplicate"] == 1  # msg_001
    assert body["skipped_untrusted_sender"] == 1  # msg_004 (parent_other)
    assert body["pending_parent_confirm"] == 2  # msg_002 (unclear) + msg_003 (permission_denied audio)

    # 3. Teacher correction updates math assignment in-place to v2 (and replay is idempotent)
    corr = fx["correction_batch"]
    corr_payload = {
        "family_id": "fam_a",
        "student_id": "stu_he",
        "conversation_id": fx["conversation_id"],
        "authorized_teachers": fx["authorized_teachers"],
        "page_complete": True,
        "watermark_at": corr["watermark_iso"],
        "messages": corr["messages"],
    }
    r1 = client.post("/v1/sync/dingtalk/ingest", json=corr_payload).json()
    assert r1["revised"] == 1 and r1["created"] == 0
    r2 = client.post("/v1/sync/dingtalk/ingest", json=corr_payload).json()
    assert r2["revised"] == 0 and r2["skipped_duplicate"] == 1

    # 4. Cross-family read is forbidden; sibling 乐乐 does not see 小禾's tasks
    assert client.get("/v1/students/stu_he/assignments", headers={"X-Family-Id": "fam_b"}).status_code == 403
    le_list = client.get("/v1/students/stu_le/assignments", headers={"X-Family-Id": "fam_a"}).json()
    assert le_list["assignments"] == []

    he_list = client.get("/v1/students/stu_he/assignments", headers={"X-Family-Id": "fam_a"}).json()
    assert len(he_list["assignments"]) == 3
    math = next(a for a in he_list["assignments"] if a["subject"] == "数学")
    assert math["version"] == 2
    assert "第 6 题选做" in (math["item_range"] or "")

    # 5. Auth failure preserves last trusted list and surfaces stale_warning
    fail = client.post(
        "/v1/sync/dingtalk/ingest",
        json={
            "family_id": "fam_a",
            "student_id": "stu_he",
            "conversation_id": fx["conversation_id"],
            "authorized_teachers": fx["authorized_teachers"],
            "page_complete": True,
            "watermark_at": corr["watermark_iso"],
            "auth_status": "auth_expired",
            "messages": [],
        },
    ).json()
    assert fail["status"] == "auth_expired" and fail["watermark_advanced"] is False

    # 6. Voice query routes today / subject / next and includes stale_warning
    code = client.post("/v1/pairing/issue", json={"hardware_uid": "mosaico-s31-voice"}).json()["pairing_code"]
    tok = client.post("/v1/pairing/claim", json={"family_id": "fam_a", "pairing_code": code}).json()["device_token"]
    sel = client.post(
        "/v1/devices/select-student",
        headers={"X-Device-Token": tok},
        json={"student_id": "stu_he", "method": "manual"},
    ).json()

    q_math = client.post(
        "/v1/voice/query",
        headers={"X-Device-Token": tok},
        json={"session_id": sel["session_id"], "profile_epoch": sel["profile_epoch"], "utterance": "数学做哪一页"},
    ).json()
    assert q_math["accepted"] is True
    assert q_math["intent"] == "query_subject"
    assert "第 24 页" in q_math["spoken_text"]
    assert "第 6 题选做" in q_math["spoken_text"]
    assert q_math["stale_warning"] is not None

    q_next = client.post(
        "/v1/voice/query",
        headers={"X-Device-Token": tok},
        json={"session_id": sel["session_id"], "profile_epoch": sel["profile_epoch"], "utterance": "接下来做什么"},
    ).json()
    assert q_next["intent"] == "query_next"
    assert "你想先做哪一科" in q_next["spoken_text"]
