"""Assemble all bot routers into one main router (priority order matters)."""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers import catalog, chat, inbox, leads, payment, setup, start, voice


def get_main_router() -> Router:
    router = Router()
    router.include_router(start.router)
    router.include_router(setup.router)  # /chatid utility
    router.include_router(inbox.router)  # /setinbox + relay operator topic replies
    router.include_router(leads.router)  # кнопки заявок в группе продаж
    router.include_router(catalog.router)
    router.include_router(payment.router)
    router.include_router(voice.router)  # voice/audio → transcribe → AI
    # AI chat catch-all must be LAST so specific handlers win first
    router.include_router(chat.router)
    return router
