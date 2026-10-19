"""Выгрузка контактов: аудитория для ретаргета в Meta и упрощённый список.

Два формата, потому что задачи разные:

* **Meta value-based audience** — ровно те колонки, что ждёт Ads Manager
  (``email×3, phone×3, madid, fn, ln, zip, ct, st, country, dob, doby, gen, age,
  uid, value``). Колонка ``value`` — сумма оплаченных заказов клиента: по ней
  Meta ищет похожих на ТЕХ, КТО ПЛАТИТ, а не на всех подряд.
* **Упрощённый** — имя, телефон, город/регион, контекст диалога: файл, который
  открывают в Excel и звонят по нему руками.

Города в базе не хранятся: их вытаскиваем из переписки — сначала словарём (быстро
и бесплатно), затем, по желанию, добиваем моделью для тех, кого словарь не узнал.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, Order, User
from app.db.models.enums import Channel, OrderStatus
from app.sales.llm import LLMError, LLMNotConfigured, chat_completion
from app.services.chat_report import business_tz_offset, period_bounds
from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)

# Города Казахстана → (город, регион). Регион нужен колонке ``st`` в шаблоне Meta.
KZ_CITIES: dict[str, tuple[str, str]] = {
    "алматы": ("Алматы", "Алматы"),
    "алмата": ("Алматы", "Алматы"),
    "астана": ("Астана", "Астана"),
    "нур-султан": ("Астана", "Астана"),
    "нурсултан": ("Астана", "Астана"),
    "шымкент": ("Шымкент", "Шымкент"),
    "павлодар": ("Павлодар", "Павлодарская область"),
    "экибастуз": ("Экибастуз", "Павлодарская область"),
    "аксу": ("Аксу", "Павлодарская область"),
    "караганда": ("Караганда", "Карагандинская область"),
    "темиртау": ("Темиртау", "Карагандинская область"),
    "балхаш": ("Балхаш", "Карагандинская область"),
    "жезказган": ("Жезказган", "Улытауская область"),
    "актобе": ("Актобе", "Актюбинская область"),
    "актау": ("Актау", "Мангистауская область"),
    "жанаозен": ("Жанаозен", "Мангистауская область"),
    "атырау": ("Атырау", "Атырауская область"),
    "уральск": ("Уральск", "Западно-Казахстанская область"),
    "костанай": ("Костанай", "Костанайская область"),
    "рудный": ("Рудный", "Костанайская область"),
    "кокшетау": ("Кокшетау", "Акмолинская область"),
    "петропавловск": ("Петропавловск", "Северо-Казахстанская область"),
    "семей": ("Семей", "Абайская область"),
    "усть-каменогорск": ("Усть-Каменогорск", "Восточно-Казахстанская область"),
    "оскемен": ("Усть-Каменогорск", "Восточно-Казахстанская область"),
    "риддер": ("Риддер", "Восточно-Казахстанская область"),
    "тараз": ("Тараз", "Жамбылская область"),
    "талдыкорган": ("Талдыкорган", "Жетысуская область"),
    "кызылорда": ("Кызылорда", "Кызылординская область"),
    "туркестан": ("Туркестан", "Туркестанская область"),
    "кентау": ("Кентау", "Туркестанская область"),
    "капшагай": ("Капшагай", "Алматинская область"),
    "конаев": ("Конаев", "Алматинская область"),
    "степногорск": ("Степногорск", "Акмолинская область"),
}


def _city_regex(name: str) -> re.Pattern[str]:
    """Шаблон города, устойчивый к падежам: «в Астану», «в Караганде», «до Костаная».

    Ищем по основе слова: у длинных названий отбрасываем конечную -а/-о/-й и
    допускаем до двух букв окончания. Короткие (Аксу, Актау) не режем — от них
    осталась бы слишком общая основа, дающая ложные срабатывания.
    """
    n = name.lower()
    stem = n[:-1] if (len(n) > 5 and n[-1] in "аой") else n
    return re.compile(rf"(?<![а-яёa-z]){re.escape(stem)}[а-яё]{{0,2}}(?![а-яёa-z])")


_CITY_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (_city_regex(needle), city, region) for needle, (city, region) in KZ_CITIES.items()
]


def detect_city(text: str) -> tuple[str, str]:
    """Ищет город Казахстана в тексте переписки. Возвращает (город, регион) или ('', '')."""
    low = (text or "").lower()
    for pattern, city, region in _CITY_PATTERNS:
        if pattern.search(low):
            return city, region
    return "", ""


def normalize_phone(raw: str | None) -> str:
    """Приводит телефон к международному виду: Meta сама хеширует, но формат должен быть чистым."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if len(digits) == 10:  # 7071234567
        digits = "7" + digits
    if len(digits) == 11 and digits[0] == "8":  # 8707...
        digits = "7" + digits[1:]
    return "+" + digits


