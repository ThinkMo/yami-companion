from __future__ import annotations

from backend.agent.harness import YamiAgentHarness


def test_guardian_v2_today_stages_growth_analytics_emotion_and_skill_lifecycle(client, db_session):
    client.post("/v1/guardian/dev/seed-demo")
    headers = {"X-Family-Id": "fam_a"}

    # 1. Today endpoint returns 3-stage study flow (review / homework / preview) + class performances
    today = client.get("/v1/guardian/students/stu_he/today", headers=headers)
    assert today.status_code == 200
    td = today.json()
    assert set(td["stages"].keys()) == {"review", "homework", "preview"}
    assert isinstance(td["class_performances"], list)

    # Verify 1-click parent homework completion toggle
    if td["assignments"]:
        aid = td["assignments"][0]["id"]
        tog = client.patch(
            f"/v1/guardian/students/stu_he/assignments/{aid}/complete",
            headers=headers,
            json={"completed": True},
        )
        assert tog.status_code == 200 and tog.json()["completed"] is True

    # Verify one-click confirmation of class performance / teacher praise
    if td["class_performances"]:
        gid = td["class_performances"][0]["id"]
        ver = client.post(f"/v1/guardian/students/stu_he/growth/{gid}/verify", headers=headers)
        assert ver.status_code == 200 and ver.json()["verified"] is True

    # 2. Passive emotion detection during child voice turn -> feeds Growth Emotion Barometer
    harness = YamiAgentHarness()
    harness.run_turn(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        utterance="这道分数应用题好难，我有点担心考不好",
    )
    ga = client.get("/v1/guardian/students/stu_he/growth/analytics?month=2026-11", headers=headers)
    assert ga.status_code == 200
    gad = ga.json()
    assert "daily_completion" in gad and "curriculum_progress" in gad and "emotion_barometer" in gad
    assert "monthly_calendar" in gad
    cal = gad["monthly_calendar"]
    assert cal["month"] == "2026-11"
    assert len(cal["days"]) == 30
    assert 0 <= cal["first_weekday"] <= 6
    assert len(gad["daily_completion"]["last_7_days"]) == 7
    # Real textbook catalog (textbooks.json + directories.json) loaded for 4 subjects with real units
    cur_map = {c["subject_key"]: c for c in gad["curriculum_progress"]}
    assert set(cur_map.keys()) == {"chinese", "math", "english", "science"}
    assert cur_map["math"]["total_units"] >= 4

    # 2a. Conversation per-day filter works
    from backend.models import ConversationTurn

    db_session.add(
        ConversationTurn(
            family_id="fam_a",
            student_id="stu_he",
            skill_name="homework-companion",
            user_text="这道分数应用题怎么做",
            assistant_text="我们先一起把题目里的已知条件画出来吧",
        )
    )
    db_session.commit()
    conv_all = client.get("/v1/guardian/students/stu_he/conversations", headers=headers)
    assert conv_all.status_code == 200 and len(conv_all.json()) >= 1
    first_day = str(conv_all.json()[0]["created_at"])[:10]
    conv_hit = client.get(f"/v1/guardian/students/stu_he/conversations?day={first_day}", headers=headers)
    assert conv_hit.status_code == 200 and len(conv_hit.json()) >= 1
    conv_miss = client.get("/v1/guardian/students/stu_he/conversations?day=2020-01-01", headers=headers)
    assert conv_miss.status_code == 200 and conv_miss.json() == []

    # 2b. DingTalk robot AppId / AppSecret / Class Group ID persistence
    dt_patch = client.patch(
        "/v1/guardian/family/webhook",
        headers=headers,
        json={
            "dingtalk_webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=demo123",
            "dingtalk_app_id": "ding_test_appid",
            "dingtalk_app_secret": "super_secret_key",
            "dingtalk_class_group_id": "cid_class_group_001==",
        },
    )
    assert dt_patch.status_code == 200
    dt_get = client.get("/v1/guardian/family/webhook", headers=headers).json()
    assert dt_get["dingtalk_app_id"] == "ding_test_appid"
    assert dt_get["dingtalk_app_secret_configured"] is True
    assert dt_get["dingtalk_class_group_id"] == "cid_class_group_001=="

    emo = gad["emotion_barometer"]
    assert "非临床" in emo["disclaimer"] or "临床心理诊断" in emo["disclaimer"]
    assert emo["highest_risk"] in ("attention", "high")
    assert len(emo["recent_signals"]) >= 1
    assert emo["latest_parent_tip"]

    # 2b. Pet breed switcher persists volt_robo (雷电机甲 · 闪闪) cleanly
    sw = client.patch(
        "/v1/guardian/students/stu_he/pet",
        headers=headers,
        json={"breed": "volt_robo", "pet_name": None},
    )
    assert sw.status_code == 200
    assert sw.json()["pet"]["breed"] == "volt_robo"
    assert sw.json()["pet"]["breed_cn"] == "雷电机甲"
    assert sw.json()["pet"]["pet_name"] == "闪闪"

    # 3. Skill Lifecycle Panel: list skills, disable voice-dictation -> Harness respects disabled state
    sk_list = client.get("/v1/guardian/students/stu_he/skills", headers=headers)
    assert sk_list.status_code == 200
    names = {s["name"] for s in sk_list.json()["skills"]}
    assert {"homework-companion", "voice-dictation", "english-speaking", "pet-companion-house", "chat-companion"}.issubset(names)

    dis = client.patch(
        "/v1/guardian/students/stu_he/skills/voice-dictation",
        headers=headers,
        json={"enabled": False},
    )
    assert dis.status_code == 200 and dis.json()["enabled"] is False and dis.json()["lifecycle_status"] == "paused"

    # When voice-dictation is disabled, utterance mentioning dictation does NOT start dictation
    turn_disabled = harness.run_turn(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        utterance="听写第3单元第4课组词",
    )
    assert turn_disabled.skill_name != "voice-dictation"

    # Re-enable voice-dictation -> works again
    en = client.patch(
        "/v1/guardian/students/stu_he/skills/voice-dictation",
        headers=headers,
        json={"enabled": True},
    )
    assert en.status_code == 200 and en.json()["enabled"] is True

    # 3b. Custom Task full lifecycle: create -> list -> complete today -> pause -> delete
    ct_add = client.post(
        "/v1/guardian/students/stu_he/custom-tasks",
        headers=headers,
        json={"title": "睡前阅读 20 分钟", "cadence": "daily"},
    )
    assert ct_add.status_code == 200
    ct_id = ct_add.json()["id"]
    assert ct_add.json()["status"] == "active" and ct_add.json()["done_today"] is False

    ct_done = client.patch(
        f"/v1/guardian/students/stu_he/custom-tasks/{ct_id}",
        headers=headers,
        json={"done_today": True},
    )
    assert ct_done.status_code == 200 and ct_done.json()["done_today"] is True and ct_done.json()["streak_days"] == 1

    ct_pause = client.patch(
        f"/v1/guardian/students/stu_he/custom-tasks/{ct_id}",
        headers=headers,
        json={"status": "paused", "title": "睡前阅读 25 分钟"},
    )
    assert ct_pause.status_code == 200 and ct_pause.json()["status"] == "paused" and ct_pause.json()["title"] == "睡前阅读 25 分钟"

    ct_del = client.delete(f"/v1/guardian/students/stu_he/custom-tasks/{ct_id}", headers=headers)
    assert ct_del.status_code == 200 and ct_del.json()["deleted"] is True

    # 3c. Manual Teacher Praise logging -> appears in teacher_praise_board + habit_summary auto-aggregated
    pr_add = client.post(
        "/v1/guardian/students/stu_he/praise",
        headers=headers,
        json={
            "subject": "数学",
            "teacher_name": "王老师",
            "title": "上台讲题思路清晰",
            "content": "王老师点名表扬小禾主动讲解三位数乘两位数验算方法",
        },
    )
    assert pr_add.status_code == 200 and pr_add.json()["verified"] is True

    ga2 = client.get("/v1/guardian/students/stu_he/growth/analytics?month=2026-11", headers=headers).json()
    assert "teacher_praise_board" in ga2 and ga2["teacher_praise_board"]["total_count"] >= 1
    assert any(it["title"] == "上台讲题思路清晰" and it["teacher_name"] == "王老师" for it in ga2["teacher_praise_board"]["items"])
    assert "habit_summary" in ga2 and "avg_homework_pct_7d" in ga2["habit_summary"]

    # 4. Guardian Web v2 HTML structure check (方案 1 · 晨光白瓷 3-col layout + consolidated Growth IA)
    html = client.get("/guardian").text
    assert 'id="tab-skills"' in html
    assert 'id="tab-dictation"' not in html
    assert 'id="growth-list"' not in html
    assert "摄像头拍题辅导" not in html
    assert "时间流逝与" not in html
    assert "PostgreSQL + Graphiti" not in html
    assert "Neo4j" not in html
    assert "#1A73E8" in html
    assert 'class="context-rail"' in html
    assert "成长日历" in html
    assert 'id="praise-board-list"' in html
    assert 'id="habit-auto-stats"' in html
    assert 'id="account-dialog"' in html
    assert 'id="help-dialog"' in html
    assert 'id="growth-month-picker"' in html
    assert 'id="growth-today-label"' in html
    assert 'id="growth-cal-grid"' in html
    assert 'id="custom-tasks-list"' in html
    assert 'id="conv-date-filter"' in html
    assert 'id="set-dt-appid"' in html
    assert 'id="set-dt-secret"' in html
    assert 'id="set-dt-group"' in html
    assert "previewSelectedBreed()" in html
    assert 'role="tabpanel"' in html
    assert "skip-link" in html
    # Today strict order: Homework -> Todo -> Review/Preview -> Custom Tasks -> Teacher Feedback
    assert (
        html.index('id="assignments-list"')
        < html.index('id="action-section"')
        < html.index('id="today-review-list"')
        < html.index('id="custom-tasks-list"')
        < html.index('id="today-class-perf-list"')
    )
    # Growth strict order: 成长日历 -> 教师表扬看板 -> 教材学情 -> 习惯统计 -> 学习记忆
    assert (
        html.index('id="growth-cal-grid"')
        < html.index('id="praise-board-list"')
        < html.index('id="growth-curriculum-units"')
        < html.index('id="habit-auto-stats"')
        < html.index('id="memory-list"')
        < html.index('id="tab-settings"')
    )
