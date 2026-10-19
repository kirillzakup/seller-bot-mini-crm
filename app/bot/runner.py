"""aiogram bot lifecycle.

The token comes from the DB (admin → Настройки), falling back to env BOT_TOKEN.
``reload_bot()`` (re)starts long-polling with the current token and is called both
at app startup and whenever the token changes in the admin — so configuring the
bot from the panel takes effect immediately, no .env edit / restart needed.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import get_main_router

logger = logging.getLogger(__name__)

_bot: Bot | None = None
_dp: Dispatcher | None = None
_task: asyncio.Task | None = None


def get_bot() -> Bot | None:
    """Current Bot instance, or None if no valid token is configured yet."""
    return _bot


def _get_dispatcher() -> Dispatcher:
    global _dp
    if _dp is None:
        _dp = Dispatcher(storage=MemoryStorage())
        _dp.include_router(get_main_router())
    return _dp


async def _stop() -> None:
    global _bot, _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Error stopping polling task")
        _task = None
    if _bot is not None:
        try:
            await _bot.session.close()
        except Exception:  # noqa: BLE001
            pass
        _bot = None


async def reload_bot() -> str:
    """(Re)start polling with the token from the DB.

    Returns a short status: 'no_token' | 'invalid_token' | 'started:@username'.
    """
    global _bot, _task
    from app.db.base import AsyncSessionLocal
    from app.services.settings_service import get_bot_token, set_setting

    await _stop()

    async with AsyncSessionLocal() as session:
        token = await get_bot_token(session)
    if not token:
        logger.warning("Bot token not set — polling disabled. Set it in admin → Настройки.")
        return "no_token"

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
    except Exception as e:  # noqa: BLE001 — bad token / network
        logger.error("Bot token rejected: %s", e)
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001
            pass
        return "invalid_token"

    _bot = bot
    logger.info("Bot started: @%s (id=%s)", me.username, me.id)
    async with AsyncSessionLocal() as session:
        if me.username:
            await set_setting(session, "bot_username", me.username)
        await session.commit()

    dp = _get_dispatcher()
    _task = asyncio.create_task(dp.start_polling(_bot, handle_signals=False))
    return f"started:@{me.username}"


async def shutdown_bot() -> None:
    await _stop()