def _display_name(u: User) -> tuple[str, str]:
    first = (u.first_name or "").strip()
    last = (u.last_name or "").strip()
    if not first and not last and u.username:
        first = u.username.strip()
    return first, last


def phone_of(u: User) -> str:
    """Телефон клиента: из диалога, а если там пусто — из идентификатора канала.

    В WhatsApp Cloud API идентификатор пользователя (``wa_id``) И ЕСТЬ его номер
    в международном формате без плюса. Поле ``phone`` заполняется, только когда
    клиент продиктовал номер сам, поэтому опора на него теряла почти всю базу:
    из 141 переписки в WhatsApp номер «находился» у 11.
    """
    explicit = normalize_phone(u.phone)
    if explicit:
        return explicit
    if u.channel == Channel.WHATSAPP:
        raw = str(u.external_id or "").strip()
        # 10–15 цифр — диапазон номера по E.164; всё прочее не телефон
        if raw.isdigit() and 10 <= len(raw) <= 15:
            return normalize_phone(raw)
    return ""


def account_of(u: User) -> str:
    """Аккаунт в канале — единственный контакт для Instagram и Telegram, где телефона нет."""
    uname = (u.username or "").strip()
    if uname:
        return uname if uname.startswith("@") else "@" + uname
    ext = str(u.external_id or "").strip()
    if not ext:
        return ""
    if u.channel == Channel.TELEGRAM:
        return f"tg:{ext}"
    if u.channel == Channel.INSTAGRAM:
        return f"ig:{ext}"
    return ext


