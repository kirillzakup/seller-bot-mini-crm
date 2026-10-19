"""ElevenLabs voice integration — the bot can both listen and speak.

Listen: ElevenLabs Speech-to-Text (Scribe) turns an inbound voice message into
text, which then flows through the normal AI sales dialog.
Speak: ElevenLabs Text-to-Speech turns the reply into audio; an optional ffmpeg
transcode (mp3 → ogg/opus) yields a native Telegram voice note when available.

All knobs (api key, voice, models, reply policy) live in the DB ``settings`` table
under the ``voice`` key and are edited in the admin — same pattern as the LLM
config, so nothing here requires a redeploy to change.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)

API_ROOT = "https://api.elevenlabs.io/v1"
_TTS_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_STT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

# Reply policy modes (cfg["reply_mode"])
MODE_OFF = "off"
MODE_MIRROR = "mirror"  # speak only when the client sent voice
MODE_ALWAYS = "always"  # speak every reply
MODE_ON_REQUEST = "on_request"  # speak only when the client asks for voice

# Substrings that flag a "reply by voice" request: the "голос" stem (голосом/голосов/голосам/
# голосовое) + audio-answer phrasings, but NOT bare "аудио" (so «аудиокурс» doesn't trigger).
_VOICE_REQUEST_HINTS = (
    "голос", "озвуч", "аудиосообщ", "аудио сообщ", "аудио ответ", "ответь аудио", "войс", "voice",
)


def _voice_requested(user_text: str | None) -> bool:
    """True if the client explicitly asked for a spoken/voice reply."""
    text = (user_text or "").lower()
    return any(hint in text for hint in _VOICE_REQUEST_HINTS)


@dataclass(frozen=True)
class VoiceReply:
    """A ready-to-send spoken reply."""

    data: bytes
    mime: str  # "audio/ogg" (opus voice note) or "audio/mpeg" (mp3 audio)
    filename: str


async def voice_config(session: AsyncSession) -> dict:
    """The ``voice`` settings dict (admin → Настройки).

    Falls back to env (``ELEVENLABS_API_KEY`` / ``ELEVENLABS_VOICE_ID``) for the
    two secrets when the DB value is empty — same DB→env pattern as the bot token.
    """
    from app.config import get_settings

    cfg = dict(await get_setting(session, "voice") or {})
    s = get_settings()
    if not (cfg.get("api_key") or "").strip():
        cfg["api_key"] = s.elevenlabs_api_key
    if not (cfg.get("voice_id") or "").strip():
        cfg["voice_id"] = s.elevenlabs_voice_id
    return cfg


def is_enabled(cfg: dict) -> bool:
    """Voice features require the master toggle AND an API key."""
    return bool(cfg.get("enabled")) and bool((cfg.get("api_key") or "").strip())


def should_speak(cfg: dict, *, was_voice: bool, user_text: str | None) -> bool:
    """Decide whether THIS reply should be spoken, per the configured policy."""
    if not is_enabled(cfg):
        return False
    mode = (cfg.get("reply_mode") or MODE_MIRROR).lower()
    if mode == MODE_ALWAYS:
        return True
    if mode == MODE_ON_REQUEST:
        return _voice_requested(user_text)
    if mode == MODE_MIRROR:
        # Speak when the client sent voice OR explicitly asks for a spoken reply.
        return was_voice or _voice_requested(user_text)
    return False  # MODE_OFF / unknown


def _clean_lang(value: str | None) -> str:
    """Return a clean ISO-639 code (2-3 letters) or "".

    Ignores invalid input like "ru/kz", "en-US" or "русский" so we never send a
    bad ``language_code`` — an empty result lets the model auto-detect.
    """
    v = (value or "").strip().lower()
    return v if re.fullmatch(r"[a-z]{2,3}", v) else ""


def _is_language_error(resp: httpx.Response) -> bool:
    """True if a 400 is specifically about an unsupported ``language_code``."""
    try:
        detail = resp.json().get("detail", {})
    except ValueError:
        detail = {}
    if isinstance(detail, dict) and (
        detail.get("status") == "unsupported_language" or detail.get("param") == "language_code"
    ):
        return True
    body = (resp.text or "").lower()
    return "language_code" in body and ("unsupported" in body or "does not support" in body)


_STT_ATTEMPTS = 3
_STT_BACKOFF = (1.0, 3.0)  # пауза перед 2-й и 3-й попыткой


async def transcribe(audio: bytes, *, filename: str, cfg: dict) -> str | None:
    """Speech → text via ElevenLabs Scribe. Returns None on any failure.

    Временный сбой (таймаут, 429, 5xx) повторяем: без этого одна секундная
    заминка на стороне ElevenLabs навсегда съедала сообщение клиента — бот
    отвечал «не расслышала», а вопрос уходил в никуда. Постоянные ошибки
    (400 на битом аудио, 401 на ключе) не повторяем: результат тот же, а
    клиент ждёт лишние секунды.
    """
    api_key = (cfg.get("api_key") or "").strip()
    if not api_key or not audio:
        return None
    data = {"model_id": (cfg.get("stt_model") or "scribe_v1").strip()}
    lang = _clean_lang(cfg.get("language"))
    if lang:
        data["language_code"] = lang

    for attempt in range(1, _STT_ATTEMPTS + 1):
        # Файловый кортеж пересобираем на каждой попытке: httpx читает поток байтов,
        # и переиспользованный объект на повторе ушёл бы пустым.
        files = {"file": (filename, audio, "application/octet-stream")}
        try:
            async with httpx.AsyncClient(timeout=_STT_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_ROOT}/speech-to-text",
                    headers={"xi-api-key": api_key},
                    data=data,
                    files=files,
                )
            if resp.status_code >= 400:
                transient = resp.status_code == 429 or resp.status_code >= 500
                logger.error(
                    "ElevenLabs STT failed %s (попытка %s/%s): %s",
                    resp.status_code, attempt, _STT_ATTEMPTS, resp.text[:300],
                )
                if not transient or attempt == _STT_ATTEMPTS:
                    return None
            else:
                text = (resp.json().get("text") or "").strip()
                return text or None
        except (httpx.HTTPError, ValueError) as e:
            logger.error(
                "ElevenLabs STT error (попытка %s/%s): %s", attempt, _STT_ATTEMPTS, e
            )
            if attempt == _STT_ATTEMPTS:
                return None
        await asyncio.sleep(_STT_BACKOFF[min(attempt - 1, len(_STT_BACKOFF) - 1)])
    return None


async def _synthesize_mp3(text: str, *, cfg: dict) -> bytes | None:
    api_key = (cfg.get("api_key") or "").strip()
    voice_id = (cfg.get("voice_id") or "").strip()
    if not api_key or not voice_id or not text.strip():
        return None
    model = (cfg.get("tts_model") or "eleven_multilingual_v2").strip()
    body: dict = {"text": text, "model_id": model}
    lang = _clean_lang(cfg.get("language"))
    if lang:
        body["language_code"] = lang
    url = f"{API_ROOT}/text-to-speech/{voice_id}"
    params = {"output_format": "mp3_44100_128"}
    headers = {"xi-api-key": api_key, "Accept": "audio/mpeg"}
    try:
        async with httpx.AsyncClient(timeout=_TTS_TIMEOUT) as client:
            resp = await client.post(url, params=params, headers=headers, json=body)
            # If the model doesn't support the requested language, retry letting it
            # auto-detect from the text instead of failing the whole reply.
            if resp.status_code == 400 and "language_code" in body and _is_language_error(resp):
                body = {k: v for k, v in body.items() if k != "language_code"}
                resp = await client.post(url, params=params, headers=headers, json=body)
        if resp.status_code >= 400:
            logger.error("ElevenLabs TTS failed %s: %s", resp.status_code, resp.text[:300])
            return None
        return resp.content or None
    except httpx.HTTPError as e:
        logger.error("ElevenLabs TTS error: %s", e)
        return None


def to_ogg_opus(mp3: bytes) -> bytes | None:
    """Transcode MP3 → OGG/Opus for a native Telegram voice note.

    Returns None when ffmpeg is unavailable (e.g. local Windows dev) — the caller
    then falls back to sending the mp3 as a plain audio message.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not mp3:
        return None
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0",
                # WhatsApp/Telegram voice notes want a MONO opus stream at opus's native
                # 48 kHz. Feeding libopus ElevenLabs' 44.1 kHz stereo mp3 without these
                # made it resample and emit off-granule OGG pages → choppy/stuttering
                # playback. Force mono + 48 kHz + the voip profile for clean speech.
                "-ac", "1", "-ar", "48000",
                "-c:a", "libopus", "-b:a", "32k", "-vbr", "on", "-application", "voip",
                "-f", "ogg", "pipe:1",
            ],
            input=mp3,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
        err = proc.stderr.decode("utf-8", "replace")[:200]
        logger.warning("ffmpeg transcode failed (rc=%s): %s", proc.returncode, err)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("ffmpeg transcode error: %s", e)
    return None


