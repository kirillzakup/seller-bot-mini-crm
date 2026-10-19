"""Setup/utility commands. /chatid surfaces a chat's opaque Telegram ID so the
owner can paste it into the admin (⚙️ Настройки → «Чат операторов»)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()

_CHAT_KIND = {
    "private": "личный чат",
    "group": "группа",
    "supergroup": "супергруппа",
    "channel": "канал",
}


@router.message(Command("chatid"))
async def chatid(message: Message) -> None:
    chat = message.chat
    kind = _CHAT_KIND.get(chat.type, chat.type)
    await message.answer(
        f"🆔 ID этого чата: <code>{chat.id}</code>\n"
        f"Тип: {kind}\n\n"
        "Скопируйте это число в админку → ⚙️ Настройки → «Чат операторов» "
        "(и/или «Чат для уведомлений админу»). Тогда сюда будут приходить запросы "
        "клиентов на оператора и чеки об оплате на проверку."
    )
