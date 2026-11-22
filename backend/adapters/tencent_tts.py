from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import math
import re
import struct
import time
from typing import AsyncIterator, Callable, Optional
from urllib.parse import quote, urlencode
import urllib.request
import uuid
import wave

TENCENT_TTS_WS_HOST = "tts.cloud.tencent.com"
TENCENT_TTS_WS_PATH = "/stream_ws"
TENCENT_TTS_ACTION = "TextToStreamAudioWS"

_CN_DIGITS = "零一二三四五六七八九"


def _int_to_cn(n: int) -> str:
    if n < 0:
        return "负" + _int_to_cn(-n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + ("" if n == 10 else _CN_DIGITS[n - 10])
    if n < 100:
        tens, ones = divmod(n, 10)
        return _CN_DIGITS[tens] + "十" + ("" if ones == 0 else _CN_DIGITS[ones])
    return str(n)


def normalize_for_chinese_tts(text: str) -> str:
    """Normalize fractions, page numbers, and ranges for natural elementary-school Chinese speech."""
    out = text

    # Fractions like 1/6 -> 六分之一
    out = re.sub(
        r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)",
        lambda m: f"{_int_to_cn(int(m.group(2)))}分之{_int_to_cn(int(m.group(1)))}",
        out,
    )
    # Item ranges like 1–5 题 or 1-5题 -> 第一题到第五题
    out = re.sub(
        r"(\d{1,2})\s*[–\-~]\s*(\d{1,2})\s*题",
        lambda m: f"第{_int_to_cn(int(m.group(1)))}题到第{_int_to_cn(int(m.group(2)))}题",
        out,
    )
    # Page numbers like 第 24 页 -> 第二十四页
    out = re.sub(
        r"第\s*(\d{1,3})\s*页",
        lambda m: f"第{_int_to_cn(int(m.group(1)))}页",
        out,
    )
    return out


@dataclass(frozen=True)
class TTSSpec:
    voice_type: int = 101001
    codec: str = "pcm"
    sample_rate: int = 16000
    speed: float = 0.0
    volume: float = 0.0


@dataclass(frozen=True)
class TTSChunk:
    playback_id: str
    seq: int
    profile_epoch: int
    pcm_bytes: bytes
    is_final: bool = False


