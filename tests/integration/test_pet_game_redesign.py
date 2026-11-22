from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.rewards.pet_service import (
    HABITAT_UPGRADE_COST,
    RECALL_COST,
    TASK_COIN_REWARDS,
    adopt_pet,
    feed_pet,
    get_or_create_pet,
    grant_pet_coins,
    pet_to_dict,
    play_with_pet,
    recall_pet,
    simulate_pet_unattended_days,
    upgrade_pet_habitat,
)


def test_pet_time_decay_runaway_recall_and_economy(client, db_session):
    client.post("/v1/guardian/dev/seed-demo")
    t0 = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)

    # 1. Verify standardized economy constants (1 task = 20 coins, 5 tasks = 100 coins recall)
    assert RECALL_COST == 100
    assert TASK_COIN_REWARDS["homework_complete"] == 20
    assert TASK_COIN_REWARDS["dictation_complete"] == 20
    assert TASK_COIN_REWARDS["photo_submission"] == 20
    assert HABITAT_UPGRADE_COST == {1: 40, 2: 80, 3: 140}

    pet = get_or_create_pet(db_session, family_id="fam_a", student_id="stu_he", now=t0)
    d0 = pet_to_dict(pet, now=t0)
    assert d0["is_runaway"] is False
    assert d0["action_state"] == 0
    assert d0["action_state_key"] == "happy_idle"
    assert len(d0["foods"]) == 3
    assert [f["cost"] for f in d0["foods"]] == [10, 20, 15]

    # 2. Feeding transitions to action_state=1 (eating_joy) and updates hunger/happiness
    pet.coins = 50
    db_session.commit()
    fed = feed_pet(db_session, family_id="fam_a", student_id="stu_he", food_name="鲜肉餐", now=t0)
    assert fed["fed"] is True
    assert fed["cost"] == 20
    assert fed["pet"]["action_state"] == 1
    assert fed["pet"]["action_state_key"] == "eating_joy"

    # 3. After 3.5 days (84 hours) unattended -> hungry_sad (action_state=2)
    t_3d = t0 + timedelta(hours=84)
    pet_3d = get_or_create_pet(db_session, family_id="fam_a", student_id="stu_he", now=t_3d)
    d_3d = pet_to_dict(pet_3d, now=t_3d)
    assert d_3d["is_runaway"] is False
    assert d_3d["action_state"] == 2
    assert d_3d["action_state_key"] == "hungry_sad"
    assert d_3d["hunger"] < 60

    # 4. After 7 days (168 hours) unattended -> triggers is_runaway=True (action_state=3)
    t_7d = t0 + timedelta(hours=168)
    pet_7d = get_or_create_pet(db_session, family_id="fam_a", student_id="stu_he", now=t_7d)
    d_7d = pet_to_dict(pet_7d, now=t_7d)
    assert d_7d["is_runaway"] is True
    assert d_7d["hunger"] == 0 and d_7d["happiness"] == 0
    assert d_7d["action_state"] == 3
    assert d_7d["action_state_key"] == "runaway"

    # 5. Feeding or playing while runaway is blocked without deducting coins
    coins_before = d_7d["coins"]
    blocked_feed = feed_pet(db_session, family_id="fam_a", student_id="stu_he", food_name="肉骨头", now=t_7d)
    assert blocked_feed["fed"] is False
    assert blocked_feed["is_runaway"] is True
    assert blocked_feed["coins"] == coins_before
    blocked_play = play_with_pet(db_session, family_id="fam_a", student_id="stu_he", now=t_7d)
    assert blocked_play["played"] is False
    assert blocked_play["is_runaway"] is True

    # 5b. Adopting/switching breed while runaway resets to healthy non-runaway pet
    adopted = adopt_pet(db_session, family_id="fam_a", student_id="stu_he", breed_or_species="布偶猫", nickname="布丁", now=t_7d)
    assert adopted["adopted"] is True
    assert adopted["pet"]["is_runaway"] is False
    assert adopted["pet"]["hunger"] == 88 and adopted["pet"]["happiness"] == 94
    # Re-trigger runaway for recall test below
    simulate_pet_unattended_days(db_session, family_id="fam_a", student_id="stu_he", days=7, now=t_7d)
    pet_7d = get_or_create_pet(db_session, family_id="fam_a", student_id="stu_he", now=t_7d)

    # 6. Recall fails when coins < 100
    pet_7d.coins = 60
    db_session.commit()
    fail_recall = recall_pet(db_session, family_id="fam_a", student_id="stu_he", now=t_7d)
    assert fail_recall["recalled"] is False
    assert fail_recall["cost"] == 100
    assert fail_recall["pet"]["is_runaway"] is True
    assert fail_recall["pet"]["coins"] == 60

    # 7. Completing 2 more tasks (+40 coins -> 100 coins total) allows recall!
    grant_pet_coins(db_session, family_id="fam_a", student_id="stu_he", amount=40, reason="完成2项学习任务")
    ok_recall = recall_pet(db_session, family_id="fam_a", student_id="stu_he", now=t_7d)
    assert ok_recall["recalled"] is True
    assert ok_recall["pet"]["is_runaway"] is False
    assert ok_recall["pet"]["coins"] == 0
    assert ok_recall["pet"]["hunger"] == 65
    assert ok_recall["pet"]["happiness"] == 75
    assert ok_recall["pet"]["action_state"] in (0, 1)

    # 8. Simulate unattended days helper works for 3d and 7d and 0d reset
    sim7 = simulate_pet_unattended_days(db_session, family_id="fam_a", student_id="stu_he", days=7)
    assert sim7["pet"]["is_runaway"] is True and sim7["pet"]["action_state"] == 3
    sim0 = simulate_pet_unattended_days(db_session, family_id="fam_a", student_id="stu_he", days=0)
    assert sim0["pet"]["is_runaway"] is False and sim0["pet"]["action_state"] == 0


