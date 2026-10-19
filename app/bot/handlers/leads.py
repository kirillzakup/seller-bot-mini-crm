"""Кнопки заявки в группе продаж: забрал в работу → в работе / продажа / отказ /
не целевой.

Сумму продажи, причину отказа и «что сейчас по клиенту» бот спрашивает ответом
на своё же сообщение, а не
через состояние в памяти: менеджеров несколько, они жмут кнопки одновременно, и
перезапуск бота не должен терять недописанный ответ. Номер заявки едет в тексте
вопроса — по нему и находим, к чему относится ответ.
"""

from __future__ import annotations

import calendar
import html
import logging
import re
from datetime import UTC, date, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ForceReply, Message

from app.db.base import AsyncSessionLocal
from app.db.models import Lead
from app.db.models.enums import LeadStatus
from app.services import leads_service as leads_svc
from app.services.notify import get_notify_chat_ids
from app.services.settings_service import get_local_tz

router = Router()
logger = logging.getLogger(__name__)

_ASK_RE = re.compile(r"⟦заявка\s+#(\d+)\s+(сумма|причина|нецелевой|сделано|ожидание|договор)⟧")
_WAIT_EXAMPLES = "15.10, 15.10 14:00, завтра, через 3 дня, через неделю, в понедельник…"


# ── Состав менеджеров ─────────────────────────────────────────────────────
# Имя из профиля Telegram для отчёта не годится: у одного «Асем», у другого
# «🔥ASEM🔥», у третьего пусто. Поэтому менеджер один раз представляется
# командой, и дальше во всех карточках и отчётах стоит его настоящее имя.


async def _sales_chat_ids(session) -> set[int]:
    """Чаты, где бот слушается менеджеров: уведомления плюс тема заявок.

    Тему заявок можно перенести в другую группу — тогда кнопки и команды
    обязаны работать и там, иначе карточка приедет туда, где на неё нельзя
    нажать.
    """
    ids = set(await get_notify_chat_ids(session))
    ids.update(chat for chat, _thread in await leads_svc.leads_targets(session))
    return ids


async def _in_sales_chat(session, message: Message) -> bool:
    """Команды состава принимаем только из чата продаж.

    Иначе любой клиент, пишущий боту в Telegram, мог бы записать себя
    в менеджеры и засорить список.
    """
    return message.chat.id in await _sales_chat_ids(session)


@router.message(Command("manager", "менеджер", "seller", "продавец", ignore_case=True))
async def set_seller(message: Message, command: CommandObject) -> None:
    """Менеджер представляется: /менеджер Асем Нурланова."""
    who = message.from_user
    if who is None:
        return
    async with AsyncSessionLocal() as session:
        if not await _in_sales_chat(session, message):
            await message.reply("Эту команду нужно отправить в группе продаж.")
            return
    name = (command.args or "").strip()
    if not name:
        await message.reply(
            "Напишите своё имя после команды, например:\n"
            "<code>/продавец Асем Нурланова</code>\n\n"
            "Оно будет стоять в карточках заявок и в отчёте по продажам."
        )
        return
    async with AsyncSessionLocal() as session:
        await leads_svc.register_seller(session, who.id, name)
        await session.commit()
    await message.reply(
        f"✅ Записал: <b>{html.escape(name[:120])}</b>.\n"
        "Теперь заявки, которые вы забираете, подписываются этим именем."
    )


@router.message(Command("managers", "менеджеры", "sellers", "продавцы", ignore_case=True))
async def list_sellers(message: Message) -> None:
    async with AsyncSessionLocal() as session:
        if not await _in_sales_chat(session, message):
            return
        team = await leads_svc.sellers(session)
    if not team:
        await message.reply(
            "Пока никто не представился. Каждый менеджер должен один раз отправить "
            "сюда <code>/менеджер Имя Фамилия</code> — иначе в отчёте будут ники из Telegram."
        )
        return
    rows = "\n".join(f"• {html.escape(n)}" for n in sorted(team.values()))
    await message.reply(f"👥 <b>Отдел продаж</b> ({len(team)}):\n{rows}")


@router.message(Command("unmanager", "уволить", "unseller", ignore_case=True))
async def drop_seller(message: Message) -> None:
    """Убрать себя из списка — например, при передаче рабочего аккаунта."""
    who = message.from_user
    if who is None:
        return
    async with AsyncSessionLocal() as session:
        if not await _in_sales_chat(session, message):
            return
        removed = await leads_svc.remove_seller(session, who.id)
        await session.commit()
    await message.reply("Убрал вас из списка менеджеров." if removed else "Вас и не было в списке.")


