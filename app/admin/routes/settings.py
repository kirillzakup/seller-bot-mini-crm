"""Admin settings: bot token, persona, welcome text, LLM config, operator/admin chats."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import require_login, templates
from app.db.base import get_session
from app.knowledge.rag import active_products
from app.sales.prompts import DEFAULT_SILENCE_TEXTS
from app.services import admin_auth
from app.services.env_file import write_env_values
from app.services.media import active_media
from app.services.settings_service import get_setting, set_setting

router = APIRouter(prefix="/settings")


def _int_or_none(value: str):
    value = (value or "").strip()
    try:
        return int(value)
    except ValueError:
        return None


def _float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


MIN_REMINDER_ROWS = 5


def _reminder_rows(followup: dict) -> list[dict]:
    """Rows for the reminder editor: existing steps, padded to a minimum count.
    Falls back to legacy ``silence_texts`` (one per day) for display on old data."""
    steps = followup.get("silence_steps") or []
    if not steps:
        texts = followup.get("silence_texts") or DEFAULT_SILENCE_TEXTS
        steps = [{"day": i, "at": "12:00", "text": str(t)} for i, t in enumerate(texts)]
    rows = [
        {
            "day": s.get("day", i),
            "at": s.get("at", "12:00"),
            "text": s.get("text", ""),
            "image_id": s.get("image_id"),
        }
        for i, s in enumerate(steps)
    ]
    while len(rows) < MIN_REMINDER_ROWS:
        rows.append({"day": len(rows), "at": "12:00", "text": "", "image_id": None})
    return rows


def _build_silence_steps(
    days: list[str], times: list[str], texts: list[str], images: list[str]
) -> list[dict]:
    """Zip the reminder-editor columns into ``{day, at, text, image_id?}`` steps,
    dropping rows with an empty text."""
    steps: list[dict] = []
    for i, (day, at, text) in enumerate(zip(days, times, texts, strict=False)):
        text = (text or "").strip()
        if not text:
            continue
        try:
            d = max(0, int(day))
        except (TypeError, ValueError):
            d = 0
        step: dict = {"day": d, "at": (at or "12:00").strip() or "12:00", "text": text}
        image = images[i] if i < len(images) else ""
        if str(image).strip().isdigit():
            step["image_id"] = int(image)
        steps.append(step)
    return steps


@router.get("")
async def settings_form(
    request: Request,
    saved: int | None = None,
    bot: str | None = None,
    pw: str | None = None,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    s = {
        "bot_token": await get_setting(session, "bot_token") or "",
        "bot_username": await get_setting(session, "bot_username") or "",
        "shop_name": await get_setting(session, "shop_name") or "",
        "welcome_text": await get_setting(session, "welcome_text") or "",
        "bot_name": await get_setting(session, "bot_name") or "",
        "bot_gender": await get_setting(session, "bot_gender") or "female",
        "persona_prompt": await get_setting(session, "persona_prompt") or "",
        "sales_script": await get_setting(session, "sales_script") or "",
        "style_guide": await get_setting(session, "style_guide") or "",
        "operator_chat_id": await get_setting(session, "operator_chat_id") or "",
        "operator_chat_thread_id": await get_setting(session, "operator_chat_thread_id") or "",
        "admin_chat_id": await get_setting(session, "admin_chat_id") or "",
    }
    llm = await get_setting(session, "llm") or {}
    voice = await get_setting(session, "voice") or {}
    followup = await get_setting(session, "followup") or {}
    downsell = await get_setting(session, "downsell") or {}
    humanize = {
        "enabled": True,
        "min_seconds": 1.5,
        "max_seconds": 5.0,
        "per_char_seconds": 0.04,
        **(await get_setting(session, "humanize") or {}),
    }
    debounce = {"enabled": True, "seconds": 8, **(await get_setting(session, "debounce") or {})}
    products = await active_products(session)
    rem_rows = _reminder_rows(followup)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "s": s,
            "media": await active_media(session),
            "llm": llm,
            "voice": voice,
            "followup": followup,
            "rem_rows": rem_rows,
            "downsell": downsell,
            "humanize": humanize,
            "debounce": debounce,
            "products": products,
            "saved": saved,
            "bot": bot,
            "pw": pw,
            "admin_username": admin,
            "has_custom_password": await admin_auth.has_custom_password(session),
        },
    )


@router.post("")
async def save_settings(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    bot_token: str = Form(""),
    shop_name: str = Form(""),
    welcome_text: str = Form(""),
    bot_name: str = Form(""),
    bot_gender: str = Form("female"),
    persona_prompt: str = Form(""),
    sales_script: str = Form(""),
    style_guide: str = Form(""),
    operator_chat_id: str = Form(""),
    operator_chat_thread_id: str = Form(""),
    admin_chat_id: str = Form(""),
    silence_enabled: str = Form(None),
    rem_day: list[str] = Form(default=[]),
    rem_at: list[str] = Form(default=[]),
    rem_text: list[str] = Form(default=[]),
    rem_image: list[str] = Form(default=[]),
    think_default_hours: str = Form("24"),
    downsell_enabled: str = Form(None),
    downsell_product_id: str = Form(""),
    humanize_enabled: str = Form(None),
    humanize_min: str = Form("1.5"),
    humanize_max: str = Form("5.0"),
    debounce_enabled: str = Form(None),
    debounce_seconds: str = Form("8"),
    llm_base_url: str = Form(""),
    llm_api_key: str = Form(""),
    llm_model: str = Form(""),
    llm_temperature: str = Form("0.4"),
    voice_enabled: str = Form(None),
    voice_api_key: str = Form(""),
    voice_id: str = Form(""),
    voice_tts_model: str = Form("eleven_multilingual_v2"),
    voice_stt_model: str = Form("scribe_v1"),
    voice_reply_mode: str = Form("mirror"),
    voice_format: str = Form("voice"),
    voice_language: str = Form(""),
):
    new_token = (bot_token or "").strip()
    old_token = (await get_setting(session, "bot_token") or "").strip()

    await set_setting(session, "bot_token", new_token)
    await set_setting(session, "shop_name", shop_name)
    await set_setting(session, "welcome_text", welcome_text)
    await set_setting(session, "bot_name", bot_name.strip())
    await set_setting(session, "bot_gender", "male" if bot_gender == "male" else "female")
    await set_setting(session, "persona_prompt", persona_prompt)
    await set_setting(session, "sales_script", sales_script)
    await set_setting(session, "style_guide", style_guide)
    await set_setting(session, "operator_chat_id", _int_or_none(operator_chat_id))
    await set_setting(session, "operator_chat_thread_id", _int_or_none(operator_chat_thread_id))
    await set_setting(session, "admin_chat_id", _int_or_none(admin_chat_id))
    steps = _build_silence_steps(rem_day, rem_at, rem_text, rem_image)
    await set_setting(
        session,
        "followup",
        {
            "silence_enabled": bool(silence_enabled),
            "silence_steps": steps,
            "silence_texts": [s["text"] for s in steps],  # legacy mirror for old readers
            "think_default_hours": _float(think_default_hours, 24.0),
        },
    )
    await set_setting(
        session,
        "downsell",
        {"enabled": bool(downsell_enabled), "product_id": _int_or_none(downsell_product_id)},
    )
    prev_h = await get_setting(session, "humanize") or {}
    await set_setting(
        session,
        "humanize",
        {
            "enabled": bool(humanize_enabled),
            "min_seconds": _float(humanize_min, 1.5),
            "max_seconds": _float(humanize_max, 5.0),
            "per_char_seconds": prev_h.get("per_char_seconds", 0.04),
        },
    )
    await set_setting(
        session,
        "debounce",
        {
            "enabled": bool(debounce_enabled),
            "seconds": max(2.0, min(_float(debounce_seconds, 8.0), 60.0)),
        },
    )
    await set_setting(
        session,
        "llm",
        {
            "base_url": llm_base_url,
            "api_key": llm_api_key,
            "model": llm_model,
            "temperature": _float(llm_temperature, 0.4),
        },
    )
    await set_setting(
        session,
        "voice",
        {
            "enabled": bool(voice_enabled),
            "api_key": voice_api_key.strip(),
            "voice_id": voice_id.strip(),
            "tts_model": voice_tts_model.strip() or "eleven_multilingual_v2",
            "stt_model": voice_stt_model.strip() or "scribe_v1",
            "reply_mode": voice_reply_mode.strip() or "mirror",
            "format": voice_format.strip() or "voice",
            "language": voice_language.strip(),
        },
    )

    # Mirror the env-shaped secrets back into .env so the file reflects what was
    # entered in the panel (DB stays the live runtime source). .env is bind-mounted
    # from the host in Docker, so this survives restarts/rebuilds.
    write_env_values(
        {
            "BOT_TOKEN": new_token,
            "LLM_BASE_URL": llm_base_url.strip(),
            "LLM_API_KEY": llm_api_key.strip(),
            "LLM_MODEL": llm_model.strip(),
            "LLM_TEMPERATURE": str(_float(llm_temperature, 0.4)),
            "ELEVENLABS_API_KEY": voice_api_key.strip(),
            "ELEVENLABS_VOICE_ID": voice_id.strip(),
        }
    )

    # Restart polling only when the token actually changed. The token must be
    # committed first: reload_bot() reads it from a fresh DB session and would
    # otherwise pick up the previous value.
    bot_status = ""
    if new_token != old_token:
        await session.commit()
        from app.bot.runner import reload_bot

        status = await reload_bot()
        bot_status = "ok" if status.startswith("started:") else status

    suffix = f"&bot={bot_status}" if bot_status else ""
    return RedirectResponse(f"/admin/settings?saved=1{suffix}", status_code=303)


@router.post("/password")
async def change_password(
    request: Request,
    admin: str = Depends(require_login),
    session: AsyncSession = Depends(get_session),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    """Change the admin panel password. Requires the current password, stores the new
    one as a bcrypt hash in the DB (see app/services/admin_auth.py)."""
    if not await admin_auth.verify_login(session, admin, current_password):
        return RedirectResponse("/admin/settings?pw=wrong", status_code=303)
    if len(new_password) < admin_auth.MIN_PASSWORD_LEN:
        return RedirectResponse("/admin/settings?pw=short", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse("/admin/settings?pw=mismatch", status_code=303)
    await admin_auth.set_password(session, new_password)
    return RedirectResponse("/admin/settings?pw=ok", status_code=303)