def test_guardian_and_device_pet_endpoints(client, db_session):
    client.post("/v1/guardian/dev/seed-demo")
    headers = {"X-Family-Id": "fam_a"}

    # 1. Simulate 7 days unattended via Guardian endpoint -> runaway
    sim = client.post(
        "/v1/guardian/students/stu_he/pet/simulate-time",
        headers=headers,
        json={"days": 7},
    )
    assert sim.status_code == 200
    assert sim.json()["pet"]["is_runaway"] is True
    assert sim.json()["pet"]["action_state"] == 3

    # 2. Device profile-sync reports prun="1" and pact="3"
    ps = client.get("/v1/devices/profile-sync", params={"hardware_uid": "mosaico-yami-s31"})
    assert ps.status_code == 200
    assert ps.json()["prun"] == "1"
    assert ps.json()["pact"] == "3"

    # 3. Grant coins and recall via device pet-feed endpoint (Right Tap while runaway)
    grant_pet_coins(db_session, family_id="fam_a", student_id="stu_he", amount=120, reason="测试攒币")
    dev_recall = client.post("/v1/devices/pet-feed", params={"hardware_uid": "mosaico-yami-s31", "slot": 0})
    assert dev_recall.status_code == 200
    assert dev_recall.json()["recalled"] is True
    assert dev_recall.json()["pet"]["is_runaway"] is False

    # 4. Guardian Web feed, play, and upgrade endpoints
    f_res = client.post("/v1/guardian/students/stu_he/pet/feed", headers=headers, json={"food": "肉骨头"})
    assert f_res.status_code == 200 and f_res.json()["fed"] is True

    p_res = client.post("/v1/guardian/students/stu_he/pet/play", headers=headers)
    assert p_res.status_code == 200 and p_res.json()["played"] is True

    grant_pet_coins(db_session, family_id="fam_a", student_id="stu_he", amount=60, reason="测试升级")
    u_res = client.post("/v1/guardian/students/stu_he/pet/upgrade", headers=headers)
    assert u_res.status_code == 200 and u_res.json()["upgraded"] is True


