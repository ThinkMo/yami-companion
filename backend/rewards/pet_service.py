from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from backend.models import PetProfile, Student


# Standardized economy constants:
# 1 standard learning task = 20 coins; 5 tasks = 100 coins to recall a runaway pet after 7 days (168h) unattended.
RECALL_COST: int = 100
RUNAWAY_HOURS: float = 168.0  # 7 days * 24 hours
HUNGRY_HOURS: float = 72.0    # 3 days * 24 hours
JOY_ACTIVE_MINUTES: float = 15.0

TASK_COIN_REWARDS: dict[str, int] = {
    "homework_complete": 20,
    "dictation_complete": 20,
    "photo_submission": 20,
    "photo_question": 15,
    "english_speaking": 15,
    "daily_play": 5,
}

# 3 themes x 3 characters = 9 distinct animated Hatch-Pet companions (ids 0..8 map 1:1 to Mosaico live sprite atlases)
PET_BREEDS: dict[str, dict[str, Any]] = {
    # Fantasy / Starlight (species_id=0)
    "nebula_dragon": {"id": 0, "species": "fantasy", "species_cn": "奇幻星灵", "breed_cn": "星云小龙", "default_name": "星宝"},
    "cloud_puff":    {"id": 1, "species": "fantasy", "species_cn": "奇幻星灵", "breed_cn": "棉花糖云", "default_name": "绵绵"},
    "mochi_fox":     {"id": 2, "species": "fantasy", "species_cn": "奇幻星灵", "breed_cn": "麻薯灵狐", "default_name": "莓莓"},
    # Plush / Cute Friends (species_id=1)
    "capybara":      {"id": 3, "species": "plush",   "species_cn": "萌趣伙伴", "breed_cn": "泡芙水豚", "default_name": "佛系橘"},
    "astro_penguin": {"id": 4, "species": "plush",   "species_cn": "萌趣伙伴", "breed_cn": "宇航企鹅", "default_name": "啵啵"},
    "chibi_rex":     {"id": 5, "species": "plush",   "species_cn": "萌趣伙伴", "breed_cn": "呆萌恐龙", "default_name": "咔咔"},
    # Tech / Collector Editions (species_id=2)
    "volt_robo":     {"id": 6, "species": "tech",    "species_cn": "星际潮玩", "breed_cn": "雷电机甲", "default_name": "闪闪"},
    "aurora_dragon": {"id": 7, "species": "tech",    "species_cn": "星际潮玩", "breed_cn": "极光星龙", "default_name": "幻幻"},
    "sakura_fox":    {"id": 8, "species": "tech",    "species_cn": "星际潮玩", "breed_cn": "樱花灵狐", "default_name": "桃桃"},
}

LEGACY_BREED_FALLBACK: dict[str, str] = {
    "orange": "capybara",
    "ragdoll": "cloud_puff",
    "calico": "mochi_fox",
    "golden": "nebula_dragon",
    "husky": "astro_penguin",
    "corgi": "chibi_rex",
    "kabuto": "volt_robo",
    "hercules": "aurora_dragon",
    "elephas": "sakura_fox",
}

BREED_IDLE_PILLS: dict[str, str] = {
    "nebula_dragon": "扑闪着小翅膀在屋里巡游探险",
    "cloud_puff":    "软乎乎地在阳光地毯上飘来飘去",
    "mochi_fox":     "摇着蓬松大尾巴好奇地四处张望",
    "capybara":      "顶着小橘子慢悠悠地在屋里散步",
    "astro_penguin": "戴着宇航头盔一摇一摆走太空步",
    "chibi_rex":     "迈着小短腿开心地在房间里溜达",
    "volt_robo":     "闪着像素笑脸能量满满巡逻中",
    "aurora_dragon": "泛着极光星辉轻盈地在屋内巡航",
    "sakura_fox":    "伴着花瓣香气轻快地在房间踱步",
}

