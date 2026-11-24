from __future__ import annotations

from backend.agent.harness import YamiAgentHarness


def test_agent_harness_tools_vision_consent_and_guardian_island(client, db_session):
    # Seed demo family & assignments
    seed = client.post("/v1/guardian/dev/seed-demo")
    assert seed.status_code == 200

    # 1. Agent Harness tool execution: complete math assignment -> awards 20 pet coins
    harness = YamiAgentHarness()
    res = harness._execute_tool(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        name="mark_assignment_completed",
        arguments={"subject": "数学"},
    )
    assert res["found"] is True
    assert res["completed"] is True
    assert res["pet_coins_gained"] == 20
    assert res["pet_coins_total"] >= 20

    # 2. Pet endpoint reflects granted coins
    pet_resp = client.get("/v1/guardian/students/stu_he/pet", headers={"X-Family-Id": "fam_a"})
    assert pet_resp.status_code == 200
    assert pet_resp.json()["pet"]["coins"] >= 20

    # 3. Vision photo submission works when camera_consent=True
    fake_jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 256 + b"\xff\xd9"
    sub = client.post(
        "/v1/vision/capture-turn",
        params={"purpose": "submission", "subject": "英语"},
        headers={"Content-Type": "image/jpeg", "X-Hardware-Uid": "mosaico-cam-1"},
        content=fake_jpg,
    )
    assert sub.status_code == 200
    assert sub.headers["x-yami-intent"] == "vision_submission"
    assert len(sub.content) > 1000

    # 4. Vision tutoring returns Socratic hint (never direct answer)
    tut = client.post(
        "/v1/vision/capture-turn",
        params={"purpose": "question", "hint": "这道分数题怎么比大小"},
        headers={"Content-Type": "image/jpeg", "X-Hardware-Uid": "mosaico-cam-1"},
        content=fake_jpg,
    )
    assert tut.status_code == 200
    assert tut.headers["x-yami-intent"] == "vision_tutoring"

    # 5. Revoking camera consent blocks /v1/vision/capture-turn with 403
    rev = client.patch(
        "/v1/guardian/students/stu_he/settings",
        headers={"X-Family-Id": "fam_a"},
        json={"camera_consent": False},
    )
    assert rev.status_code == 200
    blocked = client.post(
        "/v1/vision/capture-turn",
        params={"purpose": "question"},
        headers={"Content-Type": "image/jpeg", "X-Hardware-Uid": "mosaico-cam-1"},
        content=fake_jpg,
    )
    assert blocked.status_code == 403

    # 6. Metrics endpoint reports configured providers
    m = client.get("/v1/agent/metrics")
    assert m.status_code == 200
    assert "configured_providers" in m.json()


