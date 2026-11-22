from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.services.identity import (
    claim_pairing_code,
    issue_pairing_code,
    revoke_device,
    select_student,
    validate_turn_session,
)


def test_pairing_code_lifecycle_and_expiry(client, db_session):
    now = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)
    device, pairing = issue_pairing_code(db_session, hardware_uid="mosaico-s31-001", has_camera=True, now=now)
    assert len(pairing.code) == 6
    assert device.has_camera is True

    # Expired code must fail
    try:
        claim_pairing_code(db_session, family_id="fam_a", code=pairing.code, now=now + timedelta(minutes=11))
        assert False, "expected expiry error"
    except ValueError as exc:
        assert str(exc) == "pairing_code_expired"

    # Valid claim succeeds once, replay fails
    claimed = claim_pairing_code(db_session, family_id="fam_a", code=pairing.code, now=now + timedelta(minutes=2))
    assert claimed.family_id == "fam_a"
    assert claimed.device_token is not None

    try:
        claim_pairing_code(db_session, family_id="fam_a", code=pairing.code, now=now + timedelta(minutes=3))
        assert False, "expected consumed code rejection"
    except ValueError as exc:
        assert str(exc) == "invalid_pairing_code"


def test_two_family_and_sibling_isolation_with_stale_epoch_rejection(client, db_session):
    # Pair device to fam_a via HTTP
    issue_resp = client.post("/v1/pairing/issue", json={"hardware_uid": "mosaico-s31-002", "has_camera": True})
    assert issue_resp.status_code == 200
    code = issue_resp.json()["pairing_code"]

    claim_resp = client.post("/v1/pairing/claim", json={"family_id": "fam_a", "pairing_code": code})
    assert claim_resp.status_code == 200
    token = claim_resp.json()["device_token"]

    # Cross-family student selection is forbidden
    cross = client.post(
        "/v1/devices/select-student",
        headers={"X-Device-Token": token},
        json={"student_id": "stu_other", "method": "manual"},
    )
    assert cross.status_code == 403

    # Voiceprint edge cases: low confidence, sibling margin too small, overlapping speech, missing consent
    low = client.post(
        "/v1/devices/select-student",
        headers={"X-Device-Token": token},
        json={"student_id": "stu_he", "method": "voiceprint", "confidence": 0.70},
    ).json()
    assert low["confirmed"] is False and low["reason"] == "low_confidence_require_manual"

    ambig = client.post(
        "/v1/devices/select-student",
        headers={"X-Device-Token": token},
        json={"student_id": "stu_he", "method": "voiceprint", "confidence": 0.88, "second_best_confidence": 0.80},
    ).json()
    assert ambig["confirmed"] is False and ambig["reason"] == "ambiguous_sibling_require_manual"

    overlap = client.post(
        "/v1/devices/select-student",
        headers={"X-Device-Token": token},
        json={"student_id": "stu_he", "method": "voiceprint", "confidence": 0.95, "overlapping_speech": True},
    ).json()
    assert overlap["confirmed"] is False and overlap["reason"] == "overlapping_speech_require_manual"

    no_consent = client.post(
        "/v1/devices/select-student",
        headers={"X-Device-Token": token},
        json={"student_id": "stu_le", "method": "voiceprint", "confidence": 0.96},
    ).json()
    assert no_consent["confirmed"] is False and no_consent["reason"] == "voiceprint_consent_required"

    # Confirm 小禾, capture her session & epoch
    he_sel = client.post(
        "/v1/devices/select-student",
        headers={"X-Device-Token": token},
        json={"student_id": "stu_he", "method": "voiceprint", "confidence": 0.94, "second_best_confidence": 0.65},
    ).json()
    assert he_sel["confirmed"] is True
    he_session = he_sel["session_id"]
    he_epoch = he_sel["profile_epoch"]

    # Switch to 乐乐 (manual): profile_epoch increments
    le_sel = client.post(
        "/v1/devices/select-student",
        headers={"X-Device-Token": token},
        json={"student_id": "stu_le", "method": "manual"},
    ).json()
    assert le_sel["confirmed"] is True
    assert le_sel["profile_epoch"] > he_epoch

    # Late async request from 小禾's old session/epoch MUST be rejected
    stale = client.post(
        "/v1/voice/query",
        headers={"X-Device-Token": token},
        json={"session_id": he_session, "profile_epoch": he_epoch, "utterance": "今天有什么作业"},
    ).json()
    assert stale["accepted"] is False
    assert stale["rejected_reason"] in ("session_inactive", "stale_profile_epoch")


def test_device_revoke_blocks_further_access(db_session):
    device, pairing = issue_pairing_code(db_session, hardware_uid="mosaico-s31-003")
    device = claim_pairing_code(db_session, family_id="fam_a", code=pairing.code)
    confirmed, _, sess, epoch = select_student(db_session, device=device, student_id="stu_he", method="manual")
    assert confirmed and sess is not None

    revoke_device(db_session, device.id)
    ok, reason, _ = validate_turn_session(db_session, device=device, session_id=sess.id, profile_epoch=epoch)
    assert ok is False
    assert reason in ("session_inactive", "stale_profile_epoch")