# ── Кнопки под карточкой ──────────────────────────────────────────────────


async def _operator_lead(session, callback: CallbackQuery) -> Lead | None:
    """Заявка из callback_data — но только если жмут из чата продаж."""
    if callback.message is None:
        return None
    if callback.message.chat.id not in await _sales_chat_ids(session):
        return None
    try:
        lead_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        return None
    return await session.get(Lead, lead_id)


def _not_yours(lead: Lead, who) -> str:
    """Имя того, кто ведёт заявку, если жмёт не он; иначе пусто.

    Заявку, которую ещё никто не забрал, может трогать любой менеджер. Забранную —
    только тот, кто её забрал: иначе коллега случайно закрывает чужого клиента, и в
    отчёте продажа или отказ записываются не на того.
    """
    if not lead.taken_by_tg_id or who is None or who.id == lead.taken_by_tg_id:
        return ""
    return lead.taken_by_name or "другой менеджер"


async def _own_lead(session, callback: CallbackQuery) -> Lead | None:
    """Заявка из кнопки — если она есть и её ведёт тот, кто нажал. Иначе объясняем и None."""
    lead = await _operator_lead(session, callback)
    if lead is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return None
    owner = _not_yours(lead, callback.from_user)
    if owner:
        await callback.answer(f"⛔ Это не ваша заявка — её ведёт {owner}.", show_alert=True)
        return None
    return lead


def _pressed_elsewhere(callback: CallbackQuery, lead: Lead) -> bool:
    """Кнопку нажали не на самой карточке, а на напоминании из утренней сводки."""
    msg = callback.message
    return msg is not None and msg.message_id != lead.card_message_id


async def _sync_pressed(callback: CallbackQuery, lead: Lead) -> None:
    """Обновить кнопки на напоминании, где нажали: карточку перерисует refresh_card,
    а на самом напоминании иначе так и висела бы «Забрал в работу»."""
    if not _pressed_elsewhere(callback, lead):
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=leads_svc.card_kb(lead))
    except Exception:  # noqa: BLE001 — «not modified» и удалённое сообщение не критичны
        pass


@router.callback_query(F.data.startswith("lead:take:"))
async def take(callback: CallbackQuery) -> None:
    async with AsyncSessionLocal() as session:
        lead = await _operator_lead(session, callback)
        if lead is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        if lead.taken_by_name:
            await callback.answer(f"Уже в работе у {lead.taken_by_name}", show_alert=True)
            return
        who = callback.from_user
        name, registered = await leads_svc.seller_name(session, who)
        lead.taken_by_name = name
        lead.taken_by_tg_id = who.id
        lead.taken_at = datetime.now(UTC)
        lead.status = LeadStatus.IN_WORK
        lead_id = lead.id
        await leads_svc.refresh_card(session, lead)
        await _sync_pressed(callback, lead)
        await session.commit()

    # Отдельным сообщением, а не только правкой карточки: правка молчаливая, и в
    # группе не видно, что заявку уже разобрали.
    note = f"🙋 <b>{html.escape(name)}</b> забрал(а) заявку #{lead_id}."
    if not registered:
        note += (
            "\n\n⚠️ Вы ещё не представились — в отчёте будет ваше имя из Telegram. "
            "Отправьте сюда <code>/менеджер Имя Фамилия</code>, чтобы это исправить."
        )
    await callback.message.reply(note)
    await callback.answer("Заявка ваша — клиент за вами")


