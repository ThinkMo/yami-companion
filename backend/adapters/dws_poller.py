"""Automatic DingTalk personal-account sync powered by official `dws` CLI + Doubao Seed Agent Harness.

Pulls new messages from configured DingTalk conversations (e.g. "工作通知:XX小学"
and school class groups) via `dws chat +chat-messages --conversation-id <cid>`, deduplicates
by `messageId`, and routes each new message through `run_dingtalk_agent_turn` (Doubao Seed 2.1
+ DeepSeek) to create Assignments, Parent Todos (e.g. 需签字通知), and Teacher Praise records.

Usage:
    # One-shot sync:
    .venv/bin/python -m backend.adapters.dws_poller --once

    # Continuous background polling every 120s:
    .venv/bin/python -m backend.adapters.dws_poller --interval 120
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.agent.dingtalk_agent import run_dingtalk_agent_turn
from backend.db import SessionLocal
from backend.models import Family, Student

logging.basicConfig(level=logging.INFO, format="%(asctime)s [YAMI_DWS_SYNC] %(levelname)s %(message)s")
logger = logging.getLogger("yami.dws_poller")

STATE_FILE = Path(__file__).resolve().parents[2] / ".dws_synced_ids.json"


def _find_dws_bin() -> str:
    found = shutil.which("dws")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "dws"
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError("dws binary not found in PATH or ~/.local/bin/dws")


def _load_seen_ids() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()


def _save_seen_ids(seen: set[str]) -> None:
    # Keep most recent 2000 message IDs
    trimmed = list(seen)[-2000:]
    STATE_FILE.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")


def fetch_dws_messages(conversation_id: str, limit: int = 10) -> list[dict[str, Any]]:
    dws_bin = _find_dws_bin()
    cmd = [dws_bin, "chat", "+chat-messages", "--conversation-id", conversation_id]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"dws failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[:240]}")
    out = proc.stdout.strip()
    j_start = out.find("{")
    if j_start < 0:
        return []
    payload = json.loads(out[j_start:])
    msgs = payload.get("messages") or []
    return msgs[:limit]


def sync_family_via_dws(
    db: Session,
    *,
    family_id: str = "fam_a",
    conversation_ids: list[str] | None = None,
    limit_per_conv: int = 8,
) -> dict[str, Any]:
    fam = db.get(Family, family_id)
    if fam is None:
        raise ValueError(f"family_not_found: {family_id}")

    if conversation_ids is None:
        raw_cids = (fam.dingtalk_class_group_id or "").strip()
        conversation_ids = [c.strip() for c in raw_cids.replace("，", ",").split(",") if c.strip()]
    if not conversation_ids:
        return {"status": "skipped", "reason": "no_conversation_ids_configured", "synced": 0}

    student = db.scalar(
        select(Student).where(Student.family_id == family_id).order_by(Student.id.asc())
    )
    if student is None:
        return {"status": "skipped", "reason": "no_student_in_family", "synced": 0}

    seen = _load_seen_ids()
    synced_items: list[dict[str, Any]] = []
    errors: list[str] = []

    for cid in conversation_ids:
        try:
            msgs = fetch_dws_messages(cid, limit=limit_per_conv)
        except Exception as exc:
            logger.warning("Failed fetching dws conversation %s: %s", cid, exc)
            errors.append(f"{cid}: {exc}")
            continue

        # Process oldest-first so chronological order is preserved
        for m in reversed(msgs):
            mid = str(m.get("messageId") or "").strip()
            text = str(m.get("text") or "").strip()
            sender = str(m.get("sender") or "学校通知").strip()
            ctime = str(m.get("createTime") or "").strip()
            if not mid or mid in seen or not text:
                continue

            # Strip long dingtalk:// URLs so LLM focuses on clean human content
            clean_text = text.split("url: dingtalk://")[0].strip()
            if not clean_text:
                seen.add(mid)
                continue

            prompt_text = f"【自动同步自钉钉 · {sender} · {ctime}】\n{clean_text}"
            res = run_dingtalk_agent_turn(
                db,
                family_id=family_id,
                student=student,
                user_text=prompt_text,
                sender_nick=sender,
            )
            seen.add(mid)
            synced_items.append(
                {
                    "message_id": mid,
                    "sender": sender,
                    "create_time": ctime,
                    "provider": (res or {}).get("agent_provider", "none"),
                    "executed_actions": (res or {}).get("executed_actions", []),
                }
            )
            logger.info("Synced msg=%s sender=%s actions=%s", mid, sender, (res or {}).get("executed_actions"))

    _save_seen_ids(seen)
    return {
        "status": "ok",
        "family_id": family_id,
        "student_name": student.name,
        "conversations": conversation_ids,
        "synced_count": len(synced_items),
        "synced_items": synced_items,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Yami automatic DingTalk dws poller")
    parser.add_argument("--family", default=os.environ.get("YAMI_FAMILY_ID", "fam_a"))
    parser.add_argument("--cid", action="append", default=[], help="Explicit conversation ID(s)")
    parser.add_argument("--once", action="store_true", help="Run a single sync pass and exit")
    parser.add_argument("--interval", type=int, default=120, help="Polling interval in seconds")
    args = parser.parse_args()

    cids = args.cid if args.cid else None
    while True:
        db = SessionLocal()
        try:
            summary = sync_family_via_dws(db, family_id=args.family, conversation_ids=cids)
            logger.info("Pass done: synced=%d errors=%d", summary.get("synced_count", 0), len(summary.get("errors", [])))
            if args.once:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
                return
        finally:
            db.close()
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
