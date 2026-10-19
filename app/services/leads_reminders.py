"""Утренняя сводка по незакрытым заявкам в группе продаж.

Каждый день в 9:00 бот присылает в тему заявок мини-отчёт (сколько не забрано,
в работе, в ожидании, итоги вчера) и два списка — «Не забраны» и «В работе».
Номер заявки в списке — ссылка на её карточку в группе: менеджер жмёт и сразу
попадает к кнопкам. Раньше сводка слала по сообщению на заявку, и за ними терялось
главное — сколько всего висит и у кого.

Telegram пускает в группу около 20 сообщений в минуту, поэтому шлём с паузой и
ждём, если он всё-таки попросил подождать.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime, time, timedelta

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import LinkPreviewOptions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Lead, User
from app.db.models.enums import LeadStatus
from app.db.models.lead import as_utc
from app.services import leads_service
from app.services.settings_service import get_local_tz

logger = logging.getLogger(__name__)

_SEND_PAUSE_S = 3.2
# Строк в одном сообщении списка: укладываемся в лимит Telegram 4096 знаков.
_LIST_CHUNK = 25
# Под списком ссылок Telegram рисует превью первой — в сводке оно только мешает.
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _age(since: datetime | None) -> str:
    """« уже 3 дн.» — или пусто, если сегодня."""
    if not since:
        return ""
    days = (datetime.now(UTC) - as_utc(since)).days
    if days < 1:
        return ""
    # Без точки на конце: точку ставит предложение, иначе выходит «25 дн..».
    return " уже сутки" if days == 1 else f" уже {days} дн"


async def _lead_rows(session: AsyncSession, leads: list[Lead], *, owner: bool) -> list[str]:
    """Строки списка: номер — ссылка на карточку, имя, (кто ведёт), сколько висит."""
    rows = []
    for lead in leads:
        user = await session.get(User, lead.user_id)
        name = html.escape(((user.full_name if user else "") or "Клиент")[:40])
        who = f" · {html.escape(lead.taken_by_name)}" if owner and lead.taken_by_name else ""
        age = _age(lead.taken_at if owner else lead.created_at).replace(" уже ", " · ")
        card = leads_service.card_link(lead)
        if card:
            rows.append(f'<a href="{html.escape(card)}">#{lead.id}</a> {name}{who}{age}')
        else:  # карточки в группе нет — ведём в переписку в админке
            dialog = html.escape(leads_service.dialog_link(lead))
            rows.append(f'#{lead.id} {name}{who}{age} · <a href="{dialog}">переписка</a>')
    return rows


def _list_messages(title: str, rows: list[str], hint: str) -> list[str]:
    """Список, порезанный на сообщения по _LIST_CHUNK строк."""
    messages = []
    for start in range(0, len(rows), _LIST_CHUNK):
        head = f"{title}: {len(rows)}</b>"
        head += f" — продолжение, с {start + 1}-й" if start else f"\n{hint}"
        messages.append(head + "\n\n" + "\n".join(rows[start : start + _LIST_CHUNK]))
    return messages


async def _yesterday(session: AsyncSession, tz) -> str:
    """Итоги прошлого дня по закрытым заявкам — строкой в мини-отчёт."""
    today = datetime.now(tz).date()
    start = datetime.combine(today - timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(today, time.min, tzinfo=tz).astimezone(UTC)
    closed = (
        await session.execute(
            select(Lead)
            .where(Lead.closed_at.is_not(None))
            .where(Lead.closed_at >= start)
            .where(Lead.closed_at < end)
        )
    ).scalars().all()
    sales = [lead for lead in closed if lead.status == LeadStatus.SALE]
    amount = f"{sum(lead.amount_kzt or 0 for lead in sales):,}".replace(",", " ")
    rejects = sum(1 for lead in closed if lead.status == LeadStatus.REJECT)
    not_target = sum(1 for lead in closed if lead.status == LeadStatus.NOT_TARGET)
    return (
        f"Вчера: 💰 продаж {len(sales)} на {amount} ₸ · ❌ отказов {rejects}"
        f" · 🚫 не целевых {not_target}"
    )


async def _waiting(session: AsyncSession, tz) -> tuple[int, int]:
    """(всего в ожидании, сколько из них вернётся сегодня)."""
    end_of_today = datetime.combine(
        datetime.now(tz).date() + timedelta(days=1), time.min, tzinfo=tz
    ).astimezone(UTC)
    parked = (
        await session.execute(select(Lead).where(Lead.status == LeadStatus.WAITING))
    ).scalars().all()
    today = sum(1 for lead in parked if lead.remind_at and as_utc(lead.remind_at) < end_of_today)
    return len(parked), today


async def _open_leads(session: AsyncSession) -> list[Lead]:
    new = (
        await session.execute(
            select(Lead).where(Lead.status == LeadStatus.NEW).order_by(Lead.created_at)
        )
    ).scalars().all()
    in_work = (
        await session.execute(
            select(Lead).where(Lead.status == LeadStatus.IN_WORK).order_by(Lead.taken_at)
        )
    ).scalars().all()
    return [*new, *in_work]


async def _deliver(bot, chat_id: int, thread_id: int | None, text: str, kb=None):
    """(дошло ли, отправленное сообщение). Сообщение нужно, когда оно станет карточкой."""
    for _ in range(3):
        try:
            sent = await bot.send_message(
                chat_id,
                text,
                reply_markup=kb,
                message_thread_id=thread_id,
                link_preview_options=_NO_PREVIEW,
            )
            return True, sent
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception:  # noqa: BLE001 — одно упавшее напоминание не рвёт сводку
            logger.exception("заявки: сообщение в чат продаж не ушло")
            return False, None
    return False, None


async def _send(bot, chat_id: int, thread_id: int | None, text: str, kb=None) -> bool:
    delivered, _sent = await _deliver(bot, chat_id, thread_id, text, kb)
    return delivered


async def remind_open_leads(session: AsyncSession, bot=None) -> int:
    """Утренняя сводка: мини-отчёт, список «Не забраны», список «В работе».

    ``bot`` передают из скриптов, где бот не запущен; в работающем сервисе берём
    общий. Возвращает, сколько сообщений ушло.
    """
    if bot is None:
        from app.bot.runner import get_bot

        bot = get_bot()
    if bot is None:
        return 0
    targets = await leads_service.leads_targets(session)
    if not targets:
        return 0
    leads = await _open_leads(session)
    if not leads:
        return 0
    untaken = [lead for lead in leads if lead.status == LeadStatus.NEW]
    in_work = [lead for lead in leads if lead.status == LeadStatus.IN_WORK]
    tz = await get_local_tz(session)
    parked, back_today = await _waiting(session, tz)

    base = get_settings().base_url.rstrip("/")
    report = (
        f"☀️ <b>Заявки на утро {datetime.now(tz):%d.%m}</b>\n"
        f"🆕 Не забраны: {len(untaken)}\n"
        f"⏳ В работе: {len(in_work)}\n"
        f"🕓 В ожидании: {parked} (сегодня вернутся: {back_today})\n"
        f"{await _yesterday(session, tz)}\n\n"
        f"Все заявки: {base}/admin/leads"
    )
    messages = [report]
    messages += _list_messages(
        "🆕 <b>Не забраны",
        await _lead_rows(session, untaken, owner=False),
        "Нажмите на номер — откроется карточка с кнопкой «Забрал в работу».",
    )
    messages += _list_messages(
        "⏳ <b>В работе",
        await _lead_rows(session, in_work, owner=True),
        "Нажмите на номер — откроется карточка: отметьте итог или «В ожидании».",
    )
    chat_id, thread_id = targets[0]
    sent = 0
    for i, text in enumerate(messages):
        if i:
            await asyncio.sleep(_SEND_PAUSE_S)
        if await _send(bot, chat_id, thread_id, text):
            sent += 1
    logger.info(
        "сводка заявок: %s сообщений, незабранных %s, в работе %s",
        sent, len(untaken), len(in_work),
    )
    return sent


DUE_HEADER = "🔔 <b>Напоминание: клиент просил связаться сегодня</b>"


async def repost_waiting_leads(session: AsyncSession, bot=None) -> int:
    """Заявки «В ожидании», чей день настал, — снова в тему заявок полной карточкой.

    Новое сообщение становится карточкой заявки (дальнейшие нажатия правят его),
    заявка возвращается «в работу» с кнопкой «🕓 В ожидании» — можно отложить ещё раз.
    Нет бота или чата продаж — ничего не меняем: следующий запуск попробует снова.
    Возвращает, сколько карточек ушло.
    """
    if bot is None:
        from app.bot.runner import get_bot

        bot = get_bot()
    if bot is None:
        return 0
    targets = await leads_service.leads_targets(session)
    if not targets:
        return 0
    due = (
        await session.execute(
            select(Lead)
            .where(Lead.status == LeadStatus.WAITING)
            .where(Lead.remind_at.is_not(None))
            .where(Lead.remind_at <= datetime.now(UTC))
            .order_by(Lead.remind_at)
        )
    ).scalars().all()

    chat_id, thread_id = targets[0]
    sent = 0
    for i, lead in enumerate(due):
        if i:
            await asyncio.sleep(_SEND_PAUSE_S)
        before = (lead.status, lead.remind_at)
        # Ссылку на прежнюю карточку берём до того, как новое сообщение её заменит:
        # там история — кто взял, что сделано, когда отложили.
        old_card = leads_service.card_link(lead)
        # Карточку рисуем уже в новом состоянии: «в работе», кнопка «В ожидании».
        lead.status, lead.remind_at, lead.work_started = LeadStatus.IN_WORK, None, True
        user = await session.get(User, lead.user_id)
        header = DUE_HEADER + (f"\n📌 Прежняя карточка: {old_card}" if old_card else "")
        text = f"{header}\n\n{await leads_service.render_card(session, lead, user)}"
        delivered, msg = await _deliver(
            bot, chat_id, thread_id, text, leads_service.card_kb(lead)
        )
        if not delivered:
            lead.status, lead.remind_at = before  # не дошло — вернём в следующий запуск
            continue
        if msg is not None:
            lead.card_chat_id, lead.card_message_id = msg.chat.id, msg.message_id
        # Коммит после каждой: упади задача посреди пачки, уже отправленные карточки
        # не должны уйти второй раз.
        await session.commit()
        sent += 1
    if due:
        logger.info("заявки из ожидания: вернул %s из %s", sent, len(due))
    return sent