def to_m4a(mp3: bytes) -> bytes | None:
    """Transcode MP3 → M4A (AAC) for an Instagram audio message. Instagram's send API
    accepts ONLY aac/m4a/wav/mp4 (not mp3 or ogg/opus), so this is what the bot speaks in
    there. Writes to a temp FILE so ffmpeg can stamp the MP4 moov atom, with +faststart so
    it plays without a full download. Returns None if ffmpeg is unavailable.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not mp3:
        return None
    out_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tf:
            out_path = tf.name
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", "pipe:0",
                "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart",
                out_path,
            ],
            input=mp3,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0:
            with open(out_path, "rb") as f:
                data = f.read()
            if data:
                return data
        err = proc.stderr.decode("utf-8", "replace")[:200]
        logger.warning("ffmpeg m4a transcode failed (rc=%s): %s", proc.returncode, err)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("ffmpeg m4a transcode error: %s", e)
    finally:
        if out_path:
            try:
                os.unlink(out_path)
            except OSError:
                pass
    return None


async def synthesize_reply(text: str, *, cfg: dict, instagram: bool = False) -> VoiceReply | None:
    """Text → a sendable spoken reply.

    WhatsApp/Telegram get a native voice note (ogg/opus) when ``format == voice``.
    Instagram's API rejects mp3/ogg audio — it accepts only aac/m4a/wav/mp4 — so
    ``instagram=True`` transcodes to m4a (AAC). Falls back to mp3 if ffmpeg is missing.
    """
    from app.sales.textfmt import normalize_for_tts

    text = normalize_for_tts(text)  # "тг"→"тенге", "с 0"→"с нуля", drop ** and quotes
    mp3 = await _synthesize_mp3(text, cfg=cfg)
    if not mp3:
        return None
    if instagram:  # IG accepts aac/m4a/wav/mp4 — NOT mp3/ogg
        m4a = to_m4a(mp3)
        if m4a:
            return VoiceReply(data=m4a, mime="audio/mp4", filename="reply.m4a")
    elif (cfg.get("format") or "voice").lower() == "voice":
        ogg = to_ogg_opus(mp3)
        if ogg:
            return VoiceReply(data=ogg, mime="audio/ogg", filename="reply.ogg")
    return VoiceReply(data=mp3, mime="audio/mpeg", filename="reply.mp3")