FOOD_CATALOG: dict[str, dict[str, Any]] = {
    "能量星果": {"cost": 10, "hunger": 25, "happy": 15, "exp": 15, "fav_species": "fantasy", "icon": "fish",   "short_name": "能量星果"},
    "星云大餐": {"cost": 20, "hunger": 45, "happy": 35, "exp": 35, "fav_species": "fantasy", "icon": "honey",  "short_name": "星云大餐"},
    "鲜脆小食": {"cost": 10, "hunger": 25, "happy": 15, "exp": 15, "fav_species": "plush",   "icon": "bone",   "short_name": "鲜脆小食"},
    "豪华大餐": {"cost": 20, "hunger": 45, "happy": 35, "exp": 35, "fav_species": "plush",   "icon": "honey",  "short_name": "豪华大餐"},
    "电池糖":   {"cost": 10, "hunger": 25, "happy": 15, "exp": 15, "fav_species": "tech",    "icon": "honey",  "short_name": "电池糖"},
    "超充能量": {"cost": 20, "hunger": 45, "happy": 35, "exp": 35, "fav_species": "tech",    "icon": "honey",  "short_name": "超充能量"},
    "彩虹曲奇": {"cost": 15, "hunger": 15, "happy": 30, "exp": 20, "fav_species": "all",     "icon": "cookie", "short_name": "曲奇"},
}

SPECIES_FOOD_SLOTS: dict[str, list[str]] = {
    "fantasy": ["能量星果", "星云大餐", "彩虹曲奇"],
    "plush":   ["鲜脆小食", "豪华大餐", "彩虹曲奇"],
    "tech":    ["电池糖", "超充能量", "彩虹曲奇"],
}

HABITAT_NAMES: dict[str, dict[int, str]] = {
    "fantasy": {1: "奇幻树屋", 2: "云端木阁", 3: "阳光庭院", 4: "星空庄园"},
    "plush":   {1: "温馨小窝", 2: "原木小筑", 3: "阳光庭院", 4: "星空庄园"},
    "tech":    {1: "未来舱室", 2: "星舰基站", 3: "光子花园", 4: "星空庄园"},
}

HABITAT_UPGRADE_COST: dict[int, int] = {
    1: 40,   # Lv.1 -> Lv.2 (2 tasks)
    2: 80,   # Lv.2 -> Lv.3 (4 tasks)
    3: 140,  # Lv.3 -> Lv.4 (7 tasks)
}


def _ensure_utc(dt: Optional[datetime], fallback: datetime) -> datetime:
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _match_breed(raw: str) -> Optional[str]:
    text = (raw or "").strip().lower()
    if not text:
        return None
    mapping = [
        ("nebula_dragon", ("星云小龙", "星宝", "nebula", "小龙", "恐龙宝宝", "dragon")),
        ("cloud_puff",    ("棉花糖", "云朵", "绵绵", "cloud", "puff")),
        ("mochi_fox",     ("麻薯", "灵狐", "莓莓", "mochi", "fox", "狐狸")),
        ("capybara",      ("水豚", "卡皮巴拉", "佛系橘", "泡芙", "capybara")),
        ("astro_penguin", ("宇航企鹅", "企鹅", "啵啵", "penguin", "astro")),
        ("chibi_rex",     ("恐龙", "霸王龙", "咔咔", "rex", "dino")),
        ("volt_robo",     ("雷电", "机甲", "机器人", "闪闪", "robo", "volt")),
        ("aurora_dragon", ("极光", "星龙", "幻幻", "aurora")),
        ("sakura_fox",    ("樱花", "桃桃", "sakura")),
    ]
    for key, kws in mapping:
        if any(k in text for k in kws):
            return key
    for old_key, new_key in LEGACY_BREED_FALLBACK.items():
        if old_key in text:
            return new_key
    legacy_cn = [
        ("nebula_dragon", ("金毛", "小狗", "狗")),
        ("capybara",      ("橘猫", "小橘", "布偶", "三花", "猫")),
        ("volt_robo",     ("独角仙", "长戟", "毛象", "甲壳虫", "甲虫")),
    ]
    for key, kws in legacy_cn:
        if any(k in text for k in kws):
            return key
    return None


