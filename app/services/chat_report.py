"""Аналитический отчёт по всем чатам за период.

Разделение труда здесь принципиальное:

* **Python считает** — диалоги, лиды, конверсию, время ответа, обрывы. Числа
  должны сходиться с базой, поэтому LLM их не выводит и не «оценивает».
* **LLM читает** — транскрипты, и пишет качественную часть: где бот теряет
  клиента, что переписать в скрипте, какие настройки поменять.

Предложения модели никогда не применяются сами: :func:`apply_report` — отдельное
действие, которое снимает бэкап прежних значений, поэтому любое изменение
откатывается одной кнопкой (:func:`rollback_report`).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatReport, Message, Order, User
from app.db.models.enums import OrderStatus
from app.sales.llm import LLMError, LLMNotConfigured, chat_completion
from app.services.settings_service import get_setting, set_setting

logger = logging.getLogger(__name__)

_ROLE_LABELS = {"user": "Клиент", "assistant": "Бот", "operator": "Оператор"}

# Просим API вернуть синтаксически валидный JSON. На длинных разборах модель иначе
# добавляет пояснения вокруг ответа или ставит сырые переводы строк внутри значений —
# и весь разбор пропадает из-за неразобранной строки.
_JSON_MODE = {"type": "json_object"}

# Настройки, которые модели РАЗРЕШЕНО предлагать к изменению. Всё остальное —
# токены, ключи LLM, доступы Meta, id чатов — не должно попадать под автоправку
# ни при какой формулировке ответа модели.
ALLOWED_SETTING_KEYS = {
    "welcome_text",
    "persona_prompt",
    "sales_script",
    "followup",
    "loyalty",
}
# Внутри вложенных настроек — тоже только безопасные подключи
ALLOWED_SUBKEYS = {
    "followup": {
        "silence_enabled",
        "silence_steps",
        "silence_hours",
        "silence_interval_hours",
        "silence_texts",
        "think_default_hours",
    },
    "loyalty": {"birthday_enabled", "birthday_text", "checkin_text"},
}

MAP_SYSTEM = (
    "Ты — аналитик отдела продаж. Тебе дают НЕСКОЛЬКО реальных диалогов клиентов с "
    "ботом-продавцом одной компании. Изучи КАЖДЫЙ диалог и верни по нему компактную "
    "сводку. Ничего не выдумывай: если чего-то в диалоге нет — оставь поле пустым.\n"
    "Ответь СТРОГО одним JSON-объектом без пояснений:\n"
    '{"items": [{'
    '"id": <число из заголовка [диалог #N]>, '
    '"outcome": "купил|заявка|тёплый|холодный|молчит|нецелевой", '
    '"city": "<город/регион клиента, если назван, иначе пусто>", '
    '"topic": "<что интересовало, 3-6 слов>", '
    '"objection": "<главное возражение/сомнение клиента, кратко, или пусто>", '
    '"issue": "<где бот ошибся, затупил или упустил клиента, кратко, или пусто>", '
    '"quote": "<короткая ДОСЛОВНАЯ цитата из диалога, иллюстрирующая проблему, или пусто>"'
    "}]}"
)

# Бот отвечает клиентам голосовыми: ElevenLabs читает текст буквально, поэтому «216 ₸/шт»
# звучит как «двести шестнадцать тенге СЛЭШ шэ-тэ». normalize_for_tts() чистит известные
# символы уже на выходе, но модель, переписывая скрипт, изобретает новые сокращения, для
# которых правила ещё нет — дешевле запретить их на входе, чем гоняться на выходе.
VOICE_RULES = (
    "ОЗВУЧКА. Бот отвечает клиентам ГОЛОСОВЫМИ сообщениями — синтезатор читает текст "
    "буквально, символ за символом. Поэтому и сам скрипт, и любые тексты, которые уходят "
    "клиенту ДОСЛОВНО (welcome_text, silence_texts, birthday_text, checkin_text), пиши "
    "произносимыми:\n"
    "- единицы и валюту — словами: «тенге» вместо ₸, «за штуку» вместо /шт, «процентов» "
    "вместо %, «на» вместо × в размерах, «квадратных метров» вместо м², «кубометр» вместо м3;\n"
    "- без сокращений: шт., кг, мм, см, т.д., т.п. — только полные слова;\n"
    "- без символов / ₸ % № @ и без markdown-разметки (звёздочки, решётки, таблицы);\n"
    "- ссылки и адреса почты в произносимый текст не вставляй: бот отправляет их отдельным "
    "текстовым сообщением, вслух они не читаются;\n"
    "- короткие предложения, как говорят вслух, а не пункты списка через тире.\n"
    "В самом sales_script продублируй это правило как инструкцию боту — иначе он снова "
    "начнёт писать цены значками."
)

REPORT_SYSTEM = (
    "Ты — руководитель отдела продаж, который разбирает работу бота с клиентами. "
    "Менеджер — это ИИ-бот компании (описание бизнеса и товары — в блоке ниже). "
    "Тебе дают посчитанные метрики за период и СВОДКИ ПО ВСЕМ диалогам (с цитатами).\n\n"
    "Твоя задача — найти, где бот теряет деньги, и предложить конкретные правки.\n"
    "Требования к разбору:\n"
    "1. Опирайся на факты: в каждой находке приводи короткую цитату из предоставленных сводок.\n"
    "2. Не пересчитывай метрики — они уже посчитаны и верны, используй их как факт.\n"
    "3. Формулируй правки так, чтобы их можно было применить дословно, без додумывания.\n"
    "4. Если данных мало для вывода — так и напиши, не выдумывай.\n\n"
    "Верни СТРОГО один JSON-объект без пояснений вокруг:\n"
    "{\n"
    '  "summary": "3-6 предложений: что происходит с продажами и главный вывод",\n'
    '  "findings": [{"title": "...", "severity": "high|medium|low", '
    '"evidence": "цитата из диалога", "recommendation": "что сделать"}],\n'
    '  "sales_script": "полный текст улучшенного скрипта продаж (или пустая строка)",\n'
    '  "settings": {"welcome_text": "...", "followup": {"silence_hours": 3}},\n'
    '  "segments": [{"name": "Отвалились на цене", "criteria": "признак", '
    '"user_ids": [1, 2]}]\n'
    "}\n"
    "В settings клади ТОЛЬКО те ключи, которые реально предлагаешь изменить.\n\n" + VOICE_RULES
)


def _naive(dt: datetime | None) -> datetime | None:
    """SQLite возвращает наивные datetime, Postgres — с зоной. Приводим к одному виду."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def period_bounds(
    period_from: date, period_to: date, tz_offset_hours: float = 0.0
) -> tuple[datetime, datetime]:
    """Границы периода включительно, приведённые к UTC.

    В базе время хранится НАИВНЫМ UTC (``func.now()`` у SQLite), а даты в форме
    владелец выбирает по своему часовому поясу. Без поправки отчёт «за сегодня»
    на UTC+5 брал окно 05:00 сегодня — 04:59 завтра: ночные переписки уезжали
    в соседние сутки, а метрики за день не сходились с тем, что видно в «Диалогах».
    """
    delta = timedelta(hours=float(tz_offset_hours or 0))
    return (
        datetime.combine(period_from, time.min) - delta,
        datetime.combine(period_to, time.max) - delta,
    )