class TencentTTSAdapter:
    """
    Tencent Cloud Real-Time TTS WebSocket adapter (Action=TextToStreamAudioWS).
    Docs: https://cloud.tencent.com/document/product/1073/49616
    Credentials remain strictly server-side; devices only receive epoch-tagged PCM chunks.
    """

    def __init__(self, *, app_id: int, secret_id: str, secret_key: str) -> None:
        self.app_id = app_id
        self.secret_id = secret_id
        self.secret_key = secret_key

    def build_signed_ws_url(
        self,
        *,
        text: str,
        spec: TTSSpec,
        session_id: Optional[str] = None,
        timestamp: Optional[int] = None,
        ttl_seconds: int = 300,
    ) -> str:
        ts = timestamp if timestamp is not None else int(time.time())
        sid = session_id or str(uuid.uuid4())
        normalized = normalize_for_chinese_tts(text)

        params = {
            "Action": TENCENT_TTS_ACTION,
            "AppId": str(self.app_id),
            "Codec": spec.codec,
            "Expired": str(ts + ttl_seconds),
            "SampleRate": str(spec.sample_rate),
            "SecretId": self.secret_id,
            "SessionId": sid,
            "Speed": str(spec.speed),
            "Text": normalized,
            "Timestamp": str(ts),
            "VoiceType": str(spec.voice_type),
            "Volume": str(spec.volume),
        }
        sorted_items = sorted(params.items(), key=lambda kv: kv[0])
        canonical_query = "&".join(f"{k}={v}" for k, v in sorted_items)
        sign_source = f"GET{TENCENT_TTS_WS_HOST}{TENCENT_TTS_WS_PATH}?{canonical_query}"
        digest = hmac.new(
            self.secret_key.encode("utf-8"),
            sign_source.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = base64.b64encode(digest).decode("utf-8")

        encoded_params = urlencode(sorted_items, quote_via=quote)
        return f"wss://{TENCENT_TTS_WS_HOST}{TENCENT_TTS_WS_PATH}?{encoded_params}&Signature={quote(signature, safe='')}"

    async def stream_from_chunks(
        self,
        *,
        raw_chunks: AsyncIterator[bytes],
        playback_id: str,
        profile_epoch: int,
        get_current_epoch: Callable[[], int],
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[TTSChunk]:
        """
        Guards outgoing PCM frames against mid-playback cancel or student switching
        (profile_epoch mismatch). Stale/cancelled audio stops immediately.
        """
        seq = 0
        async for pcm in raw_chunks:
            if cancel_event.is_set() or get_current_epoch() != profile_epoch:
                return
            seq += 1
            yield TTSChunk(
                playback_id=playback_id,
                seq=seq,
                profile_epoch=profile_epoch,
                pcm_bytes=pcm,
                is_final=False,
            )
        if not cancel_event.is_set() and get_current_epoch() == profile_epoch:
            yield TTSChunk(
                playback_id=playback_id,
                seq=seq + 1,
                profile_epoch=profile_epoch,
                pcm_bytes=b"",
                is_final=True,
            )


def _tc3_post(
    *,
    service: str,
    host: str,
    action: str,
    version: str,
    payload: dict,
    secret_id: str,
    secret_key: str,
    region: str = "ap-beijing",
    timeout: int = 12,
) -> dict:
    """Tencent Cloud API v3 (TC3-HMAC-SHA256) signed JSON POST."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ts = int(time.time())
    date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    hashed_payload = hashlib.sha256(body).hexdigest()

    canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\nx-tc-action:{action.lower()}\n"
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"

    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{ts}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    def _hmac_sha256(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, service)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    req = urllib.request.Request(
        f"https://{host}/",
        data=body,
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": host,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(ts),
            "X-TC-Version": version,
            "X-TC-Region": region,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def boost_pcm_loudness(pcm_bytes: bytes, *, target_rms: float = 7200.0, max_gain: float = 5.5) -> bytes:
    """Normalize 16kHz 16-bit mono PCM loudness with smooth tanh soft-limiting (zero harsh clipping)."""
    n = len(pcm_bytes) // 2
    if n == 0:
        return pcm_bytes
    samples = struct.unpack(f"<{n}h", pcm_bytes[: n * 2])
    rms = (sum(s * s for s in samples) / n) ** 0.5
    if rms < 10.0:
        return pcm_bytes
    gain = min(max_gain, target_rms / rms)
    out = bytearray(n * 2)
    for i, s in enumerate(samples):
        x = (s * gain) / 31000.0
        # Smooth soft limiter keeps transients loud without digital wrap/clipping
        y = int(31000.0 * math.tanh(x))
        struct.pack_into("<h", out, i * 2, max(-32767, min(32767, y)))
    return bytes(out)


def synthesize_tencent_http_pcm(
    *,
    text: str,
    secret_id: str,
    secret_key: str,
    voice_type: int = 101001,
    speed: float = 0.0,
) -> bytes:
    """Call Tencent Cloud TextToVoice (16kHz 16-bit mono PCM) using SecretId + SecretKey."""
    spoken = normalize_for_chinese_tts(text).strip()
    if not spoken or not secret_id or not secret_key:
        return b""
    ascii_letters = sum(1 for ch in spoken if ("a" <= ch.lower() <= "z"))
    primary_lang = 2 if ascii_letters >= max(6, len(spoken) // 2) else 1
    res = _tc3_post(
        service="tts",
        host="tts.tencentcloudapi.com",
        action="TextToVoice",
        version="2019-08-23",
        payload={
            "Text": spoken,
            "SessionId": str(uuid.uuid4()),
            "Volume": 8,
            "Speed": speed,
            "VoiceType": int(voice_type),
            "PrimaryLanguage": primary_lang,
            "SampleRate": 16000,
            "Codec": "pcm",
        },
        secret_id=secret_id,
        secret_key=secret_key,
    )
    audio_b64 = res.get("Response", {}).get("Audio")
    if not audio_b64:
        return b""
    raw_pcm = base64.b64decode(audio_b64)
    return boost_pcm_loudness(raw_pcm)


def transcribe_tencent_asr_pcm(
    *,
    pcm_bytes: bytes,
    secret_id: str,
    secret_key: str,
    sample_rate: int = 16000,
) -> str:
    """Transcribe 16kHz 16-bit mono PCM via Tencent Cloud SentenceRecognition (16k_zh)."""
    if not pcm_bytes or not secret_id or not secret_key:
        return ""
    wav_io = io.BytesIO()
    with wave.open(wav_io, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    wav_bytes = wav_io.getvalue()
    res = _tc3_post(
        service="asr",
        host="asr.tencentcloudapi.com",
        action="SentenceRecognition",
        version="2019-06-14",
        payload={
            "EngSerViceType": "16k_zh",
            "SourceType": 1,
            "VoiceFormat": "wav",
            "Data": base64.b64encode(wav_bytes).decode("ascii"),
            "DataLen": len(wav_bytes),
        },
        secret_id=secret_id,
        secret_key=secret_key,
    )
    return str(res.get("Response", {}).get("Result") or "").strip()