def _match_food(raw: str, species: str) -> str:
    text = (raw or "").strip()
    for name in FOOD_CATALOG:
        if name in text:
            return name
    if "星果" in text or "鱼" in text:
        return "能量星果"
    if "星云" in text or "罐头" in text:
        return "星云大餐"
    if "小食" in text or "骨头" in text:
        return "鲜脆小食"
    if "豪华" in text or "大餐" in text or "鲜肉" in text:
        return "豪华大餐"
    if "超充" in text or "黑糖" in text:
        return "超充能量"
    if "电池" in text or "果冻" in text:
        return "电池糖"
    if "曲奇" in text or "零食" in text or "饼干" in text:
        return "彩虹曲奇"
    if species == "fantasy":
        return "能量星果"
    if species == "tech":
        return "电池糖"
    return "鲜脆小食"


def _apply_time_decay(pet: PetProfile, now: datetime) -> bool:
    """
    Lazy time-decay engine:
      - Full 100% hunger/happiness decays linearly over RUNAWAY_HOURS (168h = 7 days) at ~0.5952%/h.
      - If unattended >= 168h or hunger <= 0 or happiness <= 0 -> pet.is_runaway = True.
    """
    care_dt = _ensure_utc(getattr(pet, "last_care_at", None), now)
    decay_dt = _ensure_utc(getattr(pet, "last_decay_at", None), care_dt)
    changed = False

    if getattr(pet, "last_care_at", None) is None:
        pet.last_care_at = care_dt
        changed = True
    if getattr(pet, "last_decay_at", None) is None:
        pet.last_decay_at = decay_dt
        changed = True

    unattended_hours = max(0.0, (now - care_dt).total_seconds() / 3600.0)
    delta_hours = max(0.0, (now - decay_dt).total_seconds() / 3600.0)

    if unattended_hours >= RUNAWAY_HOURS and not pet.is_runaway:
        pet.is_runaway = True
        pet.hunger = 0
        pet.happiness = 0
        pet.last_action_kind = "runaway"
        pet.last_decay_at = now
        pet.last_event_text = f"整整一周没喂食和陪伴，{pet.pet_name}留下一封小信离家出走了，需花{RECALL_COST}币召回。"
        return True

    if delta_hours >= 0.25 and not pet.is_runaway:
        rate_per_hour = 100.0 / RUNAWAY_HOURS
        drop = int(round(delta_hours * rate_per_hour))
        if drop >= 1:
            pet.hunger = max(0, pet.hunger - drop)
            pet.happiness = max(0, pet.happiness - drop)
            pet.last_decay_at = now
            changed = True
            if pet.hunger <= 0 or pet.happiness <= 0:
                pet.is_runaway = True
                pet.hunger = 0
                pet.happiness = 0
                pet.last_action_kind = "runaway"
                pet.last_event_text = f"{pet.pet_name}太饿太孤单，留下一封小信离家出走了，需花{RECALL_COST}币召回。"
            elif pet.hunger < 40 or pet.happiness < 40 or unattended_hours >= HUNGRY_HOURS:
                pet.last_action_kind = "hungry"

    return changed


def _compute_action_state(pet: PetProfile, now: datetime) -> tuple[int, str, str, float]:
    """
    Returns (action_state: 0..3, action_state_key: str, status_pill_text: str, unattended_hours: float).
      0 = happy_idle
      1 = eating_joy
      2 = hungry_sad
      3 = runaway
    """
    care_dt = _ensure_utc(getattr(pet, "last_care_at", None), now)
    unattended_hours = max(0.0, (now - care_dt).total_seconds() / 3600.0)
    minutes_since_care = unattended_hours * 60.0

    if pet.is_runaway or pet.hunger <= 0 or pet.happiness <= 0:
        return (
            3,
            "runaway",
            "7天没被照顾，留下一封小信离家出走了…",
            round(unattended_hours, 1),
        )

    if pet.hunger < 40 or pet.happiness < 40 or unattended_hours >= HUNGRY_HOURS:
        return (
            2,
            "hungry_sad",
            "肚子咕咕叫，眼巴巴守着空食盆等你…",
            round(unattended_hours, 1),
        )

    kind = getattr(pet, "last_action_kind", "idle") or "idle"
    if kind in ("feed", "play", "upgrade", "recall") and minutes_since_care <= JOY_ACTIVE_MINUTES:
        if kind == "feed":
            food = pet.last_food or "美食"
            pill = f"津津有味品尝{food}，幸福得直冒爱心！"
        elif kind == "play":
            pill = "开心地蹭蹭你的手心，发出满足呼噜声！"
        elif kind == "upgrade":
            pill = "搬进宽敞的新小窝，兴奋地转圈圈！"
        else:
            pill = "终于被你接回家啦，扑进怀里摇尾巴！"
        return (1, "eating_joy", pill, round(unattended_hours, 1))

    canonical_breed = pet.breed if pet.breed in PET_BREEDS else LEGACY_BREED_FALLBACK.get(pet.breed, "nebula_dragon")
    idle_pill = BREED_IDLE_PILLS.get(canonical_breed, "扑闪着小翅膀在屋里巡游探险")
    return (0, "happy_idle", idle_pill, round(unattended_hours, 1))


