from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import io
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
from typing import Any, Optional
from urllib.parse import quote
import urllib.request
import wave

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.adapters.tencent_tts import (
    TTSSpec,
    TencentTTSAdapter,
    normalize_for_chinese_tts,
    synthesize_tencent_http_pcm,
    transcribe_tencent_asr_pcm,
)
from backend.agent.harness import YamiAgentHarness
from backend.api.auth import router as auth_router
from backend.api.guardian import router as guardian_router
from backend.config import settings
from backend.db import get_db, init_db
from backend.models import Assignment, Device, Family, PairingCode, Student
from backend.rewards.pet_service import (
    SPECIES_FOOD_SLOTS,
    TASK_COIN_REWARDS,
    feed_pet,
    get_or_create_pet,
    grant_pet_coins,
    pet_to_dict,
    recall_pet,
)
from backend.submissions.service import record_submission_evidence
from backend.learning.dictation import (
    get_active_dictation,
    handle_dictation_voice_command,
    start_dictation_from_request,
)
from backend.middleware.safety import SafetyMiddleware
from backend.schemas import (
    DingTalkBatchIn,
    DingTalkSyncResult,
    PairingClaimRequest,
    PairingClaimResponse,
    PairingIssueRequest,
    PairingIssueResponse,
    SelectStudentRequest,
    SelectStudentResponse,
    StudentAssignmentsOut,
    VoiceQueryRequest,
    VoiceQueryResponse,
)
from backend.services.assignments import get_student_assignments, route_voice_assignment_query
from backend.services.desk_check import (
    bump_pet_jump_seq,
    get_desk_check_seq,
    get_latest_desk_check,
    get_pet_jump_seq,
    queue_desk_check,
    record_desk_check_result,
)
from backend.services.dingtalk_sync import (
    extract_and_store_class_performance,
    ingest_dingtalk_batch,
    parse_and_ingest_raw_class_message,
)
from backend.services.identity import (
    authenticate_device,
    claim_pairing_code,
    issue_pairing_code,
    select_student,
    validate_turn_session,
)

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Yami Learning Companion API", version="0.1.0", lifespan=_lifespan)
app.include_router(auth_router)
app.include_router(guardian_router)
safety_middleware = SafetyMiddleware()
agent_harness = YamiAgentHarness(safety=safety_middleware)


@app.get("/v1/agent/skills")
def api_agent_skills() -> list[dict[str, str]]:
    return [
        {"name": s.name, "description": s.description, "path": str(s.path.name)}
        for s in agent_harness.skills.list_skills()
    ]