async def collect_contacts(
    session: AsyncSession,
    *,
    period_from: date | None = None,
    period_to: date | None = None,
    only_with_phone: bool = True,
    user_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Собирает контакты с городом, ценностью и контекстом диалога.

    ``user_ids`` позволяет выгрузить не всех, а конкретный сегмент из отчёта.
    """
    q = select(User)
    if user_ids:
        q = q.where(User.id.in_(user_ids))
    users = list((await session.execute(q)).scalars())

    dt_from, dt_to = (
        period_bounds(period_from, period_to, await business_tz_offset(session))
        if period_from and period_to
        else (None, None)
    )

    # Переписку берём ОДНИМ запросом на всех и раскладываем по клиентам.
    # Запрос на каждого клиента отдельно превращал выгрузку 166 контактов
    # в 166 обращений к базе — SQLite с единственным писателем это душит.
    mq = select(Message).where(Message.user_id.in_([u.id for u in users])).order_by(Message.id)
    if dt_from and dt_to:
        mq = mq.where(Message.created_at >= dt_from, Message.created_at <= dt_to)
    msgs_by_user: dict[int, list[Message]] = {}
    if users:
        for m in (await session.execute(mq)).scalars():
            msgs_by_user.setdefault(m.user_id, []).append(m)

    # Оплаченные заказы → ценность клиента для value-based аудитории
    paid_rows = list(
        (
            await session.execute(
                select(Order.user_id, Order.amount_kzt).where(
                    Order.status.in_([OrderStatus.PAID, OrderStatus.FULFILLED])
                )
            )
        ).all()
    )
    value_by_user: dict[int, int] = {}
    for uid, amount in paid_rows:
        value_by_user[uid] = value_by_user.get(uid, 0) + int(amount or 0)

    contacts: list[dict[str, Any]] = []
    for u in users:
        phone = phone_of(u)
        account = account_of(u)
        # Для файла Meta нужен телефон; для списка на обзвон достаточно любого
        # контакта — у Instagram и Telegram это аккаунт.
        if only_with_phone and not phone:
            continue
        if not only_with_phone and not phone and not account:
            continue

        msgs = msgs_by_user.get(u.id, [])
        if dt_from and not msgs:  # в выбранный период человек не писал
            continue

        client_text = "\n".join(
            (m.content or "") for m in msgs if m.role.value == "user"
        )
        city, region = detect_city(client_text)

        first, last = _display_name(u)
        contacts.append(
            {
                "user_id": u.id,
                "first_name": first,
                "last_name": last,
                "phone": phone,
                "account": account,
                "email": (u.email or "").strip(),
                "city": city,
                "region": region,
                "channel": u.channel.value,
                "source": u.source or "",
                "stage": u.stage or "",
                "birthday": u.birthday,
                "value": value_by_user.get(u.id, 0),
                "messages": len(msgs),
                # Контекст без модели: первая и последняя реплики клиента
                "context": _plain_context(msgs),
            }
        )
    return contacts


def _plain_context(msgs: list[Message]) -> str:
    """Контекст диалога без обращения к модели — по крайним репликам клиента."""
    client = [(m.content or "").strip() for m in msgs if m.role.value == "user"]
    client = [c for c in client if c]
    if not client:
        return ""
    if len(client) == 1:
        return _cut(client[0], 180)
    return f"{_cut(client[0], 90)} … {_cut(client[-1], 90)}"


def _cut(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


async def enrich_with_llm(
    session: AsyncSession, contacts: list[dict[str, Any]], *, limit: int = 60
) -> tuple[int, str | None]:
    """Одним запросом уточняет город и человекочитаемый контекст сразу для пачки контактов.

    Возвращает (сколько уточнено, ошибка). Пачкой, а не по одному, — иначе на
    сотне контактов это сотня запросов к модели.
    """
    targets = [c for c in contacts if c.get("context")][:limit]
    if not targets:
        return 0, None

    llm_cfg = await get_setting(session, "llm") or {}
    if not (llm_cfg.get("base_url") and llm_cfg.get("api_key")):
        return 0, "LLM не настроен — выгружаю контекст без уточнения моделью."

    payload = [
        {"id": c["user_id"], "city_guess": c["city"], "dialog": _cut(c["context"], 400)}
        for c in targets
    ]
    try:
        msg = await chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты обрабатываешь заявки клиентов компании. Для каждого диалога "
                        "верни город клиента (если он назван или явно следует из текста) и "
                        "одну фразу — суть запроса, по которой менеджер поймёт, о чём был "
                        "разговор. Отвечай СТРОГО JSON-массивом: "
                        '[{"id": 1, "city": "Павлодар", "context": "Спрашивал цену и условия"}]. '
                        "Если город не назван — пустая строка."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            tools=None,
            config=llm_cfg,
        )
    except (LLMError, LLMNotConfigured) as e:
        logger.warning("enrich_with_llm failed: %s", e)
        return 0, f"Модель недоступна ({e}) — выгружаю контекст как есть."

    text = (msg.get("content") or "").strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return 0, "Модель вернула неожиданный формат — выгружаю контекст как есть."
    try:
        rows = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return 0, "Модель вернула неразбираемый JSON — выгружаю контекст как есть."

    by_id = {c["user_id"]: c for c in contacts}
    changed = 0
    for row in rows if isinstance(rows, list) else []:
        c = by_id.get(row.get("id"))
        if not c:
            continue
        city = str(row.get("city") or "").strip()
        context = str(row.get("context") or "").strip()
        if city and not c["city"]:
            c["city"] = city
            c["region"] = KZ_CITIES.get(city.lower(), ("", ""))[1]
        if context:
            c["context"] = context
        changed += 1
    return changed, None


META_HEADER = [
    "email", "email", "email",
    "phone", "phone", "phone",
    "madid", "fn", "ln", "zip", "ct", "st", "country",
    "dob", "doby", "gen", "age", "uid", "value",
]


def meta_audience_csv(contacts: list[dict[str, Any]]) -> str:
    """CSV ровно по шаблону value-based аудитории Meta.

    Без BOM: Ads Manager читает файл как UTF-8, лишняя метка ломает первую колонку.
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(META_HEADER)
    today = date.today()
    for c in contacts:
        bday = c.get("birthday")
        dob = bday.strftime("%m/%d/%y") if isinstance(bday, date) else ""
        doby = bday.strftime("%Y") if isinstance(bday, date) else ""
        age = str(today.year - bday.year) if isinstance(bday, date) else ""
        w.writerow(
            [
                c.get("email", ""), "", "",          # email ×3 — у нас максимум один
                c.get("phone", ""), "", "",          # phone ×3
                "",                                   # madid: мобильного рекламного id нет
                c.get("first_name", ""),
                c.get("last_name", ""),
                "",                                   # zip: индексы не собираем
                c.get("city", ""),
                c.get("region", ""),
                "KZ",
                dob, doby,
                "",                                   # gen: пол не спрашиваем и не угадываем
                age,
                c.get("user_id", ""),
                c.get("value", 0),
            ]
        )
    return buf.getvalue()


def simple_csv(contacts: list[dict[str, Any]]) -> str:
    """Упрощённая выгрузка: имя, телефон, город/регион, контекст диалога.

    С BOM — иначе Excel открывает кириллицу «кракозябрами».
    """
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(
        [
            "Имя",
            "Телефон",
            "Аккаунт",  # для Instagram и Telegram это единственный контакт
            "Город",
            "Регион",
            "Контекст диалога",
            "Канал",
            "Оплачено, ₸",
        ]
    )
    for c in contacts:
        name = " ".join(x for x in [c.get("first_name", ""), c.get("last_name", "")] if x)
        w.writerow(
            [
                name,
                c.get("phone", ""),
                c.get("account", ""),
                c.get("city", ""),
                c.get("region", ""),
                c.get("context", ""),
                c.get("channel", ""),
                c.get("value", 0),
            ]
        )
    return "﻿" + buf.getvalue()


def export_filename(prefix: str, ext: str = "csv") -> str:
    return f"{prefix}_{datetime.now():%Y-%m-%d}.{ext}"