def get_or_create_pet(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    now: Optional[datetime] = None,
) -> PetProfile:
    cur_now = _ensure_utc(now, datetime.now(timezone.utc))
    stu = db.get(Student, student_id)
    if stu is None or stu.family_id != family_id:
        raise PermissionError("student_outside_family")
    pet = db.get(PetProfile, student_id)
    if pet is None:
        pet = PetProfile(
            student_id=student_id,
            family_id=family_id,
            adopted=True,
            species="fantasy",
            breed="nebula_dragon",
            pet_name="星宝",
            coins=45,
            hunger=88,
            happiness=92,
            exp=0,
            level=1,
            habitat_lv=1,
            is_runaway=False,
            last_action_kind="idle",
            last_care_at=cur_now,
            last_decay_at=cur_now,
            last_event_text="星宝正扑闪着小翅膀在屋里巡游探险呢！",
        )
        db.add(pet)
        db.commit()
        db.refresh(pet)
        return pet

    migrated = False
    if pet.breed not in PET_BREEDS:
        new_breed = LEGACY_BREED_FALLBACK.get(pet.breed, "nebula_dragon")
        meta = PET_BREEDS[new_breed]
        pet.breed = new_breed
        pet.species = meta["species"]
        if pet.pet_name in ("暖暖", "小橘", "布丁", "花花", "二哈", "短腿基", "铁角", "长戟王", "毛象君"):
            pet.pet_name = meta["default_name"]
        migrated = True
    elif pet.species != PET_BREEDS[pet.breed]["species"]:
        pet.species = PET_BREEDS[pet.breed]["species"]
        migrated = True

    if _apply_time_decay(pet, cur_now) or migrated:
        db.commit()
        db.refresh(pet)
    return pet


def pet_to_dict(pet: PetProfile, *, now: Optional[datetime] = None) -> dict[str, Any]:
    cur_now = _ensure_utc(now, datetime.now(timezone.utc))
    canonical_breed = pet.breed if pet.breed in PET_BREEDS else LEGACY_BREED_FALLBACK.get(pet.breed, "nebula_dragon")
    meta = PET_BREEDS[canonical_breed]
    sp = meta["species"]
    hab_name = HABITAT_NAMES.get(sp, HABITAT_NAMES["fantasy"]).get(pet.habitat_lv, "温馨小窝")
    next_hab_cost = HABITAT_UPGRADE_COST.get(pet.habitat_lv)
    action_state, action_state_key, status_pill_text, unattended_hours = _compute_action_state(pet, cur_now)

    slot_keys = SPECIES_FOOD_SLOTS.get(sp, SPECIES_FOOD_SLOTS["fantasy"])
    foods = []
    for idx, fkey in enumerate(slot_keys):
        fmeta = FOOD_CATALOG[fkey]
        foods.append(
            {
                "slot": idx,
                "key": fkey,
                "label": fmeta["short_name"],
                "cost": fmeta["cost"],
                "hunger": fmeta["hunger"],
                "happy": fmeta["happy"],
                "icon": fmeta["icon"],
            }
        )

    return {
        "student_id": pet.student_id,
        "species": sp,
        "species_cn": meta["species_cn"],
        "breed": canonical_breed,
        "breed_cn": meta["breed_cn"],
        "breed_id": meta["id"],
        "atlas_url": f"/guardian/assets/pets/pet_{meta['id']}_{canonical_breed}_atlas.png",
        "stage_url": f"/guardian/assets/pets/stage_lv{max(1, min(4, int(pet.habitat_lv or 1)))}.png",
        "pet_name": pet.pet_name,
        "coins": pet.coins,
        "hunger": pet.hunger,
        "happiness": pet.happiness,
        "level": pet.level,
        "exp": pet.exp,
        "habitat_lv": pet.habitat_lv,
        "habitat_name": hab_name,
        "next_habitat_cost": next_hab_cost,
        "is_runaway": bool(pet.is_runaway),
        "recall_cost": RECALL_COST,
        "action_state": action_state,
        "action_state_key": action_state_key,
        "status_pill_text": status_pill_text,
        "unattended_hours": unattended_hours,
        "foods": foods,
        "last_food": pet.last_food,
        "last_event_text": pet.last_event_text,
    }