@router.callback_query(F.data.startswith("lead:nt:"))
async def not_target(callback: CallbackQuery) -> None:
    """Не наш клиент — спрашиваем почему: иначе в отчёте не видно, кого отсеиваем."""
    async with AsyncSessionLocal() as session:
        lead = await _own_lead(session, callback)
        if lead is None:
            return
        lead_id = lead.id
    await _ask(
        callback,
        lead_id,
        "нецелевой",
        "почему клиент не целевой? Напишите коротко — например «посредник» или «другой регион».",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lead:back:"))
async def back_to_work(callback: CallbackQuery) -> None:
    """Вернуть закрытую заявку в работу — отменить ошибочно нажатую кнопку."""
    async with AsyncSessionLocal() as session:
        lead = await _own_lead(session, callback)
        if lead is None:
            return
        lead.status = LeadStatus.IN_WORK
        lead.closed_at = None
        lead.amount_kzt = None
        lead.reject_reason = ""
        await leads_svc.refresh_card(session, lead)
        await _sync_pressed(callback, lead)
        await session.commit()
    await callback.answer("Вернул в работу")


async def _ask(callback: CallbackQuery, lead_id: int, kind: str, question: str) -> None:
    """Задать вопрос менеджеру так, чтобы Telegram сам открыл ему поле ответа.

    Упоминание в тексте — не украшение: ForceReply(selective=True) открывает поле
    ответа только тому, кто в сообщении упомянут.
    """
    who = callback.from_user
    label = html.escape(who.full_name or "менеджер")
    mention = f'<a href="tg://user?id={who.id}">{label}</a>'
    await callback.message.answer(
        f"{mention}, {question}\n<code>⟦заявка #{lead_id} {kind}⟧</code>",
        reply_markup=ForceReply(selective=True),
    )


@router.callback_query(F.data.startswith("lead:work:"))
async def still_working(callback: CallbackQuery) -> None:
    """«В работе».

    На самой карточке — статус без вопросов: он говорит сам за себя. На утреннем
    напоминании — спрашиваем, что уже сделано: иначе в кабинете у заявки, которая
    неделю «в работе», не видно никакой работы.
    """
    async with AsyncSessionLocal() as session:
        lead = await _own_lead(session, callback)
        if lead is None:
            return
        lead.status = LeadStatus.IN_WORK
        lead.closed_at = None
        lead.work_started = True
        lead.remind_at = None  # из ожидания вернули в работу — напоминание больше не нужно
        lead_id, from_reminder = lead.id, _pressed_elsewhere(callback, lead)
        await leads_svc.refresh_card(session, lead)
        # На напоминании кнопка тоже сменится на «🕓 В ожидании» — следующий шаг.
        await _sync_pressed(callback, lead)
        await session.commit()
    if from_reminder:
        await _ask(
            callback,
            lead_id,
            "сделано",
            "что уже сделано по клиенту? Коротко: звонил, отправил расчёт, ждём оплату…",
        )
    await callback.answer("Отмечено: в работе")


@router.callback_query(F.data.startswith("lead:wait:"))
async def waiting(callback: CallbackQuery) -> None:
    """«В ожидании»: клиент хочет, но позже, — спрашиваем, когда о нём напомнить.

    Статус меняется только после ответа с датой: без даты бот не знает, когда
    вернуть карточку, и заявка повисла бы в ожидании навсегда.
    """
    async with AsyncSessionLocal() as session:
        lead = await _own_lead(session, callback)
        if lead is None:
            return
        lead_id = lead.id
    await _ask(
        callback,
        lead_id,
        "ожидание",
        "когда следующая связь и о чём договорились? Напишите одним сообщением, например: "
        f"«20.10 14:00 перезвонить, ждёт расчёт доставки». Дата: {_WAIT_EXAMPLES}",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lead:sale:"))
async def sale(callback: CallbackQuery) -> None:
    async with AsyncSessionLocal() as session:
        lead = await _own_lead(session, callback)
        if lead is None:
            return
        lead_id = lead.id
    await _ask(callback, lead_id, "сумма", "напишите сумму продаж — числом в тенге.")
    await callback.answer()


@router.callback_query(F.data.startswith("lead:reject:"))
async def reject(callback: CallbackQuery) -> None:
    async with AsyncSessionLocal() as session:
        lead = await _own_lead(session, callback)
        if lead is None:
            return
        lead_id = lead.id
    await _ask(callback, lead_id, "причина", "напишите подробно причину отказа.")
    await callback.answer()


def _is_ask_reply(message: Message) -> bool:
    """Это ответ ровно на наш вопрос о сумме/причине?

    Фильтром, а не проверкой внутри хендлера: в aiogram сработавший хендлер
    останавливает цепочку, и широкий фильтр «любой ответ» проглотил бы ответы
    операторов в темах инбокса.
    """
    parent = message.reply_to_message
    if parent is None or not (parent.from_user and parent.from_user.is_bot):
        return False
    return bool(_ASK_RE.search(parent.text or parent.caption or ""))


# ── Дата напоминания ──────────────────────────────────────────────────────
# Менеджер пишет, как говорит: «завтра», «через 3 дня», «15.10 14:00», «в пятницу».
# Разбираем сами, без модели: ответ нужен сразу, а ошибка в дате — потерянный клиент.

_REMIND_DEFAULT_HOUR = 10  # время не назвали — к началу рабочего дня
_REMIND_MAX_DAYS = 3 * 366  # «через 999 недель» — опечатка, а не договорённость

_TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2})(?!\d))?(?!\d)")
_MONTHS = ("янв", "фев", "мар", "апр", "ма", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
_MONTH_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(" + "|".join(_MONTHS) + r")[а-я]*\.?(?:\s+(\d{4})(?!\d))?"
)
_NUMBER_WORDS = {
    "один": 1, "одну": 1, "пару": 2, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}