def test_voice_self_report_submission_and_full_duplex_ws(client, db_session):
    client.post("/v1/guardian/dev/seed-demo")

    # 1. Student voice self-report: "英语我已经在钉钉交好了" -> child_said_done (never grants island material)
    with client.websocket_connect("/v1/ws/voice") as ws:
        ws.send_bytes(b"\x00\x00" * 400)
        ws.send_json({"type": "end_of_turn", "utterance": "英语我已经在钉钉交好了"})
        tr = ws.receive_json()
        assert tr["type"] == "transcript" and "交好了" in tr["text"]
        cl = ws.receive_json()
        assert cl["type"] == "clause" and "英语" in cl["text"]
        pcm = ws.receive_bytes()
        assert len(pcm) > 1000
        done = ws.receive_json()
        assert done["type"] == "turn_done" and done["provider"] == "doubao_seed"

        # 2. Instant cancel / Barge-In frame
        ws.send_bytes(b"\x00\x00" * 400)
        ws.send_json({"type": "cancel"})
        cancelled = ws.receive_json()
        assert cancelled["type"] == "cancelled"

        # 3. Pet House voice query via full-duplex WS triggers pet-companion-house skill & auto-jump
        ws.send_json({"type": "end_of_turn", "utterance": "看看我的宠物小屋"})
        tr2 = ws.receive_json()
        assert tr2["type"] == "transcript"
        got_turn_done = False
        for _ in range(20):
            ev = ws.receive()
            if ev.get("text"):
                import json as _json
                payload = _json.loads(ev["text"])
                if payload.get("type") == "turn_done":
                    assert payload["skill_name"] == "pet-companion-house"
                    got_turn_done = True
                    break
        assert got_turn_done is True

    # Legacy island endpoint is removed (404)
    assert client.get("/v1/guardian/students/stu_he/island", headers={"X-Family-Id": "fam_a"}).status_code == 404

    # Verify submission status recorded cleanly
    today = client.get("/v1/guardian/students/stu_he/today", headers={"X-Family-Id": "fam_a"}).json()
    eng = [a for a in today["assignments"] if a["subject"] == "英语"][0]
    assert eng["submission_status"] in ("child_said_done", "needs_parent", "photo", "parent_confirmed")

    # 3. Guardian Web student profile list & edit (name, grade, textbook, voiceprint)
    listed = client.get("/v1/guardian/students", headers={"X-Family-Id": "fam_a"})
    assert listed.status_code == 200 and len(listed.json()) >= 2

    edited = client.patch(
        "/v1/guardian/students/stu_he/settings",
        headers={"X-Family-Id": "fam_a"},
        json={"name": "林小禾", "grade": 5, "textbook_version": "苏教版", "voiceprint_consent": False},
    )
    assert edited.status_code == 200
    body = edited.json()
    assert body["name"] == "林小禾"
    assert body["grade"] == 5
    assert body["textbook_version"] == "苏教版"
    assert body["voiceprint_consent"] is False

    # 4. Authoritative textbook catalog (4 subjects: chinese, math, english, science)
    cat = client.get("/v1/guardian/catalog/textbooks", params={"grade": 4})
    assert cat.status_code == 200
    sub_map = {s["key"]: s for s in cat.json()["subjects"]}
    assert set(sub_map.keys()) == {"chinese", "math", "english", "science"}
    assert "统编版" in sub_map["chinese"]["versions"]
    assert "人教版" in sub_map["math"]["versions"]

    saved_subs = client.patch(
        "/v1/guardian/students/stu_he/settings",
        headers={"X-Family-Id": "fam_a"},
        json={
            "subject_textbooks": {
                "chinese": "统编版",
                "math": "北师大版",
                "english": "译林版（2012年版）",
                "science": "教科版",
            }
        },
    )
    assert saved_subs.status_code == 200
    assert saved_subs.json()["subject_textbooks"]["english"] == "译林版（2012年版）"

    # 5. Manual homework entry from Guardian Web
    created = client.post(
        "/v1/guardian/students/stu_he/assignments",
        headers={"X-Family-Id": "fam_a"},
        json={"subject": "英语", "content": "朗读 Unit 4 对话两遍", "page_ref": "P36"},
    )
    assert created.status_code == 200
    created_data = created.json()
    assert created_data["subject"] == "英语"
    assert created_data["content"] == "朗读 Unit 4 对话两遍"
    assert created_data["page_ref"] == "P36"

    # 6. Multi-turn voice conversation audit & clear
    conv_before = client.get(
        "/v1/guardian/students/stu_he/conversations",
        headers={"X-Family-Id": "fam_a"},
    )
    assert conv_before.status_code == 200
    assert isinstance(conv_before.json(), list)

    conv_cleared = client.delete(
        "/v1/guardian/students/stu_he/conversations",
        headers={"X-Family-Id": "fam_a"},
    )
    assert conv_cleared.status_code == 200
    assert "deleted_turns" in conv_cleared.json()

    # 7. Plan A Family DingTalk Webhook settings & test
    wh_get = client.get("/v1/guardian/family/webhook", headers={"X-Family-Id": "fam_a"})
    assert wh_get.status_code == 200
    assert "dingtalk_webhook_url" in wh_get.json()

    wh_patch = client.patch(
        "/v1/guardian/family/webhook",
        headers={"X-Family-Id": "fam_a"},
        json={"dingtalk_webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=demo123456"},
    )
    assert wh_patch.status_code == 200
    assert "demo123456" in wh_patch.json()["dingtalk_webhook_url"]

    wh_clear = client.patch(
        "/v1/guardian/family/webhook",
        headers={"X-Family-Id": "fam_a"},
        json={"dingtalk_webhook_url": ""},
    )
    assert wh_clear.status_code == 200
    assert wh_clear.json()["dingtalk_webhook_url"] == ""

    wh_test = client.post("/v1/guardian/family/webhook/test", headers={"X-Family-Id": "fam_a"})
    assert wh_test.status_code == 200
    assert "status" in wh_test.json()

    # 8. Live Mosaico profile & pending homework sync
    psync = client.get("/v1/devices/profile-sync", params={"hardware_uid": "mosaico-yami-s31"})
    assert psync.status_code == 200
    ps_body = psync.json()
    assert ps_body["s0"] == "林小禾"
    assert ps_body["active"] == "0"
    assert int(ps_body["cnt"]) >= 1
    assert "·" in ps_body["t0"]

    # 9. Hardware student switch + voice switch_student tool + strict per-student memory isolation
    sw_hw = client.post(
        "/v1/devices/active-student",
        params={"hardware_uid": "mosaico-yami-s31", "slot": 1},
    )
    assert sw_hw.status_code == 200
    assert sw_hw.json()["active"] == "1"
    assert sw_hw.json()["student_name"] == "乐乐"

    psync2 = client.get("/v1/devices/profile-sync", params={"hardware_uid": "mosaico-yami-s31"})
    assert psync2.status_code == 200
    assert psync2.json()["active"] == "1"

    # Voice switch back to 林小禾 via switch_student tool
    sw_voice = client.post(
        "/v1/voice/audio-turn",
        params={"utterance": "切换到林小禾"},
        headers={"X-Hardware-Uid": "mosaico-yami-s31"},
        content=b"\x00\x00" * 600,
    )
    assert sw_voice.status_code == 200
    psync3 = client.get("/v1/devices/profile-sync", params={"hardware_uid": "mosaico-yami-s31"})
    assert psync3.json()["active"] == "0"

    # Verify strict per-student memory isolation: 乐乐 has zero turns from 林小禾
    lele_id = sw_hw.json()["student_id"]
    he_convs = client.get("/v1/guardian/students/stu_he/conversations", headers={"X-Family-Id": "fam_a"}).json()
    lele_convs = client.get(f"/v1/guardian/students/{lele_id}/conversations", headers={"X-Family-Id": "fam_a"}).json()
    assert isinstance(lele_convs, list)
    assert any("林小禾" in c["assistant_text"] for c in he_convs)
    assert not any("林小禾" in c["assistant_text"] for c in lele_convs)

    # 10. Focused Skills (pet-companion-house is sole gamification; wuxia & island removed) + Assignment delete
    skills = client.get("/v1/agent/skills").json()
    skill_names = {s["name"] for s in skills}
    assert "pet-companion-house" in skill_names
    assert "wuxia-hero-academy" not in skill_names
    assert "island-growth-review" not in skill_names

    del_asg = client.delete(
        f"/v1/guardian/students/stu_he/assignments/{created_data['id']}",
        headers={"X-Family-Id": "fam_a"},
    )
    assert del_asg.status_code == 200
    assert del_asg.json()["deleted"] is True

    # 12. DingTalk raw class-group homework parsing + Parent Bot callback + Camera desk-study check
    raw_res = client.post(
        "/v1/sync/dingtalk/parse-raw",
        json={
            "family_id": "fam_a",
            "student_id": "stu_he",
            "raw_text": "各位家长好，今天作业：1. 数学练习册P48第1-4题，明早交纸质；2. 语文背诵第14课；3. 英语朗读Unit5对话，今晚钉钉打卡",
        },
    )
    assert raw_res.status_code == 200
    raw_body = raw_res.json()
    assert raw_body["created"] == 3
    assert {p["subject"] for p in raw_body["parsed_items"]} == {"数学", "语文", "英语"}

    # Parent <-> DingTalk Bot interactive callback: query today, confirm submission, forward homework, desk check
    bot_today = client.post(
        "/v1/dingtalk/bot/callback",
        headers={"X-Family-Id": "fam_a"},
        json={"senderNick": "妈妈", "text": {"content": "今天作业进度"}},
    )
    assert bot_today.status_code == 200
    assert "今日作业" in bot_today.json()["markdown"]["text"]

    bot_confirm = client.post(
        "/v1/dingtalk/bot/callback",
        headers={"X-Family-Id": "fam_a"},
        json={"senderNick": "妈妈", "text": {"content": "确认提交 数学"}},
    )
    assert bot_confirm.status_code == 200
    assert "家长已确认提交" in bot_confirm.json()["markdown"]["text"]

    # Camera desk check: blocked when camera_consent=False, allowed when True
    client.patch(
        "/v1/guardian/students/stu_he/settings",
        headers={"X-Family-Id": "fam_a"},
        json={"camera_consent": False},
    )
    bot_desk_denied = client.post(
        "/v1/dingtalk/bot/callback",
        headers={"X-Family-Id": "fam_a"},
        json={"senderNick": "妈妈", "text": {"content": "看看孩子在不在写字台"}},
    )
    assert "尚未开启摄像头授权" in bot_desk_denied.json()["markdown"]["text"]

    client.patch(
        "/v1/guardian/students/stu_he/settings",
        headers={"X-Family-Id": "fam_a"},
        json={"camera_consent": True},
    )
    req_desk = client.post("/v1/devices/request-desk-check", params={"hardware_uid": "mosaico-yami-s31"})
    assert req_desk.status_code == 200 and req_desk.json()["queued"] is True

    # Mosaico uploads desk-check JPEG -> privacy-safe summary returned & cached (zero image saved)
    fake_jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 128 + b"\xff\xd9"
    desk_up = client.post(
        "/v1/vision/desk-check",
        headers={"X-Hardware-Uid": "mosaico-yami-s31", "Content-Type": "image/jpeg"},
        content=fake_jpg,
    )
    assert desk_up.status_code == 200
    assert "summary" in desk_up.json() and "at_desk" in desk_up.json()

    bot_desk_ok = client.post(
        "/v1/dingtalk/bot/callback",
        headers={"X-Family-Id": "fam_a"},
        json={"senderNick": "妈妈", "text": {"content": "看看孩子在不在写字台"}},
    )
    assert "书桌状态" in bot_desk_ok.json()["markdown"]["text"]

    # 13. Pet Raising Game: 3 species x 3 breeds (cat/dog/beetle), foods, habitat upgrades & student isolation
    assert "pet-companion-house" in skill_names
    from backend.rewards.pet_service import (
        adopt_pet,
        feed_pet,
        get_or_create_pet,
        grant_pet_coins,
        play_with_pet,
        upgrade_pet_habitat,
    )

    ad_res = adopt_pet(db_session, family_id="fam_a", student_id="stu_he", breed_or_species="雷电机甲", nickname="闪闪王")
    assert ad_res["adopted"] is True and ad_res["pet"]["breed"] == "volt_robo" and ad_res["pet"]["species"] == "tech"

    feed_res = feed_pet(db_session, family_id="fam_a", student_id="stu_he", food_name="电池糖")
    assert feed_res["fed"] is True and feed_res["favorite_bonus"] is True

    grant_pet_coins(db_session, family_id="fam_a", student_id="stu_he", amount=60, reason="完成数学作业")
    up_res = upgrade_pet_habitat(db_session, family_id="fam_a", student_id="stu_he")
    assert up_res["upgraded"] is True and up_res["habitat_lv"] == 2 and up_res["habitat_name"] == "星舰基站"

    pl_res = play_with_pet(db_session, family_id="fam_a", student_id="stu_he")
    assert pl_res["played"] is True and "星际舞" in pl_res["message"]

    # Per-student isolation: 乐乐 still has default nebula_dragon breed untouched
    lele_pet = get_or_create_pet(db_session, family_id="fam_a", student_id=lele_id)
    assert lele_pet.species == "fantasy" and lele_pet.breed == "nebula_dragon" and lele_pet.habitat_lv == 1

    # Guardian Web Pet Switcher endpoint (GET/PATCH /v1/guardian/students/{id}/pet)
    g_pet = client.get("/v1/guardian/students/stu_he/pet", headers={"X-Family-Id": "fam_a"})
    assert g_pet.status_code == 200 and len(g_pet.json()["catalog"]) == 9
    g_patch = client.patch(
        "/v1/guardian/students/stu_he/pet",
        headers={"X-Family-Id": "fam_a"},
        json={"breed": "capybara", "pet_name": "佛系小橘"},
    )
    assert g_patch.status_code == 200 and g_patch.json()["pet"]["breed"] == "capybara"