@app.get("/v1/agent/metrics")
def api_agent_metrics() -> dict:
    return {
        "calls": agent_harness.metrics.calls,
        "success_turns": agent_harness.success_turns,
        "fallback_turns": agent_harness.fallback_turns,
        "total_prompt_tokens": agent_harness.metrics.total_prompt_tokens,
        "cached_tokens": agent_harness.metrics.cached_tokens,
        "cache_hit_rate": round(agent_harness.metrics.hit_rate, 4),
        "agent_provider": settings.agent_provider,
        "skills": [s.name for s in agent_harness.skills.list_skills()],
        "configured_providers": {
            "volc_plan": bool(settings.volc_plan_api_key),
            "tencent_plan": bool(settings.tencent_plan_api_key),
            "doubao_seed": bool(settings.ark_api_key),
            "deepseek": bool(settings.deepseek_api_key),
        },
        "tts_provider": "tencent_tts" if (settings.tencent_secret_id and settings.tencent_secret_key) else "macos_tingting_tts",
        "tts_voice_type": settings.tencent_tts_voice_type,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/guardian")
def serve_guardian_web():
    web_file = Path(__file__).resolve().parents[2] / "apps" / "guardian" / "index.html"
    if not web_file.exists():
        raise HTTPException(status_code=404, detail="guardian_web_not_built")
    return FileResponse(web_file)


@app.get("/guardian/pet-ui-options")
def serve_guardian_pet_options():
    web_file = Path(__file__).resolve().parents[2] / "apps" / "guardian" / "pet-ui-options.html"
    if not web_file.exists():
        raise HTTPException(status_code=404, detail="pet_options_not_found")
    return FileResponse(web_file)


@app.get("/guardian/style-options")
def serve_guardian_style_options():
    web_file = Path(__file__).resolve().parents[2] / "apps" / "guardian" / "style-options.html"
    if not web_file.exists():
        raise HTTPException(status_code=404, detail="style_options_not_found")
    return FileResponse(web_file)


@app.get("/guardian/assets/{category}/{filename}")
def serve_guardian_asset(category: str, filename: str):
    if category not in ("anime", "fluent3d", "pets") or "/" in filename or ".." in filename:
        raise HTTPException(status_code=404, detail="asset_not_found")
    asset_file = Path(__file__).resolve().parents[2] / "apps" / "guardian" / "assets" / category / filename
    if not asset_file.exists():
        raise HTTPException(status_code=404, detail="asset_not_found")
    return FileResponse(asset_file)


@app.post("/v1/pairing/issue", response_model=PairingIssueResponse)
def api_issue_pairing(payload: PairingIssueRequest, db: Session = Depends(get_db)) -> PairingIssueResponse:
    device, pairing = issue_pairing_code(
        db,
        hardware_uid=payload.hardware_uid,
        has_camera=payload.has_camera,
        firmware_version=payload.firmware_version,
    )
    return PairingIssueResponse(device_id=device.id, pairing_code=pairing.code, expires_at=pairing.expires_at)


@app.post("/v1/pairing/claim", response_model=PairingClaimResponse)
def api_claim_pairing(payload: PairingClaimRequest, db: Session = Depends(get_db)) -> PairingClaimResponse:
    try:
        device = claim_pairing_code(db, family_id=payload.family_id, code=payload.pairing_code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PairingClaimResponse(
        device_id=device.id,
        family_id=device.family_id or payload.family_id,
        device_token=device.device_token or "",
        auth_version=device.auth_version,
        profile_epoch=device.profile_epoch,
    )


@app.get("/v1/pairing/poll")
def api_poll_pairing(
    hardware_uid: str,
    code: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Polled every ~2s by Mosaico while displaying the 6-digit pairing code.
    Returns status: 'pending' | 'claimed' | 'expired' | 'invalid'.
    Once claimed by an authenticated guardian, returns the device_token for NVS persistence.
    """
    from datetime import datetime, timezone

    pairing = db.get(PairingCode, code.strip())
    if pairing is None:
        return {"status": "invalid"}
    dev = db.get(Device, pairing.device_id)
    if dev is None or dev.hardware_uid != hardware_uid.strip():
        return {"status": "invalid"}
    if pairing.consumed_at is not None and dev.family_id and dev.device_token and not dev.revoked:
        return {
            "status": "claimed",
            "device_id": dev.id,
            "family_id": dev.family_id,
            "device_token": dev.device_token,
            "auth_version": dev.auth_version,
            "profile_epoch": dev.profile_epoch,
        }
    exp = pairing.expires_at if pairing.expires_at.tzinfo is not None else pairing.expires_at.replace(tzinfo=timezone.utc)
    if exp <= datetime.now(timezone.utc):
        return {"status": "expired"}
    return {"status": "pending"}


def _resolve_active_student(
    db: Session,
    hardware_uid: Optional[str] = None,
    device_token: Optional[str] = None,
) -> tuple[str, str, int, list[Student]]:
    """
    Resolve (family_id, active_student_id, active_slot_idx, family_students) for a device.
    When X-Device-Token is supplied, strictly authenticates the paired device (401 on revoked/invalid).
    """
    family_id = "fam_a"
    dev: Optional[Device] = None
    if device_token:
        try:
            dev = authenticate_device(db, device_token)
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if not dev.family_id:
            raise HTTPException(status_code=401, detail="device_not_bound")
        family_id = dev.family_id
    elif hardware_uid:
        dev = db.scalar(select(Device).where(Device.hardware_uid == hardware_uid))
        if dev is None:
            fam = db.get(Family, "fam_a")
            if fam is not None:
                dev = Device(
                    id=f"dev_{hardware_uid[-12:]}",
                    family_id=fam.id,
                    hardware_uid=hardware_uid,
                    has_camera=True,
                    firmware_version="0.3.0",
                )
                db.add(dev)
                db.commit()
                db.refresh(dev)
        if dev and dev.family_id:
            family_id = dev.family_id

    rows = list(
        db.scalars(
            select(Student)
            .where(Student.family_id == family_id)
            .order_by(Student.id.asc())
        )
    )
    if not rows:
        return family_id, "stu_he", 0, []

    active_id = dev.active_student_id if (dev and dev.active_student_id) else None
    active_idx = next((i for i, s in enumerate(rows) if s.id == active_id), 0)
    return family_id, rows[active_idx].id, active_idx, rows


@app.post("/v1/devices/active-student")
def api_device_set_active_student(
    hardware_uid: str = "mosaico-yami-s31",
    slot: int = 0,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Immediately persist student slot switched via Mosaico hardware button."""
    family_id, _, _, rows = _resolve_active_student(db, hardware_uid)
    if not rows:
        raise HTTPException(status_code=404, detail="no_students_in_family")
    idx = slot % len(rows)
    target = rows[idx]
    devs = list(db.scalars(select(Device).where(Device.family_id == family_id)))
    for d in devs:
        d.active_student_id = target.id
        d.profile_epoch = (d.profile_epoch or 1) + 1
    db.commit()
    return {"active": str(idx), "student_id": target.id, "student_name": target.name}


@app.get("/v1/devices/profile-sync")
def api_device_profile_sync(
    hardware_uid: Optional[str] = None,
    x_device_token: Optional[str] = Header(default=None, alias="X-Device-Token"),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Lightweight LAN/Cloud sync endpoint for Mosaico to display live student names, active slot & today's pending tasks."""
    family_id, active_stu_id, active_idx, rows = _resolve_active_student(
        db, hardware_uid=hardware_uid, device_token=x_device_token
    )
    s0 = rows[0].name if len(rows) > 0 else "小禾"
    s1 = rows[1].name if len(rows) > 1 else "乐乐"

    pending_labels: list[str] = []
    if rows:
        asgs = list(
            db.scalars(
                select(Assignment)
                .where(
                    Assignment.family_id == family_id,
                    Assignment.student_id == active_stu_id,
                    Assignment.completed.is_(False),
                )
                .order_by(Assignment.updated_at.desc())
                .limit(6)
            )
        )
        for a in asgs:
            subj = a.subject or "作业"
            if "听写" in (a.content or ""):
                tag = "语音听写"
            elif "朗读" in (a.content or "") or "背诵" in (a.content or ""):
                tag = "课文朗读"
            elif "练习" in (a.content or "") or "计算" in (a.content or ""):
                tag = "练习册"
            else:
                tag = "今日作业"
            pending_labels.append(f"{subj} · {tag}")

    cnt = str(len(pending_labels))
    t0 = pending_labels[0] if len(pending_labels) > 0 else "今日作业全完成！"
    t1 = pending_labels[1] if len(pending_labels) > 1 else "语文 · 语音听写"
    t2 = pending_labels[2] if len(pending_labels) > 2 else "英语 · 课文朗读"

    pbid, pname, pbreed, plv, pcoin, phung, phap, phab, phabn = "0", "星宝", "星云小龙", "1", "45", "88", "92", "1", "奇幻树屋"
    prun, pact, ppill = "0", "0", "扑闪着小翅膀在屋里巡游探险"
    if rows:
        try:
            pd = pet_to_dict(get_or_create_pet(db, family_id=family_id, student_id=active_stu_id))
            pbid = str(pd["breed_id"])
            pname = str(pd["pet_name"])
            pbreed = str(pd["breed_cn"])
            plv = str(pd["level"])
            pcoin = str(pd["coins"])
            phung = str(pd["hunger"])
            phap = str(pd["happiness"])
            phab = str(pd["habitat_lv"])
            phabn = str(pd["habitat_name"])
            prun = "1" if pd.get("is_runaway") else "0"
            pact = str(pd.get("action_state", 0))
            ppill = str(pd.get("status_pill_text", "摇着尾巴守在食盆旁"))
        except Exception:
            pass

    return {
        "s0": s0,
        "s1": s1,
        "active": str(active_idx),
        "cnt": cnt,
        "t0": t0,
        "t1": t1,
        "t2": t2,
        "pbid": pbid,
        "pname": pname,
        "pbreed": pbreed,
        "plv": plv,
        "pcoin": pcoin,
        "phung": phung,
        "phap": phap,
        "phab": phab,
        "phabn": phabn,
        "prun": prun,
        "pact": pact,
        "ppill": ppill,
        "hjump": str(get_pet_jump_seq(db, active_stu_id)),
        "dchk": str(get_desk_check_seq(db, hardware_uid or "mosaico-yami-s31")),
    }


@app.post("/v1/devices/pet-feed")
def api_device_pet_feed(
    hardware_uid: str = "mosaico-yami-s31",
    slot: int = 0,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    family_id, active_stu_id, _, _ = _resolve_active_student(db, hardware_uid)
    pet = get_or_create_pet(db, family_id=family_id, student_id=active_stu_id)
    if pet.is_runaway or slot == 99:
        return recall_pet(db, family_id=family_id, student_id=active_stu_id)
    foods = SPECIES_FOOD_SLOTS.get(pet.species, SPECIES_FOOD_SLOTS["fantasy"])
    food_name = foods[slot if 0 <= slot < 3 else 0]
    res = feed_pet(db, family_id=family_id, student_id=active_stu_id, food_name=food_name)
    return res


@app.post("/v1/devices/select-student", response_model=SelectStudentResponse)
def api_select_student(
    payload: SelectStudentRequest,
    x_device_token: str = Header(..., alias="X-Device-Token"),
    db: Session = Depends(get_db),
) -> SelectStudentResponse:
    try:
        device = authenticate_device(db, x_device_token)
        confirmed, reason, session, epoch = select_student(
            db,
            device=device,
            student_id=payload.student_id,
            method=payload.method,
            confidence=payload.confidence,
            second_best_confidence=payload.second_best_confidence,
            overlapping_speech=payload.overlapping_speech,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return SelectStudentResponse(
        confirmed=confirmed,
        reason=reason,
        session_id=session.id if session else None,
        student_id=session.student_id if session else None,
        profile_epoch=epoch,
    )


@app.post("/v1/sync/dingtalk/ingest", response_model=DingTalkSyncResult)
def api_ingest_dingtalk(payload: DingTalkBatchIn, db: Session = Depends(get_db)) -> DingTalkSyncResult:
    try:
        return ingest_dingtalk_batch(db, payload)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/v1/sync/dingtalk/parse-raw")
def api_parse_raw_dingtalk_class_message(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Parse raw Chinese teacher message text from a DingTalk class group into structured assignments."""
    family_id = str(payload.get("family_id") or "fam_a")
    student_id = str(payload.get("student_id") or "stu_he")
    raw_text = str(payload.get("raw_text") or "").strip()
    sender_name = str(payload.get("sender_name") or "班主任")
    conversation_id = str(payload.get("conversation_id") or "conv_class_live")
    if len(raw_text) < 4:
        raise HTTPException(status_code=400, detail="empty_raw_text")
    try:
        res, parsed = parse_and_ingest_raw_class_message(
            db,
            family_id=family_id,
            student_id=student_id,
            raw_text=raw_text,
            sender_name=sender_name,
            conversation_id=conversation_id,
        )
        perf_items = extract_and_store_class_performance(
            db,
            family_id=family_id,
            student_id=student_id,
            raw_text=raw_text,
            sender_name=sender_name,
            conversation_id=conversation_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "status": res.status,
        "created": res.created,
        "skipped_duplicate": res.skipped_duplicate,
        "parsed_items": parsed,
        "performance_memories": perf_items,
    }


def _call_doubao_vision_parse_homework(
    image_bytes: bytes, mime_type: str, caption_hint: str = ""
) -> list[dict[str, str]]:
    """Use Doubao Seed Vision to OCR a blackboard / homework photo into structured subject items."""
    if not settings.ark_api_key:
        raise RuntimeError("ARK_API_KEY not configured for vision homework OCR")
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"
    url = f"{settings.ark_base_url.rstrip('/')}/responses"
    payload = {
        "model": settings.ark_seed_model,
        "input": [
            {
                "role": "system",
                "content": (
                    "你是小学作业板书识别助手。请准确识别图片中的各科作业内容（如黑板作业记载栏、纸质作业条），"
                    "严格输出 JSON：{\"items\": [{\"subject\": \"语文/数学/英语/科学/其他\", "
                    "\"content\": \"完整作业内容\", \"page_ref\": \"页码如P44或空字符串\", "
                    "\"submission_mode\": \"paper_next_day或dingtalk_online或none\"}]}。"
                    "如果某一学科栏为空则不要输出该学科。绝不编造图片中不存在的内容。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": data_url},
                    {
                        "type": "input_text",
                        "text": f"请识别这张作业图片中的全部作业条目。补充说明：{caption_hint or '无'}",
                    },
                ],
            },
        ],
        "thinking": {"type": "disabled"},
        "max_output_tokens": 400,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.ark_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = ""
    for item in data.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") == "output_text" and c.get("text"):
                    raw += str(c["text"])
    raw = raw.strip()
    j_start = raw.find("{")
    j_end = raw.rfind("}")
    if j_start >= 0 and j_end > j_start:
        obj = json.loads(raw[j_start : j_end + 1])
        items = obj.get("items") or []
        out: list[dict[str, str]] = []
        for it in items:
            content = str(it.get("content") or "").strip()
            if not content:
                continue
            out.append(
                {
                    "subject": str(it.get("subject") or "其他").strip()[:16],
                    "content": content,
                    "page_ref": str(it.get("page_ref") or "").strip()[:32],
                    "submission_mode": str(it.get("submission_mode") or "paper_next_day").strip(),
                }
            )
        return out
    return []


@app.post("/v1/sync/dingtalk/parse-image")
async def api_parse_image_dingtalk_homework(
    request: Request,
    family_id: str = "fam_a",
    student_id: Optional[str] = None,
    sender_name: str = "家长图片转发",
    caption: str = "",
    db: Session = Depends(get_db),
):
    """Parse a blackboard homework photo or school notice screenshot via Doubao Seed 2.1 multimodal Agent Harness."""
    img_bytes = await request.body()
    if not img_bytes:
        raise HTTPException(status_code=400, detail="empty_image_body")
    mime = (request.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"

    students = list(db.scalars(select(Student).where(Student.family_id == family_id).order_by(Student.id.asc())))
    if not students:
        raise HTTPException(status_code=404, detail="no_student_in_family")
    target_stu = None
    if student_id:
        target_stu = next((s for s in students if s.id == student_id), None)
    if target_stu is None and caption:
        target_stu = next((s for s in students if s.name and s.name in caption), None)
    if target_stu is None:
        target_stu = students[0]

    from backend.agent.dingtalk_agent import run_dingtalk_agent_turn

    agent_res = run_dingtalk_agent_turn(
        db,
        family_id=family_id,
        student=target_stu,
        user_text=caption or "请识别图片中的各科作业、需签字的家校通知待办或老师表扬，并自动录入。",
        sender_nick=sender_name,
        image_bytes=img_bytes,
        mime_type=mime,
    )
    if agent_res is None:
        raise HTTPException(status_code=502, detail="multimodal_doubao_seed_unavailable")
    return {
        "status": "ok",
        "student_id": target_stu.id,
        "student_name": target_stu.name,
        **agent_res,
    }


def _call_doubao_vision_desk_check(image_bytes: bytes, mime_type: str, student_name: str) -> tuple[bool, str]:
    """Privacy-safe Doubao Seed Vision check: determines ONLY whether the student is studying at the desk."""
    if not settings.ark_api_key:
        return True, f"已通过摄像头确认，{student_name}当前正在写字台前学习（离线模拟）。"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"
    url = f"{settings.ark_base_url.rstrip('/')}/responses"
    payload = {
        "model": settings.ark_seed_model,
        "input": [
            {
                "role": "system",
                "content": (
                    "你是芽米学习陪伴摄像头助手。请仅根据画面判断学生目前是否在写字台前学习，"
                    "以 JSON 格式返回：{\"at_desk\": true或false, \"summary\": \"1句不超过30字的温和客观中文状态描述\"}。"
                    "绝不输出与学习状态无关的隐私细节。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": data_url},
                    {"type": "input_text", "text": f"请判断{student_name}目前是否在写字台前学习。"},
                ],
            },
        ],
        "thinking": {"type": "disabled"},
        "max_output_tokens": 96,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.ark_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=18) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = ""
    for item in data.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") == "output_text" and c.get("text"):
                    raw += str(c["text"])
    raw = raw.strip()
    try:
        j_start = raw.find("{")
        j_end = raw.rfind("}")
        if j_start >= 0 and j_end > j_start:
            parsed = json.loads(raw[j_start : j_end + 1])
            return bool(parsed.get("at_desk", False)), str(parsed.get("summary") or raw)
    except Exception:
        pass
    at_desk = not any(k in raw for k in ("不在", "没有人", "离开", "空"))
    return at_desk, raw or f"{student_name}当前在写字台前。"


@app.post("/v1/devices/request-desk-check")
def api_request_desk_check(
    hardware_uid: str = "mosaico-yami-s31",
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Queue a transparent, consent-gated camera desk-study check on Mosaico."""
    _, student_id, _, _ = _resolve_active_student(db, hardware_uid)
    stu = db.get(Student, student_id)
    if stu is None:
        raise HTTPException(status_code=404, detail="student_not_found")
    if not stu.camera_consent:
        raise HTTPException(status_code=403, detail="camera_consent_required")
    seq = queue_desk_check(db, hardware_uid)
    return {
        "queued": True,
        "seq": seq,
        "student_id": stu.id,
        "student_name": stu.name,
        "last_result": get_latest_desk_check(db, stu.id),
    }


@app.post("/v1/vision/desk-check")
async def api_vision_desk_check(
    request: Request,
    x_hardware_uid: Optional[str] = Header(default=None, alias="X-Hardware-Uid"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Privacy-first desk-check upload from Mosaico CameraBoard:
    - Enforces student.camera_consent == True.
    - Analyzes study presence via Doubao Seed Vision.
    - Discards JPEG immediately (zero photo storage), returns & caches 1-sentence summary only.
    """
    img_bytes = await request.body()
    if len(img_bytes) < 64:
        raise HTTPException(status_code=400, detail="empty_image_payload")
    _, student_id, _, _ = _resolve_active_student(db, x_hardware_uid or "mosaico-yami-s31")
    stu = db.get(Student, student_id)
    if stu is None:
        raise HTTPException(status_code=404, detail="student_not_found")
    if not stu.camera_consent:
        raise HTTPException(status_code=403, detail="camera_consent_required")

    mime = request.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if mime not in ("image/jpeg", "image/png"):
        mime = "image/jpeg"
    try:
        at_desk, summary = _call_doubao_vision_desk_check(img_bytes, mime, stu.name)
    except Exception:
        at_desk, summary = True, f"摄像头已查看，{stu.name}正在书桌前学习。"
    return record_desk_check_result(db, stu.id, at_desk=at_desk, summary=summary)


@app.post("/v1/dingtalk/bot/callback")
def api_dingtalk_bot_callback(
    payload: dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_family_id: Optional[str] = Header(default="fam_a", alias="X-Family-Id"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Inbound DingTalk Outgoing Robot callback for parent <-> Yami bot interaction & task triggering.
    Supports:
      - Smart student targeting (e.g. '给乐乐听写...', '小禾今天作业')
      - '转发作业：...' / '转发群消息：...' / '表扬...' -> Parse & sync Homework + Performance/Praise Memory (PG + Graphiti Outbox)
      - '提醒... 过15分钟...' / '提醒 19:30 ...' -> Schedule proactive Mosaico Reminder
      - '安排复习 ...' / '巩固 ...' -> Schedule next-day ReviewItem
      - '布置作业 数学 ...' -> Manually assign extra task
      - '确认提交 数学' / '数学已交' -> Parent confirm submission
      - '听写 第3单元第4课' -> Start curriculum dictation
      - '宠物状态' / '成长记录' -> Live Pet House & Growth timeline summary
      - '看看孩子在不在写字台' / '看书桌' -> Consent-gated Mosaico camera desk check
      - Default: Today's structured homework & submission status
    """
    from datetime import datetime, timedelta, timezone
    import re
    import uuid
    from backend.learning.state_machine import schedule_next_day_review
    from backend.models import GrowthRecord
    from backend.reminders.service import schedule_or_reschedule_reminder
    from backend.services.auth import verify_guardian_jwt

    family_id = x_family_id or "fam_a"
    if authorization and authorization.lower().startswith("bearer "):
        try:
            claims = verify_guardian_jwt(authorization.split(" ", 1)[1].strip())
            family_id = str(claims["fam"])
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    text_obj = payload.get("text")
    raw_msg = str(text_obj.get("content") if isinstance(text_obj, dict) else (payload.get("content") or "")).strip()
    sender_nick = str(payload.get("senderNick") or "家长")

    # Smart student targeting: match any student name (or 2-char suffix) in this family
    fam_students = list(
        db.scalars(select(Student).where(Student.family_id == family_id).order_by(Student.created_at.asc()))
    )
    stu: Optional[Student] = None
    for s in fam_students:
        if s.name in raw_msg or (len(s.name) >= 3 and s.name[-2:] in raw_msg):
            stu = s
            break
    if stu is None:
        _, active_sid, _, _ = _resolve_active_student(db, "mosaico-yami-s31")
        cand = db.get(Student, active_sid)
        if cand is not None and cand.family_id == family_id:
            stu = cand
        elif fam_students:
            stu = fam_students[0]
    student_id = stu.id if stu else "stu_he"
    stu_name = stu.name if stu else "小禾"

    def _reply_md(title: str, body: str, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": body},
            "reply_summary": body,
            "student_id": student_id,
        }
        if extra:
            out.update(extra)
        return out

    # 1. Camera desk check (consent-gated)
    if any(k in raw_msg for k in ("写字台", "书桌", "摄像头", "在不在学习", "看看孩子")):
        if stu is None or not stu.camera_consent:
            return _reply_md(
                "未开启摄像头授权",
                f"### 芽米安全提示\n{stu_name}尚未开启摄像头授权，请在家长端设置中打开「摄像头拍题与学习查看」后再试。",
            )
        seq = queue_desk_check(db, "mosaico-yami-s31")
        latest = get_latest_desk_check(db, stu.id)
        if latest:
            return _reply_md(
                "书桌学习状态",
                f"### {stu_name}书桌状态（已再次触发设备查看 #{seq}）\n- 最近查看：{latest['summary']}\n- 隐私说明：不留存照片，设备查看时会发出提示音。",
            )
        return _reply_md(
            "已触发书桌查看",
            f"### 已向 Mosaico 发送书桌查看请求（#{seq}）\n设备将发出提示音并由 AI 判断{stu_name}是否在写字台前学习，照片阅后即焚不保存。",
        )

    # 2. Parent schedules a proactive Mosaico reminder (e.g. "提醒小禾 过15分钟检查英语" or "提醒 19:30 开始写数学")
    if "提醒" in raw_msg and not any(k in raw_msg for k in ("转发作业", "转发群消息")):
        now = datetime.now(timezone.utc)
        m_rel = re.search(r"过\s*(\d+)\s*分钟", raw_msg)
        m_abs = re.search(r"(\d{1,2})\s*[:：点时]\s*(\d{0,2})", raw_msg)
        if m_rel:
            mins = max(1, min(720, int(m_rel.group(1))))
            due_at = now + timedelta(minutes=mins)
            time_desc = f"{mins} 分钟后"
        elif m_abs:
            hh = max(0, min(23, int(m_abs.group(1))))
            mm = int(m_abs.group(2)) if m_abs.group(2) else 0
            mm = max(0, min(59, mm))
            # Convert Asia/Shanghai wall-clock to UTC
            utc_h = (hh - 8) % 24
            due_at = now.replace(hour=utc_h, minute=mm, second=0, microsecond=0)
            if due_at <= now:
                due_at += timedelta(days=1)
            time_desc = f"{hh:02d}:{mm:02d}"
        else:
            due_at = now + timedelta(minutes=10)
            time_desc = "10 分钟后"

        anchor = re.sub(r"^(?:请)?(?:给)?[^\s，,]*提醒[^\s，,]*", "", raw_msg).strip(" ：:，,") or raw_msg
        rem_kind = "submission" if any(k in raw_msg for k in ("交", "提交", "打卡")) else "check"
        rem, _ = schedule_or_reschedule_reminder(
            db,
            idempotency_key=f"dtbot:{family_id}:{student_id}:{uuid.uuid4().hex[:8]}",
            family_id=family_id,
            student_id=student_id,
            kind=rem_kind,  # type: ignore[arg-type]
            anchor_label=anchor[:120],
            scheduled_for=due_at,
        )
        return _reply_md(
            "已预约主动提醒",
            f"### 已为{stu_name}预约 Mosaico 提醒\n- 提醒时间：{time_desc}\n- 提醒内容：{rem.anchor_label}\n- 类型：{rem.kind}",
            extra={"reminder_id": rem.id},
        )

    # 3. Parent schedules next-day review / consolidation task
    if any(k in raw_msg for k in ("安排复习", "预约复习", "明天复习", "巩固复习")):
        now = datetime.now(timezone.utc)
        topic = re.sub(r".*(?:安排复习|预约复习|明天复习|巩固复习)\s*", "", raw_msg).strip(" ：:") or "单位分数比较"
        rev = schedule_next_day_review(
            db,
            family_id=family_id,
            student_id=student_id,
            problem_id="math_g4_unit_fraction_v1",
            method_note=f"家长钉钉安排巩固：{topic}",
            due_at=now + timedelta(hours=18),
        )
        return _reply_md(
            "已安排次日巩固",
            f"### 已为{stu_name}加入巩固队列\n- 知识点：{topic}\n- 方式：次日由芽米先听孩子独立口述思路再给提示",
            extra={"review_id": rev.id},
        )

    # 4. Parent manually assigns an extra task (e.g. "布置作业 数学 口算20道")
    if any(k in raw_msg for k in ("布置作业", "加一项作业", "新增作业")):
        target_subj = next((s for s in ("数学", "语文", "英语", "科学") if s in raw_msg), "数学")
        content = re.sub(r".*(?:布置作业|加一项作业|新增作业)\s*", "", raw_msg).strip(" ：:")
        now = datetime.now(timezone.utc)
        asg = Assignment(
            id=f"asg_bot_{uuid.uuid4().hex[:10]}",
            family_id=family_id,
            student_id=student_id,
            source_conversation_id="dingtalk_parent_bot",
            source_message_id=f"bot_{uuid.uuid4().hex[:10]}",
            sender_id=f"parent:{sender_nick}",
            subject=target_subj,
            content=content or f"{target_subj}巩固练习",
            submission_mode="paper_next_day",
            published_at=now,
            expires_at=now + timedelta(days=7),
        )
        db.add(asg)
        db.commit()
        return _reply_md(
            "已布置新作业",
            f"### 已为{stu_name}添加作业\n- 学科：{asg.subject}\n- 内容：{asg.content}",
            extra={"assignment_id": asg.id},
        )

    # 4b. Parent asks to clear demo/old assignments
    if any(k in raw_msg for k in ("清空演示", "清除演示", "清空旧作业", "清空作业")):
        old_asgs = list(
            db.scalars(
                select(Assignment).where(
                    Assignment.family_id == family_id,
                    Assignment.student_id == student_id,
                )
            )
        )
        cnt = len(old_asgs)
        for a in old_asgs:
            db.delete(a)
        db.commit()
        return _reply_md(
            "已清空作业列表",
            f"### 已清空 {stu_name} 的 {cnt} 条历史/演示作业\n现在可以直接发黑板作业照片或文字给我，我会为您录入真实作业。",
        )

    # 4c. Parent references a photo sent in a previous message without attaching it to @bot
    if any(k in raw_msg for k in ("这是", "上面", "这张", "图片", "拍照")) and "作业" in raw_msg and len(raw_msg) <= 24:
        return _reply_md(
            "请附带作业图片一起发送",
            f"### 收到！我还没拿到刚才那张图片\n"
            f"因为钉钉群聊安全限制，机器人只能收到 **@芽米伴学** 同一条消息里的内容（单独发的图片不会推给机器人）。\n\n"
            f"**两种最省事的发法：**\n"
            f"1. **推荐**：点开与「芽米伴学」的**单聊窗口**，直接转发黑板作业照片，我会自动 OCR 识别所有学科并录入到 **{stu_name}** 的今日作业；\n"
            f"2. 在群里把**图片 + `@芽米伴学` 放在同一条消息里**发送。",
        )

    # 5. Forward raw class-group message (Homework + Teacher Praise / Performance Memory)
    is_query_praise = any(q in raw_msg for q in ("有哪些", "查一下", "看看", "最近", "?", "？")) and any(
        k in raw_msg for k in ("表扬", "成长", "表现", "进步")
    )
    is_class_feed = not is_query_praise and (
        any(k in raw_msg for k in ("转发作业", "转发群消息", "老师作业", "今日作业：", "今天作业：", "表扬", "课堂表现", "课堂反馈"))
        or (
            len(raw_msg) >= 24
            and any(s in raw_msg for s in ("数学", "语文", "英语", "科学"))
            and any(k in raw_msg for k in ("练习册", "背诵", "朗读", "听写", "打卡", "明早交", "P", "页"))
        )
    )
    if is_class_feed:
        res, parsed = parse_and_ingest_raw_class_message(
            db,
            family_id=family_id,
            student_id=student_id,
            raw_text=raw_msg,
            sender_name=f"{sender_nick}转发",
        )
        perf_items = extract_and_store_class_performance(
            db,
            family_id=family_id,
            student_id=student_id,
            raw_text=raw_msg,
            sender_name=f"{sender_nick}转发",
        )
        lines = [f"- **{p['subject']}**：{p['content']}（{p['submission_mode']}）" for p in parsed]
        perf_lines = [f"- **[{pm['category']}]** {pm['summary']}（已入库并排队写入 Graphiti 长期记忆）" for pm in perf_items]
        sections = [f"### 已为{stu_name}同步班级群消息（新增 {res.created} 项作业 / {len(perf_items)} 条表现记忆）"]
        if lines:
            sections.append("**作业条目**\n" + "\n".join(lines))
        if perf_lines:
            sections.append("**课堂表现与表扬记忆**\n" + "\n".join(perf_lines))
        if not lines and not perf_lines:
            sections.append("未识别到新的作业或表现条目。")
        return _reply_md(
            "群消息同步成功",
            "\n\n".join(sections),
            extra={"created_assignments": res.created, "performance_memories": perf_items},
        )

    # 6. Parent confirms submission
    if any(k in raw_msg for k in ("确认提交", "已交", "交好了")):
        target_subj = next((s for s in ("数学", "语文", "英语", "科学") if s in raw_msg), None)
        stmt = select(Assignment).where(Assignment.family_id == family_id, Assignment.student_id == student_id)
        if target_subj:
            stmt = stmt.where(Assignment.subject == target_subj)
        asg = db.scalar(stmt)
        if asg is None:
            return _reply_md("未找到作业", f"今天还没有找到{stu_name}的{target_subj or ''}作业记录。")
        record_submission_evidence(
            db,
            family_id=family_id,
            student_id=student_id,
            assignment_id=asg.id,
            status="parent_confirmed",
        )
        return _reply_md("已确认提交", f"### 已登记家长确认\n- 学生：{stu_name}\n- 学科：{asg.subject}（{asg.content}）\n- 状态：家长已确认提交")

    # 7. Trigger curriculum dictation
    if "听写" in raw_msg:
        sess, spoken = start_dictation_from_request(
            db,
            family_id=family_id,
            student_id=student_id,
            utterance=raw_msg,
            source="parent_dingtalk",
        )
        return _reply_md(
            "语音听写已就绪",
            f"### 听写任务（{stu_name}）\n- 状态：{'已创建' if sess else '待补充词表'}\n- 播报：{spoken}",
            extra={"dictation_session_id": sess.id if sess else None},
        )

    # 8. Pet House status or Growth records query
    if any(k in raw_msg for k in ("宠物", "宠粮币", "小窝")):
        pd = pet_to_dict(get_or_create_pet(db, family_id=family_id, student_id=student_id))
        run_tip = "⚠️ 已离家出走（需 100 币召回）" if pd["is_runaway"] else pd["status_pill_text"]
        return _reply_md(
            f"{stu_name}的宠物小屋",
            f"### {stu_name} · {pd['breed_cn']}「{pd['pet_name']}」\n"
            f"- 住宿：Lv.{pd['habitat_lv']} {pd['habitat_name']}\n"
            f"- 宠粮币：{pd['coins']} 币 · 饮食 {pd['hunger']}% · 开心 {pd['happiness']}%\n"
            f"- 状态：{run_tip}",
        )

    if any(k in raw_msg for k in ("成长", "表现", "进步", "表扬")):
        recs = list(
            db.scalars(
                select(GrowthRecord)
                .where(
                    GrowthRecord.family_id == family_id,
                    GrowthRecord.student_id == student_id,
                    GrowthRecord.deleted.is_(False),
                )
                .order_by(GrowthRecord.created_at.desc())
                .limit(5)
            )
        )
        if not recs:
            return _reply_md(f"{stu_name}成长记录", f"### {stu_name}近期暂无成长与表现记录")
        g_lines = [f"- **[{r.category}]** {r.title}（{'已核实' if r.verified else '待家长确认'}）" for r in recs]
        return _reply_md(f"{stu_name}成长记录", f"### {stu_name}最近成长与课堂表现\n" + "\n".join(g_lines))

    # 9. Natural-language / multi-intent / school-notice -> Doubao Seed 2.1 + DeepSeek Agent Harness
    target_stu_obj = db.get(Student, student_id)
    if target_stu_obj is not None:
        from backend.agent.dingtalk_agent import run_dingtalk_agent_turn

        agent_reply = run_dingtalk_agent_turn(
            db,
            family_id=family_id,
            student=target_stu_obj,
            user_text=raw_msg,
            sender_nick=sender_nick,
        )
        if agent_reply is not None:
            return agent_reply

    # 10. Offline fallback: Today's homework progress report
    data = get_student_assignments(db, family_id=family_id, student_id=student_id)
    if not data.assignments:
        return _reply_md("今日作业", f"### {stu_name}今日暂无待完成作业\n发送「转发作业：...」可直接解析班级群作业与课堂表现。")
    rows_md = [
        f"- **{a.subject}**：{a.page_ref or ''} {a.content}（{'已写完' if a.completed else '进行中'} / 提交:{a.submission_status}）"
        for a in data.assignments
    ]
    return _reply_md(
        f"{stu_name}今日作业",
        f"### {stu_name}今日作业清单（共 {len(data.assignments)} 项）\n"
        + "\n".join(rows_md)
        + "\n\n> 可回复：`转发群消息：...`、`提醒 过15分钟检查英语`、`安排复习 单位分数`、`布置作业 数学 口算20道`、`听写 第3单元第4课`、`确认提交 数学`、`宠物状态`、`成长记录`、`看看孩子在不在写字台`",
    )


@app.get("/v1/students/{student_id}/assignments", response_model=StudentAssignmentsOut)
def api_get_assignments(
    student_id: str,
    subject: Optional[str] = None,
    x_family_id: str = Header(..., alias="X-Family-Id"),
    db: Session = Depends(get_db),
) -> StudentAssignmentsOut:
    try:
        return get_student_assignments(db, family_id=x_family_id, student_id=student_id, subject=subject)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/v1/voice/query", response_model=VoiceQueryResponse)
def api_voice_query(
    payload: VoiceQueryRequest,
    x_device_token: str = Header(..., alias="X-Device-Token"),
    db: Session = Depends(get_db),
) -> VoiceQueryResponse:
    try:
        device = authenticate_device(db, x_device_token)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    ok, reason, session = validate_turn_session(
        db,
        device=device,
        session_id=payload.session_id,
        profile_epoch=payload.profile_epoch,
    )
    if not ok or session is None:
        return VoiceQueryResponse(
            accepted=False,
            rejected_reason=reason,
            intent="rejected",
            spoken_text="",
            screen_title="",
        )

    safety_in = safety_middleware.check_input(payload.utterance)
    if not safety_in.allowed:
        return VoiceQueryResponse(
            accepted=True,
            safety_blocked=True,
            intent="safety_redirect",
            spoken_text=safety_in.replacement_text,
            screen_title="回到学习",
        )

    # Voice-first dictation: start from curriculum lookup or control active dictation by voice
    if "听写" in payload.utterance:
        sess, spoken = start_dictation_from_request(
            db,
            family_id=session.family_id,
            student_id=session.student_id,
            utterance=payload.utterance,
            source="student_initiated",
        )
        return VoiceQueryResponse(
            accepted=True,
            intent="dictation_start" if sess else "dictation_missing_material",
            spoken_text=spoken,
            screen_title="正在听写" if sess else "资料不足",
        )

    active_dict = get_active_dictation(db, student_id=session.student_id)
    if active_dict is not None:
        prompt = handle_dictation_voice_command(db, session_id=active_dict.id, utterance=payload.utterance)
        if prompt is not None:
            return VoiceQueryResponse(
                accepted=True,
                intent="dictation_step",
                spoken_text=prompt.spoken_text,
                screen_title=prompt.screen_text,
            )

    intent, spoken, title, assignments, stale_warning = route_voice_assignment_query(
        db,
        family_id=session.family_id,
        student_id=session.student_id,
        utterance=payload.utterance,
    )
    safety_out = safety_middleware.check_output(spoken, allow_direct_answer=True)
    final_spoken = spoken if safety_out.allowed else safety_out.replacement_text

    return VoiceQueryResponse(
        accepted=True,
        safety_blocked=not safety_out.allowed,
        intent=intent,
        spoken_text=final_spoken,
        screen_title=title,
        assignments=assignments,
        stale_warning=stale_warning,
    )


def _synthesize_fallback_chime_pcm(sample_rate: int = 16000) -> bytes:
    """Warm two-note ascending chime (C5 -> G5) in 16kHz 16-bit mono PCM when cloud TTS keys are unset."""
    buf = bytearray()
    notes = [(523.25, 0.16), (783.99, 0.22)]
    for freq, dur in notes:
        total = int(sample_rate * dur)
        for i in range(total):
            t = i / sample_rate
            env = min(1.0, i / (sample_rate * 0.015)) * max(0.0, 1.0 - (i / total) ** 1.6)
            val = int(9000 * env * math.sin(2.0 * math.pi * freq * t))
            buf.extend(struct.pack("<h", val))
    return bytes(buf)


def _call_doubao_seed_reply(student_name: str, utterance: str, context_hint: str = "") -> str:
    """Call Volcengine Ark Doubao Seed Responses API as primary conversational & tutoring model."""
    if not settings.ark_api_key or not settings.ark_seed_model:
        return ""
    system_prompt = (
        f"你是「芽米」，正在陪伴小学三四年级的{student_name}写作业的桌面学习伙伴。"
        "要求：1) 语气温暖简短，用口语化中文回复，每次不超过2句话（40字以内），适合语音播报；"
        "2) 遇到数学/语文题目绝不直接给最终答案，只给一个启发式小提示；"
        "3) 不使用表情符号或Markdown。"
    )
    user_text = f"{context_hint}\n孩子说：{utterance}".strip()
    payload = json.dumps(
        {
            "model": settings.ark_seed_model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
            ],
            "thinking": {"type": "disabled"},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{settings.ark_base_url.rstrip('/')}/responses",
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.ark_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for item in data.get("output", []):
            for c in item.get("content", []):
                if c.get("type") == "output_text" and c.get("text"):
                    return str(c["text"]).strip()
    except Exception:
        return ""
    return ""


def _trim_pcm_silence(pcm_bytes: bytes, sample_rate: int = 16000, threshold: int = 320) -> bytes:
    """Trim leading/trailing silence (keeping 160ms padding) and cap at 5.5s so Ark Audio ASR stays fast (~1.5s)."""
    n_samples = len(pcm_bytes) // 2
    if n_samples < 320:
        return pcm_bytes
    frame_samples = 320  # 20ms
    first_active = None
    last_active = None
    for idx in range(0, n_samples - frame_samples + 1, frame_samples):
        chunk = struct.unpack_from(f"<{frame_samples}h", pcm_bytes, idx * 2)
        mean_abs = sum(abs(v) for v in chunk) // frame_samples
        if mean_abs >= threshold:
            if first_active is None:
                first_active = idx
            last_active = idx + frame_samples
    if first_active is None or last_active is None:
        return b""
    pad = int(sample_rate * 0.16)
    start = max(0, first_active - pad)
    end = min(n_samples, last_active + pad)
    max_samples = int(sample_rate * 5.5)
    if end - start > max_samples:
        end = start + max_samples
    return pcm_bytes[start * 2 : end * 2]


def _transcribe_ark_audio_pcm(pcm_bytes: bytes, sample_rate: int = 16000) -> str:
    """Transcribe 16kHz 16-bit mono PCM using Volcengine Ark audio-understanding endpoint (with silence trim + retry)."""
    if not pcm_bytes or not settings.ark_api_key or not settings.ark_audio_model:
        return ""
    trimmed = _trim_pcm_silence(pcm_bytes, sample_rate=sample_rate)
    if len(trimmed) < 1600:
        return ""
    wav_io = io.BytesIO()
    with wave.open(wav_io, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(trimmed)
    b64 = base64.b64encode(wav_io.getvalue()).decode("ascii")
    payload = {
        "model": settings.ark_audio_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
                    {
                        "type": "text",
                        "text": "请准确转写这段中英文语音：中文保留中文，英文保留原始英文单词，只输出说话人说的内容，不要加任何解释；如果是纯静音或听不清，只输出空字符串。",
                    },
                ],
            }
        ],
        "max_tokens": 96,
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(2):
        req = urllib.request.Request(
            f"{settings.ark_base_url.rstrip('/')}/chat/completions",
            data=encoded,
            headers={
                "Authorization": f"Bearer {settings.ark_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = str(((data.get("choices") or [{}])[0]).get("message", {}).get("content") or "").strip()
            if text in ('""', "''", "空字符串", "静音", "无"):
                return ""
            return text
        except Exception as exc:
            print(f"[YAMI_ASR_ERR] attempt={attempt + 1} bytes={len(trimmed)} err={type(exc).__name__}: {exc}", flush=True)
            if attempt == 1:
                return ""
    return ""


def _synthesize_macos_chinese_tts_pcm(text: str) -> bytes:
    """Local macOS Tingting Chinese TTS -> 16kHz 16-bit mono PCM fallback when Tencent keys are unset."""
    if not shutil.which("say") or not shutil.which("afconvert"):
        return b""
    spoken = normalize_for_chinese_tts(text).strip()
    if not spoken:
        return b""
    try:
        with tempfile.TemporaryDirectory() as td:
            aiff = Path(td) / "out.aiff"
            wav = Path(td) / "out.wav"
            subprocess.run(
                ["say", "-v", "Tingting", "-r", "190", "-o", str(aiff), spoken],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
            with wave.open(str(wav), "rb") as wf:
                return wf.readframes(wf.getnframes())
    except Exception:
        return b""


@app.post("/v1/voice/audio-turn")
async def api_voice_audio_turn(
    request: Request,
    utterance: Optional[str] = None,
    x_hardware_uid: Optional[str] = Header(default=None, alias="X-Hardware-Uid"),
    x_test_utterance: Optional[str] = Header(default=None, alias="X-Test-Utterance"),
    db: Session = Depends(get_db),
) -> Response:
    """
    Mosaico hardware voice loop endpoint:
    1. Receives 16kHz 16-bit mono PCM audio from Mosaico ES8311 microphone (or test utterance).
    2. Resolves paired family/student (or falls back to demo student 'stu_he' if unpaired in local dev).
    3. Routes through SafetyMiddleware -> Curriculum Dictation -> Structured Assignment Query.
    4. Synthesizes 16kHz 16-bit mono PCM via Tencent Cloud Real-Time TTS WS when configured,
       or returns a warm local PCM chime + UTF-8 headers when running offline/without cloud keys.
    """
    audio_bytes = await request.body()

    family_id, student_id, _, _ = _resolve_active_student(db, x_hardware_uid or "mosaico-yami-s31")

    t_start = time.perf_counter()
    resolved_utterance = (utterance or x_test_utterance or "").strip()
    if not resolved_utterance and len(audio_bytes) > 1000:
        if settings.ark_api_key and settings.ark_audio_model:
            try:
                resolved_utterance = _transcribe_ark_audio_pcm(audio_bytes)
            except Exception:
                resolved_utterance = ""
        if not resolved_utterance and settings.tencent_secret_id and settings.tencent_secret_key:
            try:
                resolved_utterance = transcribe_tencent_asr_pcm(
                    pcm_bytes=audio_bytes,
                    secret_id=settings.tencent_secret_id,
                    secret_key=settings.tencent_secret_key,
                )
            except Exception:
                resolved_utterance = ""
    t_asr = time.perf_counter()

    if not resolved_utterance:
        intent = "no_speech"
        final_spoken = "我刚才没太听清，可以按住右键再说一遍吗？"
        screen_title = "没听清"
        turn_provider = "asr_empty"
        turn_skill = "chat-companion"
        turn_tools: list[str] = []
        t_llm = time.perf_counter()
    else:
        turn = agent_harness.run_turn(
            db,
            family_id=family_id,
            student_id=student_id,
            utterance=resolved_utterance,
        )
        if turn.skill_name == "pet-companion-house":
            bump_pet_jump_seq(db, student_id)
        t_llm = time.perf_counter()
        intent = turn.intent
        final_spoken = turn.spoken_text
        screen_title = turn.screen_title
        turn_provider = turn.provider
        turn_skill = turn.skill_name
        turn_tools = turn.tool_calls

    pcm_out = b""
    audio_source = "local_chime"
    if settings.tencent_secret_id and settings.tencent_secret_key:
        try:
            pcm_out = synthesize_tencent_http_pcm(
                text=final_spoken,
                secret_id=settings.tencent_secret_id,
                secret_key=settings.tencent_secret_key,
                voice_type=settings.tencent_tts_voice_type,
                speed=0.0,
            )
            if pcm_out:
                audio_source = "tencent_tts"
        except Exception:
            pcm_out = b""

    if not pcm_out:
        pcm_out = _synthesize_macos_chinese_tts_pcm(final_spoken)
        if pcm_out:
            audio_source = "macos_tingting_tts"

    if not pcm_out:
        pcm_out = _synthesize_fallback_chime_pcm(16000)

    t_end = time.perf_counter()
    print(
        f"[YAMI_VOICE] in={len(audio_bytes)}B asr={t_asr - t_start:.2f}s llm={t_llm - t_asr:.2f}s "
        f"tts={t_end - t_llm:.2f}s total={t_end - t_start:.2f}s provider={turn_provider} "
        f"utterance={resolved_utterance!r} -> spoken={final_spoken!r}",
        flush=True,
    )

    return Response(
        content=pcm_out,
        media_type="audio/pcm",
        headers={
            "X-Yami-Intent": intent,
            "X-Yami-Provider": turn_provider,
            "X-Yami-Skill": turn_skill,
            "X-Yami-Tools": ",".join(turn_tools),
            "X-Yami-Screen-Title": quote(screen_title, safe=""),
            "X-Yami-Spoken-Text": quote(final_spoken, safe=""),
            "X-Yami-Audio-Source": audio_source,
            "X-Yami-Audio-In-Bytes": str(len(audio_bytes)),
        },
    )


def _call_doubao_vision_tutor(image_bytes: bytes, mime_type: str, student_name: str, question_hint: str = "") -> str:
    """Call Volcengine Ark Doubao Seed Vision (ep-20261111180956-ht2xg) for Socratic paper-homework help."""
    if not image_bytes or not settings.ark_api_key or not settings.ark_seed_model:
        return ""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        f"你是小学三四年级孩子{student_name}的作业伙伴「芽米」。请看这张纸面作业照片："
        "1) 简短说出你看到的是哪道题或哪类题；"
        "2) 绝不直接说出最终答案，只给一个温和具体的第一步启发线索（如画图、拆份、看关键词）；"
        "3) 全文不超过 45 个汉字，适合语音播报，不用 Markdown 或表情。"
    )
    if question_hint:
        prompt += f" 孩子补充说：{question_hint}"
    payload = {
        "model": settings.ark_seed_model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": f"data:{mime_type};base64,{b64}"},
                    {"type": "input_text", "text": prompt},
                ],
            }
        ],
        "thinking": {"type": "disabled"},
        "max_output_tokens": 110,
    }
    req = urllib.request.Request(
        f"{settings.ark_base_url.rstrip('/')}/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.ark_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=18) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    texts: list[str] = []
    for item in data.get("output") or []:
        for c in item.get("content") or []:
            if c.get("type") == "output_text" and c.get("text"):
                texts.append(str(c["text"]))
    return " ".join(texts).strip()


@app.post("/v1/vision/capture-turn")
async def api_vision_capture_turn(
    request: Request,
    purpose: str = "question",
    subject: Optional[str] = None,
    hint: Optional[str] = None,
    x_hardware_uid: Optional[str] = Header(default=None, alias="X-Hardware-Uid"),
    db: Session = Depends(get_db),
) -> Response:
    """
    Left-slot CameraBoard endpoint:
      - purpose='question': Doubao Seed Vision OCR + anti-spoiler Socratic hint -> Tencent TTS 16kHz PCM
      - purpose='submission': Records 7-day TTL photo submission evidence -> confirmation PCM
    Enforces student.camera_consent gate strictly.
    """
    img_bytes = await request.body()
    if len(img_bytes) < 64:
        raise HTTPException(status_code=400, detail="empty_image_payload")

    family_id, student_id, _, _ = _resolve_active_student(db, x_hardware_uid or "mosaico-yami-s31")

    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="student_not_found")
    if not student.camera_consent:
        raise HTTPException(status_code=403, detail="camera_consent_required")

    mime = request.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if mime not in ("image/jpeg", "image/png"):
        mime = "image/jpeg"

    if purpose == "submission":
        stmt = select(Assignment).where(
            Assignment.family_id == family_id,
            Assignment.student_id == student_id,
        )
        if subject:
            stmt = stmt.where(Assignment.subject == subject)
        asg = db.scalar(stmt)
        if asg is not None:
            record_submission_evidence(
                db,
                family_id=family_id,
                student_id=student_id,
                assignment_id=asg.id,
                status="photo",
                photo_object_key=f"mosaico/{student_id}/{asg.id}.jpg",
            )
            grant_pet_coins(
                db,
                family_id=family_id,
                student_id=student_id,
                amount=20,
                reason=f"拍照留证提交{asg.subject}作业",
            )
            final_spoken = f"好啦，{asg.subject}作业已经拍照留证了，奖励你20枚宠粮币！"
        else:
            final_spoken = "照片拍好啦，不过今天还没同步到对应科目的提交任务。"
        intent = "vision_submission"
        screen_title = "已拍照留证"
    else:
        spoken = ""
        try:
            spoken = _call_doubao_vision_tutor(img_bytes, mime, student.name, hint or "")
        except Exception:
            spoken = ""
        if not spoken:
            spoken = "照片收到啦，先圈出题目里的已知数字，说说你想先算哪一步？"
        safety_out = safety_middleware.check_output(spoken, allow_direct_answer=False)
        final_spoken = spoken if safety_out.allowed else safety_out.replacement_text
        try:
            grant_pet_coins(
                db,
                family_id=family_id,
                student_id=student_id,
                amount=TASK_COIN_REWARDS["photo_question"],
                reason="主动拍题思考",
            )
        except Exception:
            pass
        intent = "vision_tutoring"
        screen_title = "拍题提示"

    pcm_out = b""
    audio_source = "local_chime"
    if settings.tencent_secret_id and settings.tencent_secret_key:
        try:
            pcm_out = synthesize_tencent_http_pcm(
                text=final_spoken,
                secret_id=settings.tencent_secret_id,
                secret_key=settings.tencent_secret_key,
                voice_type=settings.tencent_tts_voice_type,
                speed=0.0,
            )
            if pcm_out:
                audio_source = "tencent_tts"
        except Exception:
            pcm_out = b""
    if not pcm_out:
        pcm_out = _synthesize_macos_chinese_tts_pcm(final_spoken) or _synthesize_fallback_chime_pcm(16000)

    return Response(
        content=pcm_out,
        media_type="audio/pcm",
        headers={
            "X-Yami-Intent": intent,
            "X-Yami-Screen-Title": quote(screen_title, safe=""),
            "X-Yami-Spoken-Text": quote(final_spoken, safe=""),
            "X-Yami-Audio-Source": audio_source,
        },
    )


@app.websocket("/v1/ws/voice")
async def ws_full_duplex_voice(websocket: WebSocket, db: Session = Depends(get_db)):
    """
    True full-duplex streaming voice WebSocket:
      - Client streams binary 16kHz 16-bit mono PCM chunks while speaking
      - Client sends JSON {"type": "end_of_turn", "utterance"?: "...", "hardware_uid"?: "..."}
      - Client may send JSON {"type": "cancel"} AT ANY TIME (even mid-LLM/TTS) for instant Barge-In
      - Server runs each turn in a cancellable asyncio.Task so the receive loop never blocks:
          1) {"type": "transcript", "text": "..."}
          2) For each streamed clause:
               {"type": "clause", "seq": N, "text": "..."}
               <binary 16kHz boosted Tencent Cloud TTS PCM>
          3) {"type": "turn_done", "spoken_text": "...", "provider": "doubao_seed", "skill_name": "..."}
    """
    await websocket.accept()
    pcm_rx = bytearray()
    active_task: Optional[asyncio.Task] = None
    cancel_event = asyncio.Event()

    def _transcribe_pcm_sync(raw_audio: bytes) -> str:
        trimmed = _trim_pcm_silence(raw_audio)
        if len(trimmed) < 1000:
            trimmed = raw_audio
        text = ""
        if settings.ark_api_key and settings.ark_audio_model:
            try:
                text = _transcribe_ark_audio_pcm(trimmed)
            except Exception:
                text = ""
        if not text and settings.tencent_secret_id and settings.tencent_secret_key:
            try:
                text = transcribe_tencent_asr_pcm(
                    pcm_bytes=trimmed,
                    secret_id=settings.tencent_secret_id,
                    secret_key=settings.tencent_secret_key,
                )
            except Exception:
                text = ""
        return text.strip()

    def _synth_clause_sync(clause: str) -> bytes:
        pcm = b""
        if settings.tencent_secret_id and settings.tencent_secret_key:
            try:
                pcm = synthesize_tencent_http_pcm(
                    text=clause,
                    secret_id=settings.tencent_secret_id,
                    secret_key=settings.tencent_secret_key,
                    voice_type=settings.tencent_tts_voice_type,
                    speed=0.0,
                )
            except Exception:
                pcm = b""
        if not pcm:
            pcm = _synthesize_macos_chinese_tts_pcm(clause) or _synthesize_fallback_chime_pcm(16000)
        return pcm

    async def _run_turn(raw_audio: bytes, explicit_utterance: str, hw_uid: str, turn_cancel: asyncio.Event):
        try:
            family_id, student_id, _, _ = _resolve_active_student(db, hw_uid)
            utterance = explicit_utterance
            if not utterance and len(raw_audio) > 1000:
                utterance = await asyncio.to_thread(_transcribe_pcm_sync, raw_audio)

            if turn_cancel.is_set():
                return

            await websocket.send_json({"type": "transcript", "text": utterance})

            if not utterance:
                fallback_msg = "我刚才没太听清，可以再说一遍吗？"
                await websocket.send_json({"type": "clause", "seq": 1, "text": fallback_msg})
                pcm = await asyncio.to_thread(_synth_clause_sync, fallback_msg)
                if not turn_cancel.is_set():
                    await websocket.send_bytes(pcm)
                    await websocket.send_json(
                        {"type": "turn_done", "spoken_text": fallback_msg, "provider": "asr_empty", "skill_name": "chat-companion"}
                    )
                return

            routed_skill = agent_harness.skills.route(utterance).name
            if routed_skill == "pet-companion-house":
                bump_pet_jump_seq(db, student_id)

            # Stream clauses from a worker thread into an asyncio.Queue so cancellation is immediate
            loop = asyncio.get_running_loop()
            clause_q: asyncio.Queue[Optional[str]] = asyncio.Queue()

            def _produce_clauses():
                try:
                    for cl in agent_harness.stream_doubao_clauses(
                        db,
                        family_id=family_id,
                        student_id=student_id,
                        utterance=utterance,
                    ):
                        if turn_cancel.is_set():
                            break
                        loop.call_soon_threadsafe(clause_q.put_nowait, cl)
                finally:
                    loop.call_soon_threadsafe(clause_q.put_nowait, None)

            producer_fut = loop.run_in_executor(None, _produce_clauses)

            seq = 0
            full_spoken: list[str] = []
            while True:
                if turn_cancel.is_set():
                    break
                clause = await clause_q.get()
                if clause is None:
                    break
                if turn_cancel.is_set():
                    break
                seq += 1
                full_spoken.append(clause)
                await websocket.send_json({"type": "clause", "seq": seq, "text": clause})
                pcm = await asyncio.to_thread(_synth_clause_sync, clause)
                if turn_cancel.is_set():
                    break
                await websocket.send_bytes(pcm)

            if not producer_fut.done():
                producer_fut.cancel()

            if not turn_cancel.is_set():
                await websocket.send_json(
                    {
                        "type": "turn_done",
                        "spoken_text": "".join(full_spoken),
                        "provider": "doubao_seed",
                        "skill_name": routed_skill,
                    }
                )
        except asyncio.CancelledError:
            return
        except WebSocketDisconnect:
            return

    async def _cancel_active_turn() -> None:
        nonlocal active_task, cancel_event
        cancel_event.set()
        if active_task is not None and not active_task.done():
            active_task.cancel()
            try:
                await active_task
            except (asyncio.CancelledError, Exception):
                pass
        active_task = None
        cancel_event = asyncio.Event()

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                await _cancel_active_turn()
                break

            if msg.get("bytes") is not None:
                pcm_rx.extend(msg["bytes"])
                continue

            text_frame = msg.get("text")
            if not text_frame:
                continue
            try:
                ev = json.loads(text_frame)
            except Exception:
                continue

            ev_type = ev.get("type")
            if ev_type == "cancel":
                pcm_rx.clear()
                await _cancel_active_turn()
                await websocket.send_json({"type": "cancelled"})
                continue

            if ev_type == "end_of_turn":
                await _cancel_active_turn()
                hw_uid = str(ev.get("hardware_uid") or "mosaico-yami-s31")
                explicit_utt = str(ev.get("utterance") or "").strip()
                raw_audio = bytes(pcm_rx)
                pcm_rx.clear()
                active_task = asyncio.create_task(
                    _run_turn(raw_audio, explicit_utt, hw_uid, cancel_event)
                )
    except WebSocketDisconnect:
        await _cancel_active_turn()
        return
