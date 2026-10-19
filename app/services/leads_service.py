"""Заявки: когда клиент спросил по делу — карточка уходит менеджерам.

Заявка появляется там, где бот передаёт клиента человеку: сделал расчёт,
упёрся в вопрос без ответа, клиент попросил оператора или звонок, пришла
заявка с сайта. До этого момента бот работает сам — и плодить карточки на
каждый вопрос про цену незачем.

Пока по клиенту есть незакрытая заявка, новая не создаётся: человек может
написать десять сообщений подряд, продавец ведёт его по одной карточке.
Закрыли (продажа/отказ/не целевой) — следующий предметный вопрос откроет
новую заявку, и это правильно: вернувшийся клиент — это новая продажа.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import UTC, datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Lead, Message, Order, User
from app.db.models.enums import OPEN_LEAD_STATUSES, Channel, LeadStatus, OrderStatus
from app.db.models.lead import as_utc
from app.sales.llm import LLMError, LLMNotConfigured, chat_completion
from app.services.contacts_export import phone_of
from app.services.notify import get_notify_targets
from app.services.settings_service import get_local_tz, get_setting, set_setting

logger = logging.getLogger(__name__)

_STATUS_LABEL = {
    LeadStatus.NEW: "🆕 Новая",
    LeadStatus.IN_WORK: "⏳ В работе",
    LeadStatus.WAITING: "🕓 В ожидании",
    LeadStatus.SALE: "💰 Продажа",
    LeadStatus.REJECT: "❌ Отказ",
    LeadStatus.NOT_TARGET: "🚫 Не целевой",
}

# Статус заказа словами менеджера: в карточке он видит, дошёл ли клиент до оплаты.
_ORDER_STATUS_LABEL = {
    OrderStatus.DRAFT: "черновик",
    OrderStatus.AWAITING_PAYMENT: "ждёт оплату",
    OrderStatus.PENDING_CONFIRMATION: "чек на проверке",
    OrderStatus.PAID: "оплачен",
    OrderStatus.FULFILLED: "выполнен",
    OrderStatus.CANCELED: "отменён",
    OrderStatus.REFUNDED: "возврат",
    OrderStatus.EXPIRED: "истёк",
}

# Рабочий пояс по умолчанию (Казахстан) — как в get_local_tz. Нужен card_text, когда
# пояс не передали: функция синхронная и в настройки сама не ходит.
_DEFAULT_TZ = timezone(timedelta(hours=5))

_CHANNEL_LABEL = {
    Channel.TELEGRAM: "Telegram",
    Channel.WHATSAPP: "WhatsApp",
    Channel.INSTAGRAM: "Instagram",
}

# Маркер в вопросе о сумме/причине: по нему обработчик ответа находит заявку.
# Состояние живёт в самом сообщении Telegram, а не в памяти процесса —
# перезапуск бота не теряет недописанный ответ продавца.
ASK_MARKER = "⟦заявка"


def contact_link(user: User | None) -> tuple[str, str] | None:
    """(подпись, ссылка) — как продавцу дойти до клиента в его же канале."""
    if user is None:
        return None
    if user.channel == Channel.INSTAGRAM and user.username:
        return "Instagram", f"https://instagram.com/{user.username}"
    if user.channel == Channel.TELEGRAM and user.username:
        return "Telegram", f"https://t.me/{user.username}"
    digits = "".join(c for c in (phone_of(user) or "") if c.isdigit())
    if user.channel == Channel.WHATSAPP and digits:
        return "WhatsApp", f"https://wa.me/{digits}"
    return None


async def open_lead(session: AsyncSession, user: User) -> Lead | None:
    """Незакрытая заявка клиента, если она есть."""
    res = await session.execute(
        select(Lead)
        .where(Lead.user_id == user.id)
        .where(Lead.status.in_(OPEN_LEAD_STATUSES))
        .order_by(Lead.id.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


def _source_of(user: User) -> str:
    """Откуда пришёл клиент: канал плюс рекламная метка, если она была."""
    label = _CHANNEL_LABEL.get(user.channel, str(user.channel))
    ad = (getattr(user, "ad_ref", None) or "").strip()
    return f"{label} · {ad}"[:120] if ad else label


async def _fill_from_dialog(session: AsyncSession, lead: Lead, user: User) -> None:
    """Достать из переписки товар, объём и суть запроса — одним вызовом модели.

    Не критично: модель выключена или ответила мусором — карточка всё равно уйдёт,
    просто с сырым текстом последнего сообщения вместо аккуратной сводки.
    """
    llm_cfg = await get_setting(session, "llm") or {}
    if not (llm_cfg.get("base_url") and llm_cfg.get("api_key")):
        return

    res = await session.execute(
        select(Message).where(Message.user_id == user.id).order_by(Message.id.desc()).limit(20)
    )
    msgs = list(reversed(res.scalars().all()))
    dialog = "\n".join(
        f"{'Клиент' if m.role.value == 'user' else 'Бот'}: "
        f"{' '.join((m.content or '').split())[:300]}"
        for m in msgs
        if (m.content or "").strip()
    )[-4000:]
    if not dialog:
        return

    try:
        msg = await chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты разбираешь переписку клиента с отделом продаж. "
                        "Верни СТРОГО JSON-объект без пояснений: "
                        '{"city":"", "product":"", "volume":"", "request":""}. '
                        "city — город клиента, если назван. product — что именно "
                        "интересует (товар, модель), если названо. volume — объём "
                        "или площадь, если названы. request — одна фраза о сути "
                        "запроса для менеджера. Чего в переписке нет — пустая строка."
                    ),
                },
                {"role": "user", "content": dialog},
            ],
            tools=None,
            config=llm_cfg,
        )
    except (LLMError, LLMNotConfigured) as e:
        logger.warning("lead %s: LLM enrich failed: %s", lead.id, e)
        return

    text = (msg.get("content") or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return

    lead.city = (str(data.get("city") or "").strip() or lead.city)[:120]
    lead.product = str(data.get("product") or "").strip()[:255]
    lead.volume = str(data.get("volume") or "").strip()[:255]
    lead.request = (str(data.get("request") or "").strip() or lead.request)[:2000]
    await session.flush()


# ── Карточка в группе продаж ──────────────────────────────────────────────


def remind_label(when: datetime, tz: timezone | None = None) -> str:
    """«15.10 14:00» в рабочем поясе — так дату напоминания видит менеджер."""
    return as_utc(when).astimezone(tz or _DEFAULT_TZ).strftime("%d.%m %H:%M")


async def latest_order(session: AsyncSession, user_id: int | None) -> Order | None:
    """Последний заказ клиента — его номер менеджер видит прямо в карточке."""
    if user_id is None:
        return None
    res = await session.execute(
        select(Order).where(Order.user_id == user_id).order_by(Order.id.desc()).limit(1)
    )
    return res.scalar_one_or_none()


def card_link(lead: Lead) -> str:
    """Ссылка на карточку заявки в группе продаж — или пусто, если карточки нет.

    Повторные сообщения (утренняя сводка, возврат из ожидания) отсылают к ней:
    там вся история кнопок, и менеджеру не нужно листать тему в поисках заявки.
    Формат t.me/c/<id>/<сообщение> работает для супергрупп, в том числе с темами.
    """
    return message_link(lead.card_chat_id, lead.card_message_id)


def message_link(chat_id: int | None, message_id: int | None) -> str:
    """t.me/c/<id>/<сообщение> — для супергрупп, в том числе с темами; пусто, если нечего."""
    if not (chat_id and message_id):
        return ""
    chat = str(chat_id)
    internal = chat[4:] if chat.startswith("-100") else chat.lstrip("-")
    return f"https://t.me/c/{internal}/{message_id}"


def tg_dialog_link(inbox_chat: int | None, user: User | None) -> str:
    """Тема клиента в Telegram-группе «Диалоги», где зеркалится вся его переписка.

    Менеджеру удобнее ответить клиенту прямо оттуда, не открывая админку.
    """
    return message_link(inbox_chat, user.inbox_topic_id if user else None)


def dialog_link(lead: Lead) -> str:
    """Переписка с клиентом в «Диалогах» админки."""
    return f"{get_settings().base_url.rstrip('/')}/admin/dialogs?u={lead.user_id}"


async def render_card(session: AsyncSession, lead: Lead, user: User | None) -> str:
    """card_text с номером заказа, рабочим поясом и чатом «Диалоги» — их достаёт тот,
    у кого есть сессия."""
    from app.services.inbox import inbox_chat_id  # lazy: inbox тянет бота и настройки

    order = await latest_order(session, lead.user_id)
    return card_text(
        lead,
        user,
        order=order,
        tz=await get_local_tz(session),
        inbox_chat=await inbox_chat_id(session),
    )


def card_text(
    lead: Lead,
    user: User | None,
    *,
    order: Order | None = None,
    tz: timezone | None = None,
    inbox_chat: int | None = None,
) -> str:
    """HTML карточки. Всё, что нужно менеджеру, чтобы начать разговор, — в одном экране.

    Заказ и часовой пояс передаёт render_card: сама функция синхронная и в базу не ходит.
    """
    name = html.escape((user.full_name if user else "") or "Клиент")
    lines = [f"📋 <b>Заявка #{lead.id}</b> · {_STATUS_LABEL[lead.status]}", "", f"👤 {name}"]

    phone = phone_of(user) if user else ""
    if phone:
        lines.append(f"📞 <code>{html.escape(phone)}</code>")
    link = contact_link(user)
    if link:
        label, url = link
        lines.append(f"🔗 {label}: {url}")
    if order is not None:
        # Номер заказа — то, по чему менеджер находит оплату и говорит с клиентом
        # об одном и том же счёте.
        amount = f"{int(order.amount_kzt or 0):,}".replace(",", " ")
        status = _ORDER_STATUS_LABEL.get(order.status, str(order.status))
        lines.append(f"🧾 Заказ: <code>{html.escape(order.code)}</code> · {amount} ₸ · {status}")
    lines.append(f"📍 Откуда: {html.escape(lead.source or '—')}")
    if lead.city:
        lines.append(f"🏙 Город: {html.escape(lead.city)}")
    if lead.product:
        lines.append(f"🛒 Интерес: {html.escape(lead.product)}")
    if lead.volume:
        lines.append(f"📦 Объём: {html.escape(lead.volume)}")
    if lead.request:
        lines.append("")
        lines.append(f"💬 <i>{html.escape(lead.request)}</i>")

    if lead.work_note:
        lines.append("")
        lines.append(f"📝 <b>Сделано:</b> {html.escape(latest_work_note(lead))}")
    if lead.status == LeadStatus.WAITING and lead.remind_at:
        lines.append("")
        lines.append(f"🕓 В ожидании до {remind_label(lead.remind_at, tz)}")
    if lead.wait_note and lead.status in (LeadStatus.WAITING, LeadStatus.IN_WORK):
        if lead.status != LeadStatus.WAITING:
            lines.append("")
        lines.append(f"🤝 Договорились: {html.escape(lead.wait_note)}")
    if lead.taken_by_name:
        lines.append("")
        lines.append(f"🙋 Взял: <b>{html.escape(lead.taken_by_name)}</b>")
        secs = lead.reaction_seconds
        if secs is not None:
            lines.append(f"⏱ Реакция: {_human_delay(secs)}")
    if lead.status == LeadStatus.SALE and lead.amount_kzt:
        lines.append(f"💰 Сумма: <b>{lead.amount_kzt:,}</b> ₸".replace(",", " "))
    if lead.reject_reason and lead.status in (LeadStatus.REJECT, LeadStatus.NOT_TARGET):
        mark = "❌" if lead.status == LeadStatus.REJECT else "🚫"
        lines.append(f"{mark} Причина: {html.escape(lead.reject_reason)}")

    lines.append("")
    lines.append(f"💬 Переписка: {dialog_link(lead)}")
    tg = tg_dialog_link(inbox_chat, user)
    if tg:
        lines.append(f"🗂 Диалог в Telegram: {tg}")
    return "\n".join(lines)


def _human_delay(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"
    return f"{seconds // 86400} дн"


def card_kb(lead: Lead, *, for_digest: bool = False) -> InlineKeyboardMarkup:
    """Кнопки под карточкой — свои для каждой стадии.

    «⏳ В работе» на карточке жмут один раз — дальше на её месте «🕓 В ожидании»:
    клиент хочет, но позже, и менеджер назначает день, когда бот вернёт карточку.
    В утренней сводке (``for_digest``) «В работе» остаётся всегда: там она спрашивает
    «что сделано», и без неё история работы по клиенту перестала бы копиться.
    """
    if lead.status == LeadStatus.NEW:
        rows = [
            [InlineKeyboardButton(text="🙋 Забрал в работу", callback_data=f"lead:take:{lead.id}")]
        ]
    elif lead.status in (LeadStatus.IN_WORK, LeadStatus.WAITING):
        started = lead.work_started or lead.status == LeadStatus.WAITING
        if started and not for_digest:
            first = InlineKeyboardButton(text="🕓 В ожидании", callback_data=f"lead:wait:{lead.id}")
        else:
            first = InlineKeyboardButton(text="⏳ В работе", callback_data=f"lead:work:{lead.id}")
        rows = [
            [
                first,
                InlineKeyboardButton(text="💰 Продажа", callback_data=f"lead:sale:{lead.id}"),
            ],
            [
                InlineKeyboardButton(text="❌ Отказ", callback_data=f"lead:reject:{lead.id}"),
                InlineKeyboardButton(text="🚫 Не целевой", callback_data=f"lead:nt:{lead.id}"),
            ],
        ]
    else:
        # Итог проставлен. Кнопка на случай промаха: вернуть в работу и переотметить —
        # иначе ошибочный «Отказ» останется в отчёте навсегда.
        rows = [
            [InlineKeyboardButton(text="↩️ Вернуть в работу", callback_data=f"lead:back:{lead.id}")]
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


LEADS_CHAT_KEY = "leads_chat_id"
LEADS_THREAD_KEY = "leads_thread_id"


async def set_leads_chat(session: AsyncSession, chat_id: int, thread_id: int | None) -> None:
    """Запомнить тему, в которую падают карточки заявок."""
    await set_setting(session, LEADS_CHAT_KEY, int(chat_id))
    await set_setting(session, LEADS_THREAD_KEY, int(thread_id) if thread_id else None)


async def leads_targets(session: AsyncSession) -> list[tuple[int, int | None]]:
    """Куда слать карточки: своя тема, если её назначили, иначе общие чаты.

    Отдельная настройка нужна потому, что заявки живут не там же, где чеки и
    эскалации: у продавцов своя тема, и смешивать их с прочими уведомлениями
    значит хоронить заявки в общем потоке.
    """
    chat = await get_setting(session, LEADS_CHAT_KEY)
    if chat in (None, ""):
        return await get_notify_targets(session)
    thread = await get_setting(session, LEADS_THREAD_KEY)
    try:
        return [(int(chat), int(thread) if thread not in (None, "") else None)]
    except (TypeError, ValueError):
        return await get_notify_targets(session)


async def post_card(session: AsyncSession, lead: Lead, user: User | None) -> bool:
    """Отправить карточку в группу продаж и запомнить, где она висит.

    Возвращает, дошла ли она хоть куда-то: по этому признаку бот решает,
    говорить ли клиенту «менеджер уведомлён». Соврать здесь — значит
    потерять клиента молча.
    """
    from app.bot.runner import get_bot

    bot = get_bot()
    if bot is None:
        logger.warning("lead %s: бот не настроен, карточка не ушла", lead.id)
        return False
    targets = await leads_targets(session)
    if not targets:
        logger.warning("lead %s: чат продаж не настроен, карточка не ушла", lead.id)
        return False
    text, kb = await render_card(session, lead, user), card_kb(lead)
    delivered = False
    for chat_id, thread_id in targets:
        try:
            sent = await bot.send_message(
                chat_id, text, reply_markup=kb, message_thread_id=thread_id
            )
        except Exception:  # noqa: BLE001 — карточка не должна ронять обработку сообщения
            logger.exception("lead %s: карточка не ушла в чат %s", lead.id, chat_id)
            continue
        delivered = True
        if lead.card_chat_id is None:  # правим потом только первую (группу продаж)
            lead.card_chat_id = sent.chat.id
            lead.card_message_id = sent.message_id
            await session.flush()
    return delivered


async def refresh_card(session: AsyncSession, lead: Lead) -> None:
    """Перерисовать карточку на месте — чтобы смена статуса не плодила сообщения."""
    from app.bot.runner import get_bot

    bot = get_bot()
    if bot is None or not (lead.card_chat_id and lead.card_message_id):
        return
    user = await session.get(User, lead.user_id)
    try:
        await bot.edit_message_text(
            chat_id=lead.card_chat_id,
            message_id=lead.card_message_id,
            text=await render_card(session, lead, user),
            reply_markup=card_kb(lead),
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return  # кнопку нажали повторно — карточка уже такая
        logger.warning("lead %s: Telegram не дал обновить карточку: %s", lead.id, e)
    except Exception:  # noqa: BLE001 — карточка не должна ронять смену статуса
        logger.exception("lead %s: карточка не перерисовалась", lead.id)


# ── Состав менеджеров ─────────────────────────────────────────────────────
# Имя из профиля Telegram для отчёта не годится: у одного «Асем», у другого
# «🔥ASEM🔥», у третьего вообще пусто. Поэтому менеджер один раз представляется
# командой в группе, и дальше во всех карточках и отчётах стоит его настоящее имя.

SELLERS_KEY = "sales_team"


async def sellers(session: AsyncSession) -> dict[str, str]:
    """{telegram id строкой: настоящее имя}."""
    raw = await get_setting(session, SELLERS_KEY) or {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


async def register_seller(session: AsyncSession, tg_id: int, name: str) -> None:
    team = await sellers(session)
    team[str(tg_id)] = name.strip()[:120]
    await set_setting(session, SELLERS_KEY, team)


async def remove_seller(session: AsyncSession, tg_id: int) -> bool:
    team = await sellers(session)
    if team.pop(str(tg_id), None) is None:
        return False
    await set_setting(session, SELLERS_KEY, team)
    return True


async def seller_name(session: AsyncSession, tg_user) -> tuple[str, bool]:
    """(имя для отчёта, представился ли он командой).

    Второй флаг нужен, чтобы подсказать незарегистрированному менеджеру
    представиться — но не мешать ему работать прямо сейчас.
    """
    if tg_user is None:
        return "менеджер", False
    known = (await sellers(session)).get(str(tg_user.id))
    if known:
        return known, True
    fallback = (tg_user.full_name or tg_user.username or "менеджер")[:120]
    return fallback, False


async def record_web_lead(
    session: AsyncSession,
    *,
    name: str,
    phone: str,
    source: str,
    gclid: str = "",
    utm: str = "",
    note: str = "",
) -> Lead | None:
    """Заявка с сайта → та же карточка с кнопками, что и у заявок из переписки.

    Контакт заводим по номеру как клиента WhatsApp. Это не формальность: когда
    человек потом напишет в WhatsApp, переписка приклеится к этой же записи, и
    продавец увидит и заявку с сайта, и диалог в одном месте.
    """
    from app.db.models.enums import Channel
    from app.services.users import get_or_create_contact

    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits:
        return None

    user = await get_or_create_contact(
        session,
        channel=Channel.WHATSAPP,
        external_id=digits,
        first_name=(name or "").strip() or None,
        phone=phone,
        reset_reminders=False,
    )
    await session.flush()

    lead = await open_lead(session, user)
    is_new = lead is None
    if lead is None:
        lead = Lead(user_id=user.id, status=LeadStatus.NEW)
        session.add(lead)
    lead.source = f"Сайт · {source}"[:120] if source else "Сайт"
    # Метку не перетираем: первый клик по рекламе и привёл клиента, а последующие
    # заходы (прямой, закладка) кампанию не заслужили.
    if gclid and not lead.gclid:
        lead.gclid = gclid[:255]
    if utm and not lead.utm:
        lead.utm = utm[:500]
    if note:
        # Состав заказа важнее дежурной фразы: дописываем, а не затираем, —
        # человек мог до этого спрашивать что-то ещё.
        if note not in (lead.request or ""):
            joined = (lead.request or "") + chr(10) + "• " + note
            lead.request = joined.strip() if lead.request else note
    elif not lead.request:
        lead.request = "Оставил номер на сайте — просит связаться."
    await session.flush()

    if is_new or not lead.card_message_id:
        await post_card(session, lead, user)
    else:
        await refresh_card(session, lead)
    logger.info("lead %s: заявка с сайта (%s)", lead.id, digits)
    return lead


async def record_handoff(
    session: AsyncSession, user: User, *, reason: str, kind: str = "оператор"
) -> Lead | None:
    """Бот передал клиента менеджеру → заявка с карточкой, а не просто уведомление.

    Сюда сходятся все передачи: «нужен оператор», просьба перезвонить, запрос
    коммерческого предложения. Раньше каждая из них была текстом в группе —
    взять её в работу было нечем, и чем всё кончилось, никто не знал.
    """
    lead = await open_lead(session, user)
    is_new = lead is None
    if lead is None:
        lead = Lead(user_id=user.id, source=_source_of(user), status=LeadStatus.NEW)
        session.add(lead)
        await _inherit_ad_tags(session, lead, user)

    if is_new:
        await _fill_from_dialog(session, lead, user)

    note = " ".join((reason or "").split())[:2000]
    if note and note not in (lead.request or ""):
        # Дописываем, а не затираем: у заявки может быть уже расчёт и суть запроса.
        lead.request = f"{lead.request}\n• {note}".strip() if lead.request else note
    lead.category = kind
    await session.flush()

    if is_new or not lead.card_message_id:
        await post_card(session, lead, user)
    else:
        await refresh_card(session, lead)
    logger.info("lead %s: передача менеджеру (%s)", lead.id, kind)
    return lead


async def remind_leads_in_work(session: AsyncSession, bot=None) -> int:
    """Утренняя сводка по всем незакрытым заявкам — см. app/services/leads_reminders.py."""
    from app.services.leads_reminders import remind_open_leads  # lazy: модуль импортирует нас

    return await remind_open_leads(session, bot)


# ── Что сделано по заявке ─────────────────────────────────────────────────
# Менеджер отвечает на вопрос «что уже сделано?» из утренней сводки. Пишем в
# историю с датой и именем, а не затираем: по заявке видно, как шла работа.
_WORK_NOTE_LIMIT = 3000


def add_work_note(lead: Lead, author: str, text: str, now: datetime | None = None) -> None:
    stamp = (now or datetime.now(UTC)).strftime("%d.%m")
    line = " ".join(f"{stamp} {author}: {text}".split())
    history = f"{lead.work_note}\n{line}" if (lead.work_note or "").strip() else line
    lead.work_note = history[-_WORK_NOTE_LIMIT:]


def latest_work_note(lead: Lead) -> str:
    lines = [ln for ln in (lead.work_note or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _days_in_work(lead: Lead) -> str:
    """« уже 3 дня» — или пусто, если забрали сегодня."""
    from datetime import UTC, datetime

    if not lead.taken_at:
        return ""
    days = (datetime.now(UTC) - as_utc(lead.taken_at)).days
    if days < 1:
        return ""
    if days == 1:
        return " уже сутки"
    return f" уже {days} дн."


async def _inherit_ad_tags(session: AsyncSession, lead: Lead, user: User) -> None:
    """Перенести рекламные метки с прошлой заявки клиента на новую.

    Человек кликает по объявлению один раз, а обращается потом несколько.
    Без переноса вторая заявка выглядела бы «пришёл сам», и реклама не получила
    бы засчитанную продажу, которую на самом деле принесла.
    """
    previous = (
        (
            await session.execute(
                select(Lead)
                .where(Lead.user_id == user.id, Lead.gclid != "")
                .order_by(Lead.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if previous is not None:
        lead.gclid = previous.gclid
        lead.utm = lead.utm or previous.utm
