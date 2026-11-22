from __future__ import annotations

from datetime import datetime, timezone

from backend.memory.graphiti_client import InMemoryGraphitiStore
from backend.memory.service import process_memory_outbox, recall_for_turn
from backend.models import Assignment, GrowthRecord, Reminder, ReviewItem


def test_dingtalk_class_group_homework_plus_performance_memory_and_sibling_isolation(client, db_session):
    client.post("/v1/guardian/dev/seed-demo")
    headers = {"X-Family-Id": "fam_a"}

    raw_class_msg = (
        "各位家长好，今天作业：1. 数学练习册P50第1-5题，明早交纸质；"
        "2. 英语朗读Unit6对话，今晚钉钉打卡。\n"
        "另外表扬小禾今天数学画图清晰，发言积极！\n"
        "提醒小禾计算应用题时注意写单位，不要漏写。\n"
        "表扬乐乐今天古诗背诵全对，非常棒！"
    )

    # 1. Forward class message for 小禾 via DingTalk Bot callback
    resp1 = client.post(
        "/v1/dingtalk/bot/callback",
        headers=headers,
        json={"senderNick": "小禾妈妈", "text": {"content": f"转发群消息：{raw_class_msg}"}},
    )
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["student_id"] == "stu_he"
    assert body1["created_assignments"] >= 2
    perfs = body1["performance_memories"]
    # Captured 小禾's praise + 小禾's feedback, and DID NOT attribute 乐乐's praise to 小禾!
    assert len(perfs) == 2
    assert any(p["category"] == "teacher_praise" and "小禾" in p["summary"] for p in perfs)
    assert any("写单位" in p["summary"] for p in perfs)
    assert all("乐乐" not in p["summary"] for p in perfs)

    # 2. Idempotency: re-sending exact same message creates 0 duplicate assignments & 0 duplicate memories
    resp2 = client.post(
        "/v1/dingtalk/bot/callback",
        headers=headers,
        json={"senderNick": "小禾妈妈", "text": {"content": f"转发群消息：{raw_class_msg}"}},
    )
    assert resp2.status_code == 200
    assert resp2.json()["created_assignments"] == 0
    assert resp2.json()["performance_memories"] == []

    # 3. Project outbox into Graphiti -> cross-session recall hits teacher praise & feedback for 小禾 only
    graphiti = InMemoryGraphitiStore()
    now = datetime(2026, 11, 21, 13, 0, tzinfo=timezone.utc)
    processed = process_memory_outbox(db_session, graphiti=graphiti, now=now)
    assert processed >= 2

    recalled_he = recall_for_turn(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        intent="help",
        topic_id="class_performance",
        graphiti=graphiti,
    )
    assert any("画图清晰" in r.summary or "写单位" in r.summary for r in recalled_he)

    recalled_le = recall_for_turn(
        db_session,
        family_id="fam_a",
        student_id="stu_le",
        intent="help",
        topic_id="class_performance",
        graphiti=graphiti,
    )
    assert recalled_le == []

    # 4. Growth records from DingTalk await parent confirmation (verified=False) per P09
    growth_he = client.get("/v1/guardian/students/stu_he/growth", headers=headers).json()
    dt_praise = [g for g in growth_he if g["category"] == "teacher_praise" and "画图清晰" in g["content"]]
    assert len(dt_praise) == 1
    assert dt_praise[0]["verified"] is False


def test_dingtalk_parent_bot_task_triggers_and_smart_student_targeting(client, db_session):
    client.post("/v1/guardian/dev/seed-demo")
    headers = {"X-Family-Id": "fam_a"}

    # 1. Parent schedules a relative reminder via DingTalk Bot
    r_rem = client.post(
        "/v1/dingtalk/bot/callback",
        headers=headers,
        json={"senderNick": "妈妈", "text": {"content": "提醒小禾 过15分钟检查英语朗读有没有交"}},
    )
    assert r_rem.status_code == 200
    rem_id = r_rem.json()["reminder_id"]
    rem = db_session.get(Reminder, rem_id)
    assert rem is not None and rem.student_id == "stu_he" and rem.kind == "submission"

    # 2. Smart student targeting: explicitly target sibling 乐乐 for dictation
    r_dict = client.post(
        "/v1/dingtalk/bot/callback",
        headers=headers,
        json={"senderNick": "爸爸", "text": {"content": "给乐乐听写 第3单元第4课组词"}},
    )
    assert r_dict.status_code == 200
    assert r_dict.json()["student_id"] == "stu_le"
    assert "听写任务" in r_dict.json()["markdown"]["text"]

    # 3. Parent schedules next-day review via DingTalk Bot
    r_rev = client.post(
        "/v1/dingtalk/bot/callback",
        headers=headers,
        json={"senderNick": "妈妈", "text": {"content": "安排复习 单位分数比较"}},
    )
    assert r_rev.status_code == 200
    rev = db_session.get(ReviewItem, r_rev.json()["review_id"])
    assert rev is not None and rev.status in ("scheduled", "pending")

    # 4. Parent manually assigns an extra task via DingTalk Bot
    r_asg = client.post(
        "/v1/dingtalk/bot/callback",
        headers=headers,
        json={"senderNick": "妈妈", "text": {"content": "布置作业 数学 口算练习20道"}},
    )
    assert r_asg.status_code == 200
    asg = db_session.get(Assignment, r_asg.json()["assignment_id"])
    assert asg is not None and asg.subject == "数学" and "口算" in asg.content

    # 5. Parent queries Pet House & Growth records via DingTalk Bot
    r_pet = client.post(
        "/v1/dingtalk/bot/callback",
        headers=headers,
        json={"senderNick": "妈妈", "text": {"content": "看看小禾宠物状态"}},
    )
    assert r_pet.status_code == 200
    assert "宠粮币" in r_pet.json()["markdown"]["text"]

    r_growth = client.post(
        "/v1/dingtalk/bot/callback",
        headers=headers,
        json={"senderNick": "妈妈", "text": {"content": "看看小禾成长记录"}},
    )
    assert r_growth.status_code == 200
    assert "成长" in r_growth.json()["markdown"]["title"]