def _tz_offset_from_settings() -> float:
    """Смещение из настроенного часового пояса приложения (config ``timezone``), в часах.

    Так отчёт «за сегодня» корректен из коробки: явный ``tz_offset_hours`` задавать не
    нужно — берём его из зоны бизнеса (Asia/Almaty = +5). Fallback 0 при незнакомой зоне.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from app.config import get_settings

    try:
        off = _dt.now(ZoneInfo(get_settings().timezone)).utcoffset()
        return off.total_seconds() / 3600 if off else 0.0
    except Exception:  # noqa: BLE001 — незнакомая/битая зона → без поправки
        return 0.0


async def business_tz_offset(session: AsyncSession) -> float:
    """Часовой пояс бизнеса как смещение в часах (Казахстан = UTC+5, без перехода).

    Приоритет у явной настройки ``tz_offset_hours``; если её нет — берём из
    сконфигурированной зоны приложения, чтобы границы периода были верны без ручной настройки.
    """
    explicit = await get_setting(session, "tz_offset_hours")
    if explicit is not None:
        try:
            return float(explicit)
        except (TypeError, ValueError):
            pass
    return _tz_offset_from_settings()


async def collect_metrics(
    session: AsyncSession, dt_from: datetime, dt_to: datetime
) -> tuple[dict[str, Any], list[int]]:
    """Считает жёсткие метрики за период. Возвращает (метрики, id пользователей с диалогами)."""
    msgs = list(
        (
            await session.execute(
                select(Message)
                .where(Message.created_at >= dt_from, Message.created_at <= dt_to)
                .order_by(Message.user_id, Message.id)
            )
        ).scalars()
    )

    by_user: dict[int, list[Message]] = {}
    for m in msgs:
        by_user.setdefault(m.user_id, []).append(m)

    roles = Counter(m.role.value for m in msgs)

    # Время ответа: от реплики клиента до ближайшего ответа бота в том же диалоге
    reply_gaps: list[float] = []
    unanswered = 0
    for items in by_user.values():
        for i, m in enumerate(items):
            if m.role.value != "user":
                continue
            nxt = next(
                (x for x in items[i + 1 :] if x.role.value in ("assistant", "operator")),
                None,
            )
            if nxt is not None:
                a, b = _naive(m.created_at), _naive(nxt.created_at)
                if a and b and b >= a:
                    reply_gaps.append((b - a).total_seconds())
        if items and items[-1].role.value == "user":
            unanswered += 1

    user_ids = list(by_user.keys())
    users = (
        list((await session.execute(select(User).where(User.id.in_(user_ids)))).scalars())
        if user_ids
        else []
    )
    new_leads = sum(1 for u in users if dt_from <= (_naive(u.created_at) or dt_from) <= dt_to)

    orders = list(
        (
            await session.execute(
                select(Order).where(Order.created_at >= dt_from, Order.created_at <= dt_to)
            )
        ).scalars()
    )
    paid = [o for o in orders if o.status in (OrderStatus.PAID, OrderStatus.FULFILLED)]
    revenue = sum(int(o.amount_kzt or 0) for o in paid)

    dialogs = len(by_user)
    metrics = {
        "dialogs": dialogs,
        "messages": len(msgs),
        "messages_client": roles.get("user", 0),
        "messages_bot": roles.get("assistant", 0),
        "messages_operator": roles.get("operator", 0),
        "new_leads": new_leads,
        "with_phone": sum(1 for u in users if (u.phone or "").strip()),
        "orders": len(orders),
        "orders_paid": len(paid),
        "revenue_kzt": revenue,
        # Доля диалогов, дошедших до оплаты
        "conversion_pct": round(len(paid) / dialogs * 100, 1) if dialogs else 0.0,
        # Доля диалогов, где последним написал клиент и никто не ответил
        "unanswered_dialogs": unanswered,
        "unanswered_pct": round(unanswered / dialogs * 100, 1) if dialogs else 0.0,
        "avg_reply_seconds": round(sum(reply_gaps) / len(reply_gaps)) if reply_gaps else 0,
        "by_channel": dict(Counter(u.channel.value for u in users)),
        "by_stage": dict(Counter((u.stage or "—") for u in users)),
        "by_source": dict(Counter((u.source or "—") for u in users)),
    }
    return metrics, user_ids


_MAP_BATCH = 8  # диалогов на один map-вызов (сжатие каждого чата в сводку)


async def _one_transcript(
    session: AsyncSession, uid: int, dt_from: datetime, dt_to: datetime
) -> str:
    """Транскрипт одного диалога за период (последние реплики). Пусто, если касание короткое."""
    msgs = list(
        (
            await session.execute(
                select(Message)
                .where(
                    Message.user_id == uid,
                    Message.created_at >= dt_from,
                    Message.created_at <= dt_to,
                )
                .order_by(Message.id)
            )
        ).scalars()
    )
    if len(msgs) < 2:  # односложные касания не несут смысла для разбора
        return ""
    lines = [
        f"{_ROLE_LABELS.get(m.role.value, m.role.value)}: {(m.content or '').strip()}"
        for m in msgs[-40:]
    ]
    return "\n".join(lines)


async def _map_dialogs(
    session: AsyncSession,
    llm_cfg: dict,
    user_ids: list[int],
    dt_from: datetime,
    dt_to: datetime,
    *,
    cap: int,
) -> list[dict[str, Any]]:
    """MAP: сжимает КАЖДЫЙ диалог в сводку, чтобы модель изучила ВСЕ переписки, а не выборку.

    Идёт батчами (по ``_MAP_BATCH`` диалогов на вызов) — так на сотне чатов это десяток
    дешёвых запросов вместо одного гигантского, который не влезет в контекст. Сбой батча
    не роняет разбор: пропускаем и идём дальше. ``cap`` — потолок-предохранитель.
    """
    prepared: list[tuple[int, str]] = []
    for uid in user_ids:
        tx = await _one_transcript(session, uid, dt_from, dt_to)
        if tx:
            prepared.append((uid, tx))
        if len(prepared) >= cap:
            break

    summaries: list[dict[str, Any]] = []
    for i in range(0, len(prepared), _MAP_BATCH):
        batch = prepared[i : i + _MAP_BATCH]
        blocks = "\n\n".join(f"[диалог #{uid}]\n{tx}" for uid, tx in batch)
        user_msg = blocks + "\n\nВерни JSON-сводку по каждому диалогу."
        try:
            msg = await chat_completion(
                messages=[
                    {"role": "system", "content": MAP_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                tools=None,
                config=llm_cfg,
                response_format=_JSON_MODE,
            )
        except (LLMError, LLMNotConfigured) as e:
            logger.warning("chat_report map batch failed: %s", e)
            continue
        data = _parse_json_block(msg.get("content") or "")
        items = data.get("items")
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("id") is not None:
                summaries.append(item)
    return summaries


def _summaries_digest(summaries: list[dict[str, Any]]) -> str:
    """Компактный дайджест всех сводок для reduce-шага: одна строка на диалог, с цитатой."""
    lines = []
    for s in summaries:
        parts = [f"#{s.get('id')}", f"[{s.get('outcome', '?')}]"]
        if s.get("topic"):
            parts.append(f"тема: {_strip(str(s['topic']))}")
        if s.get("objection"):
            parts.append(f"возражение: {_strip(str(s['objection']))}")
        if s.get("issue"):
            parts.append(f"ошибка бота: {_strip(str(s['issue']))}")
        if s.get("quote"):
            parts.append(f"цитата: «{_strip(str(s['quote']))}»")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _strip(text: str) -> str:
    return " ".join((text or "").split())


def _repair_json(text: str) -> str:
    """Чинит две ошибки, которые модель допускает в длинных ответах.

    1. Сырой перевод строки внутри строкового значения — по стандарту он там
       запрещён и обязан быть ``\\n``. Модель почти всегда так пишет многострочный
       скрипт продаж, и на этом разбор падал.
    2. Висящая запятая перед закрывающей скобкой.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch in "\n\r":
            out.append("\\n")  # перевод строки внутри значения — экранируем
            continue
        if in_string and ch == "\t":
            out.append("\\t")
            continue
        out.append(ch)
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _parse_json_block(content: str) -> dict[str, Any]:
    """Достаёт JSON из ответа модели, даже если он завёрнут в ```json ... ```."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.strip("`")
        text = text[4:].strip() if text.lower().startswith("json") else text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    chunk = text[start : end + 1]
    try:
        data = json.loads(chunk)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as first:
        # Вторая попытка: чиним типовые огрехи модели вместо того, чтобы терять
        # весь разбор (десятки запросов к модели) из-за одного сырого перевода строки.
        try:
            data = json.loads(_repair_json(chunk))
            logger.info("chat_report: JSON разобран после починки (%s)", first.msg)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as second:
            # В лог — окрестности места слома, чтобы причина была видна сразу,
            # а не выяснялась воспроизведением на бою.
            pos = getattr(second, "pos", 0) or 0
            logger.warning(
                "chat_report: модель вернула не-JSON (%s, поз. %s из %s). Рядом: …%s…",
                second.msg,
                pos,
                len(chunk),
                chunk[max(0, pos - 120) : pos + 120].replace("\n", "\\n"),
            )
            return {}


def _looks_like_report(data: dict[str, Any]) -> bool:
    """Отсекает ответ модели неправильной формы.

    Без этой проверки ответ вида {"findings": "всё хорошо", "settings": "нет"}
    проходил дальше: списки и словари молча заменялись пустыми, отчёт сохранялся
    и выглядел успешным, хотя не содержал ни находок, ни предложений.
    """
    if not isinstance(data.get("summary", ""), str):
        return False
    for key, want in (("findings", list), ("segments", list), ("settings", dict)):
        value = data.get(key)
        if value is not None and not isinstance(value, want):
            return False
    # осмысленным считаем ответ, где есть хотя бы сводка или находки
    return bool(str(data.get("summary") or "").strip()) or bool(data.get("findings"))


def sanitize_settings(proposed: Any) -> dict[str, Any]:
    """Оставляет только те настройки, которые модели разрешено менять.

    Без этого фильтра формулировка вроде «поменяй llm.api_key» из ответа модели
    попала бы в применение изменений.
    """
    if not isinstance(proposed, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, value in proposed.items():
        if key not in ALLOWED_SETTING_KEYS:
            continue
        allowed_sub = ALLOWED_SUBKEYS.get(key)
        if allowed_sub and isinstance(value, dict):
            sub = {k: v for k, v in value.items() if k in allowed_sub}
            if sub:
                clean[key] = sub
        elif not allowed_sub and isinstance(value, (str, int, float, bool)):
            clean[key] = value
    return clean


# Потолок-предохранитель: сколько диалогов максимум отдаём модели на разбор. Не «выборка»,
# а защита от разового прогона на десятках тысяч чатов. Обычный период — сотни, влезает.
_MAX_DIALOGS_HARD = 800


async def create_running_report(
    session: AsyncSession, *, period_from: date, period_to: date
) -> ChatReport:
    """Создаёт запись отчёта в статусе «running», чтобы страница сразу показала «идёт анализ»,
    пока фоновая задача разбирает все диалоги (это десятки LLM-вызовов и минуты)."""
    report = ChatReport(period_from=period_from, period_to=period_to, status="running")
    session.add(report)
    await session.flush()
    return report


def _fail(report: ChatReport, message: str) -> str:
    report.status = "error"
    report.summary = message
    return message


async def run_report_into(
    session: AsyncSession, report: ChatReport, *, max_dialogs: int = _MAX_DIALOGS_HARD
) -> str | None:
    """MAP-REDUCE разбор ВСЕХ диалогов за период → заполняет переданную запись отчёта.

    MAP: модель сжимает каждый чат в сводку (все переписки, не выборка). REDUCE: из всех
    сводок + точных метрик собирает отчёт и предложения. Возвращает текст ошибки или None.
    """
    offset = await business_tz_offset(session)
    dt_from, dt_to = period_bounds(report.period_from, report.period_to, offset)
    metrics, user_ids = await collect_metrics(session, dt_from, dt_to)
    if not user_ids:
        return _fail(report, "За выбранный период переписки нет — анализировать нечего.")

    llm_cfg = await get_setting(session, "llm") or {}
    if not (llm_cfg.get("base_url") and llm_cfg.get("api_key")):
        return _fail(report, "LLM не настроен — укажи base_url и ключ в ⚙️ Настройки.")

    # MAP — модель читает КАЖДЫЙ диалог.
    summaries = await _map_dialogs(
        session, llm_cfg, user_ids, dt_from, dt_to, cap=min(max_dialogs, _MAX_DIALOGS_HARD)
    )
    if not summaries:
        return _fail(report, "Диалоги за период слишком короткие для разбора.")

    current_script = await get_setting(session, "sales_script") or ""
    current_welcome = await get_setting(session, "welcome_text") or ""
    current_followup = await get_setting(session, "followup") or {}
    # Business identity so the analysis fits THIS company (school, brick supplier, …).
    shop = await get_setting(session, "shop_name") or ""
    persona_brief = (await get_setting(session, "persona_prompt") or "")[:600]

    user_msg = (
        f"БИЗНЕС: {shop}\n{persona_brief}\n\n"
        f"Период: {report.period_from:%d.%m.%Y} — {report.period_to:%d.%m.%Y}\n\n"
        f"ПОСЧИТАННЫЕ МЕТРИКИ (это факт, не пересчитывай):\n"
        f"{json.dumps(metrics, ensure_ascii=False, indent=1)}\n\n"
        f"ТЕКУЩИЙ СКРИПТ ПРОДАЖ:\n{current_script}\n\n"
        f"ТЕКУЩЕЕ ПРИВЕТСТВИЕ:\n{current_welcome}\n\n"
        f"ТЕКУЩИЕ ДОЖИМЫ:\n{json.dumps(current_followup, ensure_ascii=False)}\n\n"
        f"=== СВОДКИ ПО ВСЕМ ДИАЛОГАМ ({len(summaries)} шт.) ===\n"
        + _summaries_digest(summaries)
    )

    # REDUCE — один вызов синтезирует отчёт из всех сводок.
    try:
        msg = await chat_completion(
            messages=[
                {"role": "system", "content": REPORT_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            tools=None,
            config=llm_cfg,
            response_format=_JSON_MODE,
        )
    except (LLMError, LLMNotConfigured) as e:
        logger.warning("run_report_into: reduce LLM call failed: %s", e)
        return _fail(report, f"Ошибка обращения к модели: {e}")

    data = _parse_json_block(msg.get("content") or "")
    if not data or not _looks_like_report(data):
        return _fail(report, "Модель вернула ответ в неожиданном формате — попробуй ещё раз.")

    findings = data.get("findings")
    segments = data.get("segments")
    report.dialogs_analyzed = len(summaries)
    report.messages_analyzed = metrics["messages"]
    report.metrics = metrics
    report.summary = str(data.get("summary") or "").strip()
    report.findings = findings if isinstance(findings, list) else []
    report.proposals = {
        "sales_script": str(data.get("sales_script") or "").strip(),
        "settings": sanitize_settings(data.get("settings")),
        "segments": segments if isinstance(segments, list) else [],
    }
    report.status = "ready"
    await session.flush()
    return None


async def generate_report(
    session: AsyncSession,
    *,
    period_from: date,
    period_to: date,
    max_dialogs: int = _MAX_DIALOGS_HARD,
) -> tuple[ChatReport | None, str | None]:
    """Синхронный разбор (для тестов и мелких прогонов): создаёт запись и заполняет её.

    В проде запуск идёт в фоне (см. роут): create_running_report + run_report_into в
    отдельной задаче, чтобы длинный разбор не упёрся в таймаут прокси.
    """
    report = await create_running_report(session, period_from=period_from, period_to=period_to)
    error = await run_report_into(session, report, max_dialogs=max_dialogs)
    if error:
        await session.delete(report)  # не оставляем пустую запись в синхронном пути
        await session.flush()
        return None, error
    return report, None


async def apply_report(session: AsyncSession, report_id: int) -> tuple[bool, str]:
    """Применяет предложенные скрипт и настройки, сняв бэкап прежних значений."""
    report = await session.get(ChatReport, report_id)
    if report is None:
        return False, "Отчёт не найден."
    if report.status == "applied":
        return False, "Изменения по этому отчёту уже внесены."

    proposals = report.proposals or {}
    script = (proposals.get("sales_script") or "").strip()
    settings = sanitize_settings(proposals.get("settings"))
    if not script and not settings:
        return False, "В отчёте нет предложений, которые можно применить."

    backup: dict[str, Any] = {}
    if script:
        backup["sales_script"] = await get_setting(session, "sales_script")
        await set_setting(session, "sales_script", script)
    for key, value in settings.items():
        current = await get_setting(session, key)
        backup[key] = current
        if isinstance(current, dict) and isinstance(value, dict):
            merged = dict(current)
            merged.update(value)
            await set_setting(session, key, merged)
        else:
            await set_setting(session, key, value)

    report.backup = backup
    report.status = "applied"
    report.applied_at = datetime.now(UTC)
    await session.flush()
    changed = ", ".join(([" скрипт продаж"] if script else []) + list(settings.keys()))
    return True, f"Изменения внесены:{changed}. Откат доступен здесь же."


async def rollback_report(session: AsyncSession, report_id: int) -> tuple[bool, str]:
    """Возвращает настройки к значениям до применения отчёта."""
    report = await session.get(ChatReport, report_id)
    if report is None:
        return False, "Отчёт не найден."
    if report.status != "applied" or not report.backup:
        return False, "Откатывать нечего — изменения не применялись."

    for key, value in (report.backup or {}).items():
        await set_setting(session, key, value)
    report.status = "rolled_back"
    await session.flush()
    return True, "Прежние настройки возвращены."


async def list_reports(session: AsyncSession, limit: int = 30) -> list[ChatReport]:
    return list(
        (
            await session.execute(
                select(ChatReport).order_by(ChatReport.id.desc()).limit(limit)
            )
        ).scalars()
    )


async def count_dialogs_in_period(
    session: AsyncSession, period_from: date, period_to: date
) -> int:
    """Сколько диалогов попадёт в разбор — показываем до запуска, чтобы не гадать."""
    dt_from, dt_to = period_bounds(period_from, period_to, await business_tz_offset(session))
    return int(
        (
            await session.execute(
                select(func.count(func.distinct(Message.user_id))).where(
                    Message.created_at >= dt_from, Message.created_at <= dt_to
                )
            )
        ).scalar()
        or 0
    )


def default_period() -> tuple[date, date]:
    """Последние 30 дней — разумное окно по умолчанию."""
    today = date.today()
    return today - timedelta(days=30), today