def test_distinct_9_breed_and_runaway_assets_and_glyph_header():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    web_pets = root / "apps" / "guardian" / "assets" / "pets"
    expected_atlases = [
        "pet_0_nebula_dragon_atlas.png",
        "pet_1_cloud_puff_atlas.png",
        "pet_2_mochi_fox_atlas.png",
        "pet_3_capybara_atlas.png",
        "pet_4_astro_penguin_atlas.png",
        "pet_5_chibi_rex_atlas.png",
        "pet_6_volt_robo_atlas.png",
        "pet_7_aurora_dragon_atlas.png",
        "pet_8_sakura_fox_atlas.png",
        "stage_sunny_room.png",
    ]
    hashes = set()
    for name in expected_atlases:
        p = web_pets / name
        assert p.exists(), f"Missing Hatch-Pet atlas {name}"
        data = p.read_bytes()
        assert len(data) > 10_000
        hashes.add(hash(data))
    # Every single animated sprite atlas + sunny room stage is binary-distinct
    assert len(hashes) == 10

    hdr = (root / "firmware" / "esp-mosaico-vibe" / "projects" / "yami" / "components" / "app_hello" / "yami_pet_sprites.h").read_text(encoding="utf-8")
    assert "pet_all_rgb[9]" in hdr
    assert "pet_all_a4[9]" in hdr
    assert "bg_sunny_room_rgb" in hdr
    assert "bg_runaway_room_rgb" in hdr
    assert "gl_breed_titles[9]" in hdr
    assert "gl_food_btns[3][3]" in hdr
    assert "gl_btn_recall" in hdr


def test_mosaico_480x480_fullscreen_polaroid_ui_and_c_renderer():
    import json
    from pathlib import Path
    import subprocess

    root = Path(__file__).resolve().parents[2]
    main_json = json.loads(
        (root / "firmware" / "esp-mosaico-vibe" / "projects" / "yami" / "components" / "app_hello" / "ui" / "main.json").read_text(encoding="utf-8")
    )
    objs = {o.get("name"): o for o in main_json["objects"] if o.get("name")}
    assert objs["town_layer"]["x"] == 0 and objs["town_layer"]["y"] == 0
    assert objs["town_layer"]["w"] == 480 and objs["town_layer"]["h"] == 480
    assert objs["town_canvas"]["x"] == 0 and objs["town_canvas"]["y"] == 0
    assert objs["town_canvas"]["w"] == 480 and objs["town_canvas"]["h"] == 480

    # Compile yami_pet_gfx.c with -Wall -Wextra -Werror to verify zero C warnings
    c_file = root / "firmware" / "esp-mosaico-vibe" / "projects" / "yami" / "components" / "app_hello" / "yami_pet_gfx.c"
    inc_app = root / "firmware" / "esp-mosaico-vibe" / "projects" / "yami" / "components" / "app_hello"
    inc_gsp = root / "firmware" / "esp-mosaico-vibe" / "projects" / "yami" / "managed_components" / "espressif__esp-gsp" / "include"
    res = subprocess.run(
        ["clang", "-fsyntax-only", "-Wall", "-Wextra", "-Werror", f"-I{inc_app}", f"-I{inc_gsp}", str(c_file)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr


def test_guardian_web_polaroid_pet_card_and_assets_served(client):
    web = client.get("/guardian")
    assert web.status_code == 200
    html = web.text
    assert "pet-polaroid-screen" in html
    assert "guardianRecallPet()" in html
    assert "guardianSimulatePetTime(7)" not in html

    opt = client.get("/guardian/pet-ui-options")
    assert opt.status_code == 200
    assert "stage_sunny_room.png" in opt.text
    assert "nebula_dragon" in opt.text

    atlas_names = [
        "pet_0_nebula_dragon_atlas.png",
        "pet_1_cloud_puff_atlas.png",
        "pet_2_mochi_fox_atlas.png",
        "pet_3_capybara_atlas.png",
        "pet_4_astro_penguin_atlas.png",
        "pet_5_chibi_rex_atlas.png",
        "pet_6_volt_robo_atlas.png",
        "pet_7_aurora_dragon_atlas.png",
        "pet_8_sakura_fox_atlas.png",
    ]
    for name in atlas_names:
        r = client.get(f"/guardian/assets/pets/{name}")
        assert r.status_code == 200 and len(r.content) > 20_000

    r_stage = client.get("/guardian/assets/pets/stage_sunny_room.png")
    assert r_stage.status_code == 200 and len(r_stage.content) > 10_000

    r_run = client.get("/guardian/assets/anime/scene_runaway_empty_nest.png")
    assert r_run.status_code == 200 and len(r_run.content) > 50_000

    r_ico = client.get("/guardian/assets/fluent3d/bone.png")
    assert r_ico.status_code == 200 and len(r_ico.content) > 500




