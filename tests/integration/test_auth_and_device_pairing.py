from __future__ import annotations


def test_multi_tenant_guardian_auth_and_mosaico_pairing_poll_lifecycle(client):
    # 1. Register Family 1 (Alice) -> gets JWT + auto-created Family & Student
    r1 = client.post(
        "/v1/auth/register",
        json={
            "account": "alice@example.com",
            "password": "password123",
            "family_name": "小禾一家",
            "display_name": "小禾妈妈",
            "first_student_name": "林小禾",
            "first_student_grade": 4,
        },
    )
    assert r1.status_code == 200
    b1 = r1.json()
    jwt1 = b1["access_token"]
    fam1_id = b1["family"]["id"]
    stu1_id = b1["default_student"]["id"]
    h1 = {"Authorization": f"Bearer {jwt1}"}

    # Duplicate account registration is rejected with 409
    dup = client.post(
        "/v1/auth/register",
        json={"account": "alice@example.com", "password": "password123"},
    )
    assert dup.status_code == 409

    # 2. Register Family 2 (Bob) -> strictly isolated from Family 1
    r2 = client.post(
        "/v1/auth/register",
        json={
            "account": "bob@example.com",
            "password": "password456",
            "family_name": "乐乐一家",
            "first_student_name": "陈乐乐",
            "first_student_grade": 3,
        },
    )
    assert r2.status_code == 200
    jwt2 = r2.json()["access_token"]
    h2 = {"Authorization": f"Bearer {jwt2}"}

    # Login verification
    bad_login = client.post("/v1/auth/login", json={"account": "alice@example.com", "password": "wrong"})
    assert bad_login.status_code == 401
    ok_login = client.post("/v1/auth/login", json={"account": "alice@example.com", "password": "password123"})
    assert ok_login.status_code == 200 and ok_login.json()["family"]["id"] == fam1_id

    # Cross-tenant isolation: Family 2 cannot read Family 1's student today/pet data (403)
    assert client.get(f"/v1/guardian/students/{stu1_id}/today", headers=h1).status_code == 200
    assert client.get(f"/v1/guardian/students/{stu1_id}/today", headers=h2).status_code == 403
    assert client.get(f"/v1/guardian/students/{stu1_id}/pet", headers=h2).status_code == 403

    # 3. Mosaico boots up without NVS token -> issues 6-digit pairing code & polls 'pending'
    hw_uid = "mosaico-mac-aabbccddeeff"
    issued = client.post("/v1/pairing/issue", json={"hardware_uid": hw_uid, "has_camera": True})
    assert issued.status_code == 200
    code = issued.json()["pairing_code"]
    assert len(code) == 6

    poll_pending = client.get("/v1/pairing/poll", params={"hardware_uid": hw_uid, "code": code})
    assert poll_pending.status_code == 200 and poll_pending.json()["status"] == "pending"

    # 4. Authenticated Guardian (Family 1) claims the 6-digit pairing code
    claim = client.post("/v1/auth/devices/claim", headers=h1, json={"pairing_code": code})
    assert claim.status_code == 200
    assert claim.json()["claimed"] is True
    assert claim.json()["family_id"] == fam1_id
    dev_id = claim.json()["device_id"]

    # 5. Mosaico polls /v1/pairing/poll -> receives 'claimed' + device_token for NVS
    poll_claimed = client.get("/v1/pairing/poll", params={"hardware_uid": hw_uid, "code": code})
    assert poll_claimed.status_code == 200
    pc = poll_claimed.json()
    assert pc["status"] == "claimed"
    assert pc["family_id"] == fam1_id
    dev_token = pc["device_token"]
    assert dev_token.startswith("dtok_")

    # 6. Mosaico syncs profile using X-Device-Token -> sees Family 1's student exclusively
    ps = client.get("/v1/devices/profile-sync", headers={"X-Device-Token": dev_token})
    assert ps.status_code == 200
    assert ps.json()["s0"] == "林小禾"

    # /v1/auth/me lists the bound device
    me = client.get("/v1/auth/me", headers=h1)
    assert me.status_code == 200
    assert any(d["id"] == dev_id and d["hardware_uid"] == hw_uid for d in me.json()["devices"])

    # 7. Family 2 cannot revoke Family 1's device (404); Family 1 revokes -> device immediately gets 401
    assert client.post(f"/v1/auth/devices/{dev_id}/revoke", headers=h2).status_code == 404
    rev = client.post(f"/v1/auth/devices/{dev_id}/revoke", headers=h1)
    assert rev.status_code == 200 and rev.json()["revoked"] is True

    ps_after = client.get("/v1/devices/profile-sync", headers={"X-Device-Token": dev_token})
    assert ps_after.status_code == 401
