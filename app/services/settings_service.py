"""Read/write runtime configuration stored in the ``settings`` table.

Falls back to code defaults (seeded from env) when a key is missing, so the app
always has a sane configuration even before anything is edited in the admin.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Setting
from app.sales.prompts import (
    DEFAULT_BIRTHDAY_TEXT,
    DEFAULT_CHECKIN_TEXT,
    DEFAULT_PERSONA,
    DEFAULT_SALES_SCRIPT,
    DEFAULT_SILENCE_STEPS,
    DEFAULT_SILENCE_TEXTS,
    DEFAULT_WELCOME,
)


def default_settings() -> dict[str, Any]:
    s = get_settings()
    return {
        # Bot token: editable in admin (stored in DB), falls back to env BOT_TOKEN.
        "bot_token": s.bot_token,
        "shop_name": "Мой магазин",
        "welcome_text": DEFAULT_WELCOME,
        "persona_prompt": DEFAULT_PERSONA,
        # Step-by-step sales script the bot follows (contact → interest → price → objections)
        "sales_script": DEFAULT_SALES_SCRIPT,
        # Re-engagement follow-ups: a sequence of silence reminders + "I'll think" callback.
        "followup": {
            "silence_enabled": True,
            # Preferred: a sequence of reminders, each with an explicit send time
            # ({day, at HH:MM, text}). The silent-lead job uses this when present.
            "silence_steps": DEFAULT_SILENCE_STEPS,
            # Legacy fallback (hours-based) — used only if silence_steps is empty.
            "silence_hours": 3,  # delay before the FIRST reminder
            "silence_interval_hours": 24,  # spacing between subsequent reminders
            "silence_texts": DEFAULT_SILENCE_TEXTS,  # one message per reminder; count = len
            "think_default_hours": 24,  # default delay for schedule_follow_up
        },
        # Downsell / tripwire: a cheaper product offered on price objections / "I'll think"
        "downsell": {"enabled": False, "product_id": None},
        # Loyalty: birthday greetings + check-in outreach ("how are you / new courses")
        "loyalty": {
            "birthday_enabled": True,
            "birthday_text": DEFAULT_BIRTHDAY_TEXT,
            "checkin_text": DEFAULT_CHECKIN_TEXT,
        },
        # Continuous improvement ("самообучение"): capture operator answers into the
        # KB (for approval) + LLM analysis of successful dialogs to improve the script.
        "learning": {
            "kb_capture_enabled": True,
            "kb_min_answer_len": 15,  # ignore short operator replies ("ок", "секунду")
            "analyze_max_dialogs": 12,  # how many paid dialogs to feed the analyzer
        },
        # Business timezone as a fixed UTC offset in hours (Kazakhstan = UTC+5, no DST).
        "tz_offset_hours": 5,
        "llm": {
            "base_url": s.llm_base_url,
            "api_key": s.llm_api_key,
            "model": s.llm_model,
            "temperature": s.llm_temperature,
        },
        # Telegram chat id of the operators' group for support escalation (set later)
        "operator_chat_id": None,
        # Where to notify admins about new orders/payments (defaults to operator chat)
        "admin_chat_id": None,
        # Meta (WhatsApp Cloud API + Instagram Messaging API) — configured in admin.
        # app_id + embedded_config_id power the Embedded Signup (WhatsApp Coexistence) flow.
        "meta": {
            "app_id": "",
            "app_secret": "",
            "verify_token": "",
            "graph_version": "v21.0",
            "embedded_config_id": "",
            "whatsapp": {
                "enabled": False,
                "phone_number_id": "",
                "access_token": "",
                "waba_id": "",
            },
            "instagram": {"enabled": False, "ig_id": "", "access_token": ""},
        },
        # Voice (ElevenLabs): listen (speech-to-text) + speak (text-to-speech).
        # reply_mode: off | mirror (voice on client voice OR request) | always | on_request
        # format: voice (native ogg/opus note, needs ffmpeg) | audio (mp3)
        "voice": {
            "enabled": False,
            "api_key": "",
            "voice_id": "",
            "tts_model": "eleven_multilingual_v2",
            "stt_model": "scribe_v1",
            "reply_mode": "mirror",
            "format": "voice",
            "language": "",
        },
    }


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(Setting, key)
    if row is not None:
        return row.value
    return default_settings().get(key, default)


async def set_setting(session: AsyncSession, key: str, value: Any) -> Setting:
    row = await session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
        session.add(row)
    else:
        row.value = value
    await session.flush()
    return row


async def get_local_tz(session: AsyncSession) -> timezone:
    """Business timezone as a fixed UTC offset (Kazakhstan = UTC+5, no DST)."""
    offset = await get_setting(session, "tz_offset_hours")
    try:
        return timezone(timedelta(hours=float(offset)))
    except (TypeError, ValueError):
        return timezone(timedelta(hours=5))


async def get_bot_token(session: AsyncSession) -> str:
    """Bot token from the DB (admin), falling back to env BOT_TOKEN."""
    value = await get_setting(session, "bot_token")
    return (value or "").strip()