_IN_RE = re.compile(
    r"через\s+(?:(\d{1,3})\s*|(" + "|".join(_NUMBER_WORDS) + r")\s+)?(ден|дн|недел|месяц)[а-я]*"
)
_WEEKDAY_RE = (
    (re.compile(r"(?<![а-я])понедельник"), 0),
    (re.compile(r"(?<![а-я])вторник"), 1),
    (re.compile(r"(?<![а-я])сред[ауы]"), 2),
    (re.compile(r"(?<![а-я])четверг"), 3),
    (re.compile(r"(?<![а-я])пятниц[ауы]"), 4),
    (re.compile(r"(?<![а-я])суббот[ауы]"), 5),
    (re.compile(r"(?<![а-я])воскресень[ея]"), 6),
)


def _add_months(day: date, months: int) -> date:
    """Тот же день через N месяцев; 31-е в коротком месяце — его последний день."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return day.replace(year=year, month=month, day=min(day.day, last))


def _explicit_day(text: str, today: date) -> tuple[date, bool] | None:
    """(дата, был ли указан год) из «15.10», «15.10.2026» или «15 октября»; иначе None."""
    found = _DATE_RE.search(text)
    if found:
        day, month, year = int(found.group(1)), int(found.group(2)), found.group(3)
    else:
        found = _MONTH_RE.search(text)
        if not found:
            return None
        day, month = int(found.group(1)), _MONTHS.index(found.group(2)) + 1
        year = found.group(3)
    if year is None:
        return today.replace(month=month, day=day), False
    full_year = int(year) + 2000 if len(year) == 2 else int(year)
    return today.replace(year=full_year, month=month, day=day), True


def _relative_day(text: str, today: date) -> date | None:
    """Дата из «сегодня / завтра / через 3 дня / в пятницу»; None — если таких слов нет."""
    if "послезавтра" in text:
        return today + timedelta(days=2)
    if "завтра" in text:
        return today + timedelta(days=1)
    if "сегодня" in text:
        return today
    found = _IN_RE.search(text)
    if found:
        digits, word, unit = found.groups()
        n = int(digits) if digits else _NUMBER_WORDS.get(word, 1)  # «через неделю» — одну
        if unit == "месяц":
            return _add_months(today, n)
        return today + timedelta(days=n * 7 if unit == "недел" else n)
    for pattern, weekday in _WEEKDAY_RE:
        if pattern.search(text):
            # Ближайший такой день; «в понедельник», сказанное в понедельник, — следующий.
            return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)
    return None


def parse_remind_at(text: str, now_local: datetime) -> datetime | None:
    """Когда напомнить о клиенте — из ответа менеджера, в его часовом поясе.

    Понимает «15.10», «15.10.2026», «15 октября», «сегодня», «завтра», «послезавтра»,
    «через 3 дня / неделю / 2 недели / месяц», «в понедельник»… и время «14:00» в
    любом месте фразы (без времени — 10:00). Дата без года, которая в этом году уже
    прошла, — следующий год: «10.01», написанное в декабре, — это январь.
    Прошедшее время и непонятный текст — None: бот переспросит, а не угадает.
    """
    text = (text or "").lower().replace("ё", "е")
    hour, minute = _REMIND_DEFAULT_HOUR, 0
    timed = _TIME_RE.search(text)
    if timed:
        hour, minute = int(timed.group(1)), int(timed.group(2))
        if hour > 23 or minute > 59:
            return None
        text = f"{text[: timed.start()]} {text[timed.end():]}"

    today = now_local.date()
    try:
        explicit = _explicit_day(text, today)
    except ValueError:  # «31.02», «15.13» — такой даты нет
        return None
    if explicit is not None:
        day, has_year = explicit
        if not has_year and day < today:
            try:
                day = day.replace(year=day.year + 1)
            except ValueError:  # 29.02, а следующий год не високосный
                return None
    else:
        day = _relative_day(text, today)
        if day is None:
            if not timed:
                return None
            day = today  # только время — значит, сегодня

    when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=now_local.tzinfo)
    if when <= now_local or (when - now_local).days > _REMIND_MAX_DAYS:
        return None
    return when


# Слова-даты, которые не входят в договорённость: «завтра», «сегодня», «послезавтра».
_DAY_WORDS_RE = re.compile(r"(?<![а-я])(?:после)?завтра|(?<![а-я])сегодня")
# Предлоги, оставшиеся на краях после того, как вырезали дату: «в», «на», «до»…
_EDGE_WORDS_RE = re.compile(
    r"^(?:(?:в|во|на|к|до|с|со|и)\s+)+|(?:\s+(?:в|во|на|к|до|с|со|и))+$", re.IGNORECASE
)


def agreement_text(reply: str) -> str:
    """О чём договорились — ответ менеджера без даты и времени.

    «20.10 14:00 перезвонить, ждёт расчёт доставки» → «перезвонить, ждёт расчёт
    доставки». Дату вырезаем теми же шаблонами, что её и нашли, поэтому сколько бы
    способов записи даты ни было, в договорённость она не попадёт.
    """
    original = reply or ""
    low = original.lower().replace("ё", "е")
    if len(low) != len(original):  # редкие символы меняют длину при lower()
        low = original
    patterns = [_TIME_RE, _DATE_RE, _MONTH_RE, _IN_RE, _DAY_WORDS_RE]
    patterns += [pattern for pattern, _weekday in _WEEKDAY_RE]
    chars = list(original)
    for rx in patterns:
        for found in rx.finditer(low):
            for i in range(*found.span()):
                chars[i] = " "
    text = " ".join("".join(chars).split())
    previous = None
    while previous != text:
        previous = text
        text = _EDGE_WORDS_RE.sub("", text).strip(" ,.;:—–-")
    return text[:500]


@router.message(_is_ask_reply)
async def answer_to_ask(message: Message) -> None:
    """Ответ менеджера на вопрос о сумме или причине."""
    parent = message.reply_to_message
    found = _ASK_RE.search(parent.text or parent.caption or "")
    lead_id, kind = int(found.group(1)), found.group(2)
    reply = (message.text or "").strip()
    if not reply:
        return

    async with AsyncSessionLocal() as session:
        lead = await session.get(Lead, lead_id)
        if lead is None:
            await message.reply("Заявка не найдена.")
            return
        owner = _not_yours(lead, message.from_user)
        if owner:
            await message.reply(f"⛔ Это не ваша заявка — её ведёт {owner}. Ничего не изменил.")
            return
        if kind == "сделано":
            name, _registered = await leads_svc.seller_name(session, message.from_user)
            leads_svc.add_work_note(lead, name, reply[:500])
            if lead.status == LeadStatus.NEW:  # отписался по незабранной — значит, взял
                lead.status = LeadStatus.IN_WORK
                lead.taken_by_name = lead.taken_by_name or name
                lead.taken_at = lead.taken_at or datetime.now(UTC)
            await leads_svc.refresh_card(session, lead)
            await session.commit()
            await message.reply(f"📝 Записал в заявку #{lead_id}.")
            return
        if kind == "договор":
            name, _registered = await leads_svc.seller_name(session, message.from_user)
            lead.wait_note = reply[:500]
            leads_svc.add_work_note(lead, name, f"договорились: {reply[:500]}")
            await leads_svc.refresh_card(session, lead)
            await session.commit()
            await message.reply(f"🤝 Записал договорённость по заявке #{lead_id}.")
            return
        if kind == "ожидание":
            await _park_lead(session, message, lead, reply)
            return
        if kind == "нецелевой":
            lead.reject_reason = reply[:1000]
            lead.status = LeadStatus.NOT_TARGET
            done = "🚫 Отмечено: не целевой."
        elif kind == "сумма":
            digits = re.sub(r"[^\d]", "", reply)
            if not digits:
                await message.reply("Нужно число в тенге, например 1450000.")
                return
            lead.amount_kzt = int(digits)
            lead.status = LeadStatus.SALE
            done = f"💰 Продажа записана: {lead.amount_kzt:,} ₸".replace(",", " ")
        else:
            lead.reject_reason = reply[:1000]
            lead.status = LeadStatus.REJECT
            done = "❌ Отказ записан."
        lead.closed_at = datetime.now(UTC)
        lead.remind_at = None  # закрыли из ожидания — напоминать больше не о чем
        if not lead.taken_by_name:  # закрыли, не нажав «Забрал в работу»
            name, _registered = await leads_svc.seller_name(session, message.from_user)
            lead.taken_by_name = name
            lead.taken_by_tg_id = message.from_user.id if message.from_user else None
        await leads_svc.refresh_card(session, lead)
        await session.commit()
    await message.reply(f"{done} Заявка #{lead_id} закрыта.")


async def _park_lead(session, message: Message, lead: Lead, reply: str) -> None:
    """Ответ с датой на «когда напомнить» → заявка в ожидании до этой даты.

    Дату не угадываем: непонятную или прошедшую переспрашиваем тем же маркером,
    чтобы менеджер просто ответил ещё раз, а заявка не поменялась.
    """
    tz = await get_local_tz(session)
    now_local = datetime.now(tz)
    when = parse_remind_at(reply, now_local)
    if when is None:
        await message.reply(
            "Не понял дату или она уже прошла. Когда следующая связь и о чём договорились? "
            f"Например: «20.10 14:00 перезвонить, ждёт расчёт доставки».\n"
            f"<code>⟦заявка #{lead.id} ожидание⟧</code>",
            reply_markup=ForceReply(selective=True),
        )
        return
    agreement = agreement_text(reply)
    name, _registered = await leads_svc.seller_name(session, message.from_user)
    label = leads_svc.remind_label(when, tz)
    lead.status = LeadStatus.WAITING
    lead.remind_at = when.astimezone(UTC)
    lead.work_started = True  # вернётся из ожидания — снова с кнопкой «В ожидании»
    lead.closed_at = None
    lead.wait_note = agreement  # отложили заново без договорённости — старую не тащим
    note = f"ожидание до {label}" + (f" — договорились: {agreement}" if agreement else "")
    leads_svc.add_work_note(lead, name, note)
    if not lead.taken_by_name:  # отложил, не нажав «Забрал в работу», — значит, его клиент
        lead.taken_by_name = name
        lead.taken_by_tg_id = message.from_user.id if message.from_user else None
        lead.taken_at = lead.taken_at or datetime.now(UTC)
    await leads_svc.refresh_card(session, lead)
    await session.commit()
    day = f"{when:%d.%m}" if when.year == now_local.year else f"{when:%d.%m.%Y}"
    done = f"🕓 Напомню о заявке #{lead.id} {day} в {when:%H:%M}"
    if agreement:
        await message.reply(f"{done}\n🤝 Договорились: {html.escape(agreement)}")
        return
    # Дату назвали, а договорённость нет: заявку уже отложили, договорённость
    # спрашиваем отдельно — иначе в назначенный день менеджер не вспомнит, о чём речь.
    await message.reply(done)
    await message.reply(
        "О чём договорились с клиентом? Ответьте коротко, например: "
        "«перезвонить, ждёт расчёт доставки».\n"
        f"<code>⟦заявка #{lead.id} договор⟧</code>",
        reply_markup=ForceReply(selective=True),
    )


@router.message(Command("setleads", "заявки_сюда", ignore_case=True))
async def set_leads_here(message: Message) -> None:
    """Привязать текущую тему как место для карточек заявок.

    Отдельно от чата уведомлений: у менеджеров своя тема, и заявки не должны
    тонуть среди чеков, эскалаций и служебных сообщений.
    """
    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        await message.reply("Отправьте эту команду в группе продаж, в нужной теме.")
        return
    thread = message.message_thread_id
    async with AsyncSessionLocal() as session:
        await leads_svc.set_leads_chat(session, chat.id, thread)
        await session.commit()
    where = "в эту тему" if thread else "в эту группу"
    await message.reply(
        f"✅ Готово: карточки заявок теперь падают {where}.\n"
        f"Чат <code>{chat.id}</code>"
        + (f", тема <code>{thread}</code>." if thread else ".")
    )