def grant_pet_coins(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    amount: int,
    reason: str,
    now: Optional[datetime] = None,
) -> PetProfile:
    pet = get_or_create_pet(db, family_id=family_id, student_id=student_id, now=now)
    pet.coins += max(0, amount)
    if not pet.is_runaway:
        pet.happiness = min(100, pet.happiness + 5)
    pet.last_event_text = f"{reason}，赚到 {amount} 枚宠粮币！"
    db.commit()
    db.refresh(pet)
    return pet


def adopt_pet(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    breed_or_species: str,
    nickname: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    cur_now = _ensure_utc(now, datetime.now(timezone.utc))
    pet = get_or_create_pet(db, family_id=family_id, student_id=student_id, now=cur_now)
    matched = _match_breed(breed_or_species)
    if matched is None:
        return {
            "adopted": False,
            "catalog": [
                {"species": "奇幻星灵", "breeds": ["星云小龙", "棉花糖云", "麻薯灵狐"]},
                {"species": "萌趣伙伴", "breeds": ["泡芙水豚", "宇航企鹅", "呆萌恐龙"]},
                {"species": "星际潮玩", "breeds": ["雷电机甲", "极光星龙", "樱花灵狐"]},
            ],
            "message": "可以领养9只超萌的动态伙伴：星云小龙、棉花糖云、麻薯灵狐、泡芙水豚、宇航企鹅、呆萌恐龙、雷电机甲、极光星龙、樱花灵狐，你想领养哪一只呀？",
        }
    meta = PET_BREEDS[matched]
    pet.adopted = True
    pet.species = meta["species"]
    pet.breed = matched
    pet.pet_name = (nickname or "").strip()[:16] or meta["default_name"]
    pet.is_runaway = False
    pet.hunger = 88
    pet.happiness = 94
    pet.last_action_kind = "idle"
    pet.last_care_at = cur_now
    pet.last_decay_at = cur_now
    pet.last_event_text = f"成功领养了{meta['breed_cn']}「{pet.pet_name}」！"
    db.commit()
    db.refresh(pet)
    return {
        "adopted": True,
        "pet": pet_to_dict(pet, now=cur_now),
        "message": f"太棒啦！你领养了一只可爱的{meta['breed_cn']}，取名「{pet.pet_name}」，快去屏幕上看看它吧！",
    }


def feed_pet(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    food_name: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    cur_now = _ensure_utc(now, datetime.now(timezone.utc))
    pet = get_or_create_pet(db, family_id=family_id, student_id=student_id, now=cur_now)
    if pet.is_runaway:
        return {
            "fed": False,
            "is_runaway": True,
            "recall_cost": RECALL_COST,
            "coins": pet.coins,
            "pet": pet_to_dict(pet, now=cur_now),
            "message": f"{pet.pet_name}因为整整一周没被照顾已经离家出走啦！请先攒够{RECALL_COST}枚宠粮币（约5项学习任务）召回它吧。",
        }

    food_key = _match_food(food_name, pet.species)
    f = FOOD_CATALOG[food_key]
    cost = int(f["cost"])
    if pet.coins < cost:
        return {
            "fed": False,
            "is_runaway": False,
            "food_name": food_key,
            "cost": cost,
            "coins": pet.coins,
            "pet": pet_to_dict(pet, now=cur_now),
            "message": f"{food_key}需要{cost}枚宠粮币，现在有{pet.coins}枚。完成一项作业或听写就能赚到20枚宠粮币哦！",
        }

    pet.coins -= cost
    bonus = 8 if f["fav_species"] == pet.species else 0
    pet.hunger = min(100, pet.hunger + int(f["hunger"]))
    pet.happiness = min(100, pet.happiness + int(f["happy"]) + bonus)
    pet.exp += int(f["exp"]) + bonus
    pet.last_food = food_key
    pet.last_action_kind = "feed"
    pet.last_care_at = cur_now
    pet.last_decay_at = cur_now

    leveled_up = False
    while pet.exp >= pet.level * 50:
        pet.exp -= pet.level * 50
        pet.level += 1
        leveled_up = True

    fav_note = "，这是它最爱吃的！" if bonus > 0 else "！"
    lv_note = f" 还升到了 Lv.{pet.level}！" if leveled_up else ""
    pet.last_event_text = f"给{pet.pet_name}喂了{food_key}{fav_note}{lv_note}"
    db.commit()
    db.refresh(pet)
    return {
        "fed": True,
        "is_runaway": False,
        "food_name": food_key,
        "cost": cost,
        "favorite_bonus": bonus > 0,
        "leveled_up": leveled_up,
        "pet": pet_to_dict(pet, now=cur_now),
        "message": pet.last_event_text,
    }


def recall_pet(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    cur_now = _ensure_utc(now, datetime.now(timezone.utc))
    pet = get_or_create_pet(db, family_id=family_id, student_id=student_id, now=cur_now)
    if not pet.is_runaway:
        return {
            "recalled": True,
            "already_home": True,
            "cost": 0,
            "pet": pet_to_dict(pet, now=cur_now),
            "message": f"{pet.pet_name}一直乖乖待在小窝里等你呢！",
        }
    if pet.coins < RECALL_COST:
        deficit = RECALL_COST - pet.coins
        return {
            "recalled": False,
            "cost": RECALL_COST,
            "coins": pet.coins,
            "pet": pet_to_dict(pet, now=cur_now),
            "message": f"召回{pet.pet_name}需要{RECALL_COST}枚宠粮币（约5项学习任务），目前有{pet.coins}枚，还差{deficit}枚，快去写作业或听写接它回家吧！",
        }

    pet.coins -= RECALL_COST
    pet.is_runaway = False
    pet.hunger = 65
    pet.happiness = 75
    pet.last_action_kind = "recall"
    pet.last_care_at = cur_now
    pet.last_decay_at = cur_now
    pet.last_event_text = f"花费{RECALL_COST}枚宠粮币把{pet.pet_name}温情接回家啦！它扑进你怀里直摇尾巴，以后要常来看看它哦！"
    db.commit()
    db.refresh(pet)
    return {
        "recalled": True,
        "cost": RECALL_COST,
        "pet": pet_to_dict(pet, now=cur_now),
        "message": pet.last_event_text,
    }


def simulate_pet_unattended_days(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    days: float,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Simulate unattended time passage (e.g., 0=healthy reset, 3.5=hungry_sad, 7=runaway) for testing & demo."""
    cur_now = _ensure_utc(now, datetime.now(timezone.utc))
    pet = get_or_create_pet(db, family_id=family_id, student_id=student_id, now=cur_now)
    if days <= 0.05:
        pet.is_runaway = False
        pet.hunger = 90
        pet.happiness = 95
        pet.last_action_kind = "idle"
        pet.last_care_at = cur_now
        pet.last_decay_at = cur_now
        pet.last_event_text = f"{pet.pet_name}吃饱喝足，正开心地在窝里晒太阳呢！"
        db.commit()
        db.refresh(pet)
        return {"simulated_days": 0.0, "pet": pet_to_dict(pet, now=cur_now), "message": pet.last_event_text}

    past_dt = cur_now - timedelta(days=float(days))
    pet.is_runaway = False
    pet.hunger = 90
    pet.happiness = 95
    pet.last_action_kind = "idle"
    pet.last_care_at = past_dt
    pet.last_decay_at = past_dt
    _apply_time_decay(pet, cur_now)
    db.commit()
    db.refresh(pet)
    return {
        "simulated_days": float(days),
        "pet": pet_to_dict(pet, now=cur_now),
        "message": pet.last_event_text,
    }


def upgrade_pet_habitat(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    cur_now = _ensure_utc(now, datetime.now(timezone.utc))
    pet = get_or_create_pet(db, family_id=family_id, student_id=student_id, now=cur_now)
    if pet.is_runaway:
        return {
            "upgraded": False,
            "is_runaway": True,
            "pet": pet_to_dict(pet, now=cur_now),
            "message": f"{pet.pet_name}已经离家出走啦，请先花{RECALL_COST}币召回它再升级小窝吧！",
        }
    if pet.habitat_lv >= 4:
        hab_name = HABITAT_NAMES.get(pet.species, HABITAT_NAMES["fantasy"])[4]
        return {
            "upgraded": False,
            "habitat_lv": 4,
            "pet": pet_to_dict(pet, now=cur_now),
            "message": f"{pet.pet_name}已经住进最高级的「{hab_name}」啦！",
        }
    cost = HABITAT_UPGRADE_COST[pet.habitat_lv]
    if pet.coins < cost:
        next_name = HABITAT_NAMES.get(pet.species, HABITAT_NAMES["fantasy"])[pet.habitat_lv + 1]
        return {
            "upgraded": False,
            "cost": cost,
            "coins": pet.coins,
            "pet": pet_to_dict(pet, now=cur_now),
            "message": f"升级到「{next_name}」需要{cost}枚宠粮币，目前有{pet.coins}枚，完成作业就能攒够哦！",
        }

    pet.coins -= cost
    pet.habitat_lv += 1
    pet.happiness = 100
    pet.last_action_kind = "upgrade"
    pet.last_care_at = cur_now
    pet.last_decay_at = cur_now
    new_name = HABITAT_NAMES.get(pet.species, HABITAT_NAMES["fantasy"])[pet.habitat_lv]
    pet.last_event_text = f"把小窝升级成了「{new_name}」，{pet.pet_name}开心极了！"
    db.commit()
    db.refresh(pet)
    return {
        "upgraded": True,
        "habitat_lv": pet.habitat_lv,
        "habitat_name": new_name,
        "remaining_coins": pet.coins,
        "pet": pet_to_dict(pet, now=cur_now),
        "message": pet.last_event_text,
    }


def play_with_pet(
    db: Session,
    *,
    family_id: str,
    student_id: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    cur_now = _ensure_utc(now, datetime.now(timezone.utc))
    pet = get_or_create_pet(db, family_id=family_id, student_id=student_id, now=cur_now)
    if pet.is_runaway:
        return {
            "played": False,
            "is_runaway": True,
            "pet": pet_to_dict(pet, now=cur_now),
            "message": f"小窝空空如也，{pet.pet_name}离家出走啦！快攒够{RECALL_COST}币召回它吧。",
        }
    pet.happiness = min(100, pet.happiness + 12)
    pet.exp += 10
    pet.last_action_kind = "play"
    pet.last_care_at = cur_now
    pet.last_decay_at = cur_now
    if pet.species == "fantasy":
        action = f"{pet.pet_name}开心地扑闪着星光，原地蹦跳转了个圈！"
    elif pet.species == "tech":
        action = f"{pet.pet_name}屏幕亮起像素大爱心，炫酷地跳了一段星际舞！"
    else:
        action = f"{pet.pet_name}软乎乎地滚到你脚边，开心得直冒小爱心！"
    pet.last_event_text = action
    db.commit()
    db.refresh(pet)
    return {"played": True, "pet": pet_to_dict(pet, now=cur_now), "message": action}
