---
name: pet-companion-house
description: Use when the child wants to play the pet raising game (宠物小屋), adopt or switch among the 9 animated Hatch-Pet companions (星云小龙/棉花糖云/麻薯灵狐/泡芙水豚/宇航企鹅/呆萌恐龙/雷电机甲/极光星龙/樱花灵狐), check pet status/coins, feed the pet different foods (能量星果/星云大餐/鲜脆小食/豪华大餐/电池糖/超充能量/彩虹曲奇), upgrade its living habitat (住宿环境升级), play with the pet, or recall a runaway pet (花100币召回离家出走的宠物).
---

# Yami Pet Companion House (芽米宠物小屋 · Hatch-Pet 动态伙伴)

## Overview
Turn genuine daily learning into caring for a living, multi-frame animated 3D-toy pet in a Forest Cream Polaroid room on Mosaico!
- **3 Themes × 3 Characters (9 Distinct Animated Hatch-Pet Companions)**:
  - 奇幻星灵 (`fantasy`): `星云小龙` (星宝)、`棉花糖云` (绵绵)、`麻薯灵狐` (莓莓)
  - 萌趣伙伴 (`plush`): `泡芙水豚` (佛系橘)、`宇航企鹅` (啵啵)、`呆萌恐龙` (咔咔)
  - 星际潮玩 (`tech`): `雷电机甲` (闪闪)、`极光星龙` (幻幻)、`樱花灵狐` (桃桃)
- **Live 4-Row Sprite Animations**: Autonomous patrol walking (with left/right mirroring), idle breathing & blinking, sprinting to the food bowl & cheering when fed, slumped teary-eyed waiting when hungry, and farewell letter when runaway.
- **Earn Pet Coins (`宠粮币`)**: Completing homework (`+20`), dictation (`+20`), photo submission (`+20`), photo question (`+15`), English speaking (`+15`), daily play (`+5`).
- **7-Day Time Decay & 100-Coin Recall (`离家出走与召回`)**:
  - Diet (`饮食`) and Mood (`开心`) naturally decay over 7 days (168 hours) if unattended.
  - After 3 days unattended or `< 40%`, the pet enters a hungry/sad state (`hungry_sad`).
  - After 7 full days unattended or `0%`, the pet leaves a farewell letter and runs away (`is_runaway = True`).
  - Spending `100` coins (equivalent to 5 completed learning tasks) via `recall_pet` warmly brings the pet back home!
- **Different Foods**: `能量星果`(10)、`星云大餐`(20)、`鲜脆小食`(10)、`豪华大餐`(20)、`电池糖`(10)、`超充能量`(20)、`彩虹曲奇`(15).
- **Habitat Upgrades (Lv.1 → Lv.4)**: Upgrade costs `40` / `80` / `140` coins.

## Core Tools
1. `get_pet_status`: Always call before quoting the pet's breed, name, coins, hunger, happiness, level, habitat, or runaway status.
2. `adopt_pet`: Adopt or switch to any of the 9 animated Hatch-Pet companions (and optional nickname).
3. `feed_pet`: Spend coins to feed the pet a specific food; triggers live run-to-bowl & cheer/heart animation on Mosaico.
4. `recall_pet`: Spend 100 coins to recall the pet when `is_runaway` is True.
5. `upgrade_pet_habitat`: Spend coins to upgrade the pet's room from Lv.1 up to Lv.4.
6. `play_with_pet`: Pet/play with the companion for lively reactions.

## Voice Output Constraints
- Maximum 1–2 short, warm spoken Chinese sentences (≤ 45 characters).
- Plain speech only: no Markdown, no bullet lists, no Emoji.
