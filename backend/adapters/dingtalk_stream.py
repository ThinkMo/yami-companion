"""DingTalk Stream Mode bridge (outbound WebSocket — no public domain or ngrok required).

Usage:
    .venv/bin/python -m backend.adapters.dingtalk_stream

Supports:
  1. Pure text messages (group @bot or DM) -> /v1/dingtalk/bot/callback
  2. Picture messages (DM blackboard photo) -> download via DingTalk OpenAPI -> /v1/sync/dingtalk/parse-image (Vision OCR)
  3. RichText messages (group @bot + photo, or DM photo + caption) -> download image(s) -> /v1/sync/dingtalk/parse-image
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage, Credential, DingTalkStreamClient

from backend.db import SessionLocal
from backend.models import Family

logging.basicConfig(level=logging.INFO, format="%(asctime)s [YAMI_DINGTALK] %(levelname)s %(message)s")
logger = logging.getLogger("yami.dingtalk_stream")


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _resolve_credentials() -> tuple[str, str, str]:
    _load_dotenv()
    family_id = os.environ.get("YAMI_FAMILY_ID", "fam_a")
    client_id = os.environ.get("DINGTALK_CLIENT_ID", "").strip()
    client_secret = os.environ.get("DINGTALK_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return family_id, client_id, client_secret

    db = SessionLocal()
    try:
        fam = db.get(Family, family_id)
        if fam and fam.dingtalk_app_id and fam.dingtalk_app_secret:
            return family_id, fam.dingtalk_app_id.strip(), fam.dingtalk_app_secret.strip()
    finally:
        db.close()

    raise RuntimeError(
        "Missing DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET. "
        "Set them in .env or in Guardian Web -> Settings -> DingTalk Integration."
    )


class YamiDingTalkBotHandler(ChatbotHandler):
    def __init__(
        self,
        family_id: str,
        client_id: str,
        client_secret: str,
        backend_url: str = "http://127.0.0.1:8000",
    ):
        super().__init__()
        self.family_id = family_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.backend_url = backend_url.rstrip("/")
        self._token: str = ""
        self._token_exp: float = 0.0

    def _get_access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        req = urllib.request.Request(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            data=json.dumps({"appKey": self.client_id, "appSecret": self.client_secret}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self._token = data["accessToken"]
        self._token_exp = now + int(data.get("expireIn", 7200))
        return self._token

    def _download_dingtalk_image(self, download_code: str, robot_code: str) -> tuple[bytes, str]:
        token = self._get_access_token()
        req = urllib.request.Request(
            "https://api.dingtalk.com/v1.0/robot/messageFiles/download",
            data=json.dumps({"downloadCode": download_code, "robotCode": robot_code or self.client_id}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-acs-dingtalk-access-token": token,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
        dl_url = meta.get("downloadUrl")
        if not dl_url:
            raise RuntimeError(f"DingTalk downloadUrl missing: {meta}")
        with urllib.request.urlopen(dl_url, timeout=20) as img_resp:
            mime = (img_resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            return img_resp.read(), mime

    def _extract_images_and_text(self, raw_data: dict, incoming: ChatbotMessage) -> tuple[list[str], str]:
        msgtype = raw_data.get("msgtype") or getattr(incoming, "message_type", "text")
        download_codes: list[str] = []
        texts: list[str] = []

        if msgtype == "picture":
            pic = raw_data.get("content") or {}
            if isinstance(pic, dict) and pic.get("downloadCode"):
                download_codes.append(pic["downloadCode"])
        elif msgtype == "richText":
            rt = (raw_data.get("content") or {}).get("richText") or []
            for seg in rt:
                if not isinstance(seg, dict):
                    continue
                if seg.get("downloadCode"):
                    download_codes.append(seg["downloadCode"])
                elif seg.get("text"):
                    texts.append(str(seg["text"]).strip())
        else:
            if incoming.text and incoming.text.content:
                texts.append(incoming.text.content.strip())

        return download_codes, " ".join(t for t in texts if t).strip()

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        raw_data = callback.data or {}
        incoming = ChatbotMessage.from_dict(raw_data)
        sender = incoming.sender_nick or "家长"
        conv_id = incoming.conversation_id or ""
        robot_code = raw_data.get("robotCode") or self.client_id
        msgtype = raw_data.get("msgtype") or "text"
        download_codes, text = self._extract_images_and_text(raw_data, incoming)

        logger.info(
            "RX DingTalk | type=%s sender=%s conv_id=%s imgs=%d text=%s",
            msgtype, sender, conv_id, len(download_codes), text,
        )

        # Auto-record real OpenConversationId into Family if not set yet
        if conv_id:
            db = SessionLocal()
            try:
                fam = db.get(Family, self.family_id)
                if fam and not fam.dingtalk_class_group_id:
                    fam.dingtalk_class_group_id = conv_id
                    db.commit()
            except Exception:
                pass
            finally:
                db.close()

        # Case A: Contains blackboard/homework photo(s) -> Vision OCR + ingest!
        if download_codes:
            try:
                img_bytes, mime = self._download_dingtalk_image(download_codes[0], robot_code)
                qs = urllib.parse.urlencode({
                    "family_id": self.family_id,
                    "sender_name": f"{sender}图片转发",
                    "caption": text,
                })
                req = urllib.request.Request(
                    f"{self.backend_url}/v1/sync/dingtalk/parse-image?{qs}",
                    data=img_bytes,
                    headers={"Content-Type": mime, "X-Family-Id": self.family_id},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=35) as resp:
                    ocr_res = json.loads(resp.read().decode("utf-8"))
                if ocr_res.get("msgtype") == "markdown" and isinstance(ocr_res.get("markdown"), dict):
                    md = ocr_res["markdown"]
                    self.reply_markdown(md.get("title", "作业图片识别成功"), md.get("text", ""), incoming)
                else:
                    self.reply_text("收到图片啦，已交给芽米处理。", incoming)
            except Exception as exc:
                logger.exception("Image homework OCR failed: %s", exc)
                self.reply_text(f"识别作业图片时出错：{exc}", incoming)
            return AckMessage.STATUS_OK, "OK"

        # Case B: Text message -> forward to /v1/dingtalk/bot/callback
        payload = {
            "family_id": self.family_id,
            "senderNick": sender,
            "conversationId": conv_id,
            "text": {"content": text},
        }
        try:
            req = urllib.request.Request(
                f"{self.backend_url}/v1/dingtalk/bot/callback",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Family-Id": self.family_id},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.exception("Failed forwarding DingTalk message to Yami backend: %s", exc)
            self.reply_text(f"芽米后端暂时连接失败：{exc}", incoming)
            return AckMessage.STATUS_OK, "backend_error"

        if data.get("msgtype") == "markdown" and isinstance(data.get("markdown"), dict):
            md = data["markdown"]
            self.reply_markdown(md.get("title", "芽米回复"), md.get("text", ""), incoming)
        else:
            reply_txt = (data.get("text") or {}).get("content") or str(data)
            self.reply_text(reply_txt, incoming)
        return AckMessage.STATUS_OK, "OK"


def main() -> None:
    family_id, client_id, client_secret = _resolve_credentials()
    backend_url = os.environ.get("YAMI_BACKEND_URL", "http://127.0.0.1:8000")
    logger.info("Starting DingTalk Stream client | family=%s client_id=%s backend=%s", family_id, client_id, backend_url)
    credential = Credential(client_id, client_secret)
    client = DingTalkStreamClient(credential)
    client.register_callback_handler(
        ChatbotMessage.TOPIC,
        YamiDingTalkBotHandler(family_id, client_id, client_secret, backend_url),
    )
    client.start_forever()


if __name__ == "__main__":
    main()
