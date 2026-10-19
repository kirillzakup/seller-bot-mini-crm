"""The AI sales manager — channel-agnostic dialog engine.

Runs an LLM + tool-calling loop: builds a catalog-grounded system prompt, lets
the model call tools (orders, payment, knowledge, escalation), and sends the
final reply through the customer's Messenger. Used by every channel adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import Messenger
from app.db.models import Message, Order, PaymentMethod, Product, User
from app.db.models.enums import Channel, ConversationRole
from app.knowledge.rag import active_products
from app.sales import client_profile
from app.sales.adaptation import live_signals, profile_block
from app.sales.conversation import load_history, record_sent_media, save_message
from app.sales.llm import LLMError, LLMNotConfigured, chat_completion
from app.sales.product_rules import (
    active_product,
    persona_for,
    script_block,
    voice_overrides,
)
from app.sales.prompts import DEFAULT_PERSONA, SALES_METHOD, TOOL_DISCIPLINE
from app.sales.textfmt import strip_markdown
from app.sales.tools import TOOLS, run_tool
from app.services.documents import active_documents
from app.services.media import (
    active_media,
    is_picture,
    public_url,
    save_inbound_audio,
    save_operator_file,
)
from app.services.settings_service import get_local_tz, get_setting
from app.voice import elevenlabs as voice

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 5

_NOT_CONFIGURED = (
    "ИИ-менеджер ещё не настроен. Напишите «оператор», чтобы связаться с человеком, "
    "или загляните в каталог."
)
_TEMP_ERROR = (
    "Извините, временные неполадки на нашей стороне 🙇 Уже передала Ваш вопрос менеджеру — "
    "Вам ответят в ближайшее время. Можно также написать «оператор»."
)

# Throttle operator alerts about LLM failures: when the quota/provider dies, EVERY
# incoming message fails — one alert per interval is enough to wake people up.
_LLM_ALERT_INTERVAL = 15 * 60  # seconds
_last_llm_alert = 0.0


async def _llm_failure_fallback(
    session: AsyncSession, user: User, messenger: Messenger, *, stub: str, error: str
) -> None:
    """The LLM is down: keep the client warm and make the failure VISIBLE.

    Unlike a plain ``send_text``, this saves the stub into the thread (so «Диалоги»
    shows what the client actually received, not a silently ignored message) and
    pings the operator chat — throttled, so a dead quota doesn't flood the group.
    """
    global _last_llm_alert
    mid = None
    try:
        mid = await messenger.send_text(user.external_id, stub)
    except Exception:  # noqa: BLE001 — channel send must not mask the alert below
        logger.warning("LLM-fallback stub send failed for user %s", user.id)
    sent_msg = await save_message(session, user.id, "assistant", stub)
    sent_msg.status = "sent" if mid else "failed"
    if mid:
        sent_msg.channel_mid = str(mid)
    from app.services import inbox as inbox_svc  # lazy: avoid import cycle

    await inbox_svc.mirror_outgoing(session, user, stub)

    now = time.monotonic()
    if now - _last_llm_alert >= _LLM_ALERT_INTERVAL:
        _last_llm_alert = now
        from app.services.notify import send_operator_message  # lazy: avoid import cycle

        who = user.first_name or user.username or user.display_phone or f"id{user.id}"
        channel = getattr(user.channel, "value", user.channel)
        await send_operator_message(
            session,
            "⚠️ <b>Бот не может отвечать клиентам — сбой ИИ</b>\n"
            f"Клиент: <b>{who}</b> ({channel})\n"
            f"Ошибка: {error[:180]}\n"
            "Клиенту отправлена заглушка. Проверьте баланс/ключ LLM и ответьте из «Диалогов».",
        )

# Human-like pacing: show a "typing…" indicator, then pause before replying so the
# bot doesn't answer instantly (which reads as a robot). Tunable in ⚙️ Настройки.
_DEFAULT_HUMANIZE = {
    "enabled": True,
    "min_seconds": 1.5,
    "max_seconds": 5.0,
    "per_char_seconds": 0.04,
}


def _typing_delay(length: int, cfg: dict) -> float:
    """Seconds to 'type' a reply of ``length`` chars — scales with length, clamped
    to [min_seconds, max_seconds]. Returns 0 when disabled."""
    if not cfg.get("enabled", True):
        return 0.0
    lo = max(0.0, float(cfg.get("min_seconds", 1.5) or 0))
    hi = max(lo, float(cfg.get("max_seconds", 5.0) or 0))
    per = max(0.0, float(cfg.get("per_char_seconds", 0.04) or 0))
    return min(hi, max(lo, length * per))


# How each media-library kind is named to the model when listing the library.
_KIND_RU = {"circle": "кружок", "video": "видео", "voice": "голосовое", "link": "ссылка"}


def _tenge(amount: int) -> str:
    return f"{amount:,} ₸".replace(",", " ")


async def _identity_line(session: AsyncSession) -> str:
    """A hard identity anchor injected into every system prompt.

    Without it the model picks Russian gendered first-person forms (рад/рада,
    готов/готова, подобрал/подобрала) at random each turn, so the bot reads as
    "now a man, now a woman". Pinning name + gender makes the voice consistent.
    Gender defaults to feminine; set it in ⚙️ Настройки → «Личность и поведение».
    """
    name = (await get_setting(session, "bot_name") or "").strip()
    male = (await get_setting(session, "bot_gender") or "female").strip() == "male"
    if male:
        who, case, forms = (
            "мужчина",
            "мужском",
            "«рад», «готов», «сам», «уверен», «подобрал», «уточнил», «рассказал»",
        )
    else:
        who, case, forms = (
            "женщина",
            "женском",
            "«рада», «готова», «сама», «уверена», «подобрала», «уточнила», «рассказала»",
        )
    lead = "ТВОЯ ЛИЧНОСТЬ (соблюдай в каждом сообщении):"
    intro = f" Тебя зовут {name}." if name else ""
    return (
        f"{lead}{intro} Ты {who}. Говори о себе строго в {case} роде: {forms}. "
        "Никогда не меняй свой род от сообщения к сообщению."
    )


async def _reaction_meme(session: AsyncSession, text: str):
    """Deterministic reaction picture: the first active meme whose tag word appears in
    the client's message (case-insensitive substring), or None. This makes memes fire
    reliably on a повод (e.g. «позже» → the «ждун» meme) instead of relying on the LLM
    to decide + word-match. Controlled entirely by each picture's tags in 🖼 Картинки."""
    low = (text or "").lower()
    if not low.strip():
        return None
    for m in await active_media(session):
        if not is_picture(m):
            continue  # ссылка «2ГИС» с тегом «адрес» уходила клиенту битым фото
        tags = [t.strip().lower() for t in (m.tags or "").split(",") if t.strip()]
        if any(tag in low for tag in tags):
            return m
    return None


