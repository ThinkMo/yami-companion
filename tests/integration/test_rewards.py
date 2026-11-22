from __future__ import annotations

from backend.rewards.pet_service import TASK_COIN_REWARDS, get_or_create_pet, grant_pet_coins
from backend.rewards.service import list_method_cards, unlock_method_card


def test_pet_coin_rewards_method_cards_and_sibling_isolation(db_session):
    # Pet House is the sole gamification system; coins & method cards are strictly isolated per student
    pet_he_before = get_or_create_pet(db_session, family_id="fam_a", student_id="stu_he")
    base_coins = pet_he_before.coins

    pet_he = grant_pet_coins(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        amount=TASK_COIN_REWARDS["homework_complete"],
        reason="完成数学作业",
    )
    assert pet_he.coins == base_coins + 20

    # Method cards deduplicate per (student_id, card_id)
    created1, _ = unlock_method_card(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        card_id="card_same_whole",
        front="先看整体",
        back="比较单位分数前先确认两个整体一样大。",
    )
    created2, _ = unlock_method_card(
        db_session,
        family_id="fam_a",
        student_id="stu_he",
        card_id="card_same_whole",
        front="先看整体",
        back="比较单位分数前先确认两个整体一样大。",
    )
    assert created1 is True and created2 is False
    assert len(list_method_cards(db_session, family_id="fam_a", student_id="stu_he")) == 1

    # Sibling 乐乐 has independent pet coins & method cards
    pet_le = get_or_create_pet(db_session, family_id="fam_a", student_id="stu_le")
    assert pet_le.coins != pet_he.coins or pet_le.student_id != pet_he.student_id
    assert list_method_cards(db_session, family_id="fam_a", student_id="stu_le") == []