async def _sent_meme_recently(session: AsyncSession, user_id: int, within: int = 4) -> bool:
    """True if any of the last ``within`` messages already carried a picture — so we
    don't stack reaction memes on back-to-back replies."""
    rows = (
        (
            await session.execute(
                select(Message.media_url)
                .where(Message.user_id == user_id)
                .order_by(Message.id.desc())
                .limit(within)
            )
        )
        .scalars()
        .all()
    )
    return any(r for r in rows)


async def _last_user_text(session: AsyncSession, user_id: int) -> str:
    """The client's most recent message — used to pick a reaction meme after a debounced
    burst, where ``new_text`` is None because the fragments were already saved."""
    row = (
        await session.execute(
            select(Message.content)
            .where(Message.user_id == user_id, Message.role == ConversationRole.USER)
            .order_by(Message.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row or ""


# Ответ уходит клиенту двумя способами сразу: текстом в чат и голосом через синтезатор.
# Значки цен («216 ₸/шт») оставляем — в чате они читаются лучше слов, а для озвучки их
# разворачивает normalize_for_tts. А вот таблицы и псевдографика плохи в ОБОИХ каналах:
# в мессенджере они разъезжаются, вслух превращаются в поток тире и палок.
_FORMAT_RULE = (
    "Формат ответа: обычный текст мессенджера. Не используй markdown-таблицы, разметку "
    "из символов | и ---, псевдографику и заголовки с решётками — ответ читается и в чате, "
    "и вслух синтезатором речи. Сокращения, которые тяжело произнести (т.д., т.п., т.к.), "
    "пиши словами."
)


async def _client_portrait(session: AsyncSession, user: User) -> str:
    """Портрет клиента плюс живые сигналы по его последним сообщениям."""
    rows = (
        await session.execute(
            select(Message.content, Message.media_kind)
            .where(Message.user_id == user.id, Message.role == ConversationRole.USER)
            .order_by(Message.id.desc())
            .limit(12)
        )
    ).all()
    # Расшифровку голосового в длину «как пишет» не считаем — человек её не печатал.
    texts = [content or "" for content, kind in rows if kind != "voice"]
    voices = sum(1 for _, kind in rows if kind == "voice")
    return profile_block(client_profile.load(user), live_signals(texts, voices))


async def _refresh_portrait_later(session: AsyncSession, user: User) -> None:
    """После ответа — обновить портрет в фоне (только если модель настроена)."""
    llm_cfg = await get_setting(session, "llm") or {}
    if llm_cfg.get("base_url") and llm_cfg.get("api_key"):
        client_profile.schedule_refresh(user.id)


async def build_system_prompt(session: AsyncSession, user: User | None = None) -> str:
    # Характер и скрипт живут в КАРТОЧКЕ ТОВАРА, о котором идёт разговор; общие
    # настройки бота — запасной вариант (см. app/sales/product_rules.py).
    product_now = await active_product(session, user.id) if user is not None else None
    persona = persona_for(
        product_now, await get_setting(session, "persona_prompt") or DEFAULT_PERSONA
    )
    shop = await get_setting(session, "shop_name") or ""
    products = await active_products(session)
    if products:
        lines = []
        for p in products:
            line = f"- id={p.id}: {p.title} — {_tenge(p.price_kzt)}"
            if p.description:
                line += f" — {p.description.strip()[:100]}"
            lines.append(line)
        catalog = "\n".join(lines)
    else:
        catalog = "(каталог пуст)"

    parts = [persona, await _identity_line(session)]
    if shop:
        parts.append(f"Магазин: {shop}")
    # Каталог отсортирован по sort_order — это ПОРЯДОК ПРИОРИТЕТА, который владелец
    # задаёт в админке. Без явной оговорки модель читала список как простой перечень и
    # предлагала что попало: вперёд шли самые дорогие позиции.
    parts.append(
        "Актуальный каталог (реальны только эти товары и цены). Список идёт В ПОРЯДКЕ "
        "ПРИОРИТЕТА: при прочих равных начинай подбор с товаров в начале списка и "
        "предлагай их первыми; товары ниже — когда клиент просит именно их или когда "
        "верхние по смыслу не подходят.\n"
        + catalog
    )
    parts.append(_FORMAT_RULE)

    # Скрипт берётся из карточки активного товара, иначе общий.
    block = script_block(product_now, await get_setting(session, "sales_script") or "")
    if block:
        parts.append(block)

    # Downsell / tripwire: on price objection or "I'll think", offer a cheaper trial product.
    downsell = await get_setting(session, "downsell") or {}
    if downsell.get("enabled") and downsell.get("product_id"):
        dp = await session.get(Product, int(downsell["product_id"]))
        if dp is not None and dp.is_active:
            parts.append(
                f"Даунселл — это СПАСЕНИЕ СДЕЛКИ: предлагай «{dp.title}» за "
                f"{_tenge(dp.price_kzt)} (id={dp.id}) ТОЛЬКО когда основной продукт уже "
                f"презентован, возражения отработаны, а клиент всё равно уходит без покупки. "
                f"Не предлагай его в начале диалога и не вместо основного продукта. Он ПЛАТНЫЙ — "
                f"никогда не обещай его бесплатно. Если предложил — оформи через create_payment."
            )

    # What we ALREADY know about THIS client — so the bot greets a returning person by
    # name and never re-asks something it already has. Without this, build_system_prompt
    # was identical for everyone and the model re-asked the name on every «Здравствуйте».
    known: list[str] = []
    # A name counts as KNOWN only if the CLIENT confirmed it (save_contact/request_callback).
    # A platform profile name (IG/WA @handle or nickname) is NOT a real name → the bot must
    # ask how to address the person rather than greet them by a possibly-offensive handle.
    has_name = bool(
        user and getattr(user, "name_confirmed", False) and (user.first_name or "").strip()
    )
    if user is not None:
        if has_name:
            known.append(f"как зовут: {user.first_name.strip()}")
        if (user.phone or "").strip():
            known.append(f"телефон: {user.phone.strip()}")
        if (user.stage or "").strip():
            known.append(f"этап воронки: {user.stage.strip()}")
        if (user.segment or "").strip():
            known.append(f"сегмент: {user.segment.strip()}")
    if known:
        parts.append(
            "ЧТО ТЫ УЖЕ ЗНАЕШЬ ОБ ЭТОМ КЛИЕНТЕ (используй это, НЕ переспрашивай уже "
            "известное): " + "; ".join(known) + "."
        )

    if has_name:
        parts.append(
            f"Клиента зовут {user.first_name.strip()} — он уже общался с нами. НЕ спрашивай "
            "имя снова и НЕ представляйся заново, даже если клиент поздоровался: обращайся по "
            "имени и отвечай по сути его последнего сообщения с учётом истории выше. Если "
            "уместно — предложи продолжить в чате или звонок менеджера (request_callback)."
        )
    else:
        parts.append(
            "НАЧАЛО ДИАЛОГА — имя клиента им самим ещё НЕ подтверждено. ВАЖНО: если клиент уже "
            "в первом сообщении сказал, что ему нужно (расчёт, цена, фото, доставка), — СНАЧАЛА "
            "сделай это, а знакомство и вопрос об имени уложи в одну короткую фразу. Клиент, "
            "который попросил расчёт и получил анкету, уходит со словами «вы затягиваете». "
            "Если запроса ещё нет — пройди по порядку, одним живым сообщением:\n"
            "1) Коротко представься — кто ты и от какой компании.\n"
            "2) Спроси, как к клиенту можно обращаться; когда назовёт имя — сохрани его через "
            "save_contact. ⚠️ Платформа может показывать НИК или @username вместо имени — это "
            "НЕ имя человека: НИКОГДА не обращайся по нику/@username и не выдумывай имя. Если "
            "настоящее имя уже прозвучало в переписке выше — не переспрашивай, обращайся по нему.\n"
            "3) Уточни, как удобнее продолжить — здесь в переписке или чтобы менеджер "
            "перезвонил (если звонок — уточни номер и вызови request_callback).\n"
            "Пока не узнал, как обращаться, — не начинай презентацию по своей инициативе; на "
            "прямой вопрос о цене, расчёте или фото отвечай сразу."
        )

    # Методика и портрет — ближе к концу промпта: последние блоки модель держит крепче.
    parts.append((await get_setting(session, "sales_method") or "").strip() or SALES_METHOD)
    if user is not None:
        portrait = await _client_portrait(session, user)
        if portrait:
            parts.append(portrait)

    tz = await get_local_tz(session)
    now_local = datetime.now(tz)
    parts.append(
        f"Сейчас: {now_local:%Y-%m-%d %H:%M} (местное время, UTC{now_local:%z}). "
        "Если клиент говорит «я подумаю» или не готов сейчас — не дави: предложи удобное время "
        "и вызови schedule_follow_up. Если клиент назвал конкретное время (напр. «завтра в 15:00») "
        "— посчитай точную дату-время и передай её в параметр at в формате ISO 8601 "
        "(например 2026-07-03T15:00:00). Иначе укажи hours (через сколько часов вернуться)."
    )
    media = await active_media(session)
    # Only pictures belong under send_image — a circle/video/voice/link listed there would
    # be sent as a photo and fail. Everything else is offered through send_asset below.
    pics = [m for m in media if (m.kind or "image") == "image"]
    assets = [m for m in media if (m.kind or "image") != "image"]
    if pics:
        lines = []
        for i, m in enumerate(pics[:30], 1):
            tags = (m.tags or "").strip()
            povod = f" — повод/эмоция: {tags}" if tags else ""
            lines.append(f"{i}) «{m.title}»{povod}")
        parts.append(
            "Картинки и мемы, которые ты можешь отправлять клиенту инструментом send_image. "
            "Оцени СМЫСЛ и ЭМОЦИЮ последнего сообщения клиента (радость, благодарность, "
            "сомнение, грусть, разочарование, молчание и т.п.). Если настроение подходит под "
            "повод одной из картинок — вызови send_image, передав в query её НОМЕР из списка "
            "(например «1»):\n"
            + "\n".join(lines)
            + "\nКартинку шли вместе с короткой живой фразой, а не вместо ответа. Если "
            "подходящей по смыслу нет — ничего не отправляй. Не выдумывай картинок вне списка."
        )

    if assets:
        lines = []
        for m in assets[:30]:
            hint = (m.trigger or m.tags or "").strip()
            when = f" — когда: {hint}" if hint else ""
            lines.append(f"— {_KIND_RU.get(m.kind, m.kind)}: «{m.title}»{when}")
        parts.append(
            "Медиатека (инструмент send_asset) — кружки, видео, готовые голосовые и ссылки. "
            "Отправляй их, когда это усиливает доверие или наглядность: клиент спрашивает "
            "адрес/как всё выглядит, сомневается в качестве, просит прайс или каталог. "
            "В query передай НАЗВАНИЕ материала из списка. Материалов вне списка не "
            "выдумывай, ссылок тоже:\n" + "\n".join(lines)
        )

    docs = await active_documents(session)
    if docs:
        lines = []
        for i, d in enumerate(docs[:30], 1):
            extra = f" (теги: {d.tags})" if d.tags else ""
            lines.append(f"{i}) {d.category}: «{d.title}»{extra}")
        parts.append(
            "Документы, которые можно отправить клиенту инструментом send_document "
            "(прайсы, каталоги, презентации, сертификаты, достижения). Отправляй, когда "
            "клиент просит цены/документ или когда это уместно подкрепляет доверие. В query "
            "передай НОМЕР нужного документа из списка (например «1»):\n"
            + "\n".join(lines)
            + "\nНе выдумывай документов, которых нет в списке."
        )

    parts.append(
        "Непрерывность диалога: выше — вся история переписки, включая ответы менеджера-"
        "человека. Продолжай с учётом этого контекста. НЕ представляйся заново и НЕ начинай "
        "диалог сначала, если беседа уже идёт — отвечай по сути на последнее сообщение."
    )
    parts.append(
        "Доступ и ссылки: курс выдаётся ТОЛЬКО через ссылку из карточки товара (например, "
        "ссылку на Telegram-канал), которую бот отправляет после подтверждённой оплаты. "
        "НИКОГДА не отправляй клиента на сайт, не упоминай «личный кабинет на сайте» и не "
        "придумывай ссылки — у тебя их нет. Если ОПЛАТИВШИЙ клиент просит ссылку на курс или "
        "не нашёл доступ — вызови resend_access (переотправит настоящую ссылку). Если заказ "
        "ещё НЕ оплачен — оформи оплату через create_payment."
    )
    methods = (
        (
            await session.execute(
                select(PaymentMethod)
                .where(PaymentMethod.enabled.is_(True))
                .order_by(PaymentMethod.sort_order, PaymentMethod.id)
            )
        )
        .scalars()
        .all()
    )
    if methods:
        parts.append(
            "Способы оплаты (доступные сейчас): "
            + ", ".join(m.title for m in methods)
            + ". Когда клиент спрашивает, КАК оплатить / какие есть способы оплаты / «оплата "
            "как?» — это НЕ проверка оплаты: коротко назови эти варианты и СРАЗУ вызови "
            "create_payment, чтобы оформить заказ и дать ссылку на оплату. НЕ вызывай "
            "check_payment на вопрос «как оплатить» — сверяй оплату ТОЛЬКО после того, как "
            "клиент сам сказал, что оплатил или прислал чек."
        )
    style_guide = (await get_setting(session, "style_guide") or "").strip()
    if style_guide:
        parts.append(
            "МАНЕРА ОБЩЕНИЯ — пиши строго в этом стиле владельца (копируй тон, "
            "фирменные фразы и эмодзи; это важнее нейтральной вежливости):\n" + style_guide
        )
    parts.append(TOOL_DISCIPLINE)
    return "\n\n".join(parts)


async def generate_start_message(session: AsyncSession, *, user: User) -> str:
    """Produce the bot's FIRST message on /start.

    The admin "welcome_text" is not sent verbatim — it's the trigger/brief the AI
    uses to write a live opening message in the shop's persona. Falls back to the
    welcome text (or a neutral line) if the LLM isn't configured.
    """
    welcome = (await get_setting(session, "welcome_text") or "").strip()
    config = await get_setting(session, "llm") or {}

    directive = (
        "Клиент только что открыл диалог (нажал /start). Напиши короткое живое приветствие от "
        "первого лица, следуя правилам начала диалога выше (для нового клиента — представься, "
        "узнай, как обращаться, и удобный формат; знакомого — поприветствуй по имени, не "
        "представляясь заново). Без списков и без кнопок."
    )
    if welcome:
        directive += f" Опирайся на это приветствие магазина, передай его смысл и тон: «{welcome}»."

    system = await build_system_prompt(session, user)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": directive}]
    try:
        assistant = await chat_completion(messages=messages, tools=None, config=config)
        content = (assistant.get("content") or "").strip()
    except (LLMNotConfigured, LLMError):
        content = ""

    if not content:
        content = welcome or "Здравствуйте! Чем могу помочь?"

    await save_message(session, user.id, "assistant", content)
    return content


async def relay_operator_answer(session: AsyncSession, *, user: User, messenger: Messenger,
                                answer: str) -> bool:
    """Re-voice an operator's answer to the client IN THE BOT'S STYLE, so the bot stays the
    face of the chat (used when the operator replies while the bot is active). One focused
    LLM call, no tools; on any LLM failure falls back to the raw answer. Returns True if a
    message was actually delivered."""
    from app.services import inbox as inbox_svc

    answer = (answer or "").strip()
    if not answer:
        return False
    config = await get_setting(session, "llm") or {}
    persona = await get_setting(session, "persona_prompt") or DEFAULT_PERSONA
    style = (await get_setting(session, "style_guide") or "").strip()
    history = await load_history(session, user.id)
    system = (
        persona
        + (f"\n\nМАНЕРА ОБЩЕНИЯ:\n{style}" if style else "")
        + "\n\nАдминистратор передал тебе точный ответ для клиента. Перескажи его клиенту от "
        "своего лица — естественно, в своём стиле, коротко, НИЧЕГО не добавляя и не выдумывая "
        "сверх сути, и НЕ упоминая администратора."
    )
    messages = [
        {"role": "system", "content": system},
        *history[-8:],
        {"role": "user", "content": f"Ответ, который нужно передать клиенту: «{answer}»"},
    ]
    try:
        assistant = await chat_completion(messages=messages, tools=None, config=config)
        content = strip_markdown((assistant.get("content") or "").strip()) or answer
    except (LLMNotConfigured, LLMError):
        content = answer  # LLM down → deliver the operator's answer as-is
    mid = None
    try:
        mid = await messenger.send_text(user.external_id, content)
    except Exception:  # noqa: BLE001
        logger.warning("relay_operator_answer send failed for user %s", user.id)
    msg = await save_message(session, user.id, "assistant", content)
    msg.status = "sent" if mid else "failed"
    if mid:
        msg.channel_mid = str(mid)
    await inbox_svc.mirror_outgoing(session, user, content, who="bot")
    return bool(mid)


async def _send_reply(
    messenger: Messenger,
    external_id: str,
    content: str,
    *,
    voice_cfg: dict | None,
    humanize: dict | None = None,
    message_id: str | None = None,
) -> tuple[str | None, voice.VoiceReply | None]:
    """Deliver the assistant's final reply — as a voice message when requested,
    otherwise as text. On voice, links are also sent as text so they stay tappable.
    Returns ``(mid, voice_reply)``: the platform message id of the text send (for
    read-receipt tracking) and the spoken reply that was actually sent (so the caller
    can attach its audio to the saved turn for playback in «Диалоги»), else None.

    First shows a 'typing…' indicator and pauses for a human-like moment (scaled by
    reply length) so the answer doesn't land instantly.
    """
    delay = _typing_delay(len(content or ""), humanize or {})
    if delay > 0:
        try:
            await messenger.send_typing(external_id, message_id=message_id)
        except Exception:  # noqa: BLE001 — cosmetic; never block the reply
            pass
        if not os.environ.get("SELLERBOT_TESTING"):
            await asyncio.sleep(delay)

    if voice_cfg is not None:
        # Instagram accepts only aac/m4a/wav/mp4 audio (not ogg/opus/mp3) → synth m4a there.
        ig_audio = getattr(messenger, "channel", None) == Channel.INSTAGRAM
        reply = await voice.synthesize_reply(content, cfg=voice_cfg, instagram=ig_audio)
        if reply is not None:
            sent = await messenger.send_voice(
                external_id, reply.data, mime=reply.mime, filename=reply.filename
            )
            if not sent and ig_audio:
                # Instagram has no voice-note upload → save the clip (.m4a) and send it as an
                # audio attachment by URL, so the bot can also speak on Instagram.
                saved = save_operator_file(reply.data, reply.mime, reply.filename)
                if saved:
                    sent = await messenger.send_audio_by_url(
                        external_id, saved["url"], mime=reply.mime
                    )
            if sent:
                # Links also go as text so they stay tappable; that text send is the
                # one we track for receipts.
                if "http" in content.lower():
                    return await messenger.send_text(external_id, content), reply
                return None, reply
    return await messenger.send_text(external_id, content), None


# The model often PROMISES to (re)send course access or FALSELY claims payment went
# through, without actually delivering. When a reply does that but the model didn't
# call resend_access this turn, the safety net below delivers for real. The regex is
# access/payment-specific so it never fires on ordinary "секундочку, уточню расписание".
_ACCESS_STALL_RE = re.compile(
    r"(пере)?отправ\w*\s+(вам\s+)?(доступ|курс)"
    r"|дам\s+(вам\s+)?доступ"
    r"|вышл\w*\s+(вам\s+)?(доступ|курс)"
    r"|ссылк\w*\s+на\s+(курс|доступ)"  # false "here's your course link" — NOT "ссылку на оплату"
    r"|оплата\s+(прошла|подтвержд)"
    r"|заказ\w*\s+(успешно\s+)?(подтвержд|выполн|оплач)",
    re.IGNORECASE,
)


def _is_access_stall(text: str) -> bool:
    """A reply that promises to (re)send access, or falsely asserts payment went
    through — the model's most common hallucination in this flow."""
    return bool(_ACCESS_STALL_RE.search(text or ""))


async def _deliver_access_or_explain(
    session: AsyncSession, user: User, messenger: Messenger, tg_bot
) -> str | None:
    """Deterministic access handling when the model stalls or claims payment without
    verifying. Re-sends the REAL access for the client's PAID orders (returns None —
    the link was just sent), or honestly explains there's no paid order (returns text
    to send instead of the model's stall)."""
    from app.db.models.enums import OrderStatus
    from app.fulfillment.registry import build_fulfillment

    orders = (
        (
            await session.execute(
                select(Order)
                .where(
                    Order.user_id == user.id,
                    Order.status.in_([OrderStatus.PAID, OrderStatus.FULFILLED]),
                )
                .order_by(Order.id.desc())
            )
        )
        .scalars()
        .all()
    )
    if not orders:
        return (
            "Проверил — оплаченного заказа пока не вижу 🙏 Чтобы получить доступ к курсу, его "
            "нужно оформить и оплатить. Подсказать, какой курс подойдёт под вашу задачу?"
        )
    sent = 0
    for o in orders:
        product = await session.get(Product, o.product_id)
        if product is None:
            continue
        try:
            result = await build_fulfillment(product).grant(
                tg_bot=tg_bot, product=product, order=o, user=user
            )
            await messenger.send_text(user.external_id, result.message)
            sent += 1
        except Exception:  # noqa: BLE001 — one failure must not abort the rest
            logger.warning("safety-net access delivery failed for order %s", o.code)
    return None if sent else "Секунду — уточню у менеджера и вернусь с доступом."


async def _generate_reply(
    session: AsyncSession,
    *,
    user: User,
    messenger: Messenger,
    new_text: str | None = None,
    tg_bot=None,
    reply_voice: bool = False,
    situation_note: str | None = None,
    incoming_message_id: str | None = None,
    media_url: str | None = None,
    media_kind: str | None = None,
) -> None:
    """Compose and send one reply.

    ``new_text`` is the just-arrived message to append + save; pass None to reply from
    already-saved history (used after a debounced burst of fragments). ``reply_voice``
    requests a spoken reply; ``situation_note`` is a per-turn system directive (e.g. a
    story reply — don't cold-open); ``incoming_message_id`` drives WhatsApp typing.
    """
    config = await get_setting(session, "llm") or {}
    voice_cfg = await voice.voice_config(session) if reply_voice else None
    if voice_cfg is not None:
        # Голос общий; карточка товара перекрывает его, только если он там задан явно.
        voice_cfg = {**voice_cfg, **voice_overrides(await active_product(session, user.id))}
    humanize = {**_DEFAULT_HUMANIZE, **(await get_setting(session, "humanize") or {})}

    # Immediate feedback: show "typing…" the moment we start composing.
    if humanize.get("enabled", True):
        try:
            await messenger.send_typing(user.external_id, message_id=incoming_message_id)
        except Exception:  # noqa: BLE001
            pass

    history = await load_history(session, user.id)  # prior turns (across restarts/days)
    system = await build_system_prompt(session, user)
    messages: list[dict] = [{"role": "system", "content": system}, *history]
    if situation_note:  # per-turn directive (e.g. story reply — not a cold open)
        messages.append({"role": "system", "content": situation_note})
    if new_text is not None:  # None → reply from already-saved history (debounced burst)
        messages.append({"role": "user", "content": new_text})
        await save_message(
            session,
            user.id,
            "user",
            new_text,
            media_url=media_url,
            media_kind=media_kind,
            channel_mid=incoming_message_id,
        )

    delivered_access = False  # did the model actually DELIVER access (resend_access)?
    image_sent = False  # did the model already send a picture (send_image tool)?
    offered_payment = False  # did the model give a real pay link this turn (create_payment)?
    for _ in range(_MAX_TOOL_ROUNDS):
        try:
            assistant = await chat_completion(messages=messages, tools=TOOLS, config=config)
        except LLMNotConfigured as e:
            await _llm_failure_fallback(
                session, user, messenger, stub=_NOT_CONFIGURED, error=str(e)
            )
            return
        except LLMError as e:
            await _llm_failure_fallback(session, user, messenger, stub=_TEMP_ERROR, error=str(e))
            return

        messages.append(assistant)
        tool_calls = assistant.get("tool_calls") or []

        if not tool_calls:
            content = strip_markdown((assistant.get("content") or "").strip()) or "…"
            # Safety net: the model stalled («дайте секунду») or claimed payment without
            # verifying → deliver the REAL access (or an honest "no paid order") instead.
            if not delivered_access and not offered_payment and _is_access_stall(content):
                override = await _deliver_access_or_explain(session, user, messenger, tg_bot)
                if override is None:  # real access link(s) were just sent by the fallback
                    await save_message(session, user.id, "assistant", "[доступ к курсу отправлен]")
                    return
                content = override
            sent_msg = await save_message(session, user.id, "assistant", content)
            sent_msg.status = "sent"
            mid, voice_reply = await _send_reply(
                messenger,
                user.external_id,
                content,
                voice_cfg=voice_cfg,
                humanize=humanize,
                message_id=incoming_message_id,
            )
            if mid:
                sent_msg.channel_mid = str(mid)
            # Keep the spoken reply playable in «Диалоги» (text stays as the content).
            saved_voice = None
            if voice_reply is not None:
                saved_voice = save_inbound_audio(voice_reply.data)
                if saved_voice:
                    sent_msg.media_url = saved_voice["url"]
                    sent_msg.media_kind = "voice"
            from app.services import inbox as inbox_svc  # lazy: avoid import cycle

            await inbox_svc.mirror_outgoing(
                session,
                user,
                content,
                who="bot",
                media_url=saved_voice.get("url") if saved_voice else None,
                media_kind=saved_voice.get("kind") if saved_voice else None,
            )
            # Deterministic reaction meme: if the client's message hit a повод (a tag word
            # like «позже»/«подумаю») and we didn't already send a picture, attach it.
            # new_text is None after a debounced burst, so fall back to the last saved message.
            client_text = new_text or await _last_user_text(session, user.id)
            if not image_sent and client_text and not await _sent_meme_recently(session, user.id):
                meme = await _reaction_meme(session, client_text)
                if meme is not None:
                    try:
                        await messenger.send_image(user.external_id, public_url(meme.filename))
                        await record_sent_media(
                            session,
                            user,
                            title=meme.title,
                            word="картинка",
                            url=public_url(meme.filename),
                            kind="photo",
                        )
                    except Exception:  # noqa: BLE001 — a bonus picture never breaks the turn
                        logger.warning("Reaction meme send failed for user %s", user.id)
            return

        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            if name == "resend_access":
                delivered_access = True
            if name == "create_payment":
                offered_payment = True  # gave a real pay link → don't override as "no payment"
            if name == "send_image":
                image_sent = True
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await run_tool(
                name, args, session=session, user=user, messenger=messenger, tg_bot=tg_bot
            )
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})
        await session.flush()

    await messenger.send_text(user.external_id, "Уточните, пожалуйста, ваш запрос 🙏")


async def handle_text(
    session: AsyncSession,
    *,
    user: User,
    text: str,
    messenger: Messenger,
    tg_bot=None,
    reply_voice: bool = False,
    situation_note: str | None = None,
    incoming_message_id: str | None = None,
    media_url: str | None = None,
    media_kind: str | None = None,
) -> None:
    """Save one inbound message and immediately reply to it.

    ``media_url`` + ``media_kind`` attach a playable file to the saved inbound turn
    (e.g. a client's voice note, so operators can replay it in «Диалоги»).
    ``incoming_message_id`` doubles as the inbound turn's ``channel_mid`` so operators
    can react to the client's message via the channel API."""
    await _generate_reply(
        session,
        user=user,
        messenger=messenger,
        new_text=text,
        tg_bot=tg_bot,
        reply_voice=reply_voice,
        situation_note=situation_note,
        incoming_message_id=incoming_message_id,
        media_url=media_url,
        media_kind=media_kind,
    )
    await _refresh_portrait_later(session, user)


async def respond_from_history(
    session: AsyncSession,
    *,
    user: User,
    messenger: Messenger,
    tg_bot=None,
    incoming_message_id: str | None = None,
) -> None:
    """Reply from the already-saved history — used after debouncing a burst of
    fragmented messages into one answer (the fragments were saved as they arrived)."""
    await _generate_reply(
        session,
        user=user,
        messenger=messenger,
        new_text=None,
        tg_bot=tg_bot,
        incoming_message_id=incoming_message_id,
    )
    await _refresh_portrait_later(session, user)
